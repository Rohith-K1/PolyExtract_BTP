# Phase 0 — Repository Audit and Implementation Plan

*Written 2026-11-05 at the start of implementation. Status markers are kept up to date in README.md.*

## 1. Project understanding

The goal is to build a **literature-to-dataset pipeline**. It finds published papers about amorphous polymers and extracts the molecular-weight characteristics and material-property measurements those papers *already report*. Every value has to stay linked to its source evidence (DOI, page, table, verbatim text). The values are validated and human-reviewed. The curated dataset is then used to test one research question:

> Do richer molecular-weight-distribution (MWD) descriptors (Mz, DPn/DPw/DPz, skewness, kurtosis, full curves) predict properties better than Mn, Mw, and Đ alone?

The LLM is used **only as an extraction tool**. Values are never generated, imputed, or "filled in". Missing stays missing. Anything ambiguous is flagged.

## 2. Repository audit (state found)

| Item | Finding | Action |
|---|---|---|
| Git | `master` branch, **no commits**; `.gitignore` and `.env.example` untracked | Keep; nothing to migrate. Commits are left to the user. |
| `.gitignore` | Sensible (secrets, venv, caches, `data/raw`, `data/processed`, checkpoints) | Extended with `data/cache`, `data/db`, `*.sqlite` |
| `.env.example` | `OPENALEX_EMAIL, LLM_PROVIDER, LLM_API_KEY, LLM_MODEL, WEB_SEARCH_API_KEY` | Extended (`OPENALEX_API_KEY`, `CROSSREF_MAILTO`, paths) |
| `.env` | Absent: no credentials are configured | Every integration has a mock or test path |
| `.venv` | Python **3.13.5** (Anaconda base), only `pip` installed | Used as the project interpreter |
| Shell `python` | Points to an unrelated `hermes-agent` venv (3.11) | Always invoke `.venv/Scripts/python.exe` |
| Source code | None | Built from scratch |

**Network probe (2026-11-05):**
- OpenAlex: reachable, but *anonymous search is rate-limited* ("use a free API key"). The error arrives as a JSON body, so the client must detect it in the body as well as from the HTTP status.
- Crossref: reachable, HTTP 200.
- DuckDuckGo HTML: HTTP 202 (its bot-challenge response). The web-search source is therefore treated as best-effort.

## 3. Directory structure

```
polyextract_BTP/
├── pyproject.toml            # deps + extras ([web],[pdf],[ml],[app],[dev])
├── .env.example
├── config/
│   ├── discovery.yaml        # queries, sources, year range, limits
│   └── filters.yaml          # deterministic metadata-filter term groups & rules
├── src/polyextract/
│   ├── config.py             # pydantic-settings, .env loading
│   ├── logging_setup.py
│   ├── exceptions.py
│   ├── schemas/              # 4 core schemas + shared Quantity/Evidence/Provenance
│   ├── storage/              # SQLite repository
│   ├── discovery/            # Stage 1: http client, 3 adapters, filters, dedup, pipeline, report
│   ├── acquisition/          # Phase 3: OA full-text location, PDF/HTML parsing, section/table finder
│   ├── extraction/           # Phase 4: LLM client(s), prompts (versioned), cache, cost tracking, mock
│   ├── normalization/        # units + value parsing ("1.2 × 10^5", "45 kDa")
│   ├── validation/           # 5 validation categories, each its own module
│   ├── scoring/              # explainable confidence
│   ├── review/               # HITL routing, decisions, audit trail; Streamlit app
│   ├── dataset/              # verified-record builder, exports, quality dashboard
│   ├── ml/                   # Experiment A/B, grouped splits, ablation
│   └── apps/                 # 3 workflows: predict, structured retrieval, evidence-backed QA
├── tests/                    # pytest; fixtures use Crossref's test DOI prefix 10.5555
├── data/                     # (gitignored) raw/, processed/, cache/, db/
└── docs/                     # the 9 required documents + this audit
```

## 4. Architecture

