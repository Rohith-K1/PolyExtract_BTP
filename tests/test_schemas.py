"""Schema-level guarantees: provenance rules that must hold by construction."""

import pytest
from pydantic import ValidationError

from polyextract.schemas import (
    AmorphousStatus,
    CalculationRecord,
    DataOrigin,
    Evidence,
    ExtractionInfo,
    ExtractionMethod,
    LocationType,
    MolecularCharacteristicsSchema,
    PaperMetadataSchema,
    PolymerSampleSchema,
    PropertyMeasurementSchema,
    PropertyName,
    Qualifier,
    Quantity,
    ValueOrigin,
)
from polyextract.schemas.molecular import DESCRIPTOR_FIELDS, MWDCurve

PID = "doi:10.5555/test-fixture-1"


def ev(text="TEST FIXTURE: Mn = 45 kg/mol (Table 1)", **kw):
    return Evidence(paper_id=PID, location_type=LocationType.TABLE, table_id="1", page=3, source_text=text, **kw)


def q(value=45.0, unit="kg/mol", text="45 kg/mol", **kw):
    return Quantity(value=value, unit=unit, reported_text=text, evidence=[ev()], **kw)


class TestQuantity:
    def test_reported_requires_verbatim_text(self):
        with pytest.raises(ValidationError, match="reported_text"):
            Quantity(value=1.0, unit="K")

    def test_calculated_requires_calculation_record(self):
        with pytest.raises(ValidationError, match="CalculationRecord"):
            Quantity(value=1.5, value_origin=ValueOrigin.CALCULATED)

    def test_calculated_with_record_ok(self):
        c = Quantity(
            value=1.5,
            value_origin=ValueOrigin.CALCULATED,
            calculation=CalculationRecord(method="Mw/Mn", inputs={"mw": 67500, "mn": 45000}),
        )
        assert c.value_origin is ValueOrigin.CALCULATED

    def test_reported_must_not_carry_calculation(self):
        with pytest.raises(ValidationError):
            Quantity(value=1.0, reported_text="1.0", calculation=CalculationRecord(method="x"))

    def test_range(self):
        r = Quantity(value=100, value_max=105, qualifier=Qualifier.RANGE, unit="°C", reported_text="100–105 °C")
        assert r.value_max == 105
        with pytest.raises(ValidationError):
            Quantity(value=105, value_max=100, qualifier=Qualifier.RANGE, reported_text="x")
        with pytest.raises(ValidationError):
            Quantity(value=1, value_max=2, reported_text="x")  # value_max without range

    def test_normalized_needs_unit(self):
        with pytest.raises(ValidationError):
            Quantity(value=45, reported_text="45", normalized_value=45000)

    def test_unknown_fields_rejected(self):
        with pytest.raises(ValidationError):
            Quantity(value=1, reported_text="1", confidence=0.99)  # LLM-invented key


class TestPaper:
    def test_doi_normalized(self):
        p = PaperMetadataSchema(paper_id=PID, doi="https://doi.org/10.5555/TEST-Fixture-1", title="TEST FIXTURE")
        assert p.doi == "10.5555/test-fixture-1"

    def test_bad_doi_rejected(self):
        with pytest.raises(ValidationError):
            PaperMetadataSchema(paper_id=PID, doi="not-a-doi", title="TEST FIXTURE")


class TestSample:
    def test_amorphous_claim_requires_evidence(self):
        with pytest.raises(ValidationError, match="amorphous_evidence"):
            PolymerSampleSchema(sample_id="s1", paper_id=PID, polymer_name="polystyrene",
                                amorphous_status=AmorphousStatus.REPORTED_AMORPHOUS)

    def test_unknown_default(self):
        s = PolymerSampleSchema(sample_id="s1", paper_id=PID, polymer_name="polystyrene")
        assert s.amorphous_status is AmorphousStatus.UNKNOWN
        assert s.data_origin is DataOrigin.LITERATURE_EXTRACTION


class TestMolecular:
    def test_nine_descriptors(self):
        assert len(DESCRIPTOR_FIELDS) == 9

    def test_empty_rejected(self):
        with pytest.raises(ValidationError, match="at least one"):
            MolecularCharacteristicsSchema(mc_id="m1", sample_id="s1", paper_id=PID)

    def test_present_descriptors(self):
        m = MolecularCharacteristicsSchema(mc_id="m1", sample_id="s1", paper_id=PID, mn=q(), dispersity=q(1.05, None, "1.05"))
        assert m.present_descriptors() == ["mn", "dispersity"]
        assert len(m.all_evidence()) == 2

    def test_digitized_curve_requires_tool(self):
        with pytest.raises(ValidationError):
            MWDCurve(x=[1, 2, 3], y=[0, 1, 0], x_quantity="log10_M", y_quantity="dw_dlogM",
                     origin="digitized_from_figure", evidence=[ev()])


class TestMeasurement:
    def test_minimal(self):
        m = PropertyMeasurementSchema(
            measurement_id="pm1", sample_id="s1", paper_id=PID,
            property_name=PropertyName.GLASS_TRANSITION_TEMPERATURE, property_name_reported="Tg",
            value=q(100.0, "°C", "100 °C"),
        )
        assert m.conditions.temperature is None

    def test_llm_extraction_requires_model_and_prompt_version(self):
        with pytest.raises(ValidationError):
            ExtractionInfo(method=ExtractionMethod.LLM)
        ExtractionInfo(method=ExtractionMethod.LLM, model="m", prompt_version="extract-v1")
