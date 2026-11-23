"""Explainable confidence scoring for one dataset record (= one property measurement + its sample's
molecular characteristics). Built only from measurable signals; LLM self-reported confidence is NOT used.

Components (each in [0, 1]):
  schema_valid          no V1 errors
  evidence_present      fraction of the record's values that carry evidence
  evidence_match        fraction whose reported text is found verbatim in the evidence (anti-hallucination)
  evidence_specificity  table 1.0 · text 0.8 · caption 0.7 · supplementary 0.9 · figure 0.4 · metadata 0.2
  doi_valid             paper DOI present and well-formed
  units_normalized      fraction of values normalized to the expected unit
  plausibility          1 − (V2 warnings+errors / values)
  consistency           1 if no V3 findings, 0.6 if only warnings, 0 with errors
  extractor_agreement   1 both extractors agree · 0.6 single extractor · 0 disagree
overall = weighted mean; capped at 0.3 if ANY error finding exists (a high score can never
override contradictory evidence).
"""

from __future__ import annotations

from dataclasses import dataclass

from polyextract.schemas import LocationType, MolecularCharacteristicsSchema, PropertyMeasurementSchema, Quantity
from polyextract.schemas.molecular import DESCRIPTOR_FIELDS
from polyextract.validation.checks import Category, Finding, Severity, evidence_supports

SCORING_VERSION = "confidence-v1"
WEIGHTS = {
    "schema_valid": 1.0, "evidence_present": 1.5, "evidence_match": 2.0, "evidence_specificity": 1.0,
    "doi_valid": 1.0, "units_normalized": 1.5, "plausibility": 1.0, "consistency": 1.0, "extractor_agreement": 1.0,
}
SPECIFICITY = {
    LocationType.TABLE: 1.0, LocationType.SUPPLEMENTARY_TABLE: 0.9, LocationType.TEXT: 0.8,
    LocationType.SUPPLEMENTARY_TEXT: 0.75, LocationType.FIGURE_CAPTION: 0.7, LocationType.ABSTRACT: 0.6,
    LocationType.FIGURE: 0.4, LocationType.METADATA: 0.2,
}
ERROR_CAP = 0.3


@dataclass
class Confidence:
    overall: float
    components: dict[str, float]
    blocking_errors: int
    warnings: int
    version: str = SCORING_VERSION


def score_record(
    m: PropertyMeasurementSchema,
    mc: MolecularCharacteristicsSchema | None,
    findings: list[Finding],
    *,
    agreement: float = 0.6,
) -> Confidence:
    ids = {m.measurement_id, m.sample_id, m.paper_id} | ({mc.mc_id} if mc else set())
    rel = [f for f in findings if f.record_id in ids]
    qs: list[Quantity] = [m.value] + ([getattr(mc, f) for f in DESCRIPTOR_FIELDS if getattr(mc, f) is not None] if mc else [])
    n = len(qs)
    ev_present = sum(1 for q in qs if q.evidence) / n
    ev_match = sum(1 for q in qs if evidence_supports(q)) / n
    spec = sum(max((SPECIFICITY.get(e.location_type, 0.2) for e in q.evidence), default=0.0) for q in qs) / n
    v2 = [f for f in rel if f.category is Category.V2_UNITS]
    not_norm = sum(1 for f in v2 if f.code in ("not_normalized", "unit_dimension_mismatch"))
    v3 = [f for f in rel if f.category is Category.V3_SCIENCE and f.severity is not Severity.INFO]
    comps = {
        "schema_valid": 0.0 if any(f.category is Category.V1_SCHEMA and f.severity is Severity.ERROR for f in rel) else 1.0,
        "evidence_present": ev_present,
        "evidence_match": ev_match,
        "evidence_specificity": spec,
        "doi_valid": 0.0 if any(f.code in ("doi_missing", "doi_malformed") for f in rel) else 1.0,
        "units_normalized": max(0.0, 1 - not_norm / n),
        "plausibility": max(0.0, 1 - sum(1 for f in v2 if f.code in ("implausible_value", "suspicious_value")) / n),
        "consistency": 0.0 if any(f.severity is Severity.ERROR for f in v3) else (0.6 if v3 else 1.0),
        "extractor_agreement": agreement,
    }
    overall = sum(WEIGHTS[k] * v for k, v in comps.items()) / sum(WEIGHTS.values())
    errors = sum(1 for f in rel if f.severity is Severity.ERROR)
    warns = sum(1 for f in rel if f.severity is Severity.WARNING)
    if errors:
        overall = min(overall, ERROR_CAP)
    return Confidence(round(overall, 4), {k: round(v, 4) for k, v in comps.items()}, errors, warns)
