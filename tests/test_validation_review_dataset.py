"""V1–V5 validation, confidence, HITL review, verified-dataset gate, LLM anchoring and workflows.
All data here is TEST FIXTURE content (DOI prefix 10.5555)."""

import sqlite3

import pandas as pd
import pytest

from polyextract.acquisition.jats import parse_jats
from polyextract.apps.workflows import answer_with_evidence, retrieve
from polyextract.dataset.builder import quality_stats, record_rows
from polyextract.exceptions import ProvenanceError
from polyextract.extraction.llm import build_chunks, llm_to_records
from polyextract.extraction.pipeline import process_document
from polyextract.review.service import Decision, ReviewError, decide, history, queue
from polyextract.schemas import DataOrigin, PaperMetadataSchema
from polyextract.storage import records as rstore
from polyextract.storage.sqlite import Repository
from polyextract.validation.checks import (
    Category,
    RecordBundle,
    validate_completeness,
    validate_provenance,
    validate_science,
    validate_schema,
    validate_units,
)
from tests.test_table_rules import JATS

PAPER = PaperMetadataSchema(paper_id="doi:10.5555/test-fixture-jats", doi="10.5555/test-fixture-jats",
                            title="TEST FIXTURE: PMMA")


def bundle():
    doc = parse_jats(JATS, paper_id=PAPER.paper_id, doi=PAPER.doi)
    res, findings, scored = process_document(PAPER, doc)
    return doc, res, findings, scored


def codes(fs, rid_suffix=None):
    return {f.code for f in fs if rid_suffix is None or f.record_id.endswith(rid_suffix) or rid_suffix in f.record_id}


# ---- the five categories -------------------------------------------------------------------------
def test_v2_units_flags_implausible_molar_mass():
    _, res, _, _ = bundle()
    assert validate_units(RecordBundle(PAPER, res.samples, res.molecular, res.measurements)) == []  # clean data: no false positives
    mc = res.molecular[0].model_copy(deep=True)
    mc.mn.normalized_value = 9.8e9  # e.g. a ×10^6 multiplier misread
    m = res.measurements[0].model_copy(deep=True)
    m.value.normalized_unit = "Pa"  # Tg normalized to the wrong dimension
    v2 = validate_units(RecordBundle(PAPER, res.samples, [mc], [m]))
    assert {"implausible_value", "unit_dimension_mismatch"} <= codes(v2)
    assert all(f.category is Category.V2_UNITS for f in v2)


def test_v3_science_catches_mn_gt_mw_and_dispersity_mismatch():
    _, res, _, _ = bundle()
    mc2 = next(m for m in res.molecular if m.sample_id.endswith("P2"))
    b = RecordBundle(PAPER, res.samples, [mc2], [])
    f = validate_science(b)
    # Mn = 48.5e6 vs Mw = 55e3 -> Mn > Mw (error) and Đ inconsistent with Mw/Mn (error)
    assert {"mn_gt_mw", "dispersity_inconsistent"} <= codes(f)


def test_v4_provenance_value_must_appear_in_evidence():
    _, res, _, _ = bundle()
    mc1 = res.molecular[0].model_copy(deep=True)
    mc1.mn.reported_text = "999.9"  # not in the evidence row -> hallucination signal
    f = validate_provenance(RecordBundle(PAPER, res.samples, [mc1], []))
    assert "value_not_in_evidence" in codes(f)


def test_v4_rejects_test_fixture_origin_and_missing_doi():
    _, res, _, _ = bundle()
    s = res.samples[0].model_copy(update={"data_origin": DataOrigin.TEST_FIXTURE})
    f = validate_provenance(RecordBundle(PAPER.model_copy(update={"doi": None}), [s], [], []))
    assert {"not_literature_data", "doi_missing"} <= codes(f)


def test_v5_completeness_orphans_and_duplicates():
    _, res, _, _ = bundle()
    m = res.measurements[0]
    dup = m.model_copy(update={"measurement_id": m.measurement_id + "-dup"})
    orphan = m.model_copy(update={"measurement_id": "x", "sample_id": "nope"})
    f = validate_completeness(RecordBundle(PAPER, res.samples, res.molecular, [m, dup, orphan]))
    assert {"duplicate_measurement", "orphan_measurement"} <= codes(f)


def test_v1_schema_flags_unidentified_polymer():
    _, res, _, _ = bundle()
    s = res.samples[0].model_copy(update={"polymer_family": None})
    assert "polymer_identity_missing" in codes(validate_schema(RecordBundle(PAPER, [s])))


