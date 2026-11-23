"""Schema 1/4: PaperMetadataSchema."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import Field, field_validator

from polyextract.schemas.common import DataOrigin, NonEmptyStr, StrictModel
from polyextract.schemas.identifiers import normalize_doi


class FullTextStatus(str, Enum):
    NOT_ATTEMPTED = "not_attempted"
    OA_LOCATED = "oa_located"  # legal OA location known, not yet downloaded
    ACQUIRED = "acquired"
    NOT_OPEN_ACCESS = "not_open_access"  # no legal access path; never bypassed
    FAILED = "failed"


class Author(StrictModel):
    name: NonEmptyStr
    given: str | None = None
    family: str | None = None
    orcid: str | None = None


class PaperMetadataSchema(StrictModel):
    paper_id: NonEmptyStr
    doi: str | None = None
    title: NonEmptyStr
    authors: list[Author] = Field(default_factory=list)
    journal: str | None = None
    publication_year: int | None = Field(default=None, ge=1900, le=2100)
    abstract: str | None = None
    keywords: list[str] = Field(default_factory=list)
    source_url: str | None = None
    document_type: str | None = None
    retrieval_sources: list[str] = Field(default_factory=list, description="e.g. ['openalex','crossref']")
    source_ids: dict[str, str] = Field(default_factory=dict, description="Source-specific IDs (OpenAlex W-id ...).")
    field_sources: dict[str, str] = Field(
        default_factory=dict, description="Which source provided each merged field."
    )
    oa_url: str | None = None
    license: str | None = None
    fulltext_status: FullTextStatus = FullTextStatus.NOT_ATTEMPTED
    data_origin: DataOrigin = DataOrigin.LITERATURE_EXTRACTION
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("doi")
    @classmethod
    def _doi(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        n = normalize_doi(v)
        if n is None:
            raise ValueError(f"not a valid DOI: {v!r}")
        return n
