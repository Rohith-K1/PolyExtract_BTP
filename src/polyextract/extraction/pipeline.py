"""Stage 2 orchestration: discovered paper -> legal OA full text -> targeted extraction -> validation
-> confidence -> stored records routed for human review.

Only papers that passed Stage-1 filters (ACCEPT, optionally REVIEW) enter here.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from polyextract.acquisition.document import DocumentModel
from polyextract.acquisition.fulltext import FullTextLocator
from polyextract.exceptions import SourceError
from polyextract.extraction.table_rules import EXTRACTOR_VERSION, ExtractionResult, extract_tables
from polyextract.logging_setup import get_logger
from polyextract.schemas import PaperMetadataSchema
from polyextract.storage import records as rstore
from polyextract.storage.sqlite import Repository
from polyextract.validation.checks import RecordBundle, Severity, validate_bundle
from polyextract.validation.confidence import score_record

log = get_logger("extract")
LOW_RISK_THRESHOLD = 0.85


@dataclass
class ExtractStats:
    papers: int = 0
    status: Counter = field(default_factory=Counter)
    samples: int = 0
    molecular: int = 0
    measurements: int = 0
    records: int = 0
    routes: Counter = field(default_factory=Counter)
    finding_codes: Counter = field(default_factory=Counter)

    def as_dict(self) -> dict:
        return {k: (dict(v) if isinstance(v, Counter) else v) for k, v in self.__dict__.items()}


def merge_llm_only(res: ExtractionResult, llm: ExtractionResult) -> int:
    """Add LLM records for (sample, property) pairs the rules did not find. Rule records win on overlap."""
    have = {(m.sample_id.split("#s:", 1)[-1].casefold(), m.property_name) for m in res.measurements}
    sids = {s.sample_id for s in res.samples}
    mcs = {m.sample_id for m in res.molecular}
    added = 0
    for m in llm.measurements:
        if (m.sample_id.split("#s:", 1)[-1].casefold(), m.property_name) in have:
            continue
        if m.sample_id not in sids:
            s = next(x for x in llm.samples if x.sample_id == m.sample_id)
            res.samples.append(s)
            sids.add(s.sample_id)
        if m.sample_id not in mcs:
            mc = next((x for x in llm.molecular if x.sample_id == m.sample_id), None)
            if mc:
                res.molecular.append(mc)
                mcs.add(m.sample_id)
        res.measurements.append(m)
        res.issues[m.measurement_id] = ["llm_only_value"]
        added += 1
    return added


def process_document(paper: PaperMetadataSchema, doc: DocumentModel, run_id: str | None = None,
                     llm_result: ExtractionResult | None = None):
    """Pure (no I/O): extract, validate, score. Returns (result, findings, scored records)."""
    res = extract_tables(doc, run_id=run_id)
    if llm_result is not None:
        merge_llm_only(res, llm_result)
    bundle = RecordBundle(paper=paper, samples=res.samples, molecular=res.molecular,
                          measurements=res.measurements, extractor_issues=res.issues)
    findings = validate_bundle(bundle)
    mc_by_sample = {m.sample_id: m for m in res.molecular}
    scored = []
    for m in res.measurements:
        mc = mc_by_sample.get(m.sample_id)
        if mc is None:
            continue  # not a dataset record: property without molecular descriptors
        agreement = 0.6
        if llm_result is not None:
            from polyextract.extraction.agreement import agreement_score
            agreement = agreement_score(m, mc, llm_result)
        if "llm_only_value" in res.issues.get(m.measurement_id, []):
            agreement = 0.3  # single LLM reader, no deterministic confirmation
        conf = score_record(m, mc, findings, agreement=agreement)
        if conf.blocking_errors:
            route = rstore.ROUTE_BLOCKED
        elif conf.warnings or conf.overall < LOW_RISK_THRESHOLD:
            route = rstore.ROUTE_REVIEW
        else:
            route = rstore.ROUTE_PRIORITY_OK
        scored.append((m, mc, conf, route))
    return res, findings, scored


def reprocess_cached(repo: Repository, fulltext_dir: Path, run_id: str | None = None, llm=None) -> ExtractStats:
    """Re-run extraction+validation on already-acquired papers from the local XML cache (no network).
    Used after improving the extractor; review history is preserved by replace_paper_extraction.
    `llm` should be a cache_only LLMClient: cached LLM answers are re-used for agreement, no new calls."""
    from polyextract.acquisition.jats import parse_jats

    rstore.init_schema(repo.conn)
    st = ExtractStats()
    rows = repo.conn.execute(
        "SELECT d.paper_id, d.source_url, d.license, p.payload FROM documents d JOIN papers p ON p.paper_id = d.paper_id "
        "WHERE d.status = 'acquired'").fetchall()
    for r in rows:
        st.papers += 1
        pmcid = r["source_url"].split("/rest/")[1].split("/")[0]
        f = fulltext_dir / f"{pmcid}.xml"
        if not f.exists():
            st.status["xml_missing"] += 1
            continue
        paper = PaperMetadataSchema.model_validate_json(r["payload"])
        try:
            doc = parse_jats(f.read_bytes(), paper_id=paper.paper_id, doi=paper.doi, source_url=r["source_url"], license=r["license"])
            llm_res = None
            if llm is not None:
                from polyextract.extraction.llm import run_llm_extraction

                llm_res, _, call = run_llm_extraction(llm, doc, run_id)
                st.status["llm_cache_hit" if call is not None and call.cached else "llm_cache_miss"] += 1
            res, findings, scored = process_document(paper, doc, run_id, llm_result=llm_res)
            rstore.replace_paper_extraction(repo.conn, paper.paper_id, res.samples, res.molecular, res.measurements,
                                            findings, scored, EXTRACTOR_VERSION)
            repo.conn.execute("UPDATE documents SET n_tables_used=? WHERE paper_id=?", (len(res.tables_used), paper.paper_id))
            repo.conn.commit()
        except Exception:
            repo.conn.rollback()
            log.exception("reprocess failed for %s", paper.doi)
            st.status["extraction_failed"] += 1
            continue
        st.status["reprocessed"] += 1
        st.samples += len(res.samples)
        st.molecular += len(res.molecular)
        st.measurements += len(res.measurements)
        st.records += len(scored)
        for *_, route in scored:
            st.routes[route] += 1
        for fd in findings:
            if fd.severity is not Severity.INFO:
                st.finding_codes[f"{fd.severity.value}:{fd.code}"] += 1
    return st


LLM_CALLS_SQL ="""CREATE TABLE IF NOT EXISTS llm_calls (
    paper_id TEXT, model TEXT, prompt_version TEXT, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
    cached INTEGER, seconds REAL, stop_reason TEXT, error TEXT, n_dropped_unanchored INTEGER, ts TEXT)"""


def run_extraction(repo: Repository, locator: FullTextLocator, papers: list[PaperMetadataSchema],
                   fulltext_dir: Path, run_id: str | None = None, llm=None) -> ExtractStats:
    rstore.init_schema(repo.conn)
    repo.conn.execute(LLM_CALLS_SQL)
    st = ExtractStats()
    for paper in papers:
        st.papers += 1
        if not paper.doi:
            rstore.save_document_status(repo.conn, paper.paper_id, "no_doi")
            st.status["no_doi"] += 1
            continue
        try:
            loc = locator.locate(paper.doi)
        except SourceError as e:
            rstore.save_document_status(repo.conn, paper.paper_id, "locate_failed", note=str(e)[:300])
            st.status["locate_failed"] += 1
            continue
        if loc.status != "jats_available":
            rstore.save_document_status(repo.conn, paper.paper_id, loc.status, url=loc.url, license=loc.license, note=loc.note)
            st.status[loc.status] += 1
            repo.conn.commit()
            continue
        try:
            doc = locator.fetch_jats(loc, paper_id=paper.paper_id, doi=paper.doi, cache_dir=fulltext_dir)
        except (SourceError, Exception) as e:  # malformed XML is a per-paper failure, not a run failure
            rstore.save_document_status(repo.conn, paper.paper_id, "fetch_or_parse_failed", note=f"{type(e).__name__}: {e}"[:300])
            st.status["fetch_or_parse_failed"] += 1
            continue
        llm_res = None
        if llm is not None:
            from polyextract.extraction.llm import LLMAccountError, run_llm_extraction

            failed = False
            try:
                llm_res, dropped, call = run_llm_extraction(llm, doc, run_id)
            except LLMAccountError:
                raise  # billing / login / usage limit: stop the whole run
            except Exception as e:  # one paper's LLM output must never abort the run; rule records still saved
                log.exception("LLM step failed for %s", paper.doi)
                failed, llm_res, dropped, call = True, None, [], None
                repo.conn.execute("INSERT INTO llm_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (paper.paper_id, llm.model, None, 0, 0, 0.0, 0, 0.0, None,
                                   f"postprocess_failed: {type(e).__name__}: {e}"[:300], 0, rstore.now()))
            if failed:
                pass
            elif call is None:  # nothing relevant to read: record it so incremental runs don't revisit the paper
                repo.conn.execute("INSERT INTO llm_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (paper.paper_id, llm.model, None, 0, 0, 0.0, 0, 0.0, None, "no_relevant_chunks", 0,
                                   rstore.now()))
            if call is not None:
                repo.conn.execute("INSERT INTO llm_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (paper.paper_id, call.model, call.prompt_version, call.input_tokens, call.output_tokens,
                                   call.cost_usd, int(call.cached), call.seconds, call.stop_reason, call.error,
                                   sum(1 for d in dropped if d.endswith("unanchored")), rstore.now()))
                st.finding_codes["llm_values_dropped_unanchored"] += sum(1 for d in dropped if d.endswith("unanchored"))
        try:
            res, findings, scored = process_document(paper, doc, run_id, llm_result=llm_res)
            rstore.save_document_status(repo.conn, paper.paper_id, "acquired", fmt=doc.source_format, url=doc.source_url,
                                        license=doc.license, sha=doc.sha256, n_tables=len(doc.tables), n_used=len(res.tables_used))
            rstore.replace_paper_extraction(repo.conn, paper.paper_id, res.samples, res.molecular, res.measurements,
                                            findings, scored, EXTRACTOR_VERSION)
            repo.conn.commit()
        except Exception as e:  # one malformed paper must never abort the run
            repo.conn.rollback()
            log.exception("extraction failed for %s", paper.doi)
            rstore.save_document_status(repo.conn, paper.paper_id, "extraction_failed", note=f"{type(e).__name__}: {e}"[:300])
            repo.conn.commit()
            st.status["extraction_failed"] += 1
            continue
        st.status["acquired"] += 1
        st.samples += len(res.samples)
        st.molecular += len(res.molecular)
        st.measurements += len(res.measurements)
        st.records += len(scored)
        for *_, route in scored:
            st.routes[route] += 1
        for f in findings:
            if f.severity is not Severity.INFO:
                st.finding_codes[f"{f.severity.value}:{f.code}"] += 1
        log.info("%s: tables=%d used=%d records=%d", paper.doi, len(doc.tables), len(res.tables_used), len(scored))
    return st
