"""OpenAlex adapter (https://api.openalex.org/works). Cursor pagination, polite pool, optional API key."""

from __future__ import annotations

from typing import Any

from polyextract.discovery.base import SourceAdapter, strip_markup
from polyextract.discovery.http import HttpClient
from polyextract.discovery.models import SourceRecord
from polyextract.exceptions import SourceError
from polyextract.logging_setup import get_logger
from polyextract.schemas.identifiers import normalize_doi
from polyextract.schemas.paper import Author

log = get_logger("openalex")

BASE_URL = "https://api.openalex.org/works"
SELECT = ",".join(
    [
        "id", "doi", "title", "display_name", "publication_year", "type", "authorships",
        "primary_location", "abstract_inverted_index", "keywords", "open_access", "best_oa_location", "language",
        "ids",
    ]
)


def openalex_rate_limit_check(body: Any) -> float | None:
    if isinstance(body, dict) and "error" in body and "rate" in str(body.get("error", "")).lower():
        try:
            return float(body.get("retryAfter", 30))
        except (TypeError, ValueError):
            return 30.0
    return None


def reconstruct_abstract(inverted: dict[str, list[int]] | None) -> str | None:
    """OpenAlex stores abstracts as {word: [positions]} for licensing reasons; rebuild the text."""
    if not inverted:
        return None
    positions: dict[int, str] = {}
    for word, idxs in inverted.items():
        for i in idxs:
            positions[i] = word
    if not positions:
        return None
    return " ".join(positions[i] for i in sorted(positions))


def map_work(work: dict[str, Any], query: str, rank: int) -> SourceRecord:
    authors = []
    for a in work.get("authorships") or []:
        au = a.get("author") or {}
        name = au.get("display_name") or a.get("raw_author_name")
        if name:
            authors.append(Author(name=name, orcid=au.get("orcid")))
    primary = work.get("primary_location") or {}
    src = primary.get("source") or {}
    oa = work.get("open_access") or {}
    best = work.get("best_oa_location") or {}
    return SourceRecord(
        source="openalex",
        source_id=(work.get("id") or "").rsplit("/", 1)[-1] or None,
        query=query,
        rank=rank,
        doi=normalize_doi(work.get("doi")),
        title=strip_markup(work.get("title") or work.get("display_name")),
        authors=authors,
        journal=src.get("display_name"),
        publication_year=work.get("publication_year"),
        abstract=strip_markup(reconstruct_abstract(work.get("abstract_inverted_index"))),
        keywords=[k.get("display_name") for k in (work.get("keywords") or []) if k.get("display_name")],
        url=primary.get("landing_page_url"),
        document_type=work.get("type"),
        oa_url=oa.get("oa_url") or best.get("pdf_url") or best.get("landing_page_url"),
        license=best.get("license") or primary.get("license"),
        extra={
            "is_oa": oa.get("is_oa"),
            "oa_status": oa.get("oa_status"),
            "best_oa_pdf_url": best.get("pdf_url"),
            "language": work.get("language"),
            "pmcid": ((work.get("ids") or {}).get("pmcid") or "").rstrip("/").rsplit("/", 1)[-1] or None,
        },
    )


class OpenAlexAdapter(SourceAdapter):
    name = "openalex"

    def __init__(
        self,
        http: HttpClient,
        *,
        email: str | None = None,
        api_key: str | None = None,
        per_page: int = 100,
        document_types: list[str] | None = None,
        extra_filters: list[str] | None = None,
    ):
        self.http = http
        self.email = email
        self.api_key = api_key
        self.per_page = max(1, min(per_page, 200))
        self.document_types = document_types or ["article"]
        # e.g. ['has_pmcid:true'] restricts discovery to papers with legal PMC full text
        self.extra_filters = extra_filters or []

    def _filter(self, year_from: int | None, year_to: int | None) -> str:
        parts = list(self.extra_filters)
        if year_from or year_to:
            parts.append(f"publication_year:{year_from or ''}-{year_to or ''}")
        if self.document_types:
            parts.append("type:" + "|".join(self.document_types))
        return ",".join(parts)

    def search(self, query: str, *, max_results: int, year_from: int | None = None, year_to: int | None = None) -> list[SourceRecord]:
        params: dict[str, Any] = {
            "search": query,
            "per-page": min(self.per_page, max_results),
            "cursor": "*",
            "select": SELECT,
        }
        f = self._filter(year_from, year_to)
        if f:
            params["filter"] = f
        if self.email:
            params["mailto"] = self.email
        if self.api_key:
            params["api_key"] = self.api_key

        records: list[SourceRecord] = []
        while len(records) < max_results:
            body = self.http.get_json(BASE_URL, params)
            if not isinstance(body, dict) or "results" not in body:
                raise SourceError(self.name, f"unexpected payload keys: {list(body)[:5] if isinstance(body, dict) else type(body)}")
            results = body["results"] or []
            for work in results:
                if len(records) >= max_results:
                    break
                records.append(map_work(work, query, rank=len(records) + 1))
            nxt = (body.get("meta") or {}).get("next_cursor")
            if not results or not nxt:
                break
            params = {**params, "cursor": nxt}
        log.info("openalex %r -> %d records", query, len(records))
        return records

    def stats(self) -> dict:
        return self.http.stats.as_dict()

    def close(self) -> None:
        self.http.close()
