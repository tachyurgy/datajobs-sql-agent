"""Export everything the web runtime needs, from the same Python sources the eval uses:
prompt assets (system prompt, retrieval vocabulary, dictionary, few-shots, guard lists) and the parquet snapshot."""
from __future__ import annotations

import json
import shutil

from . import ROOT, SNAPSHOT
from . import guard, prompt

WEB_DATA = ROOT / "web" / "public" / "data"
FN_ASSETS = ROOT / "web" / "shared" / "assets.json"


def assets() -> dict:
    return {
        "system": prompt.SYSTEM,
        "stopwords": sorted(prompt.STOPWORDS),
        "synonyms": prompt.SYNONYMS,
        "top_k_columns": prompt.TOP_K_COLUMNS,
        "top_k_examples": prompt.TOP_K_EXAMPLES,
        "sample_chars": prompt.SAMPLE_CHARS,
        "max_same_column_name": prompt.MAX_SAME_COLUMN_NAME,
        "dictionary": prompt.load_dictionary(),
        "fewshots": prompt.load_fewshots(),
        "guard": {
            "known_tables": sorted(guard.KNOWN_TABLES),
            "deny_keywords": sorted(guard.DENY_KEYWORDS),
            "deny_functions": sorted(guard.DENY_FUNCTIONS),
            "allowed_table_functions": sorted(guard.ALLOWED_TABLE_FUNCTIONS),
            "from_in_function": sorted(guard.FROM_IN_FUNCTION),
        },
    }


def export() -> None:
    WEB_DATA.mkdir(parents=True, exist_ok=True)
    FN_ASSETS.parent.mkdir(parents=True, exist_ok=True)
    a = assets()
    FN_ASSETS.write_text(json.dumps(a, ensure_ascii=False) + "\n")
    (WEB_DATA / "guard.json").write_text(json.dumps(a["guard"]) + "\n")
    for f in SNAPSHOT.glob("*.parquet"):
        shutil.copy2(f, WEB_DATA / f.name)
    print("exported", FN_ASSETS.relative_to(ROOT), "and", len(list(SNAPSHOT.glob('*.parquet'))), "parquet files")


if __name__ == "__main__":
    export()
