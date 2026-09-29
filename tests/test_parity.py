"""The web runtime (web/public/js/core.mjs) must build byte-identical prompts and reach identical guard
verdicts to the Python package the eval runs, so the published accuracy numbers describe the live app."""
import json
import shutil
import subprocess

import pytest

from askdata import ROOT
from askdata.export_web import FN_ASSETS, assets
from askdata.gold import load
from askdata.guard import check_sql
from askdata.llm import parse_reply
from askdata.prompt import build_prompt, system_prompt

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

ATTEMPTS = [{"sql": "SELECT nope FROM fct_posting", "error": "Binder Error: column nope not found"},
            {"sql": "SELECT 1 FROM fct_posting WHERE family = 'x'", "error": "The query ran but returned 0 rows."}]
REPLIES = ['{"sql": "SELECT 1", "refuse": false, "reason": "ok"}', '```json\n{"sql": "", "refuse": true, "reason": "no data"}\n```',
           "Sure: SELECT count(*) FROM fct_posting", '{"sql": "-- refuse: nope", "refuse": false, "reason": ""}', "{bad json}"]


def test_assets_are_current():
    assert json.loads(FN_ASSETS.read_text()) == json.loads(json.dumps(assets(), ensure_ascii=False)), \
        "web/shared/assets.json is stale: run `make web-assets`"


def test_js_matches_python():
    corpus = json.loads((ROOT / "tests" / "guard_corpus.json").read_text())
    questions = [g.question for g in load()] + ["Top 5 companies for data engineers by median pay, café edition", "   "]
    fx = {"questions": questions, "attempts": ATTEMPTS, "sql": corpus["allow"] + corpus["deny"], "replies": REPLIES}
    out = subprocess.run(["node", str(ROOT / "tests" / "parity.mjs")], input=json.dumps(fx), capture_output=True,
                         text=True, check=True).stdout
    js = json.loads(out)
    py_prompts = [build_prompt(q, ab, ATTEMPTS if ab in "de" else []) for q in questions for ab in "abcde"]
    mismatches = [i for i, (a, b) in enumerate(zip(py_prompts, js["prompts"])) if a != b]
    assert not mismatches, f"{len(mismatches)} prompts differ, first: {questions[mismatches[0] // 5]!r}"
    assert js["systems"] == [system_prompt(ab) for ab in "abcde"]
    assert len(js["prompts"]) == len(py_prompts)
    for s, (ok, reason) in zip(fx["sql"], js["verdicts"]):
        v = check_sql(s)
        assert ok == v.ok, (s, v.reason, reason)
    for r, js_r in zip(REPLIES, js["replies"]):
        sql, refuse, reason, _ = parse_reply(r)
        assert [sql, refuse, reason] == js_r, r