# ---- confidence -----------------------------------------------------------------------------------
def test_confidence_capped_by_errors_and_routes():
    _, _, _, scored = bundle()
    by = {m.sample_id.split("#s:")[-1]: (conf, route) for m, mc, conf, route in scored}
    c1, r1 = by["P1"]
    c2, r2 = by["P2"]
    assert c1.components["evidence_match"] == 1.0 and c1.blocking_errors == 0
    assert c2.blocking_errors > 0 and c2.overall <= 0.3 and r2 == rstore.ROUTE_BLOCKED
    assert "P3" not in by  # no molecular descriptors -> not a dataset record


# ---- review + dataset -----------------------------------------------------------------------------
@pytest.fixture
def db(tmp_path):
    repo = Repository(tmp_path / "db.sqlite")
    rstore.init_schema(repo.conn)
    doc, res, findings, scored = bundle()
    repo.upsert_paper(PAPER)
    rstore.replace_paper_extraction(repo.conn, PAPER.paper_id, res.samples, res.molecular, res.measurements,
                                    findings, scored, "table-rules-v1")
    repo.conn.commit()
    yield repo.conn
    repo.close()


def rid(conn, label):
    return next(r["record_id"] for r in queue(conn) if r["sample_id"].endswith(label))


def test_review_accept_reject_and_audit(db: sqlite3.Connection):
    assert record_rows(db) == []  # nothing verified before human review
    r1 = rid(db, "P1")
    with pytest.raises(ReviewError, match="reviewer"):
        decide(db, r1, Decision.ACCEPT, reviewer=" ")
    assert decide(db, r1, Decision.ACCEPT, "tester", "matches Table 1") == "accepted"
    rows = record_rows(db)
    assert len(rows) == 1 and rows[0]["doi"] == "10.5555/test-fixture-jats" and rows[0]["evidence_table"] == "Table 1"
    assert rows[0]["reviewer"] == "tester"
    h = history(db, r1)
    assert h[0]["decision"] == "accept" and h[0]["comment"] == "matches Table 1"


def test_blocked_record_cannot_be_accepted_but_can_be_corrected(db):
    r2 = rid(db, "P2")
    with pytest.raises(ReviewError, match="blocking"):
        decide(db, r2, Decision.ACCEPT, "tester")
    # reviewer reads the paper: '48,500' is in g/mol despite the header -> correct Mn
    from polyextract.review.service import load_record
    v = load_record(db, r2)
    mn = v.molecular.mn.model_dump()
    mn.update(unit="g/mol", normalized_value=48500.0)
    assert decide(db, r2, Decision.CORRECT, "tester", "header unit wrong for this cell", {"molecular": {"mn": mn}}) == "corrected"
    assert any(r["record_id"] == r2 for r in record_rows(db))


def test_table_level_review_keeps_per_record_audit_and_blocks_errors(db):
    from polyextract.review.service import decide_many, group_rows, table_groups

    groups = table_groups(db)
    assert len(groups) == 1 and groups[0]["source"] == "Table 1"
    ids = groups[0]["record_ids"]
    rows = {r["sample"]: r for r in group_rows(db, ids)}
    assert rows["P1"]["reported"] == "112.4^{a}" or rows["P1"]["reported"].startswith("112.4")
    assert rows["P2"]["errors"]  # implausible Mn flagged in the row the reviewer sees
    with pytest.raises(ReviewError):
        decide_many(db, ids, Decision.CORRECT, "tester")  # corrections are never bulk
    out = decide_many(db, ids, Decision.ACCEPT, "tester", "checked against Table 1")
    assert rid_ok(db, out, "P1") and any(k.endswith("P2") or "P2" in k for k in out["skipped"])
    for r in out["applied"]:
        assert history(db, r)[0]["comment"].startswith("[table review]")  # one audit event per record
    assert all(r["record_id"] not in out["skipped"] for r in record_rows(db))


def rid_ok(conn, out, label):
    return any(conn.execute("SELECT sample_id FROM records WHERE record_id=?", (r,)).fetchone()[0].endswith(label)
               for r in out["applied"])


def test_bad_correction_is_refused(db):
    r2 = rid(db, "P2")
    with pytest.raises(ReviewError):
        decide(db, r2, Decision.CORRECT, "tester", corrections={"molecular": {"mn": None, "mw": None, "dispersity": None}})


def test_test_fixture_never_enters_verified_set(db):
    r1 = rid(db, "P1")
    decide(db, r1, Decision.ACCEPT, "tester")
    sid = db.execute("SELECT sample_id FROM records WHERE record_id=?", (r1,)).fetchone()[0]
    payload = db.execute("SELECT payload FROM samples WHERE sample_id=?", (sid,)).fetchone()[0].replace(
        '"literature_extraction"', '"test_fixture"')
    db.execute("UPDATE samples SET payload=? WHERE sample_id=?", (payload, sid))
    with pytest.raises(ProvenanceError):
        record_rows(db)


