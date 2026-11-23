"""Build the submission package:  .venv/Scripts/python.exe scripts/make_submission.py [--version v0.1]

Writes data/processed/submission_<version>/ (see polyextract/dataset/release.py for the file list).
Duplicates and computed values are taken out of review first (review/dedup.py), then the release is built.
"""

import argparse
import json
import sqlite3
from pathlib import Path

from polyextract.dataset.release import build_release
from polyextract.review.dedup import apply_dedup

ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="v0.1")
    v = ap.parse_args().version
    conn = sqlite3.connect(ROOT / "data" / "db" / "polyextract.sqlite")
    conn.row_factory = sqlite3.Row
    print("dedup:", apply_dedup(conn))
    out = ROOT / "data" / "processed" / f"submission_{v}"
    stats = build_release(conn, out, v)
    print(json.dumps({k: stats[k] for k in ("hygiene", "dataset_samples", "dataset_records", "dataset_papers",
                                            "dataset_tiers", "audit")}, indent=2, default=str))
    print("written to", out)
