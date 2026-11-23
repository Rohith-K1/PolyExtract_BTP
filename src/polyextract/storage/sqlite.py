"""SQLite repository. Pydantic models are stored as JSON payloads, with key columns indexed.

Tables are created idempotently. Later phases add samples, molecular characteristics,
measurements, validation results and review events to this same database.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from polyextract.discovery.models import DiscoveryCandidate
from polyextract.schemas.paper import PaperMetadataSchema

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS papers (
    paper_id TEXT PRIMARY KEY,
    doi TEXT,
    title TEXT NOT NULL,
    publication_year INTEGER,
    data_origin TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);

CREATE TABLE IF NOT EXISTS discovery_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    config TEXT NOT NULL,
    report TEXT
);

CREATE TABLE IF NOT EXISTS discovery_candidates (
    run_id TEXT NOT NULL REFERENCES discovery_runs(run_id),
    paper_id TEXT NOT NULL REFERENCES papers(paper_id),
    decision TEXT NOT NULL,
    score REAL NOT NULL,
    filter_version TEXT NOT NULL,
    queries TEXT NOT NULL,
    dedup_method TEXT NOT NULL,
    filter_payload TEXT NOT NULL,
    PRIMARY KEY (run_id, paper_id)
);
CREATE INDEX IF NOT EXISTS idx_cand_decision ON discovery_candidates(decision);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA_SQL)

    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- papers --------------------------------------------------------------------------
    def get_paper(self, paper_id: str) -> PaperMetadataSchema | None:
        row = self.conn.execute("SELECT payload FROM papers WHERE paper_id = ?", (paper_id,)).fetchone()
        return PaperMetadataSchema.model_validate_json(row["payload"]) if row else None

    def upsert_paper(self, paper: PaperMetadataSchema) -> PaperMetadataSchema:
        """Insert, or merge with an existing row: union sources/ids, keep earliest discovery time."""
        existing = self.get_paper(paper.paper_id)
        if existing is not None:
            merged_ids = {**paper.source_ids, **existing.source_ids}
            update = {
                "retrieval_sources": sorted(set(existing.retrieval_sources) | set(paper.retrieval_sources)),
                "source_ids": merged_ids,
                "discovered_at": min(existing.discovered_at, paper.discovered_at),
                "fulltext_status": existing.fulltext_status,  # later phases own this field
            }
            if existing.abstract and (not paper.abstract or len(existing.abstract) > len(paper.abstract)):
                update["abstract"] = existing.abstract
            paper = paper.model_copy(update=update)
        self.conn.execute(
            """INSERT INTO papers(paper_id, doi, title, publication_year, data_origin, payload, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(paper_id) DO UPDATE SET doi=excluded.doi, title=excluded.title,
                 publication_year=excluded.publication_year, data_origin=excluded.data_origin,
                 payload=excluded.payload, updated_at=excluded.updated_at""",
            (
                paper.paper_id, paper.doi, paper.title, paper.publication_year,
                paper.data_origin.value, paper.model_dump_json(), _now(),
            ),
        )
        return paper

    # ---- discovery runs ------------------------------------------------------------------
    def start_run(self, run_id: str, config: dict) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO discovery_runs(run_id, started_at, config) VALUES (?,?,?)",
                (run_id, _now(), json.dumps(config, default=str)),
            )

    def finish_run(self, run_id: str, report: dict) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE discovery_runs SET finished_at=?, report=? WHERE run_id=?",
                (_now(), json.dumps(report, default=str), run_id),
            )

    def save_candidates(self, run_id: str, candidates: list[DiscoveryCandidate]) -> None:
        with self.tx() as c:
            for cand in candidates:
                self.upsert_paper(cand.paper)
                f = cand.filter
                assert f is not None, "candidate must be filtered before saving"
                c.execute(
                    """INSERT OR REPLACE INTO discovery_candidates
                       (run_id, paper_id, decision, score, filter_version, queries, dedup_method, filter_payload)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        run_id, cand.paper.paper_id, f.decision.value, f.score, f.filter_version,
                        json.dumps(cand.queries), cand.dedup_method, f.model_dump_json(),
                    ),
                )

    def candidate_counts(self, run_id: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT decision, COUNT(*) n FROM discovery_candidates WHERE run_id=? GROUP BY decision", (run_id,)
        ).fetchall()
        return {r["decision"]: r["n"] for r in rows}

    def close(self) -> None:
        self.conn.close()
