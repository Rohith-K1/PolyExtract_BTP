"""The five validation categories. Each is an independent, pure function over a RecordBundle.

Severity semantics:
  error   -> blocks verification (record cannot be 'verified' until corrected by a reviewer)
  warning -> record must be human-reviewed; never silently accepted
  info    -> recorded for reporting only
Scientific checks FLAG, they never delete: ambiguous cases go to review.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum

from pydantic import ValidationError

from polyextract.schemas import (
    DataOrigin,
    LocationType,
    MolecularCharacteristicsSchema,
    PaperMetadataSchema,
    PolymerSampleSchema,
    PropertyMeasurementSchema,
    PropertyName,
    Quantity,
)
from polyextract.schemas.identifiers import DOI_PATTERN
from polyextract.schemas.molecular import DESCRIPTOR_FIELDS, MOLAR_MASS_FIELDS


class Category(str, Enum):
    V1_SCHEMA = "V1_schema_datatype"
    V2_UNITS = "V2_units_numeric"
    V3_SCIENCE = "V3_scientific_consistency"
    V4_PROVENANCE = "V4_evidence_doi_provenance"
    V5_COMPLETENESS = "V5_completeness_duplicates_crossfield"


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class Finding:
    category: Category
    code: str
    severity: Severity
    record_id: str
    message: str

    def as_dict(self) -> dict:
        return {"category": self.category.value, "code": self.code, "severity": self.severity.value,
                "record_id": self.record_id, "message": self.message}


@dataclass
class RecordBundle:
    paper: PaperMetadataSchema
    samples: list[PolymerSampleSchema] = field(default_factory=list)
    molecular: list[MolecularCharacteristicsSchema] = field(default_factory=list)
    measurements: list[PropertyMeasurementSchema] = field(default_factory=list)
    extractor_issues: dict[str, list[str]] = field(default_factory=dict)


# expected normalized units
EXPECTED_UNIT = {
    **{f: "g/mol" for f in MOLAR_MASS_FIELDS},
    **{f: "dimensionless" for f in DESCRIPTOR_FIELDS if f not in MOLAR_MASS_FIELDS},
    PropertyName.GLASS_TRANSITION_TEMPERATURE: "K",
    PropertyName.DECOMPOSITION_TEMPERATURE: "K",
    PropertyName.ZERO_SHEAR_VISCOSITY: "Pa·s",
    PropertyName.COMPLEX_VISCOSITY: "Pa·s",
    PropertyName.SHEAR_VISCOSITY: "Pa·s",
    PropertyName.INTRINSIC_VISCOSITY: "dL/g",
    PropertyName.STORAGE_MODULUS: "Pa",
    PropertyName.LOSS_MODULUS: "Pa",
    PropertyName.PLATEAU_MODULUS: "Pa",
    PropertyName.YOUNGS_MODULUS: "Pa",
    PropertyName.TENSILE_STRENGTH: "Pa",
    PropertyName.ELONGATION_AT_BREAK: "%",
    PropertyName.DENSITY: "g/cm3",
}
# (hard_min, soft_min, soft_max, hard_max) in normalized units. Outside hard -> error, outside soft -> warning.
RANGES = {
    "mn": (50, 300, 5e7, 1e9), "mw": (50, 300, 5e7, 1e9), "mz": (50, 300, 1e8, 1e9),
    "dispersity": (0.99, 1.0, 10, 100), "dpn": (1, 2, 1e6, 1e7), "dpw": (1, 2, 1e6, 1e7), "dpz": (1, 2, 1e6, 1e7),
    "mwd_skewness": (-50, -10, 10, 50), "mwd_kurtosis": (-3, -3, 50, 500),
    PropertyName.GLASS_TRANSITION_TEMPERATURE: (50, 120, 650, 900),
    PropertyName.DECOMPOSITION_TEMPERATURE: (300, 420, 1100, 1500),
    PropertyName.ZERO_SHEAR_VISCOSITY: (1e-4, 1e-2, 1e11, 1e14),
    PropertyName.COMPLEX_VISCOSITY: (1e-4, 1e-2, 1e11, 1e14),
    PropertyName.INTRINSIC_VISCOSITY: (1e-3, 0.01, 20, 100),
    PropertyName.STORAGE_MODULUS: (1e-3, 1, 1e10, 1e12), PropertyName.LOSS_MODULUS: (1e-3, 1, 1e10, 1e12),
    PropertyName.PLATEAU_MODULUS: (1e2, 1e4, 1e7, 1e9),
    PropertyName.YOUNGS_MODULUS: (1e3, 1e5, 1e10, 1e12),
    PropertyName.TENSILE_STRENGTH: (1e3, 1e5, 3e8, 5e9),
    PropertyName.ELONGATION_AT_BREAK: (0, 0.1, 2000, 10000),
    PropertyName.DENSITY: (0.3, 0.8, 2.2, 4.0),
}


def _quantities(b: RecordBundle):
    """Yield (record_id, field_key, Quantity) for every value in the bundle."""
    for mc in b.molecular:
        for f in DESCRIPTOR_FIELDS:
            q = getattr(mc, f)
            if q is not None:
                yield mc.mc_id, f, q
    for m in b.measurements:
        yield m.measurement_id, m.property_name, m.value


# ---- V1 -----------------------------------------------------------------------------------------
def validate_schema(b: RecordBundle) -> list[Finding]:
    out: list[Finding] = []
    for rec in [b.paper, *b.samples, *b.molecular, *b.measurements]:
        rid = getattr(rec, "measurement_id", None) or getattr(rec, "mc_id", None) or getattr(rec, "sample_id", None) or rec.paper_id
        try:
            type(rec).model_validate(rec.model_dump())
        except ValidationError as e:
            out.append(Finding(Category.V1_SCHEMA, "schema_invalid", Severity.ERROR, rid, str(e)[:300]))
    for s in b.samples:
        if s.polymer_family is None:
            out.append(Finding(Category.V1_SCHEMA, "polymer_identity_missing", Severity.WARNING, s.sample_id,
                               "polymer name/family could not be determined; reviewer must identify"))
    for m in b.measurements:
        if m.property_name is PropertyName.OTHER:
            out.append(Finding(Category.V1_SCHEMA, "property_other", Severity.WARNING, m.measurement_id,
                               f"property not in controlled vocabulary: {m.property_name_reported}"))
    return out


# ---- V2 -----------------------------------------------------------------------------------------
def validate_units(b: RecordBundle) -> list[Finding]:
    out: list[Finding] = []
    for rid, key, q in _quantities(b):
        name = key.value if isinstance(key, PropertyName) else key
        if q.value is not None and not math.isfinite(q.value):
            out.append(Finding(Category.V2_UNITS, "non_finite", Severity.ERROR, rid, f"{name}: non-finite value"))
            continue
        if q.normalized_value is None:
            out.append(Finding(Category.V2_UNITS, "not_normalized", Severity.ERROR, rid,
                               f"{name}: '{q.reported_text}' unit '{q.unit}' could not be normalized"))
            continue
        exp = EXPECTED_UNIT.get(key)
        if exp and q.normalized_unit != exp:
            out.append(Finding(Category.V2_UNITS, "unit_dimension_mismatch", Severity.ERROR, rid,
                               f"{name}: normalized unit {q.normalized_unit}, expected {exp}"))
            continue
        rng = RANGES.get(key)
        if rng:
            hmin, smin, smax, hmax = rng
            v = q.normalized_value
            if v < hmin or v > hmax:
                out.append(Finding(Category.V2_UNITS, "implausible_value", Severity.ERROR, rid,
                                   f"{name}={v:g} {exp} outside physical range [{hmin:g}, {hmax:g}]"))
            elif v < smin or v > smax:
                out.append(Finding(Category.V2_UNITS, "suspicious_value", Severity.WARNING, rid,
                                   f"{name}={v:g} {exp} outside typical range [{smin:g}, {smax:g}]"))
    return out


# ---- V3 -----------------------------------------------------------------------------------------
def _nv(q: Quantity | None) -> float | None:
    return q.normalized_value if q is not None else None


def validate_science(b: RecordBundle) -> list[Finding]:
    out: list[Finding] = []
    for mc in b.molecular:
        mn, mw, mz, d = _nv(mc.mn), _nv(mc.mw), _nv(mc.mz), _nv(mc.dispersity)
        # within one record the technique is shared, so the ordering Mn <= Mw <= Mz must hold
        if mn and mw and mn > mw * 1.02:
            out.append(Finding(Category.V3_SCIENCE, "mn_gt_mw", Severity.ERROR, mc.mc_id,
                               f"Mn ({mn:g}) > Mw ({mw:g}); averages must satisfy Mn ≤ Mw"))
        if mw and mz and mw > mz * 1.02:
            out.append(Finding(Category.V3_SCIENCE, "mw_gt_mz", Severity.ERROR, mc.mc_id, f"Mw ({mw:g}) > Mz ({mz:g})"))
        if mn and mw and d:
            ratio = mw / mn
            rel = abs(ratio - d) / d
            if rel > 0.15:
                out.append(Finding(Category.V3_SCIENCE, "dispersity_inconsistent", Severity.ERROR, mc.mc_id,
                                   f"Đ={d:g} but Mw/Mn={ratio:.3g} (Δ={rel:.0%}); possible unit/column error"))
            elif rel > 0.05:
                out.append(Finding(Category.V3_SCIENCE, "dispersity_rounding", Severity.WARNING, mc.mc_id,
                                   f"Đ={d:g} vs Mw/Mn={ratio:.3g} (Δ={rel:.0%}); rounding or different runs?"))
        if mc.repeat_unit_molar_mass_g_mol and mn and mc.dpn and mc.dpn.normalized_value:
            exp_dp = mn / mc.repeat_unit_molar_mass_g_mol
            if abs(exp_dp - mc.dpn.normalized_value) / exp_dp > 0.2:
                out.append(Finding(Category.V3_SCIENCE, "dpn_inconsistent", Severity.WARNING, mc.mc_id,
                                   f"DPn={mc.dpn.normalized_value:g} vs Mn/M0={exp_dp:.3g}"))
        if (mc.mwd_skewness or mc.mwd_kurtosis) and mc.moment_basis.value == "not_reported":
            out.append(Finding(Category.V3_SCIENCE, "moment_basis_unknown", Severity.WARNING, mc.mc_id,
                               "skewness/kurtosis without a stated distribution basis are not comparable"))
    by_sample: dict[str, dict] = defaultdict(dict)
    for m in b.measurements:
        by_sample[m.sample_id].setdefault(m.property_name, []).append(m)
    for sid, props in by_sample.items():
        tgs = [m.value.normalized_value for m in props.get(PropertyName.GLASS_TRANSITION_TEMPERATURE, []) if m.value.normalized_value]
        tds = [m.value.normalized_value for m in props.get(PropertyName.DECOMPOSITION_TEMPERATURE, []) if m.value.normalized_value]
        if tgs and tds and max(tgs) >= min(tds):
            out.append(Finding(Category.V3_SCIENCE, "tg_above_td", Severity.WARNING, sid,
                               "Tg ≥ decomposition temperature for the same sample"))
        if len(tgs) > 1:
            out.append(Finding(Category.V3_SCIENCE, "multiple_tg", Severity.INFO, sid,
                               f"{len(tgs)} Tg values (blocks/phases or heating cycles?); kept as separate measurements"))
    return out


# ---- V4 -----------------------------------------------------------------------------------------
def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("−", "-")).strip()


def evidence_supports(q: Quantity) -> bool:
    """The verbatim reported text (or its leading number) must occur in at least one evidence passage."""
    if not q.reported_text or not q.evidence:
        return False
    rt = _norm_ws(q.reported_text)
    num = re.match(r"[-+<>~≈≤≥]?\s*[\d.,\s]+", rt)
    for ev in q.evidence:
        src = _norm_ws(ev.source_text)
        if rt in src or (num and num.group(0).strip() and num.group(0).strip() in src):
            return True
    return False


def validate_provenance(b: RecordBundle) -> list[Finding]:
    out: list[Finding] = []
    p = b.paper
    if not p.doi:
        out.append(Finding(Category.V4_PROVENANCE, "doi_missing", Severity.ERROR, p.paper_id, "paper has no DOI"))
    elif not DOI_PATTERN.fullmatch(p.doi):
        out.append(Finding(Category.V4_PROVENANCE, "doi_malformed", Severity.ERROR, p.paper_id, p.doi))
    for rec in [*b.samples, *b.molecular, *b.measurements]:
        rid = getattr(rec, "measurement_id", None) or getattr(rec, "mc_id", None) or rec.sample_id
        if rec.data_origin is not DataOrigin.LITERATURE_EXTRACTION:
            out.append(Finding(Category.V4_PROVENANCE, "not_literature_data", Severity.ERROR, rid,
                               f"data_origin={rec.data_origin.value}: can never be verified"))
        if rec.paper_id != p.paper_id:
            out.append(Finding(Category.V4_PROVENANCE, "paper_link_mismatch", Severity.ERROR, rid, "record points to another paper"))
        if rec.extraction is None:
            out.append(Finding(Category.V4_PROVENANCE, "extraction_info_missing", Severity.ERROR, rid, "no extraction metadata"))
    for rid, key, q in _quantities(b):
        name = key.value if isinstance(key, PropertyName) else key
        if not q.evidence:
            out.append(Finding(Category.V4_PROVENANCE, "evidence_missing", Severity.ERROR, rid, f"{name}: no evidence"))
            continue
        for ev in q.evidence:
            if ev.paper_id != p.paper_id:
                out.append(Finding(Category.V4_PROVENANCE, "evidence_paper_mismatch", Severity.ERROR, rid, f"{name}: evidence from another paper"))
            if ev.location_type in (LocationType.TABLE, LocationType.SUPPLEMENTARY_TABLE) and not ev.table_id:
                out.append(Finding(Category.V4_PROVENANCE, "table_ref_missing", Severity.ERROR, rid, f"{name}: table evidence without table id"))
            if ev.location_type is LocationType.FIGURE:
                out.append(Finding(Category.V4_PROVENANCE, "figure_digitized", Severity.WARNING, rid, f"{name}: value read from a figure"))
        if q.value_origin.value == "reported" and not evidence_supports(q):
            out.append(Finding(Category.V4_PROVENANCE, "value_not_in_evidence", Severity.ERROR, rid,
                               f"{name}: reported text '{q.reported_text}' not found in its evidence passage"))
    for s in b.samples:
        if s.amorphous_status.value != "unknown" and not s.amorphous_evidence:
            out.append(Finding(Category.V4_PROVENANCE, "amorphous_unsupported", Severity.ERROR, s.sample_id, "amorphous claim without evidence"))
    return out


# ---- V5 -----------------------------------------------------------------------------------------
EXTRACTOR_ISSUE_SEVERITY = {
    "cross_table_label_linkage": Severity.WARNING,
    "generic_sample_label": Severity.WARNING,
    "polymer_identity_unresolved": Severity.WARNING,
    "ambiguous_multiplier_sign_in_unit": Severity.ERROR,
    "unusual_positive_multiplier_on_label": Severity.WARNING,
    "unit_missing": Severity.ERROR,
}


def validate_completeness(b: RecordBundle) -> list[Finding]:
    out: list[Finding] = []
    sample_ids = {s.sample_id for s in b.samples}
    mc_by_sample = defaultdict(list)
    for mc in b.molecular:
        mc_by_sample[mc.sample_id].append(mc)
        if mc.sample_id not in sample_ids:
            out.append(Finding(Category.V5_COMPLETENESS, "orphan_molecular", Severity.ERROR, mc.mc_id, "no such sample"))
    seen: dict[tuple, str] = {}
    conflicts: dict[tuple, list] = defaultdict(list)
    for m in b.measurements:
        if m.sample_id not in sample_ids:
            out.append(Finding(Category.V5_COMPLETENESS, "orphan_measurement", Severity.ERROR, m.measurement_id, "no such sample"))
        if not mc_by_sample.get(m.sample_id):
            out.append(Finding(Category.V5_COMPLETENESS, "no_molecular_descriptors", Severity.INFO, m.measurement_id,
                               "sample has no molecular characteristics; not a dataset record"))
        cond = m.conditions.model_dump_json()
        key = (m.sample_id, m.property_name, m.property_definition, cond)
        v = m.value.normalized_value
        if (key, v) in seen:
            out.append(Finding(Category.V5_COMPLETENESS, "duplicate_measurement", Severity.WARNING, m.measurement_id,
                               f"duplicate of {seen[(key, v)]}"))
        seen[(key, v)] = m.measurement_id
        conflicts[key].append((v, m.measurement_id, m.property_name_reported))
    for key, vals in conflicts.items():
        distinct = {round(v, 6) for v, _, _ in vals if v is not None}
        names = {n for _, _, n in vals}
        if len(distinct) > 1 and len(names) == 1:
            for _, mid, _ in vals:
                out.append(Finding(Category.V5_COMPLETENESS, "conflicting_values", Severity.WARNING, mid,
                                   f"same sample/property/conditions but {len(distinct)} different values"))
    labels = defaultdict(list)
    for s in b.samples:
        labels[(s.sample_label or "").casefold()].append(s.sample_id)
    for lab, ids in labels.items():
        if len(ids) > 1:
            for sid in ids:
                out.append(Finding(Category.V5_COMPLETENESS, "duplicate_sample", Severity.WARNING, sid, f"label '{lab}' used by {len(ids)} samples"))
    for rid, codes in b.extractor_issues.items():
        for c in codes:
            base = c.split(":", 1)[0]
            sev = EXTRACTOR_ISSUE_SEVERITY.get(base)
            if base.startswith("unrecognized_unit") or base == "unit_missing":
                continue  # already a V2 error
            if "duplicate_label_in_table" in c:
                out.append(Finding(Category.V5_COMPLETENESS, "duplicate_label_in_table", Severity.WARNING, rid, c))
                continue
            if sev:
                out.append(Finding(Category.V5_COMPLETENESS, base, sev, rid, c))
            elif base in ("skipped_column", "header_multiplier_applied", "bracketed_secondary_number_ignored"):
                out.append(Finding(Category.V5_COMPLETENESS, base, Severity.INFO, rid, c))
    return out


ALL_VALIDATORS = (validate_schema, validate_units, validate_science, validate_provenance, validate_completeness)


def validate_bundle(b: RecordBundle) -> list[Finding]:
    out: list[Finding] = []
    for v in ALL_VALIDATORS:
        out.extend(v(b))
    return out
