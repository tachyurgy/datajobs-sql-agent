"""Execution-accuracy comparison between a gold result and a predicted result.

Rules (documented in the README and on the results page):
  * Row sets are compared as multisets (order-insensitive) unless the gold item sets `order_key`;
    then the sequence of that column's values must also match, so ties can come back in any order.
  * Column names and column order are ignored. Every gold column must map to a distinct predicted
    column holding the same values row for row; extra predicted columns are allowed (a question
    answered with a bit more context is still correct).
  * Numbers match within a relative tolerance of 1e-3 (absolute 1e-6), or when the prediction equals
    the gold value rounded to the prediction's own number of decimals (>= 1). A column that holds the
    same fractions expressed as percentages (x100) also matches.
  * Dates and timestamps at midnight compare as dates; a timestamp also matches a gold date on the same day.
    Strings compare trimmed and case-insensitively. Integers compare exactly (no tolerance).
  * Matching claims exact-equality pairs first, then tolerant ones, so the result is order-independent.
  * Row counts must be equal; an empty gold result only matches an empty prediction.
"""
from __future__ import annotations

import datetime as dt
import decimal
import math
from itertools import product
from typing import Any

REL_TOL = 1e-3
ABS_TOL = 1e-6


def canon(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float, decimal.Decimal)):
        f = float(v)
        return None if math.isnan(f) else f
    if isinstance(v, dt.datetime):
        if v.hour == v.minute == v.second == v.microsecond == 0:
            return v.date().isoformat()
        return v.replace(tzinfo=None).isoformat(sep=" ")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, dt.timedelta):
        return float(v.days)
    s = str(v).strip()
    # timestamps rendered as strings, e.g. "2026-07-01 00:00:00"
    if len(s) >= 19 and s[10] == " " and s[11:19] == "00:00:00" and s[:4].isdigit():
        return s[:10]
    return s.lower()


def _decimals(x: float) -> int:
    r = repr(x)
    if "e" in r or "." not in r:
        return 0
    return len(r.split(".")[1].rstrip("0"))


def num_eq(pred: float, gold: float) -> bool:
    if pred.is_integer() and gold.is_integer():
        return pred == gold  # counts, years, ids: no tolerance (2021 must not match 2022)
    if math.isclose(pred, gold, rel_tol=REL_TOL, abs_tol=ABS_TOL):
        return True
    d = _decimals(pred)
    return d >= 1 and round(gold, d) == pred


def val_eq(pred: Any, gold: Any, scale: float = 1.0) -> bool:
    if pred is None or gold is None:
        return pred is None and gold is None
    if isinstance(pred, float) and isinstance(gold, float):
        return num_eq(pred, gold * scale)
    if isinstance(pred, float) != isinstance(gold, float):
        # "3" vs 3.0 can happen when a model casts; compare numerically when both parse
        try:
            return num_eq(float(pred), float(gold) * scale)
        except (TypeError, ValueError):
            return False
    if isinstance(pred, str) and isinstance(gold, str) and len(gold) == 10 and gold[4] == "-" and gold[7] == "-":
        return pred == gold or pred.startswith(gold + " ")  # a timestamp on the gold date answers "which date"
    return pred == gold


def _match_all(gold: list, pred: list, eq) -> bool:
    """Multiset match. Exact-equality pairs are claimed first so a tolerant comparison can never steal
    a value that has an exact partner (greedy tolerant matching is order-dependent otherwise)."""
    if len(pred) != len(gold):
        return False
    used = [False] * len(pred)
    rest = []
    for g in gold:
        for i, p in enumerate(pred):
            if not used[i] and p == g:
                used[i] = True
                break
        else:
            rest.append(g)
    for g in rest:
        for i, p in enumerate(pred):
            if not used[i] and eq(p, g):
                used[i] = True
                break
        else:
            return False
    return True


def _col(rows: list[tuple], j: int) -> list[Any]:
    return [r[j] for r in rows]


def _column_candidates(gold_col: list, pred_col: list) -> list[float]:
    """Scales under which the predicted column could equal the gold column as a multiset."""
    out = []
    for scale in (1.0, 100.0, 0.01):
        if scale != 1.0 and not all(isinstance(g, float) or g is None for g in gold_col):
            continue
        if _multiset_eq(pred_col, gold_col, scale):
            out.append(scale)
    return out


def _multiset_eq(pred: list, gold: list, scale: float) -> bool:
    return _match_all(gold, pred, lambda p, g: val_eq(p, g, scale))


def _rows_match(gold: list[tuple], pred: list[tuple], mapping: list[tuple[int, float]]) -> bool:
    proj = [tuple(p[pj] for pj, _ in mapping) for p in pred]
    scales = [sc for _, sc in mapping]
    return _match_all(gold, proj, lambda p, g: all(val_eq(x, y, sc) for x, y, sc in zip(p, g, scales)))


def results_match(gold_cols: list[str], gold_rows: list[tuple], pred_cols: list[str], pred_rows: list[tuple],
                  order_key: str | None = None) -> tuple[bool, str]:
    g = [tuple(canon(v) for v in r) for r in gold_rows]
    p = [tuple(canon(v) for v in r) for r in pred_rows]
    if len(g) != len(p):
        return False, f"row count {len(p)} != gold {len(g)}"
    if not g:
        return True, "both empty"
    if len(pred_cols) < len(gold_cols):
        return False, f"{len(pred_cols)} columns < gold {len(gold_cols)}"
    cands: list[list[tuple[int, float]]] = []
    for gj in range(len(gold_cols)):
        gc = _col(g, gj)
        opts = [(pj, sc) for pj in range(len(pred_cols)) for sc in _column_candidates(gc, _col(p, pj))]
        if not opts:
            return False, f"no predicted column matches gold column '{gold_cols[gj]}'"
        cands.append(opts)
    n_combos = 1
    for c in cands:
        n_combos *= len(c)
    if n_combos > 5000:
        cands = [c[:3] for c in cands]
    for mapping in product(*cands):
        if len({pj for pj, _ in mapping}) != len(mapping):
            continue
        if not _rows_match(g, p, list(mapping)):
            continue
        if order_key is not None:
            k = [c.lower() for c in gold_cols].index(order_key.lower())
            pj, sc = mapping[k]
            if not all(val_eq(pr[pj], gr[k], sc) for pr, gr in zip(p, g)):
                continue
        return True, "match"
    return False, "values differ" if order_key is None else "values or order differ"
