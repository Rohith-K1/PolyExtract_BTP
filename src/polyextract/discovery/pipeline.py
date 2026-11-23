"""Stage-1 orchestration: query sources -> enrich -> dedup -> filter -> persist -> report.

One failing source never stops the run. Each failure is recorded in the report, and the other
sources continue.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polyextract.discovery.base import SourceAdapter
from polyextract.discovery.crossref import CrossrefAdapter
from polyextract.discovery.dedup import deduplicate
from polyextract.discovery.filters import FilterEngine
from polyextract.discovery.models import Decision, DiscoveryCandidate, SourceRecord
from polyextract.exceptions import SourceError
from polyextract.logging_setup import get_logger
from polyextract.storage.sqlite import Repository

log = get_logger("discovery")


@dataclass
class DiscoveryConfig:
    queries: list[str]
    year_from: int | None = None
    year_to: int | None = None
    max_results_per_query: int = 100
    per_source_max: dict[str, int] = field(default_factory=dict)
    fuzzy_title_threshold: float = 0.93
    enrich_web_dois_via_crossref: bool = True

    def max_for(self, source: str) -> int:
        return self.per_source_max.get(source, self.max_results_per_query)


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


def enrich_web_records(records: list[SourceRecord], crossref: CrossrefAdapter | None, errors: list[dict]) -> list[SourceRecord]:
    """For web hits carrying a DOI not already covered by a scholarly source, add a Crossref record."""
    if crossref is None:
        return []
    scholarly_dois = {r.doi for r in records if r.source != "websearch" and r.doi}
    todo = sorted({r.doi for r in records if r.source == "websearch" and r.doi and r.doi not in scholarly_dois})
    added = []
    for doi in todo:
        try:
            rec = crossref.lookup_doi(doi)
        except SourceError as e:
            errors.append({"source": "crossref", "stage": "doi_lookup", "doi": doi, "error": str(e)})
            continue
        if rec is not None:
            added.append(rec)
    log.info("enriched %d/%d web-only DOIs via Crossref", len(added), len(todo))
    return added


def build_report(
    run_id: str,
    started: datetime,
    cfg: DiscoveryConfig,
    records: list[SourceRecord],
    per_source: dict[str, dict[str, Any]],
    errors: list[dict],
    dedup_stats: dict,
    candidates: list[DiscoveryCandidate],
    filter_version: str,
) -> dict[str, Any]:
    decisions = Counter(c.filter.decision.value for c in candidates if c.filter)
    reject_reasons = Counter(
        c.filter.reasons[0] for c in candidates if c.filter and c.filter.decision is Decision.REJECT and c.filter.reasons
    )
    group_hits = Counter(g for c in candidates if c.filter for g in c.filter.groups_matched)
    years = Counter(c.paper.publication_year for c in candidates if c.paper.publication_year)
    by_source_decision: dict[str, Counter] = {}
    for c in candidates:
        for s in c.paper.retrieval_sources:
            by_source_decision.setdefault(s, Counter())[c.filter.decision.value] += 1
    return {
        "run_id": run_id,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "filter_version": filter_version,
        "config": {
            "n_queries": len(cfg.queries),
            "year_from": cfg.year_from,
            "year_to": cfg.year_to,
            "max_results_per_query": cfg.max_results_per_query,
            "per_source_max": cfg.per_source_max,
        },
        "sources": per_source,
        "errors": errors,
        "raw_records": len(records),
        "raw_records_by_source": dict(Counter(r.source for r in records)),
        "raw_with_doi": sum(1 for r in records if r.doi),
        "dedup": dedup_stats,
        "unique_papers": len(candidates),
        "papers_with_abstract": sum(1 for c in candidates if c.paper.abstract),
        "papers_with_oa_url": sum(1 for c in candidates if c.paper.oa_url),
        "filter_decisions": dict(decisions),
        "filter_decisions_by_source": {s: dict(v) for s, v in sorted(by_source_decision.items())},
        "top_reject_reasons": reject_reasons.most_common(10),
        "group_hit_counts": dict(group_hits.most_common()),
        "year_histogram_by_decade": dict(
            sorted(Counter(f"{(y // 10) * 10}s" for y in years.elements()).items())
        ),
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [f"# Discovery run {report['run_id']}", "", f"Filter version: `{report['filter_version']}`", ""]
    lines += ["## Sources", "", "| source | queries ok | queries failed | records | http requests | cache hits | rate-limited |", "|---|---|---|---|---|---|---|"]
    for s, st in report["sources"].items():
        h = st.get("http", {})
        lines.append(
            f"| {s} | {st['queries_ok']} | {st['queries_failed']} | {st['records']} | "
            f"{h.get('requests', '-')} | {h.get('cache_hits', '-')} | {h.get('rate_limited', '-')} |"
        )
    d = report["dedup"]
    fd = report["filter_decisions"]
    lines += [
        "",
        "## Funnel",
        "",
        f"- Raw records: **{report['raw_records']}** ({report['raw_with_doi']} with DOI)",
        f"- Unique papers after dedup: **{report['unique_papers']}** (merged: {d['merged_by']})",
        f"- With abstract: {report['papers_with_abstract']} · with OA location: {report['papers_with_oa_url']}",
        f"- Filter: ACCEPT **{fd.get('accept', 0)}** · REVIEW **{fd.get('review', 0)}** · REJECT **{fd.get('reject', 0)}**",
        "",
        "## Source overlap (unique papers by combination of sources)",
        "",
    ]
    lines += [f"- {k}: {v}" for k, v in d["source_overlap"].items()]
    lines += ["", "## Top reject reasons", ""]
    lines += [f"- ({n}) {r}" for r, n in report["top_reject_reasons"]]
    if report["errors"]:
        lines += ["", f"## Errors ({len(report['errors'])})", ""]
        lines += [f"- {e['source']} / {e.get('query') or e.get('doi')}: {e['error'][:160]}" for e in report["errors"][:20]]
    return "\n".join(lines) + "\n"


def run_discovery(
    cfg: DiscoveryConfig,
    adapters: list[SourceAdapter],
    filter_engine: FilterEngine,
    repo: Repository | None,
    run_dir: Path,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    run_id = run_id or new_run_id()
    started = datetime.now(timezone.utc)
    run_dir.mkdir(parents=True, exist_ok=True)
    if repo is not None:
        repo.start_run(run_id, {"queries": cfg.queries, "sources": [a.name for a in adapters], **cfg.__dict__})

    records: list[SourceRecord] = []
    errors: list[dict] = []
    per_source: dict[str, dict[str, Any]] = {a.name: {"queries_ok": 0, "queries_failed": 0, "records": 0} for a in adapters}
    for query in cfg.queries:
        for a in adapters:
            try:
                recs = a.search(query, max_results=cfg.max_for(a.name), year_from=cfg.year_from, year_to=cfg.year_to)
            except SourceError as e:
                log.error("source failed: %s", e)
                per_source[a.name]["queries_failed"] += 1
                errors.append({"source": a.name, "query": query, "error": str(e)})
                continue
            per_source[a.name]["queries_ok"] += 1
            per_source[a.name]["records"] += len(recs)
            records.extend(recs)

    if cfg.enrich_web_dois_via_crossref:
        crossref = next((a for a in adapters if isinstance(a, CrossrefAdapter)), None)
        enriched = enrich_web_records(records, crossref, errors)
        per_source.setdefault("crossref", {"queries_ok": 0, "queries_failed": 0, "records": 0})["doi_lookups_added"] = len(enriched)
        records.extend(enriched)

    for a in adapters:
        per_source[a.name]["http"] = a.stats()

    with open(run_dir / "raw_records.jsonl", "w", encoding="utf-8") as f:
        for r in records:
            f.write(r.model_dump_json() + "\n")

    candidates, dstats = deduplicate(records, fuzzy_threshold=cfg.fuzzy_title_threshold)
    for c in candidates:
        c.filter = filter_engine.evaluate(c)
    candidates.sort(key=lambda c: (-(c.filter.score if c.filter else 0), c.paper.paper_id))

    with open(run_dir / "candidates.jsonl", "w", encoding="utf-8") as f:
        for c in candidates:
            f.write(c.model_dump_json() + "\n")
    if repo is not None:
        repo.save_candidates(run_id, candidates)

    report = build_report(run_id, started, cfg, records, per_source, errors, dstats.as_dict(), candidates, filter_engine.version)
    (run_dir / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (run_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    if repo is not None:
        repo.finish_run(run_id, report)
    log.info("discovery %s: %d raw -> %d unique -> %s", run_id, len(records), len(candidates), report["filter_decisions"])
    return report
