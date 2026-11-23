# Data Schema

Code: [src/polyextract/schemas/](../src/polyextract/schemas/). All models use `extra="forbid"`, so a key the LLM invents is a validation error and never passes through silently.

## Evidence chain

```
PaperMetadataSchema (paper_id, doi)
  └─ PolymerSampleSchema (sample_id → paper_id)
       ├─ MolecularCharacteristicsSchema (mc_id → sample_id)   9 descriptors, each a Quantity
       └─ PropertyMeasurementSchema (measurement_id → sample_id)  value: Quantity, conditions
                                         every Quantity → [Evidence(page, table_id, figure_id, source_text)]
```

## Shared types

**`Quantity`** keeps:
- `value` / `value_max`, `qualifier` (`=`, `~`, `<`, `>`, `range`), `uncertainty`
- `unit` and `reported_text`: the original, verbatim
- `normalized_value` / `normalized_unit`, stored next to the original and never replacing it
- `value_origin` (`reported` | `calculated`) plus a `calculation` record

Rules enforced at construction:
- A reported value needs its `reported_text`.
- A calculated value needs a `CalculationRecord` (method, inputs, input refs).
- A `range` needs `value_max ≥ value`.
- A normalized value needs a normalized unit.

**`Evidence`**: `paper_id`, `doi`, `document_ref`, `location_type` (text / table / figure_caption / figure / supplementary_*), `page`, `section`, `table_id`, `figure_id`, verbatim `source_text`, and an optional character span.

**`ExtractionInfo`**: `method` (llm / rule_based / manual / metadata_api), `model`, `prompt_version`, `run_id`, `extracted_at`. An LLM extraction must record its model and prompt version.

**`data_origin`**: `literature_extraction` | `test_fixture`, on every record.

## The four schemas

1. **PaperMetadataSchema**: DOI (validated and normalized), title, authors, journal, year, abstract, keywords, URL, document type, `retrieval_sources`, `source_ids` (per source), `field_sources` (which source supplied each merged field), OA URL, licence, `fulltext_status`.
2. **PolymerSampleSchema**: sample label as written in the paper, polymer name and family, monomers, repeat unit, architecture, tacticity, composition, blend flag, structure (with its origin: `reported_in_source` vs `curated_lookup`), `amorphous_status`, and identity evidence. **`amorphous_status` other than `unknown` requires `amorphous_evidence`.**
3. **MolecularCharacteristicsSchema**: the nine descriptors `mn, mw, mz, dispersity, dpn, dpw, dpz, mwd_skewness, mwd_kurtosis` (each `Quantity | None`), plus:
   - technique (conventional SEC, SEC-MALS, MALDI, NMR end-group, …)
   - calibration standard, eluent
   - `moment_basis` and `kurtosis_convention` (skewness and kurtosis are only comparable under the same definition)
   - M0 used for DP calculations
   - an optional `MWDCurve` (tabulated or digitized; digitized curves must name the tool)

   The record must contain at least one descriptor or a curve.
4. **PropertyMeasurementSchema**: `property_name` from a controlled list, with deliberately distinct entries such as zero-shear, complex, steady-shear, and intrinsic viscosity. It also holds the verbatim reported name, the definition (onset, midpoint, T5%), the value, the technique, and `ExperimentalConditions`. All conditions are optional: temperature, pressure, shear rate, frequency, strain, strain rate, heating rate, solvent, atmosphere, preparation, annealing, and `other`.

## Stage-1 models (discovery only)

`SourceRecord` holds one source's view of a paper. A web-search `snippet` is kept separate from `abstract`. `FilterDecision` holds the decision, score, matched groups, every `TermMatch` (group, pattern, field, surface text), every `RuleResult`, and human-readable reasons.
