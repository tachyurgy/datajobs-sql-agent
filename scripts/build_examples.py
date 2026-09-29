"""Build web/public/data/examples.json: cached answers for the example chips (and the fallback when the
live quota is used up). Runs the full agent (ablation e, what the site runs) through the eval's reply cache, then the same
answer prompt the Pages Function uses. Every kept example was checked by hand against an independently written query (see
the 2026-09-29 RELEASES entry); examples whose SQL or summary was wrong were dropped, not edited."""
import json
import sys

from askdata import ROOT
from askdata.agent import ask
from askdata.compare import canon
from askdata.db import connect
from askdata.evaluate import CachedLLM
from askdata.llm import answer_prompt, generate_text

MODEL = sys.argv[1] if len(sys.argv) > 1 else "gemini-2.5-flash-lite"
QUESTIONS = [
    "What do AI engineers make?",
    "What is the median pay for senior data engineers?",
    "Which companies have the most open remote-US ML engineer listings?",
    "How many doorway roles are open for data analysts?",
    "How many open remote-US listings mention both dbt and Snowflake?",
    "Which ATS platform hosts the most remote data listings?",
    "How many applicants does the average posting get?",
]


def main():
    con = connect()
    llm = CachedLLM(offline=False)
    cache = ROOT / "results" / "cache" / "answers.json"
    answers = json.loads(cache.read_text()) if cache.exists() else {}
    out = []
    for q in QUESTIONS:
        tr = ask(con, q, ablation="e", model=MODEL, llm=llm)
        attempts = [{"sql": a.sql, "error": a.error} for a in tr.attempts]
        if tr.refused:
            ans = f"I can't answer that from this warehouse. {tr.refuse_reason}".strip()
            attempts[-1].update(refused=True, reason=tr.refuse_reason)
        elif tr.result and tr.result.rows:
            rows = [[canon(v) if not isinstance(v, str) else v for v in r] for r in tr.result.rows]
            p = answer_prompt(q, tr.final_sql, tr.result.columns, rows, len(tr.result.rows))
            key = f"{MODEL}|{p}"
            if key not in answers:
                answers[key] = generate_text(MODEL, p)
            ans = answers[key]
        else:
            ans = "No answer."
        out.append({"question": q, "sql": tr.final_sql, "answer": ans, "model": MODEL, "attempts": attempts})
        print(f"\n## {q}\n{tr.final_sql}\n-> {tr.result.rows[:6] if tr.result else None}\n=> {ans}")
    cache.write_text(json.dumps(answers, indent=1) + "\n")
    (ROOT / "web" / "public" / "data" / "examples.json").write_text(json.dumps(out, indent=1) + "\n")


if __name__ == "__main__":
    main()
