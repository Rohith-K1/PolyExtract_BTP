"""Deduplicate SourceRecords into one PaperMetadataSchema per real paper.

Matching order (most to least reliable):
  1. Normalized DOI equality.
  2. Exact normalized-title match with |year difference| <= 1 (online vs print year).
  3. Fuzzy title match (SequenceMatcher ratio >= threshold) with |year difference| <= 1, only
     for records that have no DOI, so two different DOIs are never merged.

Merging keeps every source's identifiers, and `field_sources` records which source supplied
each merged field.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from polyextract.discovery.models import DiscoveryCandidate, SourceRecord
from polyextract.schemas.identifiers import make_paper_id, normalize_title
from polyextract.schemas.paper import FullTextStatus, PaperMetadataSchema

# Bibliographic fields: Crossref is the DOI registration agency, so it is preferred for these.
BIBLIO_PREFERENCE = ("crossref", "openalex", "websearch")


@dataclass
class _Group:
    records: list[SourceRecord] = field(default_factory=list)
    doi: str | None = None
    norm_title: str = ""
    year: int | None = None
    method: str = "unique"

    def add(self, r: SourceRecord, method: str) -> None:
        self.records.append(r)
        if r.doi and not self.doi:
            self.doi = r.doi
        if not self.norm_title and r.title:
            self.norm_title = normalize_title(r.title)
        if self.year is None and r.publication_year:
            self.year = r.publication_year
        if method != "unique" and self.method == "unique":
            self.method = method


@dataclass
class DedupStats:
    n_input: int = 0
    n_unique: int = 0
    merged_by: Counter = field(default_factory=Counter)
    source_overlap: Counter = field(default_factory=Counter)  # e.g. 'crossref+openalex' -> n

    def as_dict(self) -> dict:
        return {
            "n_input": self.n_input,
            "n_unique": self.n_unique,
            "merged_by": dict(self.merged_by),
            "source_overlap": dict(sorted(self.source_overlap.items())),
        }


def _years_compatible(a: int | None, b: int | None) -> bool:
    return a is None or b is None or abs(a - b) <= 1


def _pick(records: list[SourceRecord], attr: str, prefer_longest: bool = False) -> tuple[object, str | None]:
    ranked = sorted(
        records,
        key=lambda r: BIBLIO_PREFERENCE.index(r.source) if r.source in BIBLIO_PREFERENCE else 99,
    )
    vals = [(getattr(r, attr), r.source) for r in ranked if getattr(r, attr)]
    if not vals:
        return None, None
    if prefer_longest:
        return max(vals, key=lambda v: len(v[0]))
    return vals[0]


def merge_group(records: list[SourceRecord], method: str) -> DiscoveryCandidate:
    field_sources: dict[str, str] = {}

    def take(attr: str, prefer_longest: bool = False):
        v, src = _pick(records, attr, prefer_longest)
        if src:
            field_sources[attr] = src
        return v

    doi = take("doi")
    title = take("title")
    year = take("publication_year")
    # Longest abstract: Crossref abstracts are often truncated or missing; OpenAlex is often fuller.
    abstract = take("abstract", prefer_longest=True)
    keywords: list[str] = []
    for r in records:
        for k in r.keywords:
            if k not in keywords:
                keywords.append(k)
    source_ids: dict[str, str] = {}
    for r in records:
        if r.source_id and r.source not in source_ids:
            source_ids[r.source] = r.source_id
    oa_url = take("oa_url")
    paper = PaperMetadataSchema(
        paper_id=make_paper_id(doi, title, year),
        doi=doi,
        title=title or "(untitled)",
        authors=take("authors") or [],
        journal=take("journal"),
        publication_year=year,
        abstract=abstract,
        keywords=keywords,
        source_url=take("url"),
        document_type=take("document_type"),
        retrieval_sources=sorted({r.source for r in records}),
        source_ids=source_ids,
        field_sources=field_sources,
        oa_url=oa_url,
        license=take("license"),
        fulltext_status=FullTextStatus.OA_LOCATED if oa_url else FullTextStatus.NOT_ATTEMPTED,
    )
    queries = list(dict.fromkeys(r.query for r in records))
    snippets = [r.snippet for r in records if r.snippet]
    return DiscoveryCandidate(
        paper=paper, queries=queries, snippets=snippets, n_source_records=len(records), dedup_method=method
    )


def deduplicate(records: list[SourceRecord], fuzzy_threshold: float = 0.93) -> tuple[list[DiscoveryCandidate], DedupStats]:
    stats = DedupStats(n_input=len(records))
    groups: list[_Group] = []
    by_doi: dict[str, _Group] = {}
    by_title: dict[str, list[_Group]] = {}

    def index(g: _Group) -> None:
        if g.doi:
            by_doi[g.doi] = g
        if g.norm_title:
            lst = by_title.setdefault(g.norm_title, [])
            if g not in lst:
                lst.append(g)

    # DOI-bearing records first so that title-only records attach to an authoritative group.
    for r in sorted(records, key=lambda r: r.doi is None):
        nt = normalize_title(r.title)
        target, method = None, "unique"
        if r.doi and r.doi in by_doi:
            target, method = by_doi[r.doi], "doi"
        elif nt:
            for g in by_title.get(nt, []):
                if _years_compatible(g.year, r.publication_year) and not (r.doi and g.doi and r.doi != g.doi):
                    target, method = g, "title_exact"
                    break
            if target is None and not r.doi and len(nt) >= 20:
                best, best_ratio = None, 0.0
                for g in groups:
                    if not g.norm_title or not _years_compatible(g.year, r.publication_year):
                        continue
                    ratio = SequenceMatcher(None, nt, g.norm_title).ratio()
                    if ratio > best_ratio:
                        best, best_ratio = g, ratio
                if best is not None and best_ratio >= fuzzy_threshold:
                    target, method = best, "title_fuzzy"
        if target is None:
            target = _Group()
            groups.append(target)
        else:
            stats.merged_by[method] += 1
        target.add(r, method)
        index(target)

    candidates = [merge_group(g.records, g.method) for g in groups]
    stats.n_unique = len(candidates)
    for c in candidates:
        stats.source_overlap["+".join(c.paper.retrieval_sources)] += 1
    return candidates, stats
