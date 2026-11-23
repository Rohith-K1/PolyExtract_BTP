# Validation Rules

Code: [src/polyextract/validation/checks.py](../src/polyextract/validation/checks.py) and [confidence.py](../src/polyextract/validation/confidence.py). Tests: `tests/test_validation_review_dataset.py`, `tests/test_schemas.py`.

Each category is an independent pure function `validate_x(bundle) -> list[Finding]`. A finding has a category, a code, a severity, the record it applies to, and a message.

| Severity | Effect |
|---|---|
| `error` | Blocks verification. A reviewer cannot ACCEPT; they must CORRECT (the correction is re-validated) or REJECT. Confidence is capped at 0.3. |
| `warning` | The record goes to human review with the warning shown. |
| `info` | Reported only. |

Scientific checks **flag**. They never delete data.

## V1 Schema and datatype
- Every record is re-validated through its Pydantic schema (`schema_invalid`).
- Polymer identity unresolved: `polymer_identity_missing` (warning).
- Property outside the controlled vocabulary: `property_other`.
- Construction-time rules: verbatim `reported_text` for reported values; a `CalculationRecord` for calculated ones; range ordering; normalized value ⇒ normalized unit; evidence required for an amorphous claim; `extra="forbid"`; an LLM extraction must carry its model and prompt version.

## V2 Units and numbers
- `not_normalized` (error): the unit is missing or unrecognized. The unit table never guesses.
- `unit_dimension_mismatch` (error): e.g. a Tg normalized to Pa.
- `implausible_value` (error, outside the physical range) and `suspicious_value` (warning, outside the typical range). Ranges are per field, e.g. Mn 50–1e9 hard and 300–5e7 typical (g/mol); Tg 50–900 K hard and 120–650 K typical.

## V3 Scientific consistency (same SEC run ⇒ same technique)
- `mn_gt_mw`, `mw_gt_mz` (error): averages must satisfy Mn ≤ Mw ≤ Mz, with 2% tolerance.
- `dispersity_inconsistent` (error, >15% deviation) and `dispersity_rounding` (warning, 5–15%): reported Đ vs Mw/Mn.
- `dpn_inconsistent`: DPn vs Mn/M0 when M0 is known.
- `moment_basis_unknown`: skewness or kurtosis reported without a stated distribution basis.
- `tg_above_td` (warning); `multiple_tg` (info: blocks or heating cycles).

## V4 Evidence, DOI and provenance
- `doi_missing`, `doi_malformed`; `paper_link_mismatch`; `extraction_info_missing`.
- `not_literature_data` (error): `test_fixture` data can never be verified.
- `evidence_missing`, `evidence_paper_mismatch`, `table_ref_missing`, `figure_digitized` (warning).
- **`value_not_in_evidence` (error):** the verbatim reported text must occur in the cited passage. This is the anti-hallucination check. A DOI existing does not prove a value; the value has to be present in the source text.

## V5 Completeness, duplicates, cross-field
- `orphan_measurement`, `orphan_molecular`; `no_molecular_descriptors` (info: not a dataset record).
- `duplicate_measurement`, `conflicting_values` (same sample, property and conditions with different values), `duplicate_sample`, `duplicate_label_in_table`.
- Extractor issues become findings: `cross_table_label_linkage`, `generic_sample_label`, `ambiguous_multiplier_sign_in_unit` (error), `unit_missing`.

## Confidence (confidence-v1)
Components in [0,1], combined as a weighted mean:

| Component | Weight |
|---|---|
| schema_valid | 1 |
| evidence_present | 1.5 |
| evidence_match | 2 |
| evidence_specificity (table 1.0 … figure 0.4) | 1 |
| doi_valid | 1 |
| units_normalized | 1.5 |
| plausibility | 1 |
| consistency | 1 |
| extractor_agreement (rule and LLM agree 1.0 · single reader 0.6 · LLM-only 0.3 · disagree 0) | 1 |

Any error caps the overall score at 0.3. **LLM self-reported confidence is not used.**

Routes:
- `blocked_errors`: has errors.
- `needs_review`: warnings, or a score below 0.85.
- `review_low_risk`: clean and at least 0.85.

All routes still require a human decision. The route only orders the queue.
