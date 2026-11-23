"""Build a dataset release from the database (re-run any time; every number is recomputed).

Used by scripts/make_submission.py and by the Download view of the Streamlit app.

Writes data/processed/submission_<version>/:
  polyextract_dataset_<version>.csv/.jsonl   human-reviewed + machine-verified records (tier column says which);
                                             flat, one row per measurement, for ML
  samples_<version>.csv                      one row per polymer sample with its molecular descriptors
  measurements_<version>.csv                 one row per property measurement, linked to samples by sample_id
  excluded_theoretical_<version>.csv         computed values (e.g. Tg,theo) removed from the release
  polyextract_candidates_<version>.csv       every extracted candidate record, with route and review status
  stats_<version>.json                       all counts used in DATASHEET.md
  DATASHEET.md                               what the dataset is, how it was made, how far it is verified
Nothing is filtered or changed silently: every count in the datasheet is computed here from the database.
"""

from __future__ import annotations

import io
import zipfile
import json
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path

import pandas as pd

from polyextract.dataset.builder import quality_stats, record_rows
from polyextract.dataset.variants import clean_release
from polyextract.review.policy import AUDIT_MIN, POLICY_VERSION, PRECISION_FLOOR, TOLERATED_WARNINGS, audit_status

ENC = "utf-8-sig"  # BOM so Excel shows °C, σ, η correctly
SAMPLE_COLS = ["sample_id", "paper_id", "doi", "title", "journal", "year", "license", "sample_label", "polymer_name",
               "polymer_family", "architecture", "amorphous_status"]
DESCRIPTORS = ["mn", "mw", "mz", "dispersity", "dpn", "dpw", "dpz", "mwd_skewness", "mwd_kurtosis"]


def build_release(conn: sqlite3.Connection, out: Path, v: str) -> dict:
    """Write the release files for version `v` into `out` and return the stats. `conn` needs row_factory=Row."""
    from polyextract.review.dedup import dedup_counts

    out.mkdir(parents=True, exist_ok=True)
    aud = audit_status(conn)
    audited = {r[0]: r[1] for r in conn.execute(
        "SELECT a.record_id, r.review_status FROM audit_sample a JOIN records r ON r.record_id=a.record_id")}

    support = {1.0: "both_readers", 0.8: "both_readers_partial", 0.6: "table_parser_only", 0.3: "llm_only"}
    rows = record_rows(conn, ("accepted", "corrected", "machine_verified"))
    for r in rows:
        # how many independent readers found it (0.0 = they disagreed and a human decided)
        r["reader_support"] = support.get(json.loads(r["confidence_components"]).get("extractor_agreement"),
                                          "readers_disagreed")
        if r["review_status"] in ("accepted", "corrected"):
            r["verification_tier"] = "human_reviewed"
        else:
            r["verification_tier"] = ("machine_verified_audited" if aud["released"]
                                      else "machine_verified_audit_in_progress")
        r["in_audit_sample"] = r["record_id"] in audited
    ds, theo, hygiene = clean_release(pd.DataFrame(rows))
    ds.to_csv(out / f"polyextract_dataset_{v}.csv", index=False, encoding=ENC)
    ds.to_json(out / f"polyextract_dataset_{v}.jsonl", orient="records", lines=True, force_ascii=False)
    theo.to_csv(out / f"excluded_theoretical_{v}.csv", index=False, encoding=ENC)

    desc_cols = [c for c in ds.columns if any(c == d or c.startswith(d + "_") for d in DESCRIPTORS)]
    desc_cols += ["mw_technique", "calibration"]
    samples = ds[SAMPLE_COLS + desc_cols].drop_duplicates("sample_id")
    hygiene["review_duplicates_removed"] = dedup_counts(conn).get("duplicate", 0)
    hygiene["review_computed_removed"] = dedup_counts(conn).get("excluded_computed", 0)
    hygiene["samples_with_conflicting_descriptors"] = int(
        (ds[["sample_id", *desc_cols]].astype(str).drop_duplicates().groupby("sample_id").size() > 1).sum())
    samples.to_csv(out / f"samples_{v}.csv", index=False, encoding=ENC)
    meas_cols = ["record_id", "sample_id"] + [c for c in ds.columns
                                              if c not in SAMPLE_COLS + desc_cols + ["record_id"]]
    ds[meas_cols].to_csv(out / f"measurements_{v}.csv", index=False, encoding=ENC)

    cand = pd.DataFrame(record_rows(conn, None))
    cand.to_csv(out / f"polyextract_candidates_{v}.csv", index=False, encoding=ENC)

    # Extended release: verified rows + unreviewed candidates that pass all checks with zero errors and where the two
    # readers do not contradict each other. Clearly labelled; never counted as verified.
    ext_rows = []
    for r in record_rows(conn, ("pending",)):  # record_rows already drops rows with errors when statuses are given
        agr = json.loads(r["confidence_components"]).get("extractor_agreement")
        if agr not in support:
            continue  # 0.0 = the readers disagree: needs a human first
        r.update(verification_tier="candidate_unreviewed", in_audit_sample=False, reader_support=support[agr])
        ext_rows.append(r)
    ext, _, _ = clean_release(pd.DataFrame(rows + ext_rows))
    ext.to_csv(out / f"polyextract_extended_{v}.csv", index=False, encoding=ENC)
    extended = {"records": len(ext), "papers": int(ext["paper_id"].nunique()) if len(ext) else 0,
                "samples": int(ext["sample_id"].nunique()) if len(ext) else 0,
                "tiers": dict(Counter(ext["verification_tier"])) if len(ext) else {},
                "reader_support": dict(Counter(ext["reader_support"])) if len(ext) else {},
                "excluded_reader_disagreement": sum(
                    1 for r in conn.execute("SELECT components FROM records WHERE review_status='pending' AND n_errors=0")
                    if json.loads(r[0]).get("extractor_agreement") == 0.0)}

    qs = quality_stats(conn)
    llm_ok = conn.execute("SELECT COUNT(DISTINCT paper_id) FROM llm_calls WHERE error IS NULL").fetchone()[0]
    tiers = Counter(ds["verification_tier"])
    props = Counter(ds["property_name"])
    desc_cov = {d: int(ds[d].notna().sum()) for d in DESCRIPTORS if d in ds} if len(ds) else {}
    stats = {
        "version": v, "built": date.today().isoformat(), "policy": POLICY_VERSION,
        "papers_discovered": qs.get("papers_discovered"), "fulltext_status": qs.get("fulltext_status"),
        "papers_read_by_llm": llm_ok, "candidate_records": len(cand),
        "candidate_papers": int(cand["paper_id"].nunique()) if len(cand) else 0,
        "candidate_routes": dict(Counter(cand["route"])) if len(cand) else {},
        "records_before_hygiene": len(rows), "hygiene": hygiene, "dataset_samples": len(samples),
        "dataset_records": len(ds), "dataset_papers": int(ds["paper_id"].nunique()) if len(ds) else 0,
        "dataset_tiers": dict(tiers), "dataset_properties": dict(props.most_common()),
        "dataset_descriptor_coverage": desc_cov,
        "dataset_polymer_identity_unresolved": int(ds["polymer_family"].isna().sum()) if len(ds) else 0,
        "audit": aud, "extended": extended,
    }
    (out / f"stats_{v}.json").write_text(json.dumps(stats, indent=2, default=str), encoding="utf-8")
    (out / "DATASHEET.md").write_text(datasheet(stats), encoding="utf-8")
    return stats


