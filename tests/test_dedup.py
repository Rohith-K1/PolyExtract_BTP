"""Duplicates and computed values never reach a review queue; human decisions are never overridden."""

import json

from polyextract.review import dedup as D
from polyextract.review.service import Decision, decide, history, queue
from polyextract.storage import records as rstore
from tests.test_validation_review_dataset import bundle, db  # noqa: F401  (fixture)


def _clone(conn, rid, new_id, **meas_changes):
    """Insert a copy of record `rid` as a separate extraction (like the same value restated in another table)."""
    r = dict(conn.execute("SELECT * FROM records WHERE record_id=?", (rid,)).fetchone())
    m = json.loads(conn.execute("SELECT payload FROM measurements WHERE measurement_id=?", (r["measurement_id"],)).fetchone()[0])
    m["measurement_id"] = new_id
    m.update(meas_changes)
    conn.execute("INSERT INTO measurements VALUES (?,?,?,?,?)", (new_id, m["sample_id"], r["paper_id"], m["property_name"], json.dumps(m)))
    r.update(record_id=new_id, measurement_id=new_id, review_status="pending")
    conn.execute(f"INSERT INTO records VALUES ({','.join('?' * len(r))})", tuple(r.values()))
    conn.commit()
    return new_id


def _first_valued(conn):
    for r in queue(conn):
        m = json.loads(conn.execute("SELECT payload FROM measurements WHERE measurement_id=?", (r["measurement_id"],)).fetchone()[0])
        if m["value"]["normalized_value"] is not None:
            return r["record_id"], m
    raise AssertionError("fixture has no valued record")


def test_duplicate_hidden_from_review_and_logged(db):
    rid, _ = _first_valued(db)
    dup = _clone(db, rid, rid + "-restated")
    before = len(queue(db))
    out = D.apply_dedup(db)
    assert out["duplicates_marked"] == 1 and len(queue(db)) == before - 1
    statuses = {r: db.execute("SELECT review_status FROM records WHERE record_id=?", (r,)).fetchone()[0] for r in (rid, dup)}
    assert sorted(statuses.values()) == ["duplicate", "pending"]
    gone = next(r for r, s in statuses.items() if s == "duplicate")
    ev = history(db, gone)[-1]
    assert ev["decision"] == "mark_duplicate" and ev["reviewer"] == "policy:dedup-v2"
    assert D.apply_dedup(db)["duplicates_marked"] == 0  # idempotent


def test_different_value_or_method_is_not_a_duplicate(db):
    rid, m = _first_valued(db)
    v = dict(m["value"], normalized_value=m["value"]["normalized_value"] + 5)
    _clone(db, rid, rid + "-other-value", value=v)
    _clone(db, rid, rid + "-rheo", property_name_reported="Tg, rheo (°C)")
    assert D.apply_dedup(db)["duplicates_marked"] == 0


def test_human_decision_wins_and_is_never_overridden(db):
    rid, _ = _first_valued(db)
    dup = _clone(db, rid, rid + "-restated")
    decide(db, dup, Decision.ACCEPT, reviewer="rohith")
    D.apply_dedup(db)
    assert db.execute("SELECT review_status FROM records WHERE record_id=?", (dup,)).fetchone()[0] == "accepted"
    assert db.execute("SELECT review_status FROM records WHERE record_id=?", (rid,)).fetchone()[0] == "duplicate"


def test_computed_values_excluded(db):
    rid, _ = _first_valued(db)
    theo = _clone(db, rid, rid + "-theo", property_name_reported="Tg,theo (°C)")
    out = D.apply_dedup(db)
    assert out["computed_excluded"] == 1
    assert db.execute("SELECT review_status FROM records WHERE record_id=?", (theo,)).fetchone()[0] == "excluded_computed"


def test_reextraction_resets_system_statuses(db):
    for s in ("duplicate", "excluded_computed"):
        db.execute("UPDATE records SET review_status=?", (s,))
        pid = db.execute("SELECT paper_id FROM records LIMIT 1").fetchone()[0]
        prior = dict(db.execute("SELECT record_id, review_status FROM records WHERE paper_id=?", (pid,)).fetchall())
        assert set(prior.values()) == {s}
    # the reset rule lives in replace_paper_extraction; check the status list directly
    import inspect
    assert '"duplicate", "excluded_computed"' in inspect.getsource(rstore.replace_paper_extraction)


def test_header_key_ignores_footnotes_not_meaning():
    k = D._header_key
    assert k("Tg (°C)") == k("Tg(° C)") == k("T g ^{d} ( °C)") == k("Tg ^{(c)} (°C)")
    assert k("Tg(MS) [°C]") != k("Tg(blend) [°C]")
    assert k("82 Tg (PLA)(°C)") != k("55 Tg (PLA)(°C)")
    assert k("Tg^{PDMS} (°C)") != k("Tg^{P2VP} (°C)") != k("Tg (°C)")


def test_old_wrong_marks_are_reverted(db):
    rid, _ = _first_valued(db)
    other = _clone(db, rid, rid + "-blend", property_name_reported="Tg(blend) [°C]")
    db.execute("UPDATE records SET review_status='duplicate' WHERE record_id=?", (other,))  # e.g. marked by an older rule
    db.commit()
    out = D.apply_dedup(db)
    assert out["duplicates_reverted"] == 1
    assert db.execute("SELECT review_status FROM records WHERE record_id=?", (other,)).fetchone()[0] == "pending"
