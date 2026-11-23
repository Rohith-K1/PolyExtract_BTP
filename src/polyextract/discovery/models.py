"""Data models internal to Stage 1 (discovery)."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from polyextract.schemas.paper import Author, PaperMetadataSchema


class SourceRecord(BaseModel):
    """Metadata for one paper exactly as one source returned it (after field mapping only)."""

    source: str  # 'openalex' | 'crossref' | 'websearch'
    source_id: str | None = None  # OpenAlex W-id, Crossref DOI, result URL
    query: str
    rank: int  # 1-based position in that source's result list for that query
    doi: str | None = None
    title: str | None = None
    authors: list[Author] = Field(default_factory=list)
    journal: str | None = None
    publication_year: int | None = None
    abstract: str | None = None
    keywords: list[str] = Field(default_factory=list)
    url: str | None = None
    document_type: str | None = None
    snippet: str | None = Field(default=None, description="Search-engine snippet; NOT an abstract.")
    oa_url: str | None = None
    license: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Decision(str, Enum):
    ACCEPT = "accept"
    REVIEW = "review"
    REJECT = "reject"


class TermMatch(BaseModel):
    group: str
    term: str  # the pattern id / label that matched
    field: str  # title | abstract | keywords | snippet
    text: str  # the matched surface text


class RuleResult(BaseModel):
    rule_id: str
    description: str
    passed: bool
    detail: str = ""


class FilterDecision(BaseModel):
    filter_version: str
    decision: Decision
    score: float
    needs_review: bool
    groups_matched: list[str]
    matches: list[TermMatch]
    rules: list[RuleResult]
    reasons: list[str]


class DiscoveryCandidate(BaseModel):
    paper: PaperMetadataSchema
    queries: list[str] = Field(default_factory=list)
    snippets: list[str] = Field(default_factory=list)
    n_source_records: int = 1
    dedup_method: str = "unique"  # unique | doi | title_exact | title_fuzzy
    filter: FilterDecision | None = None
