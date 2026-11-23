"""Human-in-the-loop review service (UI-independent, fully testable).

Decisions: accept · reject · correct · request re-extraction (+ free-text comment on any).
Every decision appends an immutable `review_events` row containing a SNAPSHOT of what the reviewer
saw: the original extraction, validation findings and confidence components. Records are never
auto-accepted; `review_low_risk` only changes queue order (and allows batch-accept of a whole table
by a human), it does not bypass review.

Verification rule enforced here: a record with blocking errors cannot be ACCEPTED; it must be
CORRECTED (corrections are re-validated) or rejected.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import Enum

from pydantic import ValidationError

from polyextract.schemas import (
    MolecularCharacteristicsSchema,
    PaperMetadataSchema,
    PolymerSampleSchema,
    PropertyMeasurementSchema,
)
from polyextract.storage.records import now
from polyextract.validation.checks import RecordBundle, Severity, validate_bundle
from polyextract.validation.confidence import score_record


class Decision(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    CORRECT = "correct"
    REQUEST_REEXTRACTION = "request_reextraction"


STATUS_FOR = {
    Decision.ACCEPT: "accepted",
    Decision.REJECT: "rejected",
    Decision.CORRECT: "corrected",
    Decision.REQUEST_REEXTRACTION: "needs_reextraction",
}


class ReviewError(Exception):
    pass


@dataclass
class RecordView:
    record_id: str
    paper: PaperMetadataSchema
    sample: PolymerSampleSchema
    molecular: MolecularCharacteristicsSchema | None
    measurement: PropertyMeasurementSchema
    findings: list[dict]
    confidence: float
    components: dict
    route: str
    review_status: str


def load_record(conn: sqlite3.Connection, record_id: str) -> RecordView:
    r = conn.execute("SELECT * FROM records WHERE record_id=?", (record_id,)).fetchone()
    if r is None:
        raise ReviewError(f"no record {record_id}")
    paper = PaperMetadataSchema.model_validate_json(
        conn.execute("SELECT payload FROM papers WHERE paper_id=?", (r["paper_id"],)).fetchone()["payload"])
    sample = PolymerSampleSchema.model_validate_json(
        conn.execute("SELECT payload FROM samples WHERE sample_id=?", (r["sample_id"],)).fetchone()["payload"])
    mrow = conn.execute("SELECT payload FROM molecular WHERE mc_id=?", (r["mc_id"],)).fetchone() if r["mc_id"] else None
    meas = PropertyMeasurementSchema.model_validate_json(
        conn.execute("SELECT payload FROM measurements WHERE measurement_id=?", (r["measurement_id"],)).fetchone()["payload"])
    ids = (r["measurement_id"], r["sample_id"], r["mc_id"] or "", r["paper_id"])
    findings = [dict(f) for f in conn.execute(
        "SELECT category, code, severity, message, record_id FROM findings WHERE paper_id=? AND record_id IN (?,?,?,?)",
        (r["paper_id"], *ids)).fetchall()]
    return RecordView(r["record_id"], paper, sample,
                      MolecularCharacteristicsSchema.model_validate_json(mrow["payload"]) if mrow else None,
                      meas, findings, r["confidence"], json.loads(r["components"]), r["route"], r["review_status"])


def queue(conn: sqlite3.Connection, status: str = "pending", route: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
    sql = "SELECT * FROM records WHERE review_status=?"
    args: list = [status]
    if route:
        sql += " AND route=?"
        args.append(route)
    # low-risk first lets a reviewer clear easy tables quickly; blocked ones need careful correction
    sql += " ORDER BY CASE route WHEN 'review_low_risk' THEN 0 WHEN 'needs_review' THEN 1 ELSE 2 END, paper_id, record_id LIMIT ?"
    args.append(limit)
    return conn.execute(sql, args).fetchall()


def _snapshot(v: RecordView) -> str:
    return json.dumps({
        "measurement": v.measurement.model_dump(mode="json"),
        "molecular": v.molecular.model_dump(mode="json") if v.molecular else None,
        "sample": v.sample.model_dump(mode="json"),
        "findings": v.findings, "confidence": v.confidence, "components": v.components, "route": v.route,
    })


def _apply_corrections(v: RecordView, corrections: dict) -> tuple[PolymerSampleSchema, MolecularCharacteristicsSchema | None, PropertyMeasurementSchema]:
    """corrections = {'sample': {...field updates}, 'molecular': {...}, 'measurement': {...}}.
    Re-validated through the Pydantic schemas (so a correction cannot break provenance rules)."""
    s = PolymerSampleSchema.model_validate({**v.sample.model_dump(), **corrections.get("sample", {})})
    mc = v.molecular
    if corrections.get("molecular"):
        if mc is None:
            raise ReviewError("record has no molecular characteristics to correct")
        mc = MolecularCharacteristicsSchema.model_validate({**mc.model_dump(), **corrections["molecular"]})
    m = PropertyMeasurementSchema.model_validate({**v.measurement.model_dump(), **corrections.get("measurement", {})})
    return s, mc, m


def decide(conn: sqlite3.Connection, record_id: str, decision: Decision, reviewer: str,
           comment: str = "", corrections: dict | None = None) -> str:
    if not reviewer.strip():
        raise ReviewError("reviewer name required (audit trail)")
    v = load_record(conn, record_id)
    blocking = [f for f in v.findings if f["severity"] == Severity.ERROR.value]
    if decision is Decision.ACCEPT and blocking:
        raise ReviewError(f"cannot accept: {len(blocking)} blocking error(s) — correct or reject instead: "
                          + "; ".join(f["code"] for f in blocking))
    if decision is Decision.CORRECT:
        if not corrections:
            raise ReviewError("correct requires corrections")
        try:
            s, mc, m = _apply_corrections(v, corrections)
        except ValidationError as e:
            raise ReviewError(f"correction violates the schema: {str(e)[:300]}") from e
        bundle = RecordBundle(paper=v.paper, samples=[s], molecular=[mc] if mc else [], measurements=[m])
        findings = validate_bundle(bundle)
        conf = score_record(m, mc, findings)
        if conf.blocking_errors:
            raise ReviewError("correction still fails validation: " + "; ".join(
                f.code for f in findings if f.severity is Severity.ERROR))
        conn.execute("UPDATE samples SET payload=? WHERE sample_id=?", (s.model_dump_json(), s.sample_id))
        if mc:
            conn.execute("UPDATE molecular SET payload=? WHERE mc_id=?", (mc.model_dump_json(), mc.mc_id))
        conn.execute("UPDATE measurements SET payload=? WHERE measurement_id=?", (m.model_dump_json(), m.measurement_id))
        ids = [x for x in (m.measurement_id, m.sample_id, mc.mc_id if mc else None) if x]
        conn.execute(f"DELETE FROM findings WHERE record_id IN ({','.join('?' * len(ids))})", ids)
        conn.executemany("INSERT INTO findings VALUES (?,?,?,?,?,?)",
                         [(f.record_id, v.paper.paper_id, f.category.value, f.code, f.severity.value, f.message) for f in findings])
        conn.execute("UPDATE records SET confidence=?, components=?, n_errors=?, n_warnings=? WHERE record_id=?",
                     (conf.overall, json.dumps(conf.components), conf.blocking_errors, conf.warnings, record_id))
    status = STATUS_FOR[decision]
    conn.execute(
        "INSERT INTO review_events(record_id, decision, reviewer, comment, corrections, snapshot, ts) VALUES (?,?,?,?,?,?,?)",
        (record_id, decision.value, reviewer, comment, json.dumps(corrections) if corrections else None, _snapshot(v), now()),
    )
    conn.execute("UPDATE records SET review_status=?, updated_at=? WHERE record_id=?", (status, now(), record_id))
    conn.commit()
    return status


BULK_DECISIONS = (Decision.ACCEPT, Decision.REJECT, Decision.REQUEST_REEXTRACTION)


def _source_key(meas: PropertyMeasurementSchema) -> str:
    ev = meas.value.evidence[0] if meas.value and meas.value.evidence else None
    if ev is None:
        return "no evidence"
    return ev.table_id or f"text: {ev.section or 'body'}"


def table_groups(conn: sqlite3.Connection, status: str = "pending") -> list[dict]:
    """Pending records grouped by (paper, source table / text section): the unit a reviewer checks at once."""
    groups: dict[tuple[str, str], dict] = {}
    for r in conn.execute(
            "SELECT r.record_id, r.paper_id, r.route, m.payload FROM records r JOIN measurements m "
            "ON m.measurement_id = r.measurement_id WHERE r.review_status=?", (status,)):
        key = (r["paper_id"], _source_key(PropertyMeasurementSchema.model_validate_json(r["payload"])))
        g = groups.setdefault(key, {"paper_id": key[0], "source": key[1], "record_ids": [], "routes": {}})
        g["record_ids"].append(r["record_id"])
        g["routes"][r["route"]] = g["routes"].get(r["route"], 0) + 1
    order = {"review_low_risk": 0, "needs_review": 1, "blocked_errors": 2}
    return sorted(groups.values(), key=lambda g: (min(order.get(k, 3) for k in g["routes"]), -len(g["record_ids"])))


def source_table(conn: sqlite3.Connection, paper_id: str, label: str, fulltext_dir) -> dict | None:
    """Re-render the ORIGINAL table from the cached OA full text so the reviewer compares against the paper,
    not against our own extraction."""
    from pathlib import Path

    from polyextract.acquisition.jats import parse_jats

    d = conn.execute("SELECT source_url, license FROM documents WHERE paper_id=?", (paper_id,)).fetchone()
    if not d or not d["source_url"] or "/rest/" not in d["source_url"]:
        return None
    f = Path(fulltext_dir) / f"{d['source_url'].split('/rest/')[1].split('/')[0]}.xml"
    if not f.exists():
        return None
    doc = parse_jats(f.read_bytes(), paper_id=paper_id, source_url=d["source_url"], license=d["license"])
    for t in doc.tables:
        if label in (t.label, t.table_id):
            return {"label": t.label or t.table_id, "caption": t.caption, "headers": t.column_headers(),
                    "rows": t.rows, "footnotes": t.footnotes, "source_url": d["source_url"]}
    return None


def group_rows(conn: sqlite3.Connection, record_ids: list[str]) -> list[dict]:
    """One flat row per record (what was extracted), for side-by-side checking against the source."""
    out = []
    for rid in record_ids:
        v = load_record(conn, rid)
        m, mc = v.measurement, v.molecular
        row = {"record_id": rid, "sample": v.sample.sample_label, "polymer": v.sample.polymer_name,
               "property": m.property_name.value, "reported": m.value.reported_text if m.value else None,
               "unit": m.value.unit if m.value else None,
               "normalized": f"{m.value.normalized_value:.6g} {m.value.normalized_unit or ''}".strip()
               if m.value and m.value.normalized_value is not None else None}
        for f in ("mn", "mw", "dispersity"):
            q = getattr(mc, f, None) if mc else None
            row[f] = q.reported_text if q else None
        row.update({"extractor": m.extraction.method.value if m.extraction else None,
                    "confidence": round(v.confidence, 2), "route": v.route,
                    "errors": "; ".join(x["code"] for x in v.findings if x["severity"] == Severity.ERROR.value),
                    "warnings": "; ".join(x["code"] for x in v.findings if x["severity"] == Severity.WARNING.value)})
        out.append(row)
    return out


def decide_many(conn: sqlite3.Connection, record_ids: list[str], decision: Decision, reviewer: str,
                comment: str = "") -> dict:
    """Table-level review: the human checks every row against the source table, then applies one decision.
    Each record still gets its own audit event + snapshot via `decide`; blocked records are never accepted."""
    if decision not in BULK_DECISIONS:
        raise ReviewError("corrections must be made record by record")
    if not reviewer.strip():
        raise ReviewError("reviewer name required (audit trail)")
    done, skipped = [], {}
    for rid in record_ids:
        try:
            decide(conn, rid, decision, reviewer, f"[table review] {comment}".strip())
            done.append(rid)
        except ReviewError as e:
            skipped[rid] = str(e)
    return {"applied": done, "skipped": skipped}


def history(conn: sqlite3.Connection, record_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT event_id, decision, reviewer, comment, corrections, ts FROM review_events WHERE record_id=? ORDER BY event_id",
        (record_id,)).fetchall()]
