# polyextract

**Data-Centric AI for Molecular-Characteristic–Property Relationships in Amorphous Polymers**
B.Tech project, IIT Madras (supervisor: Prof. Tarak Patra).

polyextract finds published papers on amorphous polymers and extracts the molecular-weight characteristics (Mn, Mw, Mz, Đ, DPn, DPw, DPz, MWD skewness and kurtosis) and property measurements they **already report**. Every value stays linked to its source evidence. The resulting dataset is used to test whether richer MWD descriptors improve property prediction beyond Mn, Mw, and Đ.

> **No synthetic scientific data.** The LLM is used only as an extraction tool. Missing values stay missing, and calculated values are marked as calculated together with their inputs. Test fixtures carry `data_origin=test_fixture` and Crossref's reserved test DOI prefix `10.5555`, and they can never enter the curated dataset.

## Status (2026-11-06)

| Phase | Component | Status |
|---|---|---|
| 0 | Audit and plan ([docs/AUDIT_AND_PLAN.md](docs/AUDIT_AND_PLAN.md)) | ✅ |
| 1 | Config, logging, **4 schemas**, SQLite | ✅ tested |
| 2 | **3-source discovery** + explainable filters + OA-venue filter | ✅ tested + live runs |
| 3 | Europe PMC OA full text (JATS) → document model | ✅ live: 812 papers acquired |
| 4 | Rule-based table extractor ✅ live · LLM reader (Anthropic SDK, structured output, quote anchoring, cache, cost log) | ✅ code + tests · ⚠ **not yet run live (no API key)** |
| 5 | Unit normalization, **5 validation categories**, confidence | ✅ tested + live |
| 6 | HITL review service + Streamlit UI + audit trail | ✅ tested · **no human reviews done yet** |
| 7 | Verified dataset exports (JSON/JSONL/CSV/Parquet) + dashboard | ✅ |
| 8 | ML A vs B, GroupKFold by paper, ablation | ✅ code + tests · not run (needs verified data) |
| 9 | 3 workflows: predict · retrieve · evidence-backed answers | ✅ code + tests · need verified data |

### Real numbers (from the database, 2026-11-06, rule-based extractor only)
| Metric | Value |
|---|---|
| Papers discovered / passing filters (ACCEPT) | 1,549 / 164 (+ REVIEW papers processed) |
| OA full texts acquired | 812 |
| Samples / property measurements extracted | 3,525 / 5,789 |
| **Candidate records** (measurement on a sample with MW descriptors) | **530** from 46 papers |
| Routes: low-risk / needs review / blocked by errors | 16 / 374 / 140 |
| **Verified records (human-accepted)** | **0** |
| Descriptor availability (277 MW records) | Mn 150 · Mw 164 · Mz 9 · Đ 187 · DPn 10 · DPw/DPz/skew/kurt 0 |
| Top properties | Tg 274 · elongation 59 · Td 55 · tensile 48 · E 33 · [η] 30 |

### Commands
```bash
polyextract discover --sources crossref            # or all three once OPENALEX_API_KEY is set
polyextract extract --limit 1000 --include-review  # add --llm once LLM_API_KEY is set
polyextract reprocess                              # re-run extractor on cached XML (no network)
python -m streamlit run streamlit_app.py           # review, dashboard, 3 workflows
polyextract export --version v1                    # verified dataset + stats
polyextract ml --version v1                        # Experiment A vs B
```

## Quickstart

```bash
# Windows: use the project venv explicitly (the shell's `python` points to another env)
.venv/Scripts/python.exe -m pip install -e ".[dev,web]"
cp .env.example .env            # add OPENALEX_EMAIL and OPENALEX_API_KEY (free)
.venv/Scripts/python.exe -m pytest                      # 68 tests, no network
.venv/Scripts/python.exe -m polyextract.cli discover --limit-queries 3 --max-per-query 25
```

Each run writes `data/runs/discovery/<run_id>/` containing `raw_records.jsonl`, `candidates.jsonl` (with per-rule filter explanations), `report.json`, `report.md`, and `log.jsonl`. It also updates `data/db/polyextract.sqlite`.

## First live results (3 queries × ≤25 results per source, 2026-11-05)

| | Run 1 (filters-v1) | Run 2 (filters-v2 + Crossref fix) |
|---|---|---|
| Raw records → unique papers | 153 → 115 | 163 → 133 |
| ACCEPT / REVIEW / REJECT | 43 / 18 / 54 | 59 / 45 / 29 |
| Crossref accepted | 3 / 53 | **31 / 66** |

Both changes came from inspecting live output:
1. **Crossref** ignores relevance ranking under cursor pagination unless `sort=relevance` is passed explicitly. This was confirmed by isolating parameters; a regression test was added.
2. **Wikipedia and Q&A pages** from web search were being ACCEPTED. The new R0 publication-identity rule rejects known non-scholarly pages and caps any web hit without a DOI at REVIEW.

Known limitations: anonymous OpenAlex search is throttled (set `OPENALEX_API_KEY`), and DuckDuckGo intermittently blocks queries ("No results found"). Both failures are recorded per query and the run continues.

## Docs

[ARCHITECTURE](docs/ARCHITECTURE.md) · [DATA_SCHEMA](docs/DATA_SCHEMA.md) · [EXTRACTION_PIPELINE](docs/EXTRACTION_PIPELINE.md) · [VALIDATION_RULES](docs/VALIDATION_RULES.md) · [HITL_WORKFLOW](docs/HITL_WORKFLOW.md) · [ML_EXPERIMENTS](docs/ML_EXPERIMENTS.md) · [API_COST_TRACKER](docs/API_COST_TRACKER.md) · [INTERVIEW_PREPARATION](docs/INTERVIEW_PREPARATION.md)
