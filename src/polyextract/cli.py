"""Command-line entry point: `polyextract discover ...`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from polyextract.config import PROJECT_ROOT, get_settings
from polyextract.discovery.base import SourceAdapter
from polyextract.discovery.crossref import CrossrefAdapter
from polyextract.discovery.filters import FilterEngine
from polyextract.discovery.http import HttpClient
from polyextract.discovery.openalex import OpenAlexAdapter, openalex_rate_limit_check
from polyextract.discovery.pipeline import DiscoveryConfig, new_run_id, run_discovery
from polyextract.discovery.websearch import DDGSProvider, WebSearchAdapter
from polyextract.exceptions import SourceUnavailableError
from polyextract.logging_setup import get_logger, setup_logging
from polyextract.storage.sqlite import Repository


def build_adapters(src_cfg: dict, enabled: list[str], cache_dir: Path | None) -> list[SourceAdapter]:
    s = get_settings()
    ua = f"polyextract/0.1 (IIT Madras B.Tech research; mailto:{s.contact_email})" if s.contact_email else "polyextract/0.1"
    log = get_logger("cli")
    adapters: list[SourceAdapter] = []
    if "openalex" in enabled:
        c = src_cfg.get("openalex", {})
        if not s.openalex_api_key:
            log.warning("OPENALEX_API_KEY not set: anonymous OpenAlex search is heavily rate-limited")
        http = HttpClient("openalex", min_interval_s=c.get("min_interval_s", 0.2), user_agent=ua,
                          cache_dir=cache_dir, body_rate_limit_check=openalex_rate_limit_check)
        adapters.append(OpenAlexAdapter(http, email=s.openalex_email, api_key=s.openalex_api_key,
                                        per_page=c.get("per_page", 100), document_types=c.get("document_types"),
                                        extra_filters=c.get("extra_filters")))
    if "crossref" in enabled:
        c = src_cfg.get("crossref", {})
        http = HttpClient("crossref", min_interval_s=c.get("min_interval_s", 0.5), user_agent=ua, cache_dir=cache_dir)
        adapters.append(CrossrefAdapter(http, mailto=s.contact_email, rows=c.get("rows", 100),
                                        document_types=c.get("document_types"), extra_filters=c.get("extra_filters")))
    if "websearch" in enabled:
        c = src_cfg.get("websearch", {})
        if s.web_search_provider.lower() == "ddgs":
            try:
                adapters.append(WebSearchAdapter(DDGSProvider(), min_interval_s=c.get("min_interval_s", 3.0),
                                                 query_suffix=c.get("query_suffix", "")))
            except SourceUnavailableError as e:
                log.error("web search disabled: %s", e)
        else:
            log.warning("WEB_SEARCH_PROVIDER=%s not supported; web search disabled", s.web_search_provider)
    return adapters


def cmd_discover(args: argparse.Namespace) -> int:
    s = get_settings()
    data_dir = s.resolved_data_dir()
    run_id = new_run_id()
    run_dir = data_dir / "runs" / "discovery" / run_id
    setup_logging(s.log_level, run_dir / "log.jsonl")
    log = get_logger("cli")
    log.info("settings: %s", s.redacted())

    with open(args.config, encoding="utf-8") as f:
        dcfg = yaml.safe_load(f)
    queries = dcfg["queries"][: args.limit_queries] if args.limit_queries else dcfg["queries"]
    enabled = [x.strip() for x in args.sources.split(",")] if args.sources else [
        k for k, v in dcfg["sources"].items() if v.get("enabled", True)
    ]
    max_per_query = args.max_per_query or dcfg.get("max_results_per_query", 100)
    per_source_max = {}
    ws = dcfg["sources"].get("websearch", {})
    if "max_results" in ws:
        per_source_max["websearch"] = min(ws["max_results"], max_per_query)

    cfg = DiscoveryConfig(
        queries=queries, year_from=dcfg.get("year_from"), year_to=dcfg.get("year_to"),
        max_results_per_query=max_per_query, per_source_max=per_source_max,
        fuzzy_title_threshold=dcfg.get("fuzzy_title_threshold", 0.93),
        enrich_web_dois_via_crossref=dcfg.get("enrich_web_dois_via_crossref", True),
    )
    cache_dir = None if args.no_cache else data_dir / "cache" / "http"
    adapters = build_adapters(dcfg["sources"], enabled, cache_dir)
    if not adapters:
        log.error("no usable sources")
        return 2
    engine = FilterEngine.from_yaml(Path(args.filters))
    repo = Repository(data_dir / "db" / "polyextract.sqlite")
    try:
        report = run_discovery(cfg, adapters, engine, repo, run_dir, run_id=run_id)
    finally:
        for a in adapters:
            a.close()
        repo.close()
    print((run_dir / "report.md").read_text(encoding="utf-8"))
    print(f"Run artifacts: {run_dir}")
    return 0 if report["unique_papers"] or not report["errors"] else 1


def cmd_extract(args: argparse.Namespace) -> int:
    import json

    from polyextract.acquisition.fulltext import FullTextLocator
    from polyextract.extraction.pipeline import run_extraction
    from polyextract.schemas import PaperMetadataSchema

    s = get_settings()
    data_dir = s.resolved_data_dir()
    run_id = new_run_id()
    setup_logging(s.log_level, data_dir / "runs" / "extract" / run_id / "log.jsonl")
    from polyextract.storage.records import init_schema

    repo = Repository(data_dir / "db" / "polyextract.sqlite")
    init_schema(repo.conn)
    decisions = ["accept", "review"] if args.include_review else ["accept"]
    from polyextract.extraction.pipeline import LLM_CALLS_SQL

    repo.conn.execute(LLM_CALLS_SQL)
    if args.llm_todo:
        # incremental: papers never attempted, plus acquired OA papers the LLM has not read yet
        # (so a capped nightly run always makes progress instead of redoing the same top-N)
        todo = """AND (p.paper_id NOT IN (SELECT paper_id FROM documents)
                   OR (p.paper_id IN (SELECT paper_id FROM documents WHERE status='acquired')
                       AND p.paper_id NOT IN (SELECT paper_id FROM llm_calls
                                              WHERE error IS NULL OR error = 'no_relevant_chunks')
                       AND p.paper_id NOT IN (SELECT paper_id FROM llm_calls GROUP BY paper_id HAVING COUNT(*) >= 2)))"""
        args_sql: tuple = tuple(decisions)
    else:
        todo = "AND p.paper_id NOT IN (SELECT paper_id FROM documents WHERE ? = 0)"
        args_sql = (*decisions, int(args.redo))
    rows = repo.conn.execute(
        f"""SELECT p.payload FROM papers p JOIN discovery_candidates c ON c.paper_id = p.paper_id
            WHERE c.decision IN ({','.join('?' * len(decisions))}) AND p.doi IS NOT NULL {todo}
            GROUP BY p.paper_id ORDER BY MAX(c.score) DESC""",
        args_sql,
    ).fetchall()
    papers = [PaperMetadataSchema.model_validate_json(r["payload"]) for r in rows][: args.limit]
    ua = f"polyextract/0.1 (mailto:{s.contact_email})" if s.contact_email else "polyextract/0.1"
    http = HttpClient("europepmc", min_interval_s=0.2, user_agent=ua, cache_dir=data_dir / "cache" / "http")
    locator = FullTextLocator(http, email=s.contact_email)
    llm = None
    if args.llm:
        from polyextract.extraction.llm import LLMClient

        llm = LLMClient(s.llm_provider, s.llm_model, s.llm_api_key, data_dir / "cache" / "llm")
        print(f"LLM second reader: provider={llm.provider} model={llm.model}")
    print(f"Extracting {len(papers)} papers (decisions={decisions})")
    try:
        st = run_extraction(repo, locator, papers, data_dir / "raw" / "fulltext" / "epmc", run_id, llm=llm)
    except Exception as e:
        if type(e).__name__ == "LLMAccountError":
            print(f"STOPPED: {e}")
            _apply_policy(repo)  # papers finished before the stop still get the policy
            repo.close()
            return 3
        raise
    if llm is not None:
        st_d = st.as_dict()
        st_d["llm_cost_usd"] = round(sum(c.cost_usd for c in llm.calls), 4)
        st_d["llm_tokens"] = {"in": sum(c.input_tokens for c in llm.calls), "out": sum(c.output_tokens for c in llm.calls)}
        print(f"LLM: {len(llm.calls)} calls, ${st_d['llm_cost_usd']}, tokens {st_d['llm_tokens']}")
    out = data_dir / "runs" / "extract" / run_id / "stats.json"
    out.write_text(json.dumps(st.as_dict(), indent=2), encoding="utf-8")
    print(json.dumps(st.as_dict(), indent=2))
    _apply_policy(repo)
    repo.close()
    return 0


def cmd_reprocess(args: argparse.Namespace) -> int:
    import json

    from polyextract.extraction.pipeline import reprocess_cached

    s = get_settings()
    setup_logging("WARNING")
    repo = Repository(s.resolved_data_dir() / "db" / "polyextract.sqlite")
    llm = None
    if not args.no_llm_cache:
        from polyextract.extraction.llm import LLMClient

        llm = LLMClient(s.llm_provider, s.llm_model, s.llm_api_key, s.resolved_data_dir() / "cache" / "llm",
                        cache_only=True)
    st = reprocess_cached(repo, s.resolved_data_dir() / "raw" / "fulltext" / "epmc", llm=llm)
    d = st.as_dict()
    d["finding_codes"] = dict(sorted(d["finding_codes"].items(), key=lambda kv: -kv[1])[:15])
    print(json.dumps(d, indent=2))
    _apply_policy(repo)
    repo.close()
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    import json

    from polyextract.review.dedup import apply_dedup
    from polyextract.review.policy import apply_machine_verification, audit_status, draw_audit
    from polyextract.storage.records import init_schema

    s = get_settings()
    repo = Repository(s.resolved_data_dir() / "db" / "polyextract.sqlite")
    init_schema(repo.conn)
    dd = apply_dedup(repo.conn)
    out = apply_machine_verification(repo.conn)
    out["dedup"] = dd
    if args.draw_audit:
        out["audit_newly_drawn"] = draw_audit(repo.conn, args.draw_audit)
        out.update(audit_status(repo.conn))
    print(json.dumps(out, indent=2))
    repo.close()
    return 0


def _apply_policy(repo: Repository) -> None:
    from polyextract.review.dedup import apply_dedup
    from polyextract.review.policy import apply_machine_verification

    d = apply_dedup(repo.conn)  # first, so duplicates and computed values are never promoted or reviewed
    print(f"dedup: {d['duplicates_marked']} duplicate(s), {d['computed_excluded']} computed value(s) taken out of "
          f"review; {d['audit_replaced']} audit record(s) replaced")
    out = apply_machine_verification(repo.conn)
    print(f"machine-verification policy: promoted {out['promoted']} record(s); "
          f"tier size {out['machine_verified_records']}, audit done {out['audit_done']}, released={out['released']}")


def cmd_export(args: argparse.Namespace) -> int:
    import json

    from polyextract.dataset.builder import export
    from polyextract.storage.records import init_schema

    s = get_settings()
    repo = Repository(s.resolved_data_dir() / "db" / "polyextract.sqlite")
    init_schema(repo.conn)
    out = export(repo.conn, s.resolved_data_dir() / "processed", args.version)
    print(json.dumps(out, indent=2, default=str))
    repo.close()
    return 0


def cmd_ml(args: argparse.Namespace) -> int:
    import json

    import pandas as pd

    from polyextract.dataset.builder import record_rows
    from polyextract.ml.experiments import run
    from polyextract.storage.records import init_schema

    s = get_settings()
    repo = Repository(s.resolved_data_dir() / "db" / "polyextract.sqlite")
    init_schema(repo.conn)
    df = pd.DataFrame(record_rows(repo.conn))
    repo.close()
    if df.empty:
        print("No verified records yet — ML experiments need human-verified data. Nothing run.")
        return 1
    rep = run(df, args.version, s.resolved_data_dir() / "processed" / f"ml_results_{args.version}.json")
    print(json.dumps(rep.properties, indent=2, default=str)[:6000])
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="polyextract")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover", help="Stage 1: three-source literature discovery + metadata filtering")
    d.add_argument("--config", default=str(PROJECT_ROOT / "config" / "discovery.yaml"))
    d.add_argument("--filters", default=str(PROJECT_ROOT / "config" / "filters.yaml"))
    d.add_argument("--sources", help="comma list, e.g. openalex,crossref,websearch")
    d.add_argument("--max-per-query", type=int)
    d.add_argument("--limit-queries", type=int, help="use only the first N queries (smoke tests)")
    d.add_argument("--no-cache", action="store_true")
    d.set_defaults(func=cmd_discover)
    e = sub.add_parser("extract", help="Stage 2: OA full text -> table extraction -> validation -> confidence")
    e.add_argument("--limit", type=int, default=50)
    e.add_argument("--include-review", action="store_true", help="also process Stage-1 REVIEW papers")
    e.add_argument("--redo", action="store_true", help="re-process papers already attempted")
    e.add_argument("--llm", action="store_true", help="also run the LLM extractor (LLM_PROVIDER/LLM_API_KEY/LLM_MODEL)")
    e.add_argument("--llm-todo", action="store_true",
                   help="only papers not yet attempted or not yet read by the LLM (use with --llm for capped nightly runs)")
    e.set_defaults(func=cmd_extract)
    rp = sub.add_parser("reprocess", help="re-run extraction/validation on cached full texts (no network)")
    rp.add_argument("--no-llm-cache", action="store_true",
                    help="ignore cached LLM answers (default: reuse them for agreement; never makes new LLM calls)")
    rp.set_defaults(func=cmd_reprocess)
    v = sub.add_parser("verify", help="apply the dual-agreement machine-verification policy; optionally draw an audit sample")
    v.add_argument("--draw-audit", type=int, default=0, metavar="N",
                   help="add N random machine-verified records to the human audit sample (recommended total: 300)")
    v.set_defaults(func=cmd_verify)
    x = sub.add_parser("export", help="export VERIFIED records (json/jsonl/csv/parquet) + quality stats")
    x.add_argument("--version", default="v0")
    x.set_defaults(func=cmd_export)
    m = sub.add_parser("ml", help="Experiment A vs B on verified records (grouped CV by paper)")
    m.add_argument("--version", default="v0")
    m.set_defaults(func=cmd_ml)
    args = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