def zip_release(out: Path) -> bytes:
    """The release folder as an in-memory zip (for a download button)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.iterdir()):
            if f.is_file():
                z.write(f, f"{out.name}/{f.name}")
    return buf.getvalue()


def datasheet(s: dict) -> str:
    a, h, e = s["audit"], s["hygiene"], s["extended"]
    ext_tiers = "\n".join(f"| `{k}` | {n} |" for k, n in e["tiers"].items())
    ext_support = "\n".join(f"| `{k}` | {n} |" for k, n in e["reader_support"].items())
    if a["audit_done"]:
        lo, hi = a["precision_ci95"]
        audit_line = (f"{a['audit_done']} of {a['audit_drawn']} sampled records audited so far: "
                      f"{a['audit_correct']} correct, {a['audit_wrong']} wrong "
                      f"(precision {a['precision']:.1%}, Wilson 95% CI {lo:.3f} to {hi:.3f}).")
    else:
        audit_line = f"Audit sample of {a['audit_drawn']} records drawn; none audited yet."
    status = ("**Released:** the release rule is met." if a["released"] else
              f"**Not yet released:** the release rule needs at least {AUDIT_MIN} audited records with a Wilson 95% "
              f"lower bound of {PRECISION_FLOOR} or more. Until then, treat `machine_verified_audit_in_progress` "
              "records as high-confidence but not yet confirmed.")
    props = "\n".join(f"| {k} | {n} |" for k, n in s["dataset_properties"].items())
    cov = "\n".join(f"| {k} | {n} |" for k, n in s["dataset_descriptor_coverage"].items())
    tiers = "\n".join(f"| `{k}` | {n} |" for k, n in s["dataset_tiers"].items())
    ft = s["fulltext_status"] or {}
    return f"""# PolyExtract polymer dataset {s['version']}: datasheet

Built {s['built']} from the project database. Every number below is computed from the data by
`scripts/make_submission.py`. This is an interim release: extraction and verification are still running.

## What it is
Records that link a polymer sample's molecular-weight descriptors (Mn, Mw, Mz, dispersity, DPn, DPw, DPz,
skewness, kurtosis, as reported) to a measured property from the same paper. Each record carries its DOI,
the verbatim evidence text, the table or section it came from, normalized SI values and the original reported text.
No value is generated or imputed: the LLM is used only to read papers, and every value must appear in the evidence.

