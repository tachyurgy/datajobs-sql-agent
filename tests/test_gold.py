import re

from askdata.compare import results_match
from askdata.db import connect, run
from askdata.gold import load, verify
from askdata.prompt import STOPWORDS, load_fewshots


# Domain boilerplate that appears in most questions of either set and says nothing about the task.
DOMAIN = {"open", "remote", "us", "data", "listing", "listings", "role", "roles", "posting", "postings"}


def content_words(s):
    return {w for w in re.split(r"[^a-z0-9]+", s.lower()) if w and w not in STOPWORDS and w not in DOMAIN}


def test_gold_set_shape():
    items = load()
    assert len(items) == 60
    cats = {i.category for i in items}
    assert cats == {"lookup", "aggregation", "join", "window", "date", "ambiguous", "unanswerable"}
    assert sum(not i.answerable for i in items) == 7


def test_gold_queries_verify():
    s = verify(verbose=False)
    assert s["problems"] == []


def test_fewshots_execute_and_answer_no_gold_question():
    """No few-shot example may be the answer to a gold question: its SQL must not return a result that
    matches any gold result. (Lexical closeness is reported, not forbidden: several examples are template
    siblings of gold questions with a different entity, e.g. Tableau vs SQL; see README "Leakage".)"""
    con = connect()
    gold = [(g, run(con, g.gold_sql)) for g in load() if g.answerable]
    for ex in load_fewshots():
        if ex["sql"].startswith("--"):
            continue
        r = run(con, ex["sql"])
        assert r.error is None and r.rows, (ex["question"], r.error)
        w = content_words(ex["question"])
        for g, gr in gold:
            same = results_match(gr.columns, gr.rows, r.columns, r.rows)[0]
            # a lone number can coincide by chance (51 listings published in 2025 = 51 failed boards);
            # only a result match between lexically related questions counts as a leak
            related = len(w & content_words(g.question)) / len(w) >= 0.34
            assert not (same and (related or len(gr.rows) * len(gr.columns) > 1)), (ex["question"], g.id)


def fewshot_overlap_report():
    """(example, nearest gold id, share of the example's content words also in that gold question)."""
    gold = load()
    out = []
    for ex in load_fewshots():
        w = content_words(ex["question"])
        best = max(gold, key=lambda g: len(w & content_words(g.question)) / len(w))
        out.append((ex["question"], best.id, round(len(w & content_words(best.question)) / len(w), 2)))
    return out
