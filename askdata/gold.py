"""Load the gold set and verify it: execute every gold query, cross-check against the independent
`check_sql` formulation and the pinned `expect_*` values, and write results/gold_results.json."""
from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass

from . import GOLD, RESULTS
from .compare import canon, results_match
from .db import connect, run


@dataclass
class GoldItem:
    id: str
    category: str
    difficulty: int
    question: str
    answerable: bool = True
    gold_sql: str | None = None
    check_sql: str | None = None
    order_key: str | None = None
    alt_sql: list | None = None
    expect_rows: int | None = None
    expect_value: object = None
    note: str = ""


def load(path=GOLD) -> list[GoldItem]:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return [GoldItem(**{k: (v.strip() if isinstance(v, str) else v) for k, v in q.items()}) for q in data["q"]]


def alt_results(con, item: GoldItem) -> list:
    out = []
    for sql in item.alt_sql or []:
        r = run(con, sql, guarded=True)
        assert not r.error, (item.id, r.error)
        out.append((r.columns, r.rows))
    return out


def verify(verbose: bool = True) -> dict:
    con = connect()
    items = load()
    ids = [i.id for i in items]
    assert len(ids) == len(set(ids)), "duplicate ids"
    out, problems = {}, []
    for it in items:
        if not it.answerable:
            assert it.gold_sql is None, it.id
            out[it.id] = {"answerable": False}
            continue
        r = run(con, it.gold_sql, guarded=True)
        if r.error:
            problems.append(f"{it.id}: gold SQL failed: {r.error}")
            continue
        checks = []
        if not r.rows:
            problems.append(f"{it.id}: gold result is empty")
        if it.check_sql:
            c = run(con, it.check_sql, guarded=True)
            ok, why = (False, c.error) if c.error else results_match(r.columns, r.rows, c.columns, c.rows)
            checks.append(f"check_sql:{'agree' if ok else 'DISAGREE ' + why}")
            if not ok:
                problems.append(f"{it.id}: check_sql disagrees ({why})")
        for a_cols, a_rows in alt_results(con, it):
            same = results_match(r.columns, r.rows, a_cols, a_rows)[0]
            checks.append(f"alt_sql:{'same as gold' if same else 'differs (accepted alternative)'} {[tuple(canon(v) for v in x) for x in a_rows[:3]]}")
        if it.expect_rows is not None:
            ok = len(r.rows) == it.expect_rows
            checks.append(f"expect_rows={it.expect_rows}:{'ok' if ok else 'FAIL'}")
            if not ok:
                problems.append(f"{it.id}: expected {it.expect_rows} rows, got {len(r.rows)}")
        if it.expect_value is not None:
            got = canon(r.rows[0][0]) if r.rows else None
            ok = results_match(["v"], [(it.expect_value,)], ["v"], [(r.rows[0][0],)])[0] if r.rows and len(r.rows) == 1 else False
            checks.append(f"expect_value={it.expect_value}:{'ok' if ok else 'FAIL got ' + repr(got)}")
            if not ok:
                problems.append(f"{it.id}: expected value {it.expect_value!r}, got {got!r}")
        if it.order_key and it.order_key.lower() not in [c.lower() for c in r.columns]:
            problems.append(f"{it.id}: order_key {it.order_key} not a gold column")
        out[it.id] = {"answerable": True, "columns": r.columns, "n_rows": len(r.rows),
                      "preview": [[canon(v) for v in row] for row in r.rows[:12]], "checks": checks}
        if verbose:
            print(f"--- {it.id} [{it.category}] {it.question}")
            print("    cols:", r.columns, "rows:", len(r.rows), "|", "; ".join(checks))
            for row in r.rows[:8]:
                print("    ", tuple(canon(v) for v in row))
    summary = {"n_items": len(items), "n_answerable": sum(i.answerable for i in items),
               "n_with_check_sql": sum(bool(i.check_sql) for i in items),
               "n_with_expect": sum(i.expect_rows is not None or i.expect_value is not None for i in items),
               "problems": problems, "items": out}
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "gold_results.json").write_text(json.dumps(summary, indent=1, default=str) + "\n")
    return summary


if __name__ == "__main__":
    s = verify()
    print(f"\n{s['n_items']} items, {s['n_answerable']} answerable, {s['n_with_check_sql']} with an independent check query, "
          f"{s['n_with_expect']} with pinned values")
    print("PROBLEMS:" if s["problems"] else "no problems", *s["problems"], sep="\n  ")
    raise SystemExit(1 if s["problems"] else 0)
