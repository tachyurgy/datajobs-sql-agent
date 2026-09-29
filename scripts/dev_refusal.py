"""Check the answerability rule on the dev set (gold/dev.toml): ablation d vs e, refusal decisions only."""
import sys
import tomllib

from askdata import ROOT
from askdata.agent import ask
from askdata.db import connect
from askdata.evaluate import CachedLLM

MODEL = sys.argv[1] if len(sys.argv) > 1 else "gemini-2.5-flash-lite"
items = tomllib.loads((ROOT / "gold" / "dev.toml").read_text())["q"]
con, llm = connect(), CachedLLM(offline=False)
score = {"d": 0, "e": 0}
for it in items:
    line = []
    for ab in ("d", "e"):
        tr = ask(con, it["question"], ablation=ab, model=MODEL, llm=llm)
        ok = tr.refused == it["should_refuse"]
        score[ab] += ok
        line.append(f"{ab}:{'refused' if tr.refused else 'answered'}{'' if ok else '(WRONG)'}")
    print(f"{'REFUSE' if it['should_refuse'] else 'ANSWER'}  {' '.join(line)}  {it['question']}")
print({k: f"{v}/{len(items)}" for k, v in score.items()}, "calls", llm.calls)