## How it was made
1. **Discovery:** OpenAlex search with relevance filters: {s['papers_discovered']} papers.
2. **Legal full text:** open-access JATS XML only, no paywall bypass. Acquired {ft.get('acquired', 0)} papers
   (not open access: {ft.get('not_open_access', 0)}, other or unknown: {ft.get('oa_other', 0) + ft.get('unknown', 0)}).
3. **Two independent readers:** a deterministic table parser reads every paper, and an LLM (Claude Opus) reads the relevant
   tables and text. So far {s['papers_read_by_llm']} papers have been read by the LLM.
4. **Five validation checks:** schema, evidence present and verbatim, unit normalization, physical plausibility,
   and internal consistency (for example Mw/Mn against the reported dispersity).
5. **Confidence and routing:** {s['candidate_records']} candidate records from {s['candidate_papers']} papers
   (routes: {', '.join(f'{k} {n}' for k, n in s['candidate_routes'].items())}).
6. **Verification tiers** (policy `{s['policy']}`):
   - `human_reviewed`: a person accepted or corrected the record.
   - `machine_verified_*`: the table parser and the LLM independently report the same property value and every reported
     descriptor, the reported text is verbatim in the evidence, there are no errors, and the only warnings are bookkeeping ones
     ({', '.join(sorted(TOLERATED_WARNINGS))}).
   - **Audit:** a seeded uniform random sample of machine-verified records is checked by a human against the paper.

## Verification status
{audit_line}

{status}

## This release
**{s['dataset_records']} measurement records on {s['dataset_samples']} polymer samples from {s['dataset_papers']} papers.**

### Row hygiene (applied automatically at build time)
- **Computed values removed:** {h['theoretical_excluded']} rows whose column header marks them as theoretical or calculated
  (for example `Tg,theo`). They are not measurements. Listed in `excluded_theoretical_{s['version']}.csv`.
- **Taken out of review before anyone saw them:** {h['review_duplicates_removed']} duplicate extractions (the same value
  restated in a second table, or split over two columns) and {h['review_computed_removed']} computed values. Each one has an
  audit-trail entry naming the record it duplicates (`review_events`, reviewer `policy:dedup-v2`).
- **Exact duplicates merged:** {h['exact_duplicates_merged']} (same paper, sample, property, variant, value and conditions).
  Any merged ids are kept in `merged_record_ids`.
- **Several values for one sample and property:** {h['multi_value_groups']} cases. These are different measurements,
  not duplicates: for example Tg by DSC and by rheology, Tg1 and Tg2 of a block copolymer, or values under two footnoted conditions.
  The `measurement_variant` column says which is which, built from the paper's own column header.
  Cases with no distinguishing variant: {h['multi_value_groups_without_distinguishing_variant']}.
  `n_values_same_sample_property` gives the size of each group.
- Samples whose descriptors differ between rows: {h['samples_with_conflicting_descriptors']}.

| Tier | Records |
|---|---|
{tiers}

Records whose polymer family is not resolved (they keep the paper's own sample label): {s['dataset_polymer_identity_unresolved']}.

| Property | Records |
|---|---|
{props}

| Descriptor | Records reporting it |
|---|---|
{cov}

## Extended release (verified plus unreviewed candidates)
**{e['records']} evidence-linked records on {e['samples']} samples from {e['papers']} papers**, in
`polyextract_extended_{s['version']}.csv`. This is the release above **plus** candidates that nobody has reviewed yet,
included only when they pass all five checks with **zero errors** and the two readers do not contradict each other.
Rows where the readers disagree ({e['excluded_reader_disagreement']}) and rows with any error are left out.

| verification_tier | Records |
|---|---|
{ext_tiers}

| reader_support | Records |
|---|---|
{ext_support}

`candidate_unreviewed` rows are **not verified**. Use them for exploration, or as a review queue. For training or
reporting results, filter to the verified tiers.

## Files
- `polyextract_dataset_{s['version']}.csv` / `.jsonl`: this release, flat (one row per measurement, sample columns repeated),
  ready for ML. Filter on `verification_tier`.
- `samples_{s['version']}.csv`: one row per polymer sample with its molecular descriptors.
- `measurements_{s['version']}.csv`: one row per measurement, joined to samples on `sample_id`.
- `excluded_theoretical_{s['version']}.csv`: computed values removed from the release.
- `polyextract_extended_{s['version']}.csv`: the extended release (see above), with columns `verification_tier` and `reader_support`.
- `polyextract_candidates_{s['version']}.csv`: all extracted candidates, including unreviewed and blocked ones, for transparency.
  These are **not** verified data.
- `stats_{s['version']}.json`: the numbers above.

## Limitations
- Open-access papers only, which biases journal coverage (MDPI is over-represented).
- Interim: about half the acquired papers have not yet been read by the LLM, and most candidates are not yet human-reviewed.
- A machine-verified record is only as reliable as the audit shows. Read the audit line above before using it.
"""
