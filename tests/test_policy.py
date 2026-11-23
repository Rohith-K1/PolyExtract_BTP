"""Tiered verification: machine tier needs dual agreement + clean validation, and counts only after a passing audit."""

import json

import pytest

from polyextract.dataset.builder import quality_stats, record_rows
from polyextract.extraction.agreement import agreement_score
from polyextract.extraction.table_rules import ExtractionResult
from polyextract.review import policy as P
from polyextract.review.service import Decision, decide, history, load_record, queue
from polyextract.storage import records as rstore
from tests.test_validation_review_dataset import PAPER, bundle, db  # noqa: F401  (fixture)


def _make_clean_and_agreeing(conn, label):
    rid = next(r["record_id"] for r in queue(conn) if r["sample_id"].endswith(label))
    comps = json.loads(conn.execute("SELECT components FROM records WHERE record_id=?", (rid,)).fetchone()[0])
    comps.update(extractor_agreement=1.0, evidence_match=1.0)
    conn.execute("UPDATE records SET components=?, n_errors=0, n_warnings=0 WHERE record_id=?", (json.dumps(comps), rid))
    conn.execute("DELETE FROM findings WHERE record_id=?", (rid,))
    conn.commit()
    return rid


def test_only_dual_agreeing_clean_records_are_promoted_and_logged(db):
    r1 = _make_clean_and_agreeing(db, "P1")
    out = P.apply_machine_verification(db)
    assert out["promoted"] == 1
    assert db.execute("SELECT review_status FROM records WHERE record_id=?", (r1,)).fetchone()[0] == "machine_verified"
    ev = history(db, r1)[-1]
    assert ev["decision"] == "machine_verify" and ev["reviewer"] == f"policy:{P.POLICY_VERSION}"
    assert P.apply_machine_verification(db)["promoted"] == 0  # idempotent
    # not released yet -> not in the verified dataset and not counted
    assert record_rows(db) == [] and quality_stats(db)["verified_records"] == 0


def test_audit_releases_tier_only_when_precision_bound_passes(db, monkeypatch):
    r1 = _make_clean_and_agreeing(db, "P1")
    r3 = _make_clean_and_agreeing(db, "P3") if any(r["sample_id"].endswith("P3") for r in queue(db)) else None
    P.apply_machine_verification(db)
    assert P.draw_audit(db, 1) == 1
    audited = P.audit_queue(db)[0]["record_id"]
    assert decide(db, audited, Decision.ACCEPT, "auditor", "matches") == "accepted"
    st = P.audit_status(db)
    assert st["audit_done"] == 1 and st["precision"] == 1.0 and st["released"] is False  # n too small
    monkeypatch.setattr(P, "AUDIT_MIN", 1)
    monkeypatch.setattr(P, "PRECISION_FLOOR", 0.1)
    assert P.audit_status(db)["released"] is True
    rows = record_rows(db)
    tiers = {r["record_id"]: r["verification_tier"] for r in rows}
    assert tiers[audited] == "human_reviewed"
    remaining = [x for x in (r1, r3) if x and x != audited]
    for x in remaining:
        assert tiers[x] == "machine_verified_audited"


def test_failed_audit_withholds_tier(db, monkeypatch):
    _make_clean_and_agreeing(db, "P1")
    P.apply_machine_verification(db)
    P.draw_audit(db, 1)
    decide(db, P.audit_queue(db)[0]["record_id"], Decision.REJECT, "auditor", "wrong sample")
    monkeypatch.setattr(P, "AUDIT_MIN", 1)
    st = P.audit_status(db)
    assert st["audit_wrong"] == 1 and st["released"] is False


def test_reextraction_resets_machine_tier_but_not_human_decisions(db):
    r1 = _make_clean_and_agreeing(db, "P1")
    P.apply_machine_verification(db)
    doc, res, findings, scored = bundle()
    rstore.replace_paper_extraction(db, PAPER.paper_id, res.samples, res.molecular, res.measurements, findings, scored, "x")
    assert db.execute("SELECT review_status FROM records WHERE record_id=?", (r1,)).fetchone()[0] == "pending"


def test_wilson_interval():
    lo, hi = P.wilson(97, 100)
    assert abs(lo - 0.9155) < 1e-3 and abs(hi - 0.9897) < 1e-3  # standard Wilson 95% values for 97/100
    assert P.wilson(0, 0) == (0.0, 1.0)


def test_agreement_requires_descriptors_confirmed(db):
    r1 = next(r["record_id"] for r in queue(db) if r["sample_id"].endswith("P1"))
    v = load_record(db, r1)
    m, mc = v.measurement, v.molecular
    same = ExtractionResult(samples=[v.sample], molecular=[mc], measurements=[m])
    assert agreement_score(m, mc, same) == 1.0
    only_prop = ExtractionResult(samples=[v.sample], molecular=[], measurements=[m])
    assert agreement_score(m, mc, only_prop) == 0.8
    other = m.model_copy(update={"value": m.value.model_copy(update={"normalized_value": m.value.normalized_value * 2})})
    assert agreement_score(m, mc, ExtractionResult(samples=[v.sample], molecular=[mc], measurements=[other])) == 0.0


def test_quality_stats_reports_tiers(db):
    s = quality_stats(db)
    assert {"verified_human_reviewed", "machine_verified_pool", "audit"} <= set(s)
    with pytest.raises(KeyError):
        _ = s["audit"]["nonexistent"]


def test_v2_tolerates_only_bookkeeping_warnings():
    agree = {"extractor_agreement": 1.0, "evidence_match": 1.0}
    assert P.qualifies(agree, 0, set())
    assert P.qualifies(agree, 0, {"cross_table_label_linkage", "polymer_identity_missing", "polymer_identity_unresolved"})
    for bad in ("suspicious_value", "duplicate_measurement", "conflicting_values", "generic_sample_label",
                "dispersity_rounding"):
        assert not P.qualifies(agree, 0, {"cross_table_label_linkage", bad})
    assert not P.qualifies(agree, 1, set())  # any error blocks
    assert not P.qualifies({**agree, "extractor_agreement": 0.8}, 0, set())
    assert not P.qualifies({**agree, "evidence_match": 0.5}, 0, set())


def test_promotion_reads_warning_codes_from_findings(db):
    r1 = _make_clean_and_agreeing(db, "P1")
    db.execute("UPDATE records SET n_warnings=1 WHERE record_id=?", (r1,))
    db.execute("INSERT INTO findings(record_id, paper_id, category, code, severity, message) VALUES (?,?,?,?,?,?)",
               (r1, PAPER.paper_id, "V5", "suspicious_value", "warning", "test"))
    db.commit()
    assert P.apply_machine_verification(db)["promoted"] == 0
    db.execute("UPDATE findings SET code='cross_table_label_linkage' WHERE record_id=?", (r1,))
    db.commit()
    assert P.apply_machine_verification(db)["promoted"] == 1
    assert "cross_table_label_linkage" in history(db, r1)[-1]["comment"]
