"""Prompt construction: compact schema + lightweight RAG over the data dictionary + retrieved few-shots.

This module is mirrored line for line by web/public/js/core.mjs. tests/test_parity.py builds prompts
for a fixture of questions in both languages and asserts byte-identical output, so the eval measures
exactly the prompt the web app sends.

Ablations (cumulative):
  a  zero-shot, full schema (table and column names with types only)
  b  + table descriptions, dictionary notes, and the top-k retrieved column docs with sample values
  c  + top-3 retrieved few-shot examples (from a pool disjoint from the gold set)
  d  = c + up to 2 self-correction retries on an error or an empty result (see agent.py)
  e  = d + an answerability rule in the system prompt (ANSWERABILITY). Added after run 1 showed refusal was
       the weakest skill; the rule was written from the schema and checked only against gold/dev.toml, a
       separate 16-question dev set, never against the gold questions.
"""
from __future__ import annotations

import json
import math
import re
import tomllib
from functools import lru_cache

from . import DICTIONARY, FEWSHOT

TOP_K_COLUMNS = 14
TOP_K_EXAMPLES = 3
MAX_SAME_COLUMN_NAME = 2  # the marts repeat family/family_label; keep retrieval from spending every slot on them
SAMPLE_CHARS = 700

STOPWORDS = frozenset("""
a an and are as at be by do does for from how i in is it its me of on or show that the their them there these
this to was were what when where which who whom why will with list give tell many much each per all any
""".split())

# Query expansion: maps a question word to extra tokens that appear in the dictionary. Kept deliberately
# small and generic (vocabulary of pay, time, level, companies), not tuned per gold question.
SYNONYMS = {
    "salary": "comp pay usd", "salaries": "comp pay usd", "pay": "comp usd", "paid": "comp pay usd",
    "make": "comp pay", "earn": "comp pay", "compensation": "comp pay", "wage": "comp pay",
    "company": "company_name", "companies": "company_name", "employer": "company_name", "hiring": "open listing",
    "job": "posting listing", "jobs": "posting listing", "role": "family posting", "roles": "family posting",
    "opening": "open listing", "openings": "open listing",
    "skill": "skill cluster", "skills": "skill cluster", "mention": "skill", "mentions": "skill",
    "demand": "skill share listing",
    "senior": "seniority", "junior": "seniority entry", "entry": "seniority", "intern": "seniority",
    "level": "seniority", "staff": "seniority", "manager": "seniority",
    "degree": "degree_level", "phd": "degree_level phd", "masters": "degree_level masters",
    "experience": "yoe_ask years", "years": "yoe_ask",
    "published": "published_at", "posted": "published_at", "month": "published_at", "year": "published_at",
    "week": "published_at", "day": "published_at", "days": "published_at age_days_at_last_seen",
    "old": "age_days_at_last_seen published_at", "oldest": "published_at", "age": "age_days_at_last_seen",
    "board": "board_url dim_company", "boards": "dim_company board_token", "crawl": "crawl_date boards_ok",
    "ats": "platform", "platform": "platform", "remote": "remote_us", "currency": "comp_currency_original",
    "doorway": "is_doorway", "ai": "ai_engineer family", "ml": "ml_engineer family",
    "analyst": "data_analyst family", "analysts": "data_analyst family", "scientist": "data_scientist family",
    "scientists": "data_scientist family", "engineer": "family", "engineers": "family",
}

SYSTEM = """You translate questions about a job-market data warehouse into one DuckDB SQL query.
Respond with a single JSON object and nothing else: {"sql": "<query or empty>", "refuse": <true|false>, "reason": "<short>"}.
Rules:
- Write exactly one read-only query that starts with SELECT or WITH. Never modify data.
- Use only the tables and columns listed. Do not invent columns or values.
- If the question cannot be answered from these tables (the data does not exist, or it asks to change data), set "refuse": true, "sql": "" and explain in "reason".
- Return only the columns needed to answer, with readable aliases. Add ORDER BY and LIMIT only when the question asks for a ranking or a top N.
- Do not round numbers unless asked."""

ANSWERABILITY = """
Answerability (check this before writing SQL):
- The warehouse records job postings (title, role family, seniority, location and remote flags, publish and crawl dates, posted pay range, years-of-experience and degree asks, skill mentions from a fixed taxonomy) and the company job boards they came from. Nothing else.
- It does not record applicants, interviews, hires or offers; people or demographics; company facts such as size, revenue, funding, ratings or benefits; the posting description text; or anything before the first crawl.
- If answering needs a concept that no listed column holds, refuse. Do not stand in an unrelated column for it, and do not search titles or URLs for the missing concept.
- Refuse any request to change, add or delete data."""


def system_prompt(ablation: str) -> str:
    return SYSTEM + ANSWERABILITY if ablation == "e" else SYSTEM


def tokenize(text: str) -> list[str]:
    out = []
    for w in re.split(r"[^a-z0-9]+", text.lower()):
        if not w or w in STOPWORDS:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


