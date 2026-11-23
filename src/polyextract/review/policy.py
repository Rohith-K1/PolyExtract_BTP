"""Tiered verification: dual-extractor agreement + validators, quality-controlled by a random human audit.

Tier 1 MACHINE-VERIFIED (status `machine_verified`, policy POLICY_VERSION) — a pending record qualifies only if
  * the rule-based reader and the LLM reader independently report the same property value AND every
    reported molecular descriptor (extractor_agreement == 1.0),
  * the reported text is found verbatim in the evidence (evidence_match == 1.0),
  * zero error findings, and no warning findings except the TOLERATED_WARNINGS below.
  v1 (2026-11-06) tolerated no warnings at all; v2 (2026-11-23) tolerates three bookkeeping warnings that
  say nothing about whether a value is right:
    - cross_table_label_linkage: Mn/Mw and the property sit in different tables, joined by sample label.
      Agreement already requires the LLM to pair the same descriptors with the same property for that
      sample, so the join is independently confirmed.
    - polymer_identity_missing / polymer_identity_unresolved: no polymer family was resolved; the record
      keeps the paper's own sample label. Values are unaffected; such records are marked in the export.
  Warnings that can mean a wrong value (suspicious_value, duplicate_measurement, conflicting_values,
  generic_sample_label, dispersity_rounding, ...) still block promotion.
Tier 2 HUMAN-REVIEWED — everything else; a person accepts / corrects / rejects (status accepted|corrected).
Tier 3 AUDIT — a seeded uniform random sample of tier-1 records is reviewed by a human. Precision is
  estimated with a Wilson 95% interval. Tier 1 is RELEASED into the verified dataset only when at least
  AUDIT_MIN audited records exist and the interval's lower bound >= PRECISION_FLOOR. Otherwise tier 1 is
  withheld and only human-reviewed records count. Nothing here can turn a record with errors into data.

Every promotion writes a review_events row (reviewer = "policy:<version>") with the full snapshot, so the
audit trail shows exactly which records were machine-verified, by which rule version, from what evidence.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3

from polyextract.review.service import _snapshot, load_record
from polyextract.storage.records import now

POLICY_VERSION = "dual-agreement-v2"
TOLERATED_WARNINGS = frozenset({"cross_table_label_linkage", "polymer_identity_missing",
                                "polymer_identity_unresolved"})
AUDIT_MIN = 100
PRECISION_FLOOR = 0.90
HUMAN_STATUSES = ("accepted", "corrected")


def qualifies(components: dict, n_errors: int, warning_codes: set[str]) -> bool:
    return (n_errors == 0 and not (set(warning_codes) - TOLERATED_WARNINGS)
            and components.get("extractor_agreement") == 1.0 and components.get("evidence_match") == 1.0)


def warning_codes(conn: sqlite3.Connection, record_id: str) -> set[str]:
    """Warning codes attached to a record's measurement, molecular characterization or sample."""
    r = conn.execute("SELECT measurement_id, mc_id, sample_id FROM records WHERE record_id=?", (record_id,)).fetchone()
    ids = (record_id, r["measurement_id"], r["mc_id"], r["sample_id"])
    return {f[0] for f in conn.execute(
        "SELECT DISTINCT code FROM findings WHERE severity='warning' AND record_id IN (?,?,?,?)", ids)}


def apply_machine_verification(conn: sqlite3.Connection) -> dict:
    """Promote qualifying PENDING records (never touches anything a human decided). Idempotent."""
    promoted = 0
    pending = conn.execute("SELECT record_id, components, n_errors, n_warnings FROM records "
                           "WHERE review_status='pending'").fetchall()
    for r in pending:
        codes = warning_codes(conn, r["record_id"]) if r["n_warnings"] else set()
        if not qualifies(json.loads(r["components"]), r["n_errors"], codes):
            continue
        v = load_record(conn, r["record_id"])
        conn.execute("INSERT INTO review_events(record_id, decision, reviewer, comment, corrections, snapshot, ts) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (r["record_id"], "machine_verify", f"policy:{POLICY_VERSION}",
                      "rule reader and LLM reader agree; evidence verbatim; no errors; warnings only: "
                      + (", ".join(sorted(codes)) or "none"), None,
                      _snapshot(v), now()))
        conn.execute("UPDATE records SET review_status='machine_verified', updated_at=? WHERE record_id=?",
                     (now(), r["record_id"]))
        promoted += 1
    conn.commit()
    return {"promoted": promoted, "pending_checked": len(pending), **audit_status(conn)}


def draw_audit(conn: sqlite3.Connection, n: int, seed: int = 20261106) -> int:
    """Add up to n not-yet-sampled machine-verified records to the audit sample (uniform, seeded)."""
    pool = [r[0] for r in conn.execute(
        "SELECT record_id FROM records WHERE review_status='machine_verified' "
        "AND record_id NOT IN (SELECT record_id FROM audit_sample) ORDER BY record_id")]
    pick = random.Random(seed + len(pool)).sample(pool, min(n, len(pool)))
    conn.executemany("INSERT INTO audit_sample VALUES (?,?,?,?)", [(r, POLICY_VERSION, seed, now()) for r in pick])
    conn.commit()
    return len(pick)


def audit_queue(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT r.* FROM records r JOIN audit_sample a ON a.record_id=r.record_id "
                        "WHERE r.review_status='machine_verified' ORDER BY r.paper_id, r.record_id").fetchall()


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def audit_status(conn: sqlite3.Connection) -> dict:
    """Audit outcome per sampled record: accepted = machine was right; corrected/rejected = machine was wrong."""
    rows = conn.execute("SELECT r.review_status s, COUNT(*) n FROM audit_sample a JOIN records r "
                        "ON r.record_id=a.record_id GROUP BY r.review_status").fetchall()
    by = {r["s"]: r["n"] for r in rows}
    correct = by.get("accepted", 0)
    wrong = by.get("corrected", 0) + by.get("rejected", 0)
    done = correct + wrong
    lo, hi = wilson(correct, done)
    tier = conn.execute("SELECT COUNT(*) FROM records WHERE review_status='machine_verified' AND n_errors=0").fetchone()[0]
    released = done >= AUDIT_MIN and lo >= PRECISION_FLOOR
    return {
        "policy": POLICY_VERSION, "machine_verified_records": tier, "audit_drawn": sum(by.values()),
        "audit_done": done, "audit_correct": correct, "audit_wrong": wrong,
        "audit_pending": by.get("machine_verified", 0),
        "precision": round(correct / done, 4) if done else None,
        "precision_ci95": [round(lo, 4), round(hi, 4)] if done else None,
        "released": released,
        "release_rule": f">= {AUDIT_MIN} audited and Wilson 95% lower bound >= {PRECISION_FLOOR}",
    }


def verified_statuses(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Statuses that count as VERIFIED right now: human-reviewed always; machine tier only once released."""
    try:
        return HUMAN_STATUSES + (("machine_verified",) if audit_status(conn)["released"] else ())
    except sqlite3.OperationalError:
        return HUMAN_STATUSES
