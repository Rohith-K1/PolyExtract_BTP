"""Schema 4/4: PropertyMeasurementSchema with heterogeneous experimental conditions."""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from polyextract.schemas.common import (
    DataOrigin,
    ExtractionInfo,
    NonEmptyStr,
    Quantity,
    ReviewStatus,
    StrictModel,
    TextualValue,
)


class PropertyName(str, Enum):
    """Distinct physical quantities are distinct names — 'viscosity' is split on purpose."""

    GLASS_TRANSITION_TEMPERATURE = "glass_transition_temperature"
    ZERO_SHEAR_VISCOSITY = "zero_shear_viscosity"
    COMPLEX_VISCOSITY = "complex_viscosity"
    SHEAR_VISCOSITY = "shear_viscosity"  # steady-shear at stated rate
    INTRINSIC_VISCOSITY = "intrinsic_viscosity"  # solution property (dL/g)
    STORAGE_MODULUS = "storage_modulus"
    LOSS_MODULUS = "loss_modulus"
    PLATEAU_MODULUS = "plateau_modulus"
    YOUNGS_MODULUS = "youngs_modulus"
    TENSILE_STRENGTH = "tensile_strength"
    ELONGATION_AT_BREAK = "elongation_at_break"
    TOUGHNESS = "toughness"
    DECOMPOSITION_TEMPERATURE = "decomposition_temperature"  # thermal stability (TGA)
    DENSITY = "density"
    OTHER = "other"


class ExperimentalConditions(StrictModel):
    """All optional: only what the paper reports is filled. `other` holds anything unforeseen."""

    temperature: Quantity | None = None
    pressure: Quantity | None = None
    shear_rate: Quantity | None = None
    frequency: Quantity | None = None
    strain: Quantity | None = None
    strain_rate: Quantity | None = None  # tensile crosshead / strain rate
    heating_rate: Quantity | None = None  # DSC/TGA
    solvent: TextualValue | None = None
    atmosphere: TextualValue | None = None
    sample_preparation: TextualValue | None = None
    annealing: TextualValue | None = None
    other: dict[str, TextualValue] = Field(default_factory=dict)


class PropertyMeasurementSchema(StrictModel):
    measurement_id: NonEmptyStr
    sample_id: NonEmptyStr
    paper_id: NonEmptyStr
    property_name: PropertyName
    property_name_reported: NonEmptyStr = Field(description="Verbatim name, e.g. 'Tg (DSC, midpoint)'.")
    property_definition: str | None = Field(default=None, description="e.g. 'midpoint', 'onset', 'T5%'.")
    value: Quantity
    technique: str | None = Field(default=None, description="e.g. 'DSC', 'DMA', 'oscillatory shear rheometry'.")
    conditions: ExperimentalConditions = Field(default_factory=ExperimentalConditions)
    extraction: ExtractionInfo | None = None
    review_status: ReviewStatus = ReviewStatus.PENDING
    data_origin: DataOrigin = DataOrigin.LITERATURE_EXTRACTION
