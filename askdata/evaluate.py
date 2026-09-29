"""Eval harness: run the gold set through every ablation on every model, score, and summarise.

    python -m askdata.evaluate --models gemini-2.5-flash-lite gemma-4-31b-it
    python -m askdata.evaluate --offline      # re-score from the committed reply cache, no API calls

Every model reply is cached under results/cache/<model>/<sha256(model+prompt)>.json. A re-run with the
same prompts is free and deterministic; `--offline` fails on a cache miss instead of calling the API.
Ablation d reuses ablation c's first reply (identical prompt at temperature 0) and only spends calls on
its retries.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from dataclasses import asdict

from . import RESULTS, SNAPSHOT_DATE
from .agent import ask
from .compare import results_match
from .db import connect, run
from .gold import GoldItem, alt_results, load
from .llm import LLMReply, RateLimited, generate
from .prompt import build_prompt

ABLATIONS = ["a", "b", "c", "d", "e"]
ABLATION_LABELS = {
    "a": "Zero-shot, full schema",
    "b": "+ retrieved column docs and sample values",
    "c": "+ retrieved few-shot examples",
    "d": "+ self-correction (max 2 retries)",
    "e": "+ answerability rule (tuned on a separate dev set)",
}
MIN_INTERVAL_S = {"gemini-2.5-flash-lite": 4.5, "gemini-2.5-flash": 6.5, "gemma-4-31b-it": 2.5}
CACHE = RESULTS / "cache"
BOOT = 2000


class CachedLLM:
    def __init__(self, offline: bool):
        self.offline = offline
        self.calls = 0
        self.hits = 0
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def __call__(self, model: str, prompt: str, system: str | None = None) -> LLMReply:
        # replies under the default system prompt keep their original cache key
        key = f"{model}\n{prompt}" if system is None else f"{model}\n{system}\n{prompt}"
        h = hashlib.sha256(key.encode()).hexdigest()[:24]
        path = CACHE / model / f"{h}.json"
        if path.exists():
            with self._lock:
                self.hits += 1
            return LLMReply(**json.loads(path.read_text()))
        if self.offline:
            raise LookupError(f"cache miss for {model} ({h}) in offline mode")
        with self._lock:  # reserve a start slot so concurrent workers still respect the per-model pace
            now = time.monotonic()
            start = max(now, self._next.get(model, 0.0))
            self._next[model] = start + MIN_INTERVAL_S.get(model, 5.0)
        if start > now:
            time.sleep(start - now)
        reply = generate(model, prompt) if system is None else generate(model, prompt, system=system)
        with self._lock:
            self.calls += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(reply), indent=1) + "\n")
        return reply


def score(item: GoldItem, trace, gold_cache: dict, con) -> dict:
    row = {"id": item.id, "category": item.category, "difficulty": item.difficulty, "answerable": item.answerable,
           "refused": trace.refused, "refuse_reason": trace.refuse_reason, "sql": trace.final_sql,
           "retries": trace.retries, "attempts": len(trace.attempts),
           "latency_ms": round(sum(a.latency_ms for a in trace.attempts)), "tokens": trace.tokens(),
           "error": trace.attempts[-1].error if trace.attempts else None,
           "attempt_sqls": [a.sql for a in trace.attempts], "attempt_errors": [a.error for a in trace.attempts]}
    if not item.answerable:
        row["correct"] = trace.refused
        row["verdict"] = "refused" if trace.refused else "answered an unanswerable question"
        return row
    if trace.refused:
        row.update(correct=False, strict=False, verdict="refused an answerable question")
        return row
    res = trace.result
    if res is None or res.error:
        row.update(correct=False, strict=False, verdict="execution error")
        return row
    g_cols, g_rows = gold_cache[item.id]
    ok, why = results_match(g_cols, g_rows, res.columns, res.rows, item.order_key)
    strict = ok
    if not ok:
        for a_cols, a_rows in alt_results(con, item):
            if results_match(a_cols, a_rows, res.columns, res.rows, item.order_key)[0]:
                ok, why = True, "matched accepted alternative"
                break
    row.update(correct=ok, strict=strict, verdict=why, n_rows=len(res.rows),
               preview=[[str(v) for v in r] for r in res.rows[:5]], columns=res.columns)
    return row


# ------------------------------------------------------------------ error taxonomy (heuristic)
DEFAULT_FILTER = ("is_open", "remote_us", "is_canonical_listing")


def _tables(sql: str) -> set[str]:
    from .guard import KNOWN_TABLES
    words = set(re.findall(r"[a-z_]+", sql.lower()))
    return {t for t in KNOWN_TABLES if t in words}


def _literals(sql: str) -> set[str]:
    return {m.lower() for m in re.findall(r"'([^']*)'", sql)}


def classify(row: dict, item: GoldItem) -> str | None:
    if row["correct"]:
        return None
    if not item.answerable:
        return "answered an unanswerable question"
    if row["refused"]:
        return "refused an answerable question"
    err = (row.get("error") or "").lower()
    sql = (row["sql"] or "").lower()
    gold = (item.gold_sql or "").lower()
    if err and err != "empty result":
        if "referenced column" in err or "not found" in err or "does not exist" in err or "unknown table" in err:
            return "hallucinated column or table"
        return "other execution error"
    if err == "empty result":
        return "empty result (wrong filter value)"
    if all(f in gold for f in DEFAULT_FILTER) and not all(f in sql for f in DEFAULT_FILTER) and "mart_" not in sql:
        return "missing default market filter"
    if _tables(gold) - {"mart_comp_by_family", "mart_skill_demand", "mart_daily_market", "mart_crawl_coverage"} - _tables(sql) \
            or ("left join" in gold) != ("left join" in sql):
        return "wrong table or join"
    if item.category == "date":
        return "date logic"
    if _literals(gold) - _literals(sql):
        return "wrong filter value"
    if row.get("n_rows") is not None and "row count" in row.get("verdict", ""):
        return "wrong grouping or result shape"
    return "wrong aggregation or calculation"


# ------------------------------------------------------------------ statistics
def bootstrap_ci(values: list[float], n: int = BOOT, seed: int = 7) -> tuple[float, float]:
    if not values:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    k = len(values)
    means = sorted(sum(values[rng.randrange(k)] for _ in range(k)) / k for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def paired_diff_ci(x: list[float], y: list[float], n: int = BOOT, seed: int = 11) -> tuple[float, float, float]:
    rng = random.Random(seed)
    k = len(x)
    d = [b - a for a, b in zip(x, y)]
    means = sorted(sum(d[rng.randrange(k)] for _ in range(k)) / k for _ in range(n))
    return sum(d) / k, means[int(0.025 * n)], means[int(0.975 * n) - 1]


def summarise(rows: list[dict]) -> dict:
    ans = [r for r in rows if r["answerable"]]
    una = [r for r in rows if not r["answerable"]]
    ex = [float(r["correct"]) for r in ans]
    strict = [float(r.get("strict", False)) for r in ans]
    overall = [float(r["correct"]) for r in rows]
    by_cat: dict[str, list[float]] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(float(r["correct"]))
    tax: dict[str, int] = {}
    for r in rows:
        if r.get("error_class"):
            tax[r["error_class"]] = tax.get(r["error_class"], 0) + 1
    lat = [r["latency_ms"] for r in rows]
    return {
        "n": len(rows), "n_answerable": len(ans), "n_unanswerable": len(una),
        "execution_accuracy": sum(ex) / len(ex), "execution_accuracy_ci95": bootstrap_ci(ex),
        "strict_execution_accuracy": sum(strict) / len(strict),
        "refusal_accuracy": sum(r["correct"] for r in una) / len(una) if una else None,
        "false_refusals": sum(r["refused"] for r in ans),
        "overall_accuracy": sum(overall) / len(overall), "overall_accuracy_ci95": bootstrap_ci(overall),
        "by_category": {k: {"n": len(v), "accuracy": sum(v) / len(v)} for k, v in sorted(by_cat.items())},
        "mean_retries": statistics.mean(r["retries"] for r in rows),
        "questions_with_retries": sum(r["retries"] > 0 for r in rows),
        "execution_errors_final": sum(1 for r in ans if r.get("verdict") == "execution error"),
        "latency_ms_median": statistics.median(lat), "latency_ms_p90": sorted(lat)[int(0.9 * len(lat)) - 1],
        "prompt_tokens_mean": statistics.mean(r["tokens"]["prompt"] for r in rows),
        "output_tokens_mean": statistics.mean(r["tokens"]["output"] + r["tokens"]["thinking"] for r in rows),
        "error_taxonomy": dict(sorted(tax.items(), key=lambda kv: -kv[1])),
    }


def run_eval(models: list[str], offline: bool = False, only: list[str] | None = None, out_path=None,
             workers: int = 1) -> dict:
    out_path = out_path or RESULTS / "eval.json"
    con = connect()
    items = [i for i in load() if not only or i.id in only]
    gold_cache = {}
    for it in items:
        if it.answerable:
            r = run(con, it.gold_sql)
            gold_cache[it.id] = (r.columns, r.rows)
    llm = CachedLLM(offline)
    out = {"snapshot_date": SNAPSHOT_DATE, "n_questions": len(items), "models": {}, "ablations": ABLATION_LABELS}
    prior = out_path
    if prior.exists() and not only:
        out["models"] = json.loads(prior.read_text()).get("models", {})
    local = threading.local()

    def eval_item(model: str, it: GoldItem) -> dict[str, dict]:
        if not hasattr(local, "con"):
            local.con = connect()
        c = local.con
        rows, first_c = {}, None
        for ab in ABLATIONS:
            if ab == "d":
                tr = ask(c, it.question, ablation="d", model=model, llm=llm, first_reply=first_c)
            else:
                tr = ask(c, it.question, ablation=ab, model=model, llm=llm)
                if ab == "c":
                    first_c = llm(model, build_prompt(it.question, "c", []))
            row = score(it, tr, gold_cache, c)
            row["error_class"] = classify(row, it)
            rows[ab] = row
        print(f"{model} {it.id} " + " ".join(f"{a}:{'Y' if rows[a]['correct'] else '.'}" for a in ABLATIONS)
              + f"  (calls {llm.calls}, cache hits {llm.hits})", flush=True)
        return rows

    for model in models:
        status, done, failed = "complete", {}, []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {it.id: pool.submit(eval_item, model, it) for it in items}
            for qid, f in futs.items():
                try:
                    done[qid] = f.result()
                except (RateLimited, LookupError, RuntimeError) as e:
                    failed.append(f"{qid}: {e}")
        if failed:
            status = f"partial: {len(failed)} questions missing ({failed[0]})"
            print(status, flush=True)
        per_ab = {a: [done[it.id][a] for it in items if it.id in done] for a in ABLATIONS}
        if not per_ab["a"]:
            continue
        summ = {a: summarise(v) for a, v in per_ab.items()}
        base = [float(r["correct"]) for r in per_ab["a"]]
        for a in ABLATIONS[1:]:
            summ[a]["delta_vs_a_overall"] = paired_diff_ci(base, [float(r["correct"]) for r in per_ab[a]])
        for a, prev in zip(ABLATIONS[1:], ABLATIONS[:-1]):
            summ[a]["delta_vs_prev_overall"] = paired_diff_ci([float(r["correct"]) for r in per_ab[prev]],
                                                              [float(r["correct"]) for r in per_ab[a]])
        out["models"][model] = {"status": status, "n_questions": len(per_ab["a"]), "summary": summ, "rows": per_ab}
    if not only:
        RESULTS.mkdir(exist_ok=True)
        out_path.write_text(json.dumps(out, indent=1, default=str) + "\n")
    print(f"LLM calls made: {llm.calls}, cache hits: {llm.hits}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=["gemini-2.5-flash-lite", "gemma-4-31b-it"])
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--workers", type=int, default=1, help="questions evaluated concurrently (pace is still per model)")
    ap.add_argument("--out", help="results file (default results/eval.json); the report merges results/eval*.json")
    a = ap.parse_args()
    from pathlib import Path
    out = run_eval(a.models, a.offline, a.only, Path(a.out) if a.out else None, a.workers)
    if a.offline:
        committed = {}
        for f in sorted(RESULTS.glob("eval*.json")):
            committed.update(json.loads(f.read_text())["models"])
        bad = []
        for m in a.models:
            got, want = out["models"].get(m), committed.get(m)
            if want is None:
                continue
            for ab in ABLATIONS:
                g = [(r["id"], r["correct"]) for r in got["rows"][ab]] if got else []
                w = [(r["id"], r["correct"]) for r in want["rows"][ab]]
                if g != w:
                    bad.append(f"{m}/{ab}")
        if bad:
            raise SystemExit(f"offline re-score differs from committed results: {bad}")
        print("offline re-score matches the committed results")
    for m, d in out["models"].items():
        print(f"\n{m} [{d['status']}] n={d['n_questions']}")
        for ab, s in d["summary"].items():
            lo, hi = s["overall_accuracy_ci95"]
            print(f"  {ab} overall {s['overall_accuracy']:.3f} [{lo:.3f},{hi:.3f}]  exec {s['execution_accuracy']:.3f}  "
                  f"refusal {s['refusal_accuracy']}  retries {s['mean_retries']:.2f}  lat {s['latency_ms_median']:.0f}ms")


if __name__ == "__main__":
    main()
