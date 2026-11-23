"""Schema 3/4: MolecularCharacteristicsSchema — the nine molecular descriptors.

A descriptor is populated only when (a) directly reported, or (b) validly calculated from
documented source values (value_origin=CALCULATED + CalculationRecord). Nothing is inferred.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator

from polyextract.schemas.common import (
    DataOrigin,
    Evidence,
    ExtractionInfo,
    NonEmptyStr,
    Quantity,
    ReviewStatus,
    StrictModel,
    TextualValue,
)

# Order matters: this is the canonical list of the nine core descriptors.
DESCRIPTOR_FIELDS: tuple[str, ...] = (
    "mn",
    "mw",
    "mz",
    "dispersity",
    "dpn",
    "dpw",
    "dpz",
    "mwd_skewness",
    "mwd_kurtosis",
)
MOLAR_MASS_FIELDS = ("mn", "mw", "mz")
DIMENSIONLESS_FIELDS = ("dispersity", "dpn", "dpw", "dpz", "mwd_skewness", "mwd_kurtosis")


class MWTechnique(str, Enum):
    SEC_CONVENTIONAL = "sec_conventional"  # GPC/SEC with relative (e.g. PS) calibration
    SEC_UNIVERSAL = "sec_universal"
    SEC_MALS = "sec_mals"  # absolute, light scattering detector
    MALDI_TOF = "maldi_tof"
    NMR_END_GROUP = "nmr_end_group"
    VISCOMETRY = "viscometry"
    STATIC_LIGHT_SCATTERING = "static_light_scattering"
    OSMOMETRY = "osmometry"
    SUPPLIER_SPECIFIED = "supplier_specified"
    OTHER = "other"
    NOT_REPORTED = "not_reported"


class MomentBasis(str, Enum):
    NUMBER = "number_distribution"
    WEIGHT = "weight_distribution"
    W_LOG_M = "w_logM"
    NOT_REPORTED = "not_reported"


class KurtosisConvention(str, Enum):
    EXCESS = "excess"
    RAW = "raw"
    NOT_REPORTED = "not_reported"


class CurveOrigin(str, Enum):
    TABULATED_IN_SOURCE = "tabulated_in_source"
    DIGITIZED_FROM_FIGURE = "digitized_from_figure"


class MWDCurve(StrictModel):
    x: list[float]
    y: list[float]
    x_quantity: str = Field(description="'log10_M', 'M', 'elution_volume_mL'")
    y_quantity: str = Field(description="'dw_dlogM', 'w_logM', 'number_fraction', 'detector_response'")
    origin: CurveOrigin
    digitization_tool: str | None = None
    evidence: list[Evidence] = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> "MWDCurve":
        if len(self.x) != len(self.y) or len(self.x) < 3:
            raise ValueError("MWD curve needs equal-length x/y with at least 3 points")
        if self.origin is CurveOrigin.DIGITIZED_FROM_FIGURE and not self.digitization_tool:
            raise ValueError("digitized curves must record the digitization tool")
        return self


class MolecularCharacteristicsSchema(StrictModel):
    mc_id: NonEmptyStr
    sample_id: NonEmptyStr
    paper_id: NonEmptyStr

    mn: Quantity | None = None
    mw: Quantity | None = None
    mz: Quantity | None = None
    dispersity: Quantity | None = None
    dpn: Quantity | None = None
    dpw: Quantity | None = None
    dpz: Quantity | None = None
    mwd_skewness: Quantity | None = None
    mwd_kurtosis: Quantity | None = None

    technique: MWTechnique = MWTechnique.NOT_REPORTED
    technique_reported: str | None = Field(default=None, description="Verbatim technique description.")
    calibration_standard: TextualValue | None = None
    eluent: TextualValue | None = None
    moment_basis: MomentBasis = MomentBasis.NOT_REPORTED
    kurtosis_convention: KurtosisConvention = KurtosisConvention.NOT_REPORTED
    repeat_unit_molar_mass_g_mol: float | None = Field(default=None, gt=0, description="M0 used for DP calcs.")
    mwd_curve: MWDCurve | None = None

    extraction: ExtractionInfo | None = None
    review_status: ReviewStatus = ReviewStatus.PENDING
    data_origin: DataOrigin = DataOrigin.LITERATURE_EXTRACTION

    @model_validator(mode="after")
    def _non_empty(self) -> "MolecularCharacteristicsSchema":
        if not self.present_descriptors() and self.mwd_curve is None:
            raise ValueError("MolecularCharacteristics must contain at least one descriptor or an MWD curve")
        return self

    def present_descriptors(self) -> list[str]:
        return [f for f in DESCRIPTOR_FIELDS if getattr(self, f) is not None]

    def all_evidence(self) -> list[Evidence]:
        ev: list[Evidence] = []
        for f in DESCRIPTOR_FIELDS:
            q = getattr(self, f)
            if q is not None:
                ev.extend(q.evidence)
        return ev
