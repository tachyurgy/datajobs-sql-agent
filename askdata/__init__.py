"""askdata: a schema-grounded text-to-SQL agent over the Data Jobs Observatory warehouse, plus its eval harness."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "data" / "snapshot"
DICTIONARY = ROOT / "data" / "dictionary.json"
FEWSHOT = ROOT / "gold" / "fewshot.toml"
GOLD = ROOT / "gold" / "gold.toml"
RESULTS = ROOT / "results"
SNAPSHOT_DATE = "2026-09-29"
