"""Web-search adapter. Provider-pluggable; default DuckDuckGo via the `ddgs` package (no key).

Web hits are *thin*: a title, a URL and a snippet. The snippet is stored as `snippet`, never as an
abstract. When a DOI can be recognised in the URL/snippet, the pipeline enriches the hit with a
Crossref DOI lookup, which arrives as a separate, attributable SourceRecord.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Protocol

from polyextract.discovery.base import SourceAdapter, strip_markup
from polyextract.discovery.models import SourceRecord
from polyextract.exceptions import SourceError, SourceUnavailableError
from polyextract.logging_setup import get_logger
from polyextract.schemas.identifiers import extract_dois, normalize_doi

log = get_logger("websearch")

# Publisher URL patterns whose DOI is deterministically encoded in the path. The derivation is
# recorded on the record, and a Crossref lookup later confirms that the DOI exists.
URL_DOI_RULES: list[tuple[str, re.Pattern, str]] = [
    ("nature", re.compile(r"nature\.com/articles/([a-z0-9.\-]+)", re.I), "10.1038/{0}"),
    ("arxiv", re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?", re.I), "10.48550/arXiv.{0}"),
]


def doi_from_url(url: str) -> tuple[str | None, str | None]:
    """Return (doi, rule_name) for known publisher URL patterns, else (None, None)."""
    for name, pat, template in URL_DOI_RULES:
        m = pat.search(url or "")
        if m:
            return normalize_doi(template.format(m.group(1))), name
    return None, None


class WebSearchProvider(Protocol):
    name: str

    def search(self, query: str, max_results: int) -> list[dict]:
        """Return dicts with keys 'title', 'url', 'snippet'."""


class DDGSProvider:
    name = "ddgs"

    def __init__(self, backend: str = "auto"):
        try:
            from ddgs import DDGS  # noqa: F401
        except ImportError as e:  # pragma: no cover - depends on env
            raise SourceUnavailableError("websearch", "install the 'web' extra: pip install ddgs") from e
        self.backend = backend

    def search(self, query: str, max_results: int) -> list[dict]:
        from ddgs import DDGS

        try:
            hits = DDGS().text(query, max_results=max_results, backend=self.backend) or []
        except Exception as e:  # ddgs raises its own exception types (ratelimit, timeout)
            raise SourceError("websearch", f"ddgs failed: {type(e).__name__}: {e}") from e
        return [{"title": h.get("title"), "url": h.get("href"), "snippet": h.get("body")} for h in hits]


class WebSearchAdapter(SourceAdapter):
    name = "websearch"

    def __init__(
        self,
        provider: WebSearchProvider,
        *,
        min_interval_s: float = 2.0,
        query_suffix: str = "",
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.provider = provider
        self.min_interval_s = min_interval_s
        self.query_suffix = query_suffix
        self._sleep = sleep
        self._last: float | None = None
        self._stats = {"requests": 0, "hits": 0, "hits_with_doi": 0, "failures": 0}

    def search(self, query: str, *, max_results: int, year_from: int | None = None, year_to: int | None = None) -> list[SourceRecord]:
        # Year filtering is not supported by generic web search; it is applied later by metadata filters.
        if self._last is not None:
            wait = self.min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = time.monotonic()
        q = f"{query} {self.query_suffix}".strip()
        self._stats["requests"] += 1
        try:
            hits = self.provider.search(q, max_results)
        except SourceError:
            self._stats["failures"] += 1
            raise
        records = []
        for i, h in enumerate(hits, start=1):
            url = h.get("url") or ""
            snippet = strip_markup(h.get("snippet"))
            dois = extract_dois(url)
            derivation = "doi_in_url" if dois else None
            if not dois:
                d, rule = doi_from_url(url)
                if d:
                    dois, derivation = [d], f"url_rule:{rule}"
            if not dois:
                dois = extract_dois(snippet or "")
                derivation = "doi_in_snippet" if dois else None
            records.append(
                SourceRecord(
                    source="websearch",
                    source_id=url or None,
                    query=query,
                    rank=i,
                    doi=dois[0] if dois else None,
                    title=strip_markup(h.get("title")),
                    url=url or None,
                    snippet=snippet,
                    extra={"provider": self.provider.name, "all_dois_seen": dois, "doi_derivation": derivation},
                )
            )
        self._stats["hits"] += len(records)
        self._stats["hits_with_doi"] += sum(1 for r in records if r.doi)
        log.info("websearch %r -> %d hits (%d with DOI)", q, len(records), sum(1 for r in records if r.doi))
        return records

    def stats(self) -> dict:
        return dict(self._stats)
