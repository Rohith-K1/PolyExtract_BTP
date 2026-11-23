"""The four core schemas and their shared building blocks."""

from polyextract.schemas.common import (
    CalculationRecord,
    DataOrigin,
    Evidence,
    ExtractionInfo,
    ExtractionMethod,
    LocationType,
    Qualifier,
    Quantity,
    ReviewStatus,
    TextualValue,
    ValueOrigin,
)
from polyextract.schemas.measurement import ExperimentalConditions, PropertyMeasurementSchema, PropertyName
from polyextract.schemas.molecular import (
    DESCRIPTOR_FIELDS,
    MolecularCharacteristicsSchema,
    MWDCurve,
    MWTechnique,
)
from polyextract.schemas.paper import Author, FullTextStatus, PaperMetadataSchema
from polyextract.schemas.sample import AmorphousStatus, Architecture, PolymerSampleSchema

__all__ = [
    "AmorphousStatus", "Architecture", "Author", "CalculationRecord", "DataOrigin", "DESCRIPTOR_FIELDS",
    "Evidence", "ExperimentalConditions", "ExtractionInfo", "ExtractionMethod", "FullTextStatus",
    "LocationType", "MolecularCharacteristicsSchema", "MWDCurve", "MWTechnique", "PaperMetadataSchema",
    "PolymerSampleSchema", "PropertyMeasurementSchema", "PropertyName", "Qualifier", "Quantity",
    "ReviewStatus", "TextualValue", "ValueOrigin",
]
