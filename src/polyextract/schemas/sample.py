"""Schema 2/4: PolymerSampleSchema."""

from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator

from polyextract.schemas.common import (
    DataOrigin,
    Evidence,
    ExtractionInfo,
    NonEmptyStr,
    ReviewStatus,
    StrictModel,
)


class Architecture(str, Enum):
    LINEAR = "linear"
    BRANCHED = "branched"
    STAR = "star"
    COMB = "comb"
    BOTTLEBRUSH = "bottlebrush"
    CYCLIC = "cyclic"
    DENDRITIC = "dendritic"
    NETWORK = "network"
    NOT_REPORTED = "not_reported"


class AmorphousStatus(str, Enum):
    REPORTED_AMORPHOUS = "reported_amorphous"  # paper states amorphous / no melting endotherm
    SUPPORTED_BY_EVIDENCE = "supported_by_evidence"  # e.g. 'atactic PS' stated; reviewer-confirmable
    NOT_AMORPHOUS = "not_amorphous"  # semicrystalline reported
    UNKNOWN = "unknown"


class StructureOrigin(str, Enum):
    REPORTED_IN_SOURCE = "reported_in_source"
    CURATED_LOOKUP = "curated_lookup"  # standard repeat-unit SMILES from a named homopolymer


class StructureRepresentation(StrictModel):
    notation: str = Field(description="'psmiles', 'bigsmiles', 'smiles' ...")
    value: NonEmptyStr
    origin: StructureOrigin
    evidence: list[Evidence] = Field(default_factory=list)


class PolymerSampleSchema(StrictModel):
    sample_id: NonEmptyStr
    paper_id: NonEmptyStr
    sample_label: str | None = Field(default=None, description="Label used in the paper, e.g. 'PS-3'.")
    polymer_name: NonEmptyStr
    polymer_family: str | None = Field(default=None, description="Controlled vocabulary, e.g. 'polystyrene'.")
    monomers: list[str] = Field(default_factory=list)
    repeat_unit: str | None = None
    architecture: Architecture = Architecture.NOT_REPORTED
    tacticity: str | None = None
    composition: str | None = Field(default=None, description="Copolymer/blend composition as reported.")
    is_blend: bool | None = None
    structure: StructureRepresentation | None = None
    amorphous_status: AmorphousStatus = AmorphousStatus.UNKNOWN
    amorphous_evidence: list[Evidence] = Field(default_factory=list)
    identity_evidence: list[Evidence] = Field(
        default_factory=list, description="Passage(s) that establish this sample exists in the paper."
    )
    extraction: ExtractionInfo | None = None
    review_status: ReviewStatus = ReviewStatus.PENDING
    data_origin: DataOrigin = DataOrigin.LITERATURE_EXTRACTION

    @model_validator(mode="after")
    def _amorphous_needs_evidence(self) -> "PolymerSampleSchema":
        if self.amorphous_status is not AmorphousStatus.UNKNOWN and not self.amorphous_evidence:
            raise ValueError(
                f"amorphous_status={self.amorphous_status.value} requires amorphous_evidence; use 'unknown' otherwise"
            )
        if self.structure and self.structure.origin is StructureOrigin.REPORTED_IN_SOURCE and not self.structure.evidence:
            raise ValueError("structure reported_in_source requires evidence")
        return self
