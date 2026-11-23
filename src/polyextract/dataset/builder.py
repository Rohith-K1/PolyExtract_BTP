"""Verified dataset construction, exports (JSON / JSONL / CSV / Parquet) and quality statistics.

RECORD UNIT: one property measurement on one polymer sample that has molecular characteristics.
VERIFIED  := ( review_status in {accepted, corrected}                    -> tier "human_reviewed"
               OR review_status == machine_verified AND tier released    -> tier "machine_verified_audited" )
             AND no remaining error findings
             AND data_origin == literature_extraction (test fixtures are refused, not filtered quietly).
The machine tier is released only by a passing random human audit (review/policy.py).
Every number reported here is computed from the database at call time.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

import pandas as pd

from polyextract.exceptions import ProvenanceError
from polyextract.schemas import (
    DataOrigin,
    MolecularCharacteristicsSchema,
    PaperMetadataSchema,
    PolymerSampleSchema,
    PropertyMeasurementSchema,
)
from polyextract.schemas.molecular import DESCRIPTOR_FIELDS

VERIFIED_STATUSES = ("accepted", "corrected")


def _q(q, prefix: str) -> dict:
    if q is None:
        return {f"{prefix}": None, f"{prefix}_origin": None, f"{prefix}_reported": None}
    return {prefix: q.normalized_value, f"{prefix}_origin": q.value_origin.value, f"{prefix}_reported": q.reported_text}


def record_rows(conn: sqlite3.Connection, statuses: tuple[str, ...] | None | str = "verified") -> list[dict]:
    if statuses == "verified":
        from polyextract.review.policy import verified_statuses

        statuses = verified_statuses(conn)
    sql = "SELECT * FROM records"
    args: tuple = ()
    if statuses:
        sql += f" WHERE review_status IN ({','.join('?' * len(statuses))})"
        args = statuses
    out = []
    papers: dict[str, PaperMetadataSchema] = {}
    for r in conn.execute(sql, args).fetchall():
        if statuses and r["n_errors"]:
            continue  # verified must have no remaining blocking errors
        pid = r["paper_id"]
        if pid not in papers:
            papers[pid] = PaperMetadataSchema.model_validate_json(
                conn.execute("SELECT payload FROM papers WHERE paper_id=?", (pid,)).fetchone()["payload"])
        p = papers[pid]
        s = PolymerSampleSchema.model_validate_json(conn.execute("SELECT payload FROM samples WHERE sample_id=?", (r["sample_id"],)).fetchone()["payload"])
        m = PropertyMeasurementSchema.model_validate_json(conn.execute("SELECT payload FROM measurements WHERE measurement_id=?", (r["measurement_id"],)).fetchone()["payload"])
        mrow = conn.execute("SELECT payload FROM molecular WHERE mc_id=?", (r["mc_id"],)).fetchone()
        mc = MolecularCharacteristicsSchema.model_validate_json(mrow["payload"]) if mrow else None
        for rec in (p, s, m, *( [mc] if mc else [])):
            if rec.data_origin is not DataOrigin.LITERATURE_EXTRACTION and statuses:
                raise ProvenanceError(f"{r['record_id']}: non-literature record in verified set")
        ev = m.value.evidence[0] if m.value.evidence else None
        last = conn.execute("SELECT reviewer, ts FROM review_events WHERE record_id=? ORDER BY event_id DESC LIMIT 1",
                            (r["record_id"],)).fetchone()
        row = {
            "record_id": r["record_id"], "paper_id": pid, "doi": p.doi, "title": p.title, "journal": p.journal,
            "year": p.publication_year, "license": p.license,
            "sample_id": s.sample_id, "sample_label": s.sample_label, "polymer_name": s.polymer_name,
            "polymer_family": s.polymer_family, "architecture": s.architecture.value, "amorphous_status": s.amorphous_status.value,
            **{k: v for f in DESCRIPTOR_FIELDS for k, v in _q(getattr(mc, f) if mc else None, f).items()},
            "mw_technique": mc.technique.value if mc else None,
            "calibration": mc.calibration_standard.text if mc and mc.calibration_standard else None,
            "property_name": m.property_name.value, "property_reported_as": m.property_name_reported,
            "property_definition": m.property_definition, "value": m.value.normalized_value,
            "value_max": m.value.normalized_value_max, "unit": m.value.normalized_unit, "qualifier": m.value.qualifier.value,
            "value_reported": m.value.reported_text, "value_unit_reported": m.value.unit, "property_technique": m.technique,
            "heating_rate_K_min": m.conditions.heating_rate.normalized_value if m.conditions.heating_rate else None,
            "temperature_K": m.conditions.temperature.normalized_value if m.conditions.temperature else None,
            "frequency": m.conditions.frequency.normalized_value if m.conditions.frequency else None,
            "evidence_location": ev.location_type.value if ev else None, "evidence_table": ev.table_id if ev else None,
            "evidence_page": ev.page if ev else None, "evidence_text": ev.source_text if ev else None,
            "evidence_url": ev.document_ref if ev else None,
            "confidence": r["confidence"], "confidence_components": r["components"], "route": r["route"],
            "review_status": r["review_status"],
            "verification_tier": {"accepted": "human_reviewed", "corrected": "human_reviewed",
                                  "machine_verified": "machine_verified_audited"}.get(r["review_status"]),
            "reviewer": last["reviewer"] if last else None,
            "reviewed_at": last["ts"] if last else None, "extractor": r["extractor"],
        }
        out.append(row)
    return out


def export(conn: sqlite3.Connection, out_dir: Path, version: str) -> dict:
    rows = record_rows(conn)
    out_dir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    stem = out_dir / f"polyextract_verified_{version}"
    with open(f"{stem}.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    Path(f"{stem}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    df.to_csv(f"{stem}.csv", index=False)
    if not df.empty:
        df.to_parquet(f"{stem}.parquet", index=False)
    stats = quality_stats(conn)
    Path(f"{stem}_stats.json").write_text(json.dumps(stats, indent=2, default=str), encoding="utf-8")
    return {"verified_records": len(rows), "files": [f"{stem}.{x}" for x in ("jsonl", "json", "csv", "parquet")], "stats": stats}


def _count(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def quality_stats(conn: sqlite3.Connection) -> dict:
    """Dashboard metrics — all computed from stored data."""
    st: dict = {
        "papers_discovered": _count(conn, "SELECT COUNT(*) FROM papers"),
        "papers_passing_filters": _count(conn, "SELECT COUNT(DISTINCT paper_id) FROM discovery_candidates WHERE decision='accept'"),
        "papers_needing_filter_review": _count(conn, "SELECT COUNT(DISTINCT paper_id) FROM discovery_candidates WHERE decision='review' AND paper_id NOT IN (SELECT paper_id FROM discovery_candidates WHERE decision='accept')"),
        "fulltext_status": dict(conn.execute("SELECT status, COUNT(*) FROM documents GROUP BY status").fetchall()) if _count(conn, "SELECT COUNT(*) FROM documents") else {},
        "papers_processed": _count(conn, "SELECT COUNT(*) FROM documents WHERE status='acquired'"),
        "papers_with_records": _count(conn, "SELECT COUNT(DISTINCT paper_id) FROM records"),
        "samples": _count(conn, "SELECT COUNT(*) FROM samples"),
        "molecular_characteristics": _count(conn, "SELECT COUNT(*) FROM molecular"),
        "measurements": _count(conn, "SELECT COUNT(*) FROM measurements"),
        "candidate_records": _count(conn, "SELECT COUNT(*) FROM records"),
        "verified_human_reviewed": _count(conn, "SELECT COUNT(*) FROM records WHERE review_status IN ('accepted','corrected') AND n_errors=0"),
        "machine_verified_pool": _count(conn, "SELECT COUNT(*) FROM records WHERE review_status='machine_verified' AND n_errors=0"),
        "pending_review": _count(conn, "SELECT COUNT(*) FROM records WHERE review_status='pending'"),
        "rejected": _count(conn, "SELECT COUNT(*) FROM records WHERE review_status='rejected'"),
        "needs_reextraction": _count(conn, "SELECT COUNT(*) FROM records WHERE review_status='needs_reextraction'"),
        "review_events": _count(conn, "SELECT COUNT(*) FROM review_events"),
    }
    from polyextract.review.policy import audit_status

    try:
        st["audit"] = audit_status(conn)
    except sqlite3.OperationalError:
        st["audit"] = {"released": False}
    st["verified_records"] = st["verified_human_reviewed"] + (st["machine_verified_pool"] if st["audit"].get("released") else 0)
    try:
        st["routes"] = dict(conn.execute("SELECT route, COUNT(*) FROM records GROUP BY route").fetchall())
        st["property_distribution_candidates"] = dict(conn.execute("SELECT property_name, COUNT(*) FROM records GROUP BY property_name ORDER BY 2 DESC").fetchall())
        st["validation_failures"] = {f"{c}|{code}": n for c, code, n in conn.execute(
            "SELECT category, code, COUNT(*) FROM findings WHERE severity!='info' GROUP BY category, code ORDER BY 3 DESC").fetchall()}
        st["validation_by_category"] = dict(conn.execute(
            "SELECT category, COUNT(*) FROM findings WHERE severity='error' GROUP BY category").fetchall())
    except sqlite3.OperationalError:
        pass
    # descriptor availability + family distribution over candidate records' samples
    avail = Counter()
    fam = Counter()
    n_mc = 0
    try:
        for (payload,) in conn.execute("SELECT m.payload FROM molecular m WHERE m.mc_id IN (SELECT mc_id FROM records)"):
            mc = MolecularCharacteristicsSchema.model_validate_json(payload)
            n_mc += 1
            for f in mc.present_descriptors():
                avail[f] += 1
        for (payload,) in conn.execute("SELECT payload FROM samples WHERE sample_id IN (SELECT sample_id FROM records)"):
            fam[PolymerSampleSchema.model_validate_json(payload).polymer_family or "unidentified"] += 1
    except sqlite3.OperationalError:
        pass
    st["descriptor_availability"] = {f: {"n": avail[f], "pct": round(100 * avail[f] / n_mc, 1) if n_mc else 0.0} for f in DESCRIPTOR_FIELDS}
    st["descriptor_records_basis"] = n_mc
    st["polymer_family_distribution"] = dict(fam.most_common())
    verified = record_rows(conn) if st["verified_records"] else []
    if verified:
        df = pd.DataFrame(verified)
        st["verified_property_distribution"] = df["property_name"].value_counts().to_dict()
        st["verified_missing_pct"] = {c: round(100 * df[c].isna().mean(), 1) for c in [*DESCRIPTOR_FIELDS, "heating_rate_K_min", "mw_technique", "property_technique"]}
    return st