def test_quality_stats_from_db(db):
    st = quality_stats(db)
    assert st["candidate_records"] == 2 and st["verified_records"] == 0 and st["pending_review"] == 2
    assert st["descriptor_availability"]["mn"]["n"] == 2


# ---- LLM anchoring (no network) -------------------------------------------------------------------
def test_llm_values_must_be_anchored_in_context():
    doc = parse_jats(JATS, paper_id=PAPER.paper_id, doi=PAPER.doi)
    chunks = build_chunks(doc)
    tid = chunks[0].chunk_id
    data = {"samples": [{
        "sample_label": "P1", "polymer_name": "PMMA", "identity_chunk_id": tid, "identity_quote": "P1",
        "mw_technique": "SEC", "descriptors": [
            {"name": "mn", "reported_text": "9.8", "unit": "kg/mol", "chunk_id": tid, "quote": "P1 | 10 | 9.8"},
            {"name": "mw", "reported_text": "77.7", "unit": "kg/mol", "chunk_id": tid, "quote": "invented"}],
        "properties": [{"name": "glass_transition_temperature", "reported_name": "Tg", "reported_text": "112.4",
                        "unit": "°C", "technique": "DSC", "chunk_id": tid, "quote": "112.4"}]}]}
    res, dropped = llm_to_records(doc, data, chunks, model="m", provider="anthropic")
    assert res.molecular[0].mn.normalized_value == 9800 and res.molecular[0].mw is None
    assert any("mw:unanchored" in d for d in dropped)
    assert res.measurements[0].value.normalized_value == pytest.approx(385.55)
    mock_res, _ = llm_to_records(doc, data, chunks, model="m", provider="mock")
    assert mock_res.samples[0].data_origin is DataOrigin.TEST_FIXTURE


def test_llm_ranges_keep_their_upper_bound():
    # LIVE 2026-11-06: an LLM-reported range crashed the 2,055-paper run (value_max dropped) and LLM property
    # values were forced to '=' — '100–120' would have been stored as exactly 100.
    from polyextract.extraction.llm import Chunk
    from polyextract.schemas import LocationType, Qualifier

    doc = parse_jats(JATS, paper_id=PAPER.paper_id, doi=PAPER.doi)
    txt = "Sample P1 had Mn of 500–700 kg/mol and a Tg of 100–120 °C measured by DSC."
    chunks = [Chunk("P1", txt, LocationType.TEXT, section="Results")]
    data = {"samples": [{
        "sample_label": "P1", "polymer_name": "PMMA", "identity_chunk_id": "P1", "identity_quote": "Sample P1",
        "mw_technique": "", "descriptors": [
            {"name": "mn", "reported_text": "500–700", "unit": "kg/mol", "chunk_id": "P1", "quote": "Mn of 500–700 kg/mol"}],
        "properties": [{"name": "glass_transition_temperature", "reported_name": "Tg", "reported_text": "100–120",
                        "unit": "°C", "technique": "DSC", "chunk_id": "P1", "quote": "Tg of 100–120 °C"}]}]}
    res, dropped = llm_to_records(doc, data, chunks, model="m", provider="anthropic")
    mn = res.molecular[0].mn
    assert mn.qualifier is Qualifier.RANGE and mn.normalized_value == 500_000 and mn.normalized_value_max == 700_000
    tg = res.measurements[0].value
    assert tg.qualifier is Qualifier.RANGE and tg.normalized_value_max == pytest.approx(393.15)
    assert dropped == []


# ---- workflows ------------------------------------------------------------------------------------
def test_retrieval_and_grounded_answer():
    df = pd.DataFrame([
        {"record_id": f"r{i}", "polymer_name": "poly(methyl methacrylate)", "polymer_family": "poly(methyl methacrylate)",
         "sample_label": f"P{i}", "property_name": "glass_transition_temperature", "value": 370.0 + i, "unit": "K",
         "value_reported": str(97 + i), "mn": 1e4 * (i + 1), "mw": 1.1e4 * (i + 1), "dispersity": 1.1,
         "mw_technique": "sec_conventional", "property_technique": "DSC", "heating_rate_K_min": 10.0,
         "doi": "10.5555/test-fixture-jats", "evidence_table": "Table 1", "evidence_text": "row", "confidence": 0.9,
         "paper_id": "doi:10.5555/test-fixture-jats"} for i in range(5)])
    r = retrieve(df, property_name="glass_transition_temperature", value_min=372, target=373)
    assert list(r["value"]) == [373.0, 372.0, 374.0]
    a = answer_with_evidence(df, "What is the Tg of PMMA above 372?")
    assert "[1]" in a.answer and "10.5555/test-fixture-jats" in a.answer and len(a.evidence) == 3
    none = answer_with_evidence(df, "density of polystyrene?")
    assert none.evidence.empty and "No verified records" in none.answer
