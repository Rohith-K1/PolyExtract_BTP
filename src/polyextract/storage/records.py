"""Extraction / validation / review persistence (same SQLite DB as discovery).

`records` is the unit of the dataset: one row per property measurement, linked to its sample and
molecular characteristics, carrying validation counts, confidence components, review status and route.
`review_events` is append-only: the audit trail of every human decision.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from polyextract.schemas import (
    MolecularCharacteristicsSchema,
    PolymerSampleSchema,
    PropertyMeasurementSchema,
)
from polyextract.validation.checks import Finding
from polyextract.validation.confidence import Confidence

SQL = """
CREATE TABLE IF NOT EXISTS documents (
    paper_id TEXT PRIMARY KEY, status TEXT NOT NULL, source_format TEXT, source_url TEXT, license TEXT,
    sha256 TEXT, n_tables INTEGER, n_tables_used INTEGER, note TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS samples (
    sample_id TEXT PRIMARY KEY, paper_id TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS molecular (
    mc_id TEXT PRIMARY KEY, sample_id TEXT NOT NULL, paper_id TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS measurements (
    measurement_id TEXT PRIMARY KEY, sample_id TEXT NOT NULL, paper_id TEXT NOT NULL,
    property_name TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS findings (
    record_id TEXT NOT NULL, paper_id TEXT NOT NULL, category TEXT NOT NULL, code TEXT NOT NULL,
    severity TEXT NOT NULL, message TEXT);
CREATE INDEX IF NOT EXISTS idx_findings_paper ON findings(paper_id);
CREATE TABLE IF NOT EXISTS records (
    record_id TEXT PRIMARY KEY, measurement_id TEXT NOT NULL, mc_id TEXT, sample_id TEXT NOT NULL,
    paper_id TEXT NOT NULL, property_name TEXT NOT NULL, confidence REAL NOT NULL, components TEXT NOT NULL,
    n_errors INTEGER NOT NULL, n_warnings INTEGER NOT NULL, route TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'pending', extractor TEXT, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_records_status ON records(review_status);
CREATE TABLE IF NOT EXISTS audit_sample (
    record_id TEXT PRIMARY KEY, policy TEXT NOT NULL, seed INTEGER NOT NULL, drawn_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL, decision TEXT NOT NULL,
    reviewer TEXT NOT NULL, comment TEXT, corrections TEXT, snapshot TEXT NOT NULL, ts TEXT NOT NULL);
"""

# Route of a record before human review
ROUTE_BLOCKED = "blocked_errors"  # has error findings; reviewer must correct or reject
ROUTE_REVIEW = "needs_review"  # warnings or below threshold
ROUTE_PRIORITY_OK = "review_low_risk"  # clean + high confidence; still human-reviewed (fast batch accept)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SQL)


def save_document_status(conn, paper_id: str, status: str, *, fmt=None, url=None, license=None, sha=None,
                         n_tables=None, n_used=None, note: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?,?,?,?)",
        (paper_id, status, fmt, url, license, sha, n_tables, n_used, note, now()),
    )


def replace_paper_extraction(
    conn: sqlite3.Connection,
    paper_id: str,
    samples: list[PolymerSampleSchema],
    molecular: list[MolecularCharacteristicsSchema],
    measurements: list[PropertyMeasurementSchema],
    findings: list[Finding],
    scored: list[tuple[PropertyMeasurementSchema, MolecularCharacteristicsSchema | None, Confidence, str]],
    extractor: str,
) -> None:
    """Re-extraction replaces a paper's extracted rows but NEVER its review history; previously
    reviewed records keep their review status if the same record_id reappears."""
    prior = dict(conn.execute("SELECT record_id, review_status FROM records WHERE paper_id=?", (paper_id,)).fetchall())
    for t in ("samples", "molecular", "measurements", "findings", "records"):
        conn.execute(f"DELETE FROM {t} WHERE paper_id=?", (paper_id,))
    conn.executemany("INSERT INTO samples VALUES (?,?,?)", [(s.sample_id, paper_id, s.model_dump_json()) for s in samples])
    conn.executemany("INSERT INTO molecular VALUES (?,?,?,?)",
                     [(m.mc_id, m.sample_id, paper_id, m.model_dump_json()) for m in molecular])
    conn.executemany("INSERT INTO measurements VALUES (?,?,?,?,?)",
                     [(m.measurement_id, m.sample_id, paper_id, m.property_name.value, m.model_dump_json()) for m in measurements])
    conn.executemany("INSERT INTO findings VALUES (?,?,?,?,?,?)",
                     [(f.record_id, paper_id, f.category.value, f.code, f.severity.value, f.message) for f in findings])
    for m, mc, conf, route in scored:
        status = prior.get(m.measurement_id, "pending")
        if status in ("needs_reextraction", "machine_verified", "duplicate", "excluded_computed"):
            status = "pending"  # system decisions are re-made by the policies on the NEW extraction
        conn.execute(
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (m.measurement_id, m.measurement_id, mc.mc_id if mc else None, m.sample_id, paper_id, m.property_name.value,
             conf.overall, json.dumps(conf.components), conf.blocking_errors, conf.warnings, route, status, extractor, now()),
        )
