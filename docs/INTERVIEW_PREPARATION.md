# Interview Preparation

Every answer below points to code that exists. Before the interview, refresh the numbers with:
`polyextract export --version v1` (prints all dashboard stats) and the Streamlit dashboard.
**Quote only numbers printed by those commands.**

---

## 60-second story (say this first)

> "Experimental polymer data, such as molecular weights and Tg or viscosity, is scattered across thousands of papers, mostly inside tables. Under Prof. Tarak Patra I built a pipeline that turns that literature into an evidence-linked dataset. **Stage 1** is deterministic: it searches three sources (OpenAlex, Crossref, web search), deduplicates by DOI, and applies versioned keyword-rule metadata filters, including an open-access filter, so we only pay for papers we can legally read. **Stage 2** fetches open-access full text as JATS XML from Europe PMC and runs targeted extraction on the relevant tables and passages: a rule-based table reader plus an LLM as an independent second reader. Everything goes into four Pydantic schemas. Five validation categories check them, including whether the extracted value actually appears in the cited passage. Each record gets an explainable confidence score and then goes to human review in a Streamlit app. Only human-verified records reach the three applications: property prediction, property-conditioned retrieval, and evidence-backed Q&A."

## Resume point → what to say → where it lives

### 1. "LLM pipeline turning polymer literature into an evidence-linked ML/DL dataset"
- **Evidence-linked:** every value is a `Quantity` with the verbatim `reported_text` plus `Evidence` (DOI, table id, page, verbatim row). See `schemas/common.py`. A reviewer can go from any CSV cell back to the sentence in the paper (`evidence_text` and `evidence_url` columns).
- **LLM role:** a *reader*, not a generator. It must return the verbatim reported text **and a verbatim quote with a chunk id**. Code then checks the quote exists in that chunk and otherwise drops the value (`extraction/llm.py: _anchor`). Our own parser normalizes units, not the LLM.
- **ML/DL:** `ml/experiments.py` runs grouped-CV Ridge, RF and GBM. Say "DL only if the data justifies it". With hundreds to low thousands of rows per property, tree models are the honest choice.

### 2. "2-stage cascade: 3-source retrieval with metadata filters, then targeted LLM extraction"
- **Three adapters** behind one interface (`discovery/base.py`): OpenAlex (cursor paging, abstract rebuilt from the inverted index), Crossref (polite pool), web search (`ddgs`, DOIs from URLs).
- **Metadata filters (rules R0–R6, `filters-v2`):** term groups in YAML with case-sensitive abbreviations, so "Mn" counts but "mn" inside a word doesn't. Every decision stores the rule that fired and the matched term and field.
- **The open-access insight:** the first extraction run found that **only 2 of 40** keyword-discovered papers had legal full text. So I made open access a *metadata filter*: OpenAlex `has_pmcid:true`, and a Crossref ISSN filter for OA journals. I chose those journals by **measuring** Europe PMC full-text availability (Polymers 10/10, ACS Omega 10/10, Materials 10/10, Macromolecules 0/10).
- **Targeted:** `build_chunks()` sends the LLM only the tables with molecular-weight or property columns and the paragraphs mentioning GPC, DSC, Tg and similar, never the whole paper.

### 3. "Structured outputs across 4 schemas via 5 validation checks, DOI provenance, and confidence scoring"
- **4 schemas:** PaperMetadata, PolymerSample, MolecularCharacteristics (9 descriptors), PropertyMeasurement. All use `extra="forbid"`.
- **Structured outputs:** the API enforces a JSON schema, then the output is re-validated with Pydantic and mapped into the four schemas.
- **5 checks** (`validation/checks.py`): V1 schema, V2 units and ranges, V3 science (Mn ≤ Mw ≤ Mz, Đ ≈ Mw/Mn), V4 provenance (DOI plus **value-in-evidence**), V5 completeness and duplicates. Errors block; warnings route to review.
- **Confidence:** 9 measurable components (`validation/confidence.py`). Any error caps the score at 0.3. LLM self-confidence is deliberately not used.

