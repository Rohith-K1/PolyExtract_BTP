# Extraction Pipeline

## Stage 1: discovery (implemented)

```
config/discovery.yaml queries ──► OpenAlex  (search, cursor paging, abstract from inverted index)
                               ├► Crossref  (query.bibliographic + sort=relevance, cursor paging, JATS stripped)
                               └► WebSearch (ddgs; DOI from URL / publisher URL rules / snippet)
            ──► Crossref DOI lookup for web-only DOIs (enrichment)
            ──► dedup: DOI ▸ exact title (±1 yr) ▸ fuzzy title ≥0.93 (DOI-less records only)
            ──► FilterEngine (config/filters.yaml) ──► ACCEPT / REVIEW / REJECT, with reasons
            ──► SQLite + run artifacts (raw, candidates, report.json/md, log.jsonl)
```

### Filter rules (filters-v2)

| Rule | Effect |
|---|---|
| R0 | Web-only hit without a DOI: REJECT on a known non-scholarly URL; otherwise at most REVIEW |
| R1 | Hard-excluded document type (erratum, retraction, editorial, review, …): REJECT |
| R2 | Year outside the configured range: REJECT |
| R3 | No polymer terminology: REJECT |
| R4 | polymer ∧ (molecular-weight ∨ MWD) ∧ property, with no soft flags: ACCEPT |
| R5 | Partial match, soft flag (off-topic, simulation-only, semicrystalline-only, unverified identity), or missing abstract: REVIEW |
| R6 | Otherwise: REJECT |

Case-sensitive patterns (`Mn`, `Mw`, `Tg`, `PDI`, `GPC`, `Da`) stop "mn" inside ordinary words, or "da" in names, from matching. Inline markup is removed before matching, so `M<sub>w</sub>` becomes "Mw".

### Known limitations
- Preprint and published versions have different DOIs and are kept as separate papers. They need linking before sample-level dedup (Phase 7).
- Filtering uses metadata only. Papers whose abstracts never mention molecular weight are missed (a recall loss, measured later against a hand-labelled sample).
- Web search snippets are short and the provider is unstable. This source contributes few unique papers (3 in the second live run).

## Stage 2: targeted extraction (implemented)

```
ACCEPT/REVIEW paper ──► FullTextLocator: Europe PMC (DOI → PMCID, OA subset only) ▸ Unpaywall (records location/licence)
                    ──► JATS XML parser → DocumentModel (sections, tables with grid-expanded spans, captions, footnotes, SI links)
                    ──► Path 1: table_rules (deterministic)        Path 2 (optional, --llm): LLM reader on targeted chunks
                    ──► merge (rules win; LLM-only values flagged)  ──► 5 validators ──► confidence ──► route ──► SQLite
```

**Rule-based table extractor** (`extraction/table_rules.py`):
- Classifies each header into a descriptor (Mn, Mw, Mz, Đ, DP), a property (Tg, η0, [η], Td, density, σ, ε, E, G′, G″, G_N⁰), or a sample label.
- Takes the unit from `(…)`, `/…` (IUPAC quantity calculus) or `, …`, and handles ×10ⁿ multipliers. If the multiplier's sign is ambiguous, the value is left unnormalized and sent to review.
- Skips theoretical and target columns (`Mn,theo`, `target`, `feed`), recording the skip.
- Reads the technique (SEC/GPC, MALS, NMR, MALDI), calibration standard, eluent, DSC heating rate and Tg definition from the caption, footnotes and headers. Each comes with evidence.
- Links rows across tables by sample label and flags `cross_table_label_linkage`. A label repeated inside one table is *not* merged.
- Names the polymer family from the caption or title only after removing calibration phrases. If it can't, the polymer is "unidentified" and flagged.

**LLM reader** (`extraction/llm.py`, prompt `extract-v1`): official Anthropic SDK, JSON-schema structured output, Pydantic re-validation, verbatim quote anchoring, cache keyed by (prompt, model, context), and token and cost logging to the `llm_calls` table. Mock mode marks its output `test_fixture`.

Not yet implemented: PDF fallback for non-JATS OA papers (PyMuPDF is installed), downloading and parsing SI files, and figure digitization.