```
            ┌───────────── STAGE 1 (deterministic, no LLM) ─────────────┐
 queries →  OpenAlex ┐
            Crossref ├→ normalize → DOI/title dedup → explainable metadata filters → candidates (ACCEPT/REVIEW/REJECT)
            WebSearch┘        (per-source ids kept)        (rule-level reasons stored)
            └───────────────────────────────────────────────────────────┘
                                         │ ACCEPT (+ reviewed REVIEW)
            ┌───────────── STAGE 2 (targeted extraction) ───────────────┐
 OA full-text locator (Unpaywall/OpenAlex/Crossref links, licence-checked)
   → PDF/HTML parse (pages, tables, captions, SI) → relevance scorer picks passages/tables
   → LLM structured extraction (versioned prompt, JSON schema, cache, cost log)
   → Pydantic schemas → unit normalization → 5 validators → confidence score
   → review queue (Streamlit) → audit trail → verified dataset (JSON/JSONL/CSV/Parquet)
            └───────────────────────────────────────────────────────────┘
                                         │
             ML (grouped-by-paper CV, baseline vs MWD-enriched, ablation)
                                         │
             Apps: forward prediction · property-conditioned retrieval · evidence-backed QA
```

Key design decisions:
- **Modular monolith, SQLite.** One process and one file DB is enough at the expected scale (thousands of papers, tens of thousands of rows). JSON payload columns hold the Pydantic models, and key columns are indexed.
- **Every value is a `Quantity`.** Each carries `reported_text`, the original value and unit, the normalized value and unit, `value_origin ∈ {reported, calculated}`, the calculation record, and its evidence. A calculated value cannot exist without a recorded method and inputs; the schema enforces this.
- **`data_origin` on every record** (`literature_extraction` | `test_fixture`). The dataset builder refuses `test_fixture` rows.
- **No LLM in Stage 1.** Filtering is rule-based, reproducible, and explainable. The LLM sees only passages a deterministic locator selected.
- **Own unit table** (not `pint`). The polymer-specific units (kDa, kg/mol, cP, Pa·s, MPa, and °C ↔ K) form a small closed set that needs explicit, testable conversion rules. `pint` models Da as a mass rather than a molar mass.

## 5. Phased plan

| Phase | Deliverable | Done when |
|---|---|---|
| 0 | This audit | ✔ |
| 1 | Package skeleton, config, logging, exceptions, **4 schemas**, storage, tests | schema tests pass |
| 2 | HTTP client (rate limit, retry, cache), **OpenAlex / Crossref / WebSearch** adapters, filters, dedup, discovery report, CLI | mocked tests pass + live run report |
| 3 | OA full-text locator, PDF/HTML parsing with page/table/caption ids, relevant-content locator | parses real OA papers |
| 4 | LLM extraction: provider abstraction, prompts v1, structured output, cache, cost tracker, mock | mock + small live batch |
| 5 | Normalization, **5 validators**, provenance checks (evidence ↔ value match), confidence | per-category tests |
| 6 | Review queue, decisions (accept/reject/correct/re-extract + comment), audit log, Streamlit UI | round-trip test |
| 7 | Verified-record builder, exports, quality dashboard | counts come from the DB |
| 8 | ML experiments A/B, GroupKFold by paper, ablation | results recorded with seeds |
| 9 | 3 workflows | each with tests |
| 10 | E2E run, docs, limitations | honest status table |

## 6. Technical risks

1. **Full-text access** is the main bottleneck. Much polymer literature (ACS, Elsevier, Wiley) is paywalled. Without institutional text-and-data-mining (TDM) access, only open-access papers can be processed, and that caps the dataset size.
2. **Table parsing from PDFs** is lossy (merged cells, units in headers, footnotes such as "ᵃ determined by GPC in THF vs PS").
3. **Sample ↔ measurement linkage** across different tables and paragraphs ("PS-3" in Table 1 and in Figure 4). This is the hardest extraction step.
4. **API limits.** OpenAlex anonymous search is throttled and DuckDuckGo issues bot challenges.
5. **LLM cost and drift.** Mitigated by a response cache keyed on (prompt version, model, input hash), pinned prompts, and cost logs.
6. **Figure-only data.** MWD curves are almost always plots. Digitizing them is a separate, error-prone task, and digitized values must be marked as such.

## 7. Scientific risks