### 4. "Curated N verified records linking 9 molecular descriptors to properties, with HITL review across the 3 flows"
- **Record** = one property measurement on one sample that has molecular descriptors, with evidence and passing validation, **verified in one of two tiers** (below).
- **How "verified" works (say it exactly like this):** "Verification is tiered. A record is machine-verified only when two independent extractors, a rule-based table parser and the LLM, report the identical value and descriptors, the value appears verbatim in the source, and all 5 validation checks are clean. Everything else, such as disagreements, text-only values and warnings, goes to human review. To check that the machine tier is trustworthy, I audited a random sample of N machine-verified records by hand: precision X% (95% CI a–b). The tier only counts if the lower bound is at least 90%."
- **Never say** "I checked all 4.6K by hand." **Do say** how many were human-reviewed and how many were audited. Both numbers are in `polyextract export` → stats → `verified_human_reviewed` and `audit`.
- **Why this design is defensible:** it's standard data-QA practice (estimate precision from a sample); it uses two independent readers instead of one model's self-confidence; and the release gate can fail. If the audit fails, the tier isn't counted.
- **9 descriptors:** Mn, Mw, Mz, Đ, DPn, DPw, DPz, MWD skewness, MWD kurtosis. Be ready to say which are common (Mn, Mw, Đ) and which are rare (skewness, kurtosis). The dashboard shows availability per descriptor.
- **HITL:** accept, reject, correct, or request re-extraction. A correction is re-validated. Each decision writes an append-only audit row with a snapshot. Blocked records can't be accepted.
- **3 flows** read only verified records. Predictions are labelled "MODEL PREDICTION — not an experimental measurement".
- **Count:** state the number from `polyextract export`. If it's below 4.6K, say: "the pipeline produces about X candidate records per open-access paper; the bottleneck is open-access coverage plus review time."

---

## Debugging stories (interviewers love these; all real)
1. **Crossref relevance bug.** Accept rate was 3/53 against 34/49 for OpenAlex. I varied one parameter at a time and found that cursor pagination drops relevance ranking unless `sort=relevance` is sent. Fixed: 31/66. Regression test added.
2. **Wikipedia accepted as a paper.** The term match was right but the identity was wrong. Rule R0 now caps any web hit without a DOI at REVIEW and rejects known non-scholarly sites.
3. **Real tables broke the number parser.** `35,000` was read as 35 (decimal comma), `20 307` as 20 (space used as a thousands separator), `g mol–^{1}` wasn't recognised, a footnote marker sat mid-value, and in `75.1 [1.09]` the Đ was taken as the unit. Each one is now a regression test built from the exact string seen in the paper.
4. **A calibration phrase named the wrong polymer.** "PMMA … vs polystyrene standards" would have labelled samples as PS, so calibration phrases are removed before polymer identification.
5. **Silent unit bug caught before it shipped:** a placeholder mapping would have made `mPa·s` a factor of 10⁶ instead of 10⁻³. Unit tests now cover every unit.
6. **Robustness:** a sample-ID collision in one paper crashed an 845-paper run, so per-paper failures are now isolated and recorded.
7. **The LLM ignored its schema, and Windows was to blame.** Through `claude -p`, Opus returned prose, then JSON in its own format. The cause: the Windows `claude.cmd` shim truncates multi-line arguments at the first newline, so the model never saw the full instructions or the schema. The fix was calling the real `claude.exe` and passing the prompt as a file. A second leak: the CLI loaded the project's own notes into every call (+40K tokens), so calls now run from an empty directory. Result: 39 property values with 0 unanchored quotes on the test paper. Opus vs Sonnet on that paper: 39 vs 12 values at equal precision, so I chose Opus for recall.
8. **Billing failures could pass silently.** An out-of-credit API error was being caught as an ordinary per-paper error, so a long run would have "finished" with no LLM output. Account, auth and usage-limit errors now stop the run loudly.

## Likely questions
- **"Why not just RAG?"** Retrieval answers questions; we needed *structured, validated records*. The QA flow is retrieval over verified rows, and it cites row ids.
- **"How do you know the LLM didn't hallucinate?"** Quote anchoring at extraction, V4 value-in-evidence, agreement with the independent rule-based reader, and human review. Four layers, none of which trusts the model's own confidence.
- **"Why rules *and* an LLM?"** Rules are cheap, deterministic and good at clean tables. The LLM handles values in text and messy layouts. Agreement between them is the strongest confidence signal we have.
- **"What about GPC calibration differences?"** Technique and calibration standard are stored (e.g. "polystyrene standards"), so models can stratify; we never pool silently.
- **"Leakage?"** GroupKFold by paper. A and B are compared on identical rows.
- **"How many did a human actually check?"** Give both numbers: human-reviewed (incl. audited) and the machine-verified tier with its audit precision + CI. Explain why 300 audits give roughly ±2–3% precision.
- **"Couldn't both extractors make the same mistake?"** Yes. They read the same source, so a typo in the paper passes through both, as does an ambiguous sample label. That's exactly what the random audit measures, and why the tier has a precision floor.
- **"Biggest limitation?"** Open-access coverage (paywalled journals such as Macromolecules are excluded), rare MWD descriptors, and figure-only data not being digitized.

## Try it yourself (5 min each)
1. Add a polymer family regex to `FAMILIES` in `extraction/table_rules.py`, re-run `pytest`, then `polyextract extract --redo --limit 20`.
2. Review 20 records in the Streamlit app, then run `polyextract export` and open the CSV. Find a value's `evidence_text`.
