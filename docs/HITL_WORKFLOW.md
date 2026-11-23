# Human-in-the-Loop (HITL) Review

Code: [review/service.py](../src/polyextract/review/service.py) (UI-independent, tested) and [streamlit_app.py](../streamlit_app.py) (UI).
Run the UI with `.venv/Scripts/python.exe -m streamlit run streamlit_app.py`.

## Decisions
| Decision | Effect | Guard |
|---|---|---|
| accept | status `accepted` | Refused if the record has blocking errors |
| correct | Fields updated, then the record is **re-validated through all 5 checks and re-scored** | Refused if the correction still fails validation or breaks the schema |
| reject | status `rejected` | none |
| request re-extraction | status `needs_reextraction`. The next `extract --redo` re-processes the paper, and the record returns as `pending` | none |

Every decision requires a reviewer name and allows a comment. Each one appends a row to `review_events` (append-only) containing a **snapshot** of what the reviewer saw: the original extraction, the findings, the confidence components, and the route. Re-extraction never erases review history.

## Verification tiers (policy `dual-agreement-v2`; v1 on 2026-11-06, v2 on 2026-11-23)
Reviewing every record by hand is too slow for one person, so verification is tiered. Every tier is visible in the data: export column `verification_tier`, and `review_events.reviewer = "policy:dual-agreement-v1"` or `"policy:dual-agreement-v2"` for each promotion.

**Why v2.** Under v1 (zero warnings allowed) only 34 of 1,955 records qualified. 456 records had exact dual agreement, verbatim evidence and zero errors; 422 of them were held back only by three bookkeeping warnings that say nothing about whether a value is right: `cross_table_label_linkage` (descriptors and property in different tables joined by sample label, a join the LLM reader independently confirms), and `polymer_identity_missing` / `polymer_identity_unresolved` (no polymer family resolved; the record keeps the paper's sample label). v2 tolerates only those three. v2 promoted 381 more records, for a machine tier of 415. The release rule did not change.

| Tier | Which records | Who decides | Status |
|---|---|---|---|
| **Machine-verified** | The rule-based reader and the LLM reader independently report the **same property value and every reported descriptor** (`extractor_agreement` = 1.0); the reported text is **verbatim in the evidence**; **zero errors**, and no warnings except the three bookkeeping ones above | Deterministic policy ([review/policy.py](../src/polyextract/review/policy.py)) | `machine_verified` |
| **Human-reviewed** | Everything else: disagreements, LLM-only values, warnings, errors | A reviewer, table by table or record by record | `accepted` / `corrected` |
| **Audit** | A seeded **uniform random sample** of machine-verified records (100 drawn on 2026-11-23) | A reviewer, in Review → Audit | `accepted` = machine was right; `corrected` / `rejected` = wrong |

**Release rule:** machine-verified records count as **verified** only when at least 100 audited records exist **and** the Wilson 95% lower bound on precision is ≥ 0.90. Until then, only human-reviewed records count. If the audit fails, the tier stays withheld; the rule is tightened (new policy version) and a fresh sample is drawn.

Other guarantees:
- **The policy never overrides a human.** It only promotes `pending` records.
- **Re-extraction resets the machine tier.** A machine-verified record goes back to `pending` and is re-decided on the new extraction.
- **Blocked records are never promoted.**

The audit sample is drawn from the pool at the time of the draw, so **draw the final audit after the last extraction run**. The dashboard shows how many audited records there are compared with the size of the pool.

Commands:
- `polyextract verify`: apply the policy. This also runs automatically at the end of `extract` and `reprocess`.
- `polyextract verify --draw-audit 300`: add 300 random records to the audit sample.

## Policy
- **Confidence never accepts anything by itself.** A high score only orders the queue. The machine tier above needs agreement between two independent readers plus clean validation, and it counts only after an audit passes.
- **Verified** = (`accepted` / `corrected`) or (`machine_verified` and the tier has been released), with no remaining errors, and `literature_extraction` origin. The dataset builder raises an error if a test fixture is ever found in the verified set.
- **Table-level review** (Review → By table) shows the original table rebuilt from the OA full text next to the extracted rows. Nothing is pre-ticked. Each record still gets its own audit event, tagged `[table review]`.

## What the reviewer sees
The paper title and DOI link; the sample and polymer; the reported text → normalized value for the property and every descriptor; the **verbatim evidence passage** (table row, caption, location); confidence components as a bar chart; validation findings; and the audit trail.

## HITL across the 3 workflows
The three application workflows (prediction, retrieval, evidence-backed answers) read **only verified records**: human-reviewed ones, plus machine-verified ones once the audit has released that tier. Each row carries its `verification_tier`. Every result therefore traces to a reviewer decision plus a DOI and its evidence. The UI refuses to run them while no verified records exist.
