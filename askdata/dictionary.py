"""Build the data dictionary (tables, columns, types, descriptions, sample values) that grounds the prompt.

Descriptions come from the column/table comments the Observatory writes into observatory.duckdb, plus
a small set of additions for columns the upstream left undocumented. Sample values are computed from
the frozen snapshot. The output, data/dictionary.json, is committed and is the single source both the
Python agent and the web runtime read.
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

from . import DICTIONARY, SNAPSHOT, SNAPSHOT_DATE
from .db import connect

UPSTREAM_DB = Path.home() / "Desktop" / "datajobs-observatory" / "data" / "observatory.duckdb"

# Notes 1-6 restate the header of the upstream docs/SCHEMA.md. Notes 7-8 describe the snapshot and the SQL
# dialect. None of them were written against specific gold questions (see README, "Leakage").
NOTES = [
    "Grain of the central fact: one row per posting (platform:board:job_id) in fct_posting.",
    "Default filter for market questions: is_open AND remote_us AND is_canonical_listing.",
    "Pay is annual USD (comp_*_usd); comp_mid_usd is the range midpoint, null when no range was posted.",
    "Families: ai_engineer, analytics_engineer, data_analyst, data_engineer, data_scientist, ml_engineer, product_analyst.",
    "Seniority: intern, entry, mid, unlabeled, senior, staff+, manager+ (from the title).",
    "Joins: fct_posting.company_key = dim_company.company_key; posting_skills.posting_key = fct_posting.posting_key; "
    "posting_skills.skill = dim_skill.skill; posting_versions_scd2.posting_key = fct_posting.posting_key.",
    f"The snapshot is one crawl day, {SNAPSHOT_DATE}; treat it as today. There is no multi-day history yet.",
    "SQL dialect is DuckDB. Timestamps are timestamptz in UTC.",
]

EXTRA_TABLE_DOCS = {
    "dim_skill": "One row per skill in the 83-pattern skill taxonomy, with its cluster.",
}

EXTRA_COLUMN_DOCS = {
    ("posting_skills", "posting_key"): "Joins fct_posting.posting_key.",
    ("posting_skills", "skill"): "Skill name, lower case (see dim_skill).",
    ("dim_company", "company_key"): "platform:board_token; joins fct_posting.company_key.",
    ("dim_company", "platform"): "greenhouse, ashby or lever.",
    ("dim_skill", "skill"): "Skill name, lower case.",
    ("mart_skill_demand", "scope"): "'all' = all open remote-US listings in the family; 'doorway' = only doorway listings.",
    ("mart_comp_by_family", "seniority"): "Null on family-level and all-families rows. For a family-level figure filter seniority IS NULL.",
    ("posting_versions_scd2", "change_kind"): "created, pay_changed, retitled, relocated, description_edited.",
    ("posting_versions_scd2", "valid_from"): "Version start date.",
    ("posting_versions_scd2", "valid_to"): "Version end date (null while current).",
    ("mart_daily_market", "remote_us"): "Remote-US flag of the listings counted in this row.",
}

LOW_CARDINALITY = 30
SKILL_LIKE = {("dim_skill", "skill"), ("posting_skills", "skill"), ("mart_skill_demand", "skill"),
              ("dim_skill", "cluster_label"), ("posting_skills", "cluster_label"), ("mart_skill_demand", "cluster_label")}


def _upstream_comments() -> tuple[dict, dict]:
    tables, cols = {}, {}
    if not UPSTREAM_DB.exists():
        return tables, cols
    c = duckdb.connect(str(UPSTREAM_DB), read_only=True)
    for t, cm in c.execute("select table_name, comment from duckdb_tables()").fetchall():
        if cm:
            tables[t] = cm
    for t, col, cm in c.execute("select table_name, column_name, comment from duckdb_columns()").fetchall():
        if cm:
            cols[(t, col)] = cm
    c.close()
    return tables, cols


def _samples(con, table: str, col: str, dtype: str) -> list[str]:
    q = f'"{col}"'
    if dtype == "BOOLEAN":
        return []
    if dtype in ("VARCHAR",):
        n = con.execute(f"select count(distinct {q}) from {table}").fetchone()[0]
        if n <= LOW_CARDINALITY or (table, col) in SKILL_LIKE:
            vals = con.execute(f"select {q} from {table} where {q} is not null group by 1 order by count(*) desc, 1").fetchall()
            return [v[0] for v in vals]
        vals = con.execute(f"select {q} from {table} where {q} is not null group by 1 order by count(*) desc, 1 limit 3").fetchall()
        return [str(v[0])[:80] for v in vals]
    lo, hi = con.execute(f"select min({q}), max({q}) from {table}").fetchone()
    if lo is None:
        return []
    return [f"min {lo}", f"max {hi}"]


def build(out: Path = DICTIONARY) -> dict:
    con = connect(SNAPSHOT)
    tcom, ccom = _upstream_comments()
    tables = []
    order = ["fct_posting", "dim_company", "posting_skills", "dim_skill", "mart_comp_by_family", "mart_skill_demand",
             "mart_daily_market", "mart_doorway_roles", "mart_degree_requirements", "mart_crawl_coverage",
             "posting_versions_scd2"]
    for t in order:
        cols = []
        info = con.execute(f"select column_name, data_type from duckdb_columns() where table_name = '{t}' order by column_index").fetchall()
        for col, dtype in info:
            desc = EXTRA_COLUMN_DOCS.get((t, col)) or ccom.get((t, col), "")
            cols.append({"name": col, "type": dtype, "description": desc, "samples": _samples(con, t, col, dtype)})
        rows = con.execute(f"select count(*) from {t}").fetchone()[0]
        tables.append({"name": t, "description": EXTRA_TABLE_DOCS.get(t) or tcom.get(t, ""), "rows": rows, "columns": cols})
    d = {"snapshot_date": SNAPSHOT_DATE, "notes": NOTES, "tables": tables}
    out.write_text(json.dumps(d, indent=1, ensure_ascii=False, default=str) + "\n")
    return d


if __name__ == "__main__":
    d = build()
    print(sum(len(t["columns"]) for t in d["tables"]), "columns documented")
