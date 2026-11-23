"""Cross-extractor agreement: the rule-based and LLM paths are independent readers of the same table.
Agreement on (sample label, property, normalized value) is the strongest confidence signal we have."""

from __future__ import annotations

from polyextract.extraction.table_rules import ExtractionResult
from polyextract.schemas import MolecularCharacteristicsSchema, PropertyMeasurementSchema


def _close(a: float | None, b: float | None, rel: float = 0.02) -> bool:
    return a is not None and b is not None and abs(a - b) <= rel * max(abs(a), abs(b), 1e-12)


def agreement_score(m: PropertyMeasurementSchema, mc: MolecularCharacteristicsSchema | None,
                    llm: ExtractionResult) -> float:
    """1.0  both readers agree on the property value AND on every descriptor the rule reader found
    0.8  property value agrees, but the LLM did not report (confirm) some of those descriptors
    0.6  LLM silent on this record · 0.0 any disagreement.
    Only 1.0 qualifies for the machine-verified tier (review/policy.py)."""
    label = m.sample_id.split("#s:", 1)[-1].casefold()
    cands = [x for x in llm.measurements
             if x.sample_id.split("#s:", 1)[-1].casefold() == label and x.property_name == m.property_name]
    if not cands:
        return 0.6
    match = next((x for x in cands if _close(x.value.normalized_value, m.value.normalized_value)), None)
    if match is None:
        return 0.0
    lmc = next((x for x in llm.molecular if x.sample_id == match.sample_id), None)
    if mc is None:
        return 1.0
    unconfirmed = False
    for f in mc.present_descriptors():
        a, b = getattr(mc, f), getattr(lmc, f, None) if lmc else None
        if a.value_origin.value != "reported":
            continue  # our own calculation (e.g. Đ = Mw/Mn): nothing for a reader to confirm
        if b is None:
            unconfirmed = True
        elif not _close(a.normalized_value, b.normalized_value):
            return 0.0
    return 0.8 if unconfirmed else 1.0
