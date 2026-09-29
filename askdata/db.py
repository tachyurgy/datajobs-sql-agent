"""Read-only execution of guarded SQL on an in-memory DuckDB loaded from the parquet snapshot."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from . import SNAPSHOT
from .guard import check_sql

ROW_LIMIT = 1000
TIMEOUT_S = 10.0


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    truncated: bool = False
    error: str | None = None
    elapsed_ms: float = 0.0


def connect(snapshot: Path = SNAPSHOT) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    # Both runtimes (Python and DuckDB-WASM) pin UTC so that date logic on timestamptz agrees.
    con.execute("SET TimeZone = 'UTC'")
    for f in sorted(Path(snapshot).glob("*.parquet")):
        con.execute(f"CREATE TABLE {f.stem} AS SELECT * FROM read_parquet('{f.as_posix()}')")
    # Nothing after this point may touch the filesystem or network.
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con


def run(con: duckdb.DuckDBPyConnection, sql: str, *, guarded: bool = True,
        row_limit: int = ROW_LIMIT, timeout_s: float = TIMEOUT_S) -> QueryResult:
    if guarded:
        verdict = check_sql(sql)
        if not verdict.ok:
            return QueryResult(error=f"Blocked by read-only guard: {verdict.reason}")
        sql = verdict.sql
    timer = threading.Timer(timeout_s, con.interrupt)
    t0 = time.perf_counter()
    timer.start()
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(row_limit + 1)
        truncated = len(rows) > row_limit
        return QueryResult(cols, rows[:row_limit], truncated, None, (time.perf_counter() - t0) * 1000)
    except duckdb.InterruptException:
        return QueryResult(error=f"Query timed out after {timeout_s:.0f}s", elapsed_ms=(time.perf_counter() - t0) * 1000)
    except duckdb.Error as e:
        msg = str(e).split("\n\nLINE")[0].strip()
        return QueryResult(error=msg[:600], elapsed_ms=(time.perf_counter() - t0) * 1000)
    finally:
        timer.cancel()
