import json

import pytest

from askdata import ROOT
from askdata.db import connect, run
from askdata.guard import check_sql, lexical_check

CORPUS = json.loads((ROOT / "tests" / "guard_corpus.json").read_text())


@pytest.mark.parametrize("sql", CORPUS["allow"])
def test_allowed(sql):
    v = check_sql(sql)
    assert v.ok, v.reason


@pytest.mark.parametrize("sql", CORPUS["deny"])
def test_denied(sql):
    assert not check_sql(sql).ok


def test_trailing_semicolon_stripped():
    assert lexical_check("SELECT 1 ;; \n").sql == "SELECT 1"


def test_run_blocks_writes_and_leaves_data_intact():
    con = connect()
    before = con.execute("select count(*) from fct_posting").fetchone()[0]
    r = run(con, "DELETE FROM fct_posting")
    assert r.error and "guard" in r.error
    assert con.execute("select count(*) from fct_posting").fetchone()[0] == before


def test_external_access_disabled_even_unguarded():
    con = connect()
    r = run(con, "SELECT * FROM read_csv('/etc/hosts')", guarded=False)
    assert r.error


def test_row_limit_and_truncation():
    con = connect()
    r = run(con, "SELECT * FROM range(5000)", row_limit=100)
    assert len(r.rows) == 100 and r.truncated


def test_timeout_interrupts_long_query():
    con = connect()
    r = run(con, "SELECT count(*) FROM range(100000000) a, range(100000) b", timeout_s=0.5)
    assert r.error and "timed out" in r.error
