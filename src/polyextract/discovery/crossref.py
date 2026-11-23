"""Crossref adapter (https://api.crossref.org/works). Cursor pagination, polite pool, DOI lookup."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from polyextract.discovery.base import SourceAdapter, strip_markup
from polyextract.discovery.http import HttpClient
from polyextract.discovery.models import SourceRecord
from polyextract.exceptions import SourceError
from polyextract.logging_setup import get_logger
from polyextract.schemas.identifiers import normalize_doi
from polyextract.schemas.paper import Author

log = get_logger("crossref")

BASE_URL = "https://api.crossref.org/works"
SELECT = "DOI,title,author,container-title,issued,published,abstract,subject,URL,type,link,license"


def _year(item: dict[str, Any]) -> int | None:
    for key in ("issued", "published", "published-print", "published-online"):
        parts = (item.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            try:
                return int(parts[0][0])
            except (TypeError, ValueError):
                continue
    return None


def map_item(item: dict[str, Any], query: str, rank: int) -> SourceRecord:
    authors = []
    for a in item.get("author") or []:
        given, family = a.get("given"), a.get("family")
        name = " ".join(p for p in (given, family) if p) or a.get("name")
        if name:
            authors.append(Author(name=name, given=given, family=family, orcid=a.get("ORCID")))
    titles = item.get("title") or []
    journals = item.get("container-title") or []
    licenses = item.get("license") or []
    links = item.get("link") or []
    return SourceRecord(
        source="crossref",
        source_id=item.get("DOI"),
        query=query,
        rank=rank,
        doi=normalize_doi(item.get("DOI")),
        title=strip_markup(titles[0]) if titles else None,
        authors=authors,
        journal=journals[0] if journals else None,
        publication_year=_year(item),
        abstract=strip_markup(item.get("abstract")),
        keywords=list(item.get("subject") or []),
        url=item.get("URL"),
        document_type=item.get("type"),
        license=licenses[0].get("URL") if licenses else None,
        extra={
            # Publisher-declared full-text links (used in Phase 3, subject to licence checks).
            "fulltext_links": [
                {"url": l.get("URL"), "content_type": l.get("content-type"), "intended": l.get("intended-application")}
                for l in links
            ],
            "licenses": [{"url": l.get("URL"), "content_version": l.get("content-version")} for l in licenses],
        },
    )


class CrossrefAdapter(SourceAdapter):
    name = "crossref"

    def __init__(
        self,
        http: HttpClient,
        *,
        mailto: str | None = None,
        rows: int = 100,
        document_types: list[str] | None = None,
        extra_filters: list[str] | None = None,
    ):
        self.http = http
        self.mailto = mailto
        self.rows = max(1, min(rows, 1000))
        self.document_types = document_types or ["journal-article"]
        # e.g. ['issn:2073-4360', ...]: repeated same-name Crossref filters are ORed
        self.extra_filters = extra_filters or []

    def _filter(self, year_from: int | None, year_to: int | None) -> str:
        parts = list(self.extra_filters)
        if year_from:
            parts.append(f"from-pub-date:{year_from}")
        if year_to:
            parts.append(f"until-pub-date:{year_to}")
        parts.extend(f"type:{t}" for t in self.document_types)
        return ",".join(parts)

    def search(self, query: str, *, max_results: int, year_from: int | None = None, year_to: int | None = None) -> list[SourceRecord]:
        params: dict[str, Any] = {
            "query.bibliographic": query,
            "rows": min(self.rows, max_results),
            "cursor": "*",
            # Without an explicit sort, cursor-paginated Crossref results are NOT relevance-ranked
            # (live check 2026-11-05: 1/25 on-topic without it, 25/25 with it).
            "sort": "relevance",
            "order": "desc",
            "select": SELECT,
        }
        f = self._filter(year_from, year_to)
        if f:
            params["filter"] = f
        if self.mailto:
            params["mailto"] = self.mailto

        records: list[SourceRecord] = []
        while len(records) < max_results:
            body = self.http.get_json(BASE_URL, params)
            msg = body.get("message") if isinstance(body, dict) else None
            if not isinstance(msg, dict) or "items" not in msg:
                raise SourceError(self.name, "unexpected payload (no message.items)")
            items = msg["items"] or []
            for item in items:
                if len(records) >= max_results:
                    break
                records.append(map_item(item, query, rank=len(records) + 1))
            nxt = msg.get("next-cursor")
            if not items or not nxt:
                break
            params = {**params, "cursor": nxt}
        log.info("crossref %r -> %d records", query, len(records))
        return records

    def lookup_doi(self, doi: str, *, query_label: str = "doi_lookup") -> SourceRecord | None:
        """Fetch metadata for one DOI (used to enrich thin web-search hits). None if not registered."""
        params = {"mailto": self.mailto} if self.mailto else None
        try:
            body = self.http.get_json(f"{BASE_URL}/{quote(doi, safe='/')}", params)
        except SourceError as e:
            if "HTTP 404" in str(e):
                return None
            raise
        msg = body.get("message") if isinstance(body, dict) else None
        if not isinstance(msg, dict):
            return None
        return map_item(msg, query_label, rank=1)

    def stats(self) -> dict:
        return self.http.stats.as_dict()

    def close(self) -> None:
        self.http.close()
