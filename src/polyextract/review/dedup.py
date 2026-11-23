"""Keep duplicates and computed values out of every review queue (run before the verification policy).

Two things are taken out of review automatically, each with an audit-trail event (reviewer "policy:dedup-v1"):

* **Computed values** (status `excluded_computed`): the paper's column header marks the value as theoretical,
  calculated, predicted or simulated (e.g. `Tg,theo`, Fox equation). They are not measurements.
* **Duplicates** (status `duplicate`): the same measurement extracted more than once. This happens when a paper
  restates a value in a second table, or a spanned header splits one value over two columns. Key: same paper,
  same sample label (case, spaces, "-" and "_" ignored), same property, the SAME column header (case, spaces and
  table-local footnote markers ignored, so "Tg (°C)" = "Tg(° C)" but "Tg(MS)" != "Tg(blend)" and
  "82 Tg (PLA)" != "55 Tg (PLA)"), same value, upper bound, unit and conditions. One record per group stays; it is chosen in this order: human-decided, machine-verified,
  better route, higher confidence, then smallest record id. Records without a value are never grouped.

Only `pending` and `machine_verified` records are ever re-labelled; a human decision is never overridden.
Every run re-checks earlier marks: a `duplicate` that no longer matches its group is put back to `pending`
(event `unmark_duplicate`), so tightening the rule repairs old decisions.
Re-extraction puts both statuses back to `pending` (storage/records.py), and this runs again on the new data.
"""

from __future__ import annotations

import json
import re
import sqlite3

from polyextract.dataset.variants import is_theoretical
from polyextract.storage.records import now

DEDUP_VERSION = "dedup-v2"
SYSTEM_STATUSES = ("duplicate", "excluded_computed")
_MUTABLE = ("pending", "machine_verified")
_HUMAN = ("accepted", "corrected", "rejected", "needs_reextraction")
_ROUTE_RANK = {"review_low_risk": 0, "needs_review": 1, "blocked_errors": 2}


def _label(s: str) -> str:
    return re.sub(r"[\s_\-]", "", (s or "").casefold())


def _header_key(header: str | None) -> str:
    """Column header without case, whitespace or footnote markers (^{a}, ^{(c)}, *)."""
    return re.sub(r"\^\{\(?[a-z0-9†‡§*]{1,2}\)?\}|[\s*]", "", (header or "").casefold())  # keeps ^{PDMS}


def _q(x) -> str:
    return "" if x is None else repr(round(float(x), 9))


def _cond(meas: dict, name: str):
    c = (meas.get("conditions") or {}).get(name)
    return c.get("normalized_value") if isinstance(c, dict) else None


def _load(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT r.record_id, r.paper_id, r.review_status, r.route, r.confidence, m.payload mp, s.payload sp "
        "FROM records r JOIN measurements m ON m.measurement_id=r.measurement_id "
        "JOIN samples s ON s.sample_id=r.sample_id").fetchall()
    out = []
    for r in rows:
        m, s = json.loads(r["mp"]), json.loads(r["sp"])
        v = m.get("value") or {}
        out.append({"record_id": r["record_id"], "status": r["review_status"], "route": r["route"],
                    "confidence": r["confidence"] or 0.0, "header": m.get("property_name_reported"),
                    "key": None if v.get("normalized_value") is None else (
                        r["paper_id"], _label(s.get("sample_label")), m.get("property_name"),
                        _header_key(m.get("property_name_reported")), _q(v.get("normalized_value")),
                        _q(v.get("normalized_value_max")), v.get("normalized_unit") or "",
                        _q(_cond(m, "heating_rate")), _q(_cond(m, "temperature")), _q(_cond(m, "frequency")))})
    return out


def _rank(r: dict) -> tuple:
    tier = 0 if r["status"] in ("accepted", "corrected") else 1 if r["status"] == "machine_verified" else 2
    return (tier, _ROUTE_RANK.get(r["route"], 3), -r["confidence"], r["record_id"])


def _mark(conn: sqlite3.Connection, rid: str, status: str, decision: str, comment: str) -> None:
    from polyextract.review.service import _snapshot, load_record

    conn.execute("INSERT INTO review_events(record_id, decision, reviewer, comment, corrections, snapshot, ts) "
                 "VALUES (?,?,?,?,?,?,?)",
                 (rid, decision, f"policy:{DEDUP_VERSION}", comment, None, _snapshot(load_record(conn, rid)), now()))
    conn.execute("UPDATE records SET review_status=?, updated_at=? WHERE record_id=?", (status, now(), rid))


def apply_dedup(conn: sqlite3.Connection) -> dict:
    """Idempotent. Returns counts of records newly taken out of review."""
    recs = _load(conn)
    computed = duplicates = 0
    for r in recs:
        if r["status"] in _MUTABLE and is_theoretical(r["header"]):
            _mark(conn, r["record_id"], "excluded_computed", "exclude_computed",
                  f"column header marks a computed value, not a measurement: {r['header']!r}")
            r["status"] = "excluded_computed"
            computed += 1

    groups: dict[tuple, list[dict]] = {}
    for r in recs:
        if r["key"] is not None and r["status"] not in ("excluded_computed", "rejected"):
            groups.setdefault(r["key"], []).append(r)
    should_be_dup: dict[str, str] = {}  # record_id -> the record it duplicates
    for members in groups.values():
        if len(members) < 2:
            continue
        cands = [r for r in members if r["status"] != "duplicate"] or members
        keep = min(cands, key=_rank)  # an existing keeper stays the keeper
        for r in members:
            if r is not keep:
                should_be_dup[r["record_id"]] = keep["record_id"]

    reverted = 0
    for r in recs:
        rid = r["record_id"]
        if r["status"] == "duplicate" and rid not in should_be_dup:
            _mark(conn, rid, "pending", "unmark_duplicate", f"no longer a duplicate under {DEDUP_VERSION}")
            reverted += 1
        elif r["status"] in _MUTABLE and rid in should_be_dup:
            _mark(conn, rid, "duplicate", "mark_duplicate", f"duplicate of {should_be_dup[rid]}")
            duplicates += 1

    refilled = _refill_audit(conn)
    conn.commit()
    return {"computed_excluded": computed, "duplicates_marked": duplicates, "duplicates_reverted": reverted,
            "audit_replaced": refilled}


def _refill_audit(conn: sqlite3.Connection) -> int:
    """An audit-sample record that turned out to be a duplicate or computed value is replaced by a fresh random draw
    (only if not yet audited), so the audit keeps measuring the machine tier as it now stands."""
    from polyextract.review.policy import draw_audit

    gone = [r[0] for r in conn.execute(
        "SELECT a.record_id FROM audit_sample a JOIN records r ON r.record_id=a.record_id "
        f"WHERE r.review_status IN ({','.join('?' * len(SYSTEM_STATUSES))})", SYSTEM_STATUSES)]
    if not gone:
        return 0
    conn.executemany("DELETE FROM audit_sample WHERE record_id=?", [(g,) for g in gone])
    return draw_audit(conn, len(gone))


def dedup_counts(conn: sqlite3.Connection) -> dict:
    return dict(conn.execute("SELECT review_status, COUNT(*) FROM records WHERE review_status IN (?,?) "
                             "GROUP BY review_status", SYSTEM_STATUSES).fetchall())