1. **GPC calibration.** Molecular weights measured relative to PS standards differ from absolute (MALS) values. Technique, calibration, and solvent are stored, and records are not pooled blindly.
2. **Tg depends on method.** Heating rate, onset vs midpoint, and DSC vs DMA all change the value, so these are stored as conditions or definitions.
3. **"Viscosity" is ambiguous.** Zero-shear, complex at a given frequency, and intrinsic viscosity are different properties. The schema has distinct property names for each.
4. **Rich MWD descriptors are rarely reported.** Skewness and kurtosis will be sparse, so the research question may only be testable on a small subset. That outcome is reported honestly and not compensated for.
5. **Skewness and kurtosis definitions vary** (of w(log M) or of the number distribution; excess vs raw kurtosis). The definition is stored, and values with incompatible definitions are not mixed.
6. **Amorphous status** is often implicit. It is classified only when the evidence supports it, and is `unknown` otherwise.
7. **Strong known physics baseline.** Flory–Fox (Tg vs Mn) and η₀ ∝ Mw^3.4 mean Mn and Mw already carry most of the signal. Any MWD gain has to beat that baseline.
8. **Leakage.** Many records come from the same paper or series, so splits are grouped by paper.

## 8. External dependencies and credentials

| Need | Required? | Notes |
|---|---|---|
| OpenAlex API key (free) | Strongly recommended | Anonymous search is throttled |
| Contact email (`mailto`) | Recommended | Crossref and OpenAlex polite pools |
| LLM API key and provider | Required for Phase 4 live runs | Mock mode works without it |
| Unpaywall (email only) | Phase 3 | Finds legal OA full-text locations |
| Web-search key | Optional | Defaults to DuckDuckGo (`ddgs`, no key, best-effort) |
| Institutional TDM (Elsevier/Wiley/Springer) | Optional | Only if IITM licences permit |

## 9. Decisions requiring your input

1. **LLM provider, model, and budget** (Phase 4).
2. **Verification policy.** (a) Every verified record needs a human "accept". (b) A deterministic auto-validated tier, where the extracted value string is found verbatim in the evidence and all validators pass, plus a random human audit sample. 4.6K human-accepted records is a lot of review work; (b) is defensible only if the audit error rate is measured and reported. *Default until you decide: (a).*
3. **"HITL4" meaning.** Possible readings: (i) a typo for "HITL for review"; (ii) the four review decisions the spec lists (accept / reject / correct / request re-extraction); (iii) four review checkpoints. I have **not** implemented anything that depends on the number 4.
4. **"across the 3 flows"**: does this mean review covers the three application workflows, or something else?
5. **Institutional full-text access**: can IITM TDM credentials be used, or is the project OA-only?
6. **Scope priority** (Prof. Patra): which polymer families and properties come first? My suggested default is Tg plus zero-shear viscosity for common amorphous homopolymers (PS, PMMA, PC, PVC, PIB, PI, PB).

## 10. Resume claims → required evidence

| Claim | Code | Tests | Data / artifacts |
|---|---|---|---|
| 1. LLM pipeline → evidence-linked ML/DL dataset | `discovery/`, `acquisition/`, `extraction/`, `dataset/` | E2E test on fixtures; live run log | Exported dataset with evidence columns; DL mentioned only if a DL model is actually justified and trained |
| 2. 2-stage cascade, 3-source retrieval + metadata filters, then targeted LLM extraction | 3 adapters, `filters.py`, `extraction/locator` | Adapter tests (mocked), filter rule tests | Discovery report: per-source counts, filter funnel, LLM calls only on filtered set |
| 3. 4 schemas, 5 validation checks, DOI provenance, confidence | `schemas/`, `validation/v1..v5`, `scoring/` | One test module per validation category | Validation reports and confidence components stored per record |
| 4. 4.6K verified records, 9 descriptors, HITL, 3 flows | `dataset/`, `review/`, `apps/` | Review round-trip test, app tests | Count from the DB. **If it is below 4,600, the real number is what gets reported.** "HITL4" wording is pending (decision 3). |

**Record unit (proposed):** one *verified record* is one `PropertyMeasurement`. Its `PolymerSample` must have at least one molecular descriptor that is either directly reported or validly calculated with recorded inputs. Both must carry evidence, pass all blocking validations, and have review status `accepted` under the verification policy. Papers, samples, descriptors, and fields are counted and reported separately.
