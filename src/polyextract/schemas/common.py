"""Shared building blocks for the four core schemas.

Design rules enforced here (not left to downstream code):
  * Every scientific value is a `Quantity` that keeps the verbatim reported text, original unit,
    and its evidence; normalized values sit *alongside* the original, never replacing it.
  * A value is either REPORTED (copied from the source) or CALCULATED (derived from other
    documented values with a recorded method). A calculated value without a `CalculationRecord`
    is rejected at construction time.
  * Every record carries `data_origin`; TEST_FIXTURE records can never enter the curated dataset.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class StrictModel(BaseModel):
    """Base: unknown fields are errors (catches malformed / hallucinated LLM keys)."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, use_enum_values=False)


class DataOrigin(str, Enum):
    LITERATURE_EXTRACTION = "literature_extraction"
    TEST_FIXTURE = "test_fixture"


class ReviewStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    CORRECTED = "corrected"  # accepted after a reviewer correction
    REJECTED = "rejected"
    NEEDS_REEXTRACTION = "needs_reextraction"


class LocationType(str, Enum):
    METADATA = "metadata"
    ABSTRACT = "abstract"
    TEXT = "text"
    TABLE = "table"
    FIGURE_CAPTION = "figure_caption"
    FIGURE = "figure"  # value read from a plot (digitized) — weaker evidence
    SUPPLEMENTARY_TEXT = "supplementary_text"
    SUPPLEMENTARY_TABLE = "supplementary_table"


class Evidence(StrictModel):
    """A pointer into the source document plus the verbatim text supporting a value."""

    paper_id: NonEmptyStr
    doi: str | None = None
    document_ref: str | None = Field(
        default=None, description="Which document: URL or sha256 of the PDF/HTML/SI file."
    )
    location_type: LocationType
    page: int | None = Field(default=None, ge=1)
    section: str | None = None
    table_id: str | None = None
    figure_id: str | None = None
    source_text: NonEmptyStr = Field(description="Verbatim passage / table row / caption.")
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _span(self) -> "Evidence":
        if self.char_start is not None and self.char_end is not None and self.char_end < self.char_start:
            raise ValueError("char_end must be >= char_start")
        return self


class ValueOrigin(str, Enum):
    REPORTED = "reported"
    CALCULATED = "calculated"


class Qualifier(str, Enum):
    EQ = "="
    APPROX = "~"
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="
    RANGE = "range"


class CalculationRecord(StrictModel):
    method: NonEmptyStr = Field(description="e.g. 'Mw/Mn', 'Mn/M0', 'moments of tabulated w(logM)'.")
    formula: str | None = None
    inputs: dict[str, float] = Field(default_factory=dict, description="Input values used (normalized).")
    input_refs: list[str] = Field(
        default_factory=list, description="IDs/field paths of the reported values used as inputs."
    )
    notes: str | None = None


class Quantity(StrictModel):
    value: float | None = Field(default=None, description="Original numeric value as reported.")
    value_max: float | None = Field(default=None, description="Upper bound when qualifier == range.")
    qualifier: Qualifier = Qualifier.EQ
    uncertainty: float | None = Field(default=None, ge=0)
    unit: str | None = Field(default=None, description="Original unit string as reported.")
    reported_text: str | None = Field(default=None, description="Verbatim value text, e.g. '1.2 × 10^5 g/mol'.")
    normalized_value: float | None = None
    normalized_value_max: float | None = None
    normalized_unit: str | None = None
    value_origin: ValueOrigin = ValueOrigin.REPORTED
    calculation: CalculationRecord | None = None
    evidence: list[Evidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistency(self) -> "Quantity":
        if self.value is None and self.normalized_value is None:
            raise ValueError("Quantity needs a value (missing data should be None at the field level)")
        if self.value_origin is ValueOrigin.CALCULATED:
            if self.calculation is None:
                raise ValueError("calculated value requires a CalculationRecord")
        else:
            if self.calculation is not None:
                raise ValueError("reported value must not carry a CalculationRecord")
            if not self.reported_text:
                raise ValueError("reported value requires the verbatim reported_text")
        if self.qualifier is Qualifier.RANGE:
            if self.value is None or self.value_max is None:
                raise ValueError("range qualifier requires value and value_max")
            if self.value_max < self.value:
                raise ValueError("value_max must be >= value")
        elif self.value_max is not None:
            raise ValueError("value_max only allowed with qualifier 'range'")
        if self.normalized_value is not None and not self.normalized_unit:
            raise ValueError("normalized_value requires normalized_unit")
        return self


class TextualValue(StrictModel):
    """Non-numeric reported information (solvent, preparation, ...) with its evidence."""

    text: NonEmptyStr
    evidence: list[Evidence] = Field(default_factory=list)


class ExtractionMethod(str, Enum):
    LLM = "llm"
    RULE_BASED = "rule_based"
    MANUAL = "manual"
    METADATA_API = "metadata_api"


class ExtractionInfo(StrictModel):
    method: ExtractionMethod
    model: str | None = None
    prompt_version: str | None = None
    run_id: str | None = None
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _llm_fields(self) -> "ExtractionInfo":
        if self.method is ExtractionMethod.LLM and (not self.model or not self.prompt_version):
            raise ValueError("LLM extraction must record model and prompt_version")
        return self