def expand(question: str) -> list[str]:
    toks = []
    for w in re.split(r"[^a-z0-9]+", question.lower()):
        if w in SYNONYMS:
            toks.extend(tokenize(SYNONYMS[w]))
    return tokenize(question) + toks


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.2, b: float = 0.75):
        self.docs, self.k1, self.b = docs, k1, b
        self.n = len(docs)
        self.avgdl = sum(len(d) for d in docs) / self.n
        df: dict[str, int] = {}
        for d in docs:
            for t in set(d):
                df[t] = df.get(t, 0) + 1
        self.idf = {t: math.log(1 + (self.n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self.tf = []
        for d in docs:
            tf: dict[str, int] = {}
            for t in d:
                tf[t] = tf.get(t, 0) + 1
            self.tf.append(tf)

    def scores(self, query: list[str]) -> list[float]:
        q = []
        for t in query:
            if t not in q:
                q.append(t)
        out = []
        for i, d in enumerate(self.docs):
            s = 0.0
            dl = len(d)
            for t in q:
                f = self.tf[i].get(t, 0)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            out.append(s)
        return out

    def ranked(self, query: list[str]) -> list[int]:
        sc = self.scores(query)
        return [i for i in sorted(range(self.n), key=lambda i: (-sc[i], i)) if sc[i] > 0]

    def top(self, query: list[str], k: int) -> list[int]:
        return self.ranked(query)[:k]


@lru_cache(maxsize=1)
def load_dictionary() -> dict:
    return json.loads(DICTIONARY.read_text())


@lru_cache(maxsize=1)
def load_fewshots() -> list[dict]:
    with open(FEWSHOT, "rb") as f:
        return [{"question": e["question"].strip(), "sql": e["sql"].strip()} for e in tomllib.load(f)["example"]]


def column_docs(d: dict) -> list[tuple[str, str, str]]:
    """(table, column, rendered doc line) for every column."""
    out = []
    for t in d["tables"]:
        for c in t["columns"]:
            line = f"{t['name']}.{c['name']} ({c['type']})"
            if c["description"]:
                line += f": {c['description']}"
            if c["samples"]:
                s = ", ".join(json.dumps(v, ensure_ascii=False) for v in c["samples"])
                if len(s) > SAMPLE_CHARS:
                    s = s[:SAMPLE_CHARS].rsplit(", ", 1)[0] + ", ..."
                line += f" Values: {s}"
            out.append((t["name"], c["name"], line))
    return out


@lru_cache(maxsize=1)
def _column_index() -> tuple[list, BM25]:
    docs = column_docs(load_dictionary())
    return docs, BM25([tokenize(f"{t} {c} {line}") for t, c, line in docs])


@lru_cache(maxsize=1)
def _example_index() -> BM25:
    return BM25([tokenize(e["question"]) for e in load_fewshots()])


def retrieve_columns(query_tokens: list[str]) -> list[str]:
    docs, idx = _column_index()
    seen: dict[str, int] = {}
    out = []
    for i in idx.ranked(query_tokens):
        name = docs[i][1]
        if seen.get(name, 0) >= MAX_SAME_COLUMN_NAME:
            continue
        seen[name] = seen.get(name, 0) + 1
        out.append(docs[i][2])
        if len(out) == TOP_K_COLUMNS:
            break
    return out


def schema_block(d: dict, with_descriptions: bool) -> str:
    lines = []
    for t in d["tables"]:
        cols = ", ".join(f"{c['name']} {c['type']}" for c in t["columns"])
        lines.append(f"{t['name']}({cols})")
        if with_descriptions and t["description"]:
            lines.append(f"  -- {t['description']}")
    return "\n".join(lines)


def build_prompt(question: str, ablation: str, attempts: list[dict] | None = None) -> str:
    """User-turn text. `attempts` = previous [{sql, error}] for self-correction (ablation d only)."""
    d = load_dictionary()
    q = expand(question)
    parts = ["### Schema", schema_block(d, ablation != "a")]
    if ablation in ("b", "c", "d", "e"):
        parts += ["", "### Notes"] + [f"- {n}" for n in d["notes"]]
        parts += ["", "### Relevant columns"] + [f"- {line}" for line in retrieve_columns(q)]
    if ablation in ("c", "d", "e"):
        ex = load_fewshots()
        top = _example_index().top(tokenize(question), TOP_K_EXAMPLES)
        if top:
            parts += ["", "### Examples"]
            for i in top:
                parts += [f"Q: {ex[i]['question']}", f"SQL: {ex[i]['sql']}"]
    for n, a in enumerate(attempts or [], 1):
        parts += ["", f"### Attempt {n} failed", f"SQL: {a['sql']}", f"Problem: {a['error']}",
                  "Write a corrected query (or refuse if the question cannot be answered)."]
    parts += ["", "### Question", question.strip()]
    return "\n".join(parts)
