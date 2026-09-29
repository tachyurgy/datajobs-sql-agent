"""Read-only SQL guard.

The same rules are implemented in web/public/js/core.mjs for the browser; tests/test_parity.py runs a
shared corpus of allowed/denied statements through both and asserts identical verdicts.

Layers (defense in depth; any one of them rejecting is enough):
  1. Lexical: strip comments, respect string literals and quoted identifiers, allow exactly one
     statement, which must start with SELECT or WITH (optionally after opening parentheses).
  2. Keyword deny-list: no DDL/DML/session/extension keywords anywhere outside strings.
  3. Function deny-list: no file/network/SQL-eval table functions (read_*, *_scan, glob, query, ...).
  4. FROM/JOIN targets must be a known table, a CTE defined in the statement, a subquery, or one of
     a few generator table functions. A string literal after FROM/JOIN (DuckDB's "FROM 'file.csv'")
     is rejected.
  5. Python only: sqlglot must parse it as exactly one query expression (when sqlglot can parse it).
At execution time the connection also has external access disabled and its configuration locked,
and results are row-limited and time-limited.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

KNOWN_TABLES = frozenset({
    "fct_posting", "dim_company", "posting_skills", "dim_skill", "posting_versions_scd2",
    "mart_daily_market", "mart_skill_demand", "mart_comp_by_family", "mart_degree_requirements",
    "mart_doorway_roles", "mart_crawl_coverage",
})

DENY_KEYWORDS = frozenset("""
insert update delete drop create alter attach detach copy export import install load pragma set reset
call checkpoint vacuum truncate grant revoke begin commit rollback use merge prepare execute deallocate
""".split())

DENY_FUNCTIONS = frozenset("""
glob query query_table sniff_csv getenv parquet_metadata parquet_schema parquet_file_metadata
parquet_kv_metadata duckdb_secrets duckdb_extensions duckdb_settings current_setting
""".split())

ALLOWED_TABLE_FUNCTIONS = frozenset({"range", "generate_series", "unnest"})

# Functions whose argument syntax uses the FROM keyword (EXTRACT(year FROM ts), TRIM(x FROM y), ...).
FROM_IN_FUNCTION = frozenset({"extract", "substring", "trim", "overlay"})


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""
    sql: str = ""


def tokenize(sql: str) -> list[tuple[str, str]]:
    """Return (kind, value) tokens: word, string, qident, num, sym. Comments are dropped.
    Raises ValueError on an unterminated string/comment/identifier."""
    toks: list[tuple[str, str]] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j < 0:
                raise ValueError("unterminated comment")
            i = j + 2
        elif c == "'":
            j = i + 1
            while True:
                k = sql.find("'", j)
                if k < 0:
                    raise ValueError("unterminated string literal")
                if k + 1 < n and sql[k + 1] == "'":
                    j = k + 2
                    continue
                toks.append(("string", sql[i + 1:k]))
                i = k + 1
                break
        elif c == '"':
            k = sql.find('"', i + 1)
            if k < 0:
                raise ValueError("unterminated quoted identifier")
            toks.append(("qident", sql[i + 1:k].lower()))
            i = k + 1
        elif c.isalpha() or c == "_":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] in "_$"):
                j += 1
            toks.append(("word", sql[i:j].lower()))
            i = j
        elif c.isdigit():
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "."):
                j += 1
            toks.append(("num", sql[i:j]))
            i = j
        else:
            toks.append(("sym", c))
            i += 1
    return toks


def _strip_trailing_semicolons(sql: str) -> str:
    return re.sub(r"[\s;]+$", "", sql.strip())


def lexical_check(sql: str) -> Verdict:
    if not sql or not sql.strip():
        return Verdict(False, "empty statement")
    sql = _strip_trailing_semicolons(sql)
    try:
        toks = tokenize(sql)
    except ValueError as e:
        return Verdict(False, str(e))
    if not toks:
        return Verdict(False, "empty statement")
    for kind, val in toks:
        if kind == "sym" and val == ";":
            return Verdict(False, "only a single statement is allowed")
        if kind == "sym" and val == "$":
            return Verdict(False, "dollar-quoted strings and parameters are not allowed")
    first = next((v for k, v in toks if not (k == "sym" and v == "(")), "")
    if first not in ("select", "with"):
        return Verdict(False, "only SELECT or WITH queries are allowed")
    ctes = {toks[i - 1][1] for i in range(1, len(toks) - 2)
            if toks[i] == ("word", "as") and toks[i + 1] == ("sym", "(") and toks[i - 1][0] in ("word", "qident")}
    parens: list[str] = []
    for i, (kind, val) in enumerate(toks):
        if kind == "sym" and val == "(":
            prev = toks[i - 1] if i > 0 else ("", "")
            parens.append(prev[1] if prev[0] == "word" else "")
            continue
        if kind == "sym" and val == ")":
            if parens:
                parens.pop()
            continue
        if kind != "word":
            continue
        nxt = toks[i + 1] if i + 1 < len(toks) else ("", "")
        prev = toks[i - 1] if i > 0 else ("", "")
        if val in DENY_KEYWORDS and prev != ("sym", "."):
            return Verdict(False, f"keyword '{val.upper()}' is not allowed in a read-only query")
        if nxt == ("sym", "(") and (val in DENY_FUNCTIONS or val.startswith("read_") or val.endswith("_scan")):
            return Verdict(False, f"function '{val}' is not allowed")
        if val in ("from", "join"):
            if val == "from" and ((parens and parens[-1] in FROM_IN_FUNCTION) or prev == ("word", "distinct")):
                continue
            j = i + 1
            if j >= len(toks):
                return Verdict(False, f"dangling {val.upper()}")
            tk, tv = toks[j]
            if tk == "string":
                return Verdict(False, "reading files or URLs is not allowed")
            if tk == "sym" and tv == "(":
                continue
            if tk == "word" and tv == "lateral":
                continue
            if tk in ("word", "qident"):
                # schema-qualified name: take the last part
                while j + 2 < len(toks) and toks[j + 1] == ("sym", ".") and toks[j + 2][0] in ("word", "qident"):
                    j += 2
                    tv = toks[j][1]
                after = toks[j + 1] if j + 1 < len(toks) else ("", "")
                if after == ("sym", "("):
                    if tv not in ALLOWED_TABLE_FUNCTIONS:
                        return Verdict(False, f"table function '{tv}' is not allowed")
                elif tv not in KNOWN_TABLES and tv not in ctes:
                    return Verdict(False, f"unknown table '{tv}'")
    return Verdict(True, "", sql)


def check_sql(sql: str) -> Verdict:
    v = lexical_check(sql)
    if not v.ok:
        return v
    try:
        import sqlglot
        from sqlglot import exp
        stmts = [s for s in sqlglot.parse(v.sql, read="duckdb") if s is not None]
    except Exception:  # sqlglot cannot parse every DuckDB construct; the lexical layer is authoritative
        return v
    if len(stmts) != 1:
        return Verdict(False, "only a single statement is allowed")
    if not isinstance(stmts[0], exp.Query):
        return Verdict(False, f"statement type {type(stmts[0]).__name__} is not a query")
    for node in stmts[0].walk():
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Command)):
            return Verdict(False, f"{type(node).__name__} is not allowed in a read-only query")
    return v
