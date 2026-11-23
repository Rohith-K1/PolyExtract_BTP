"""Release hygiene: distinguishing labels, computed values removed, exact duplicates merged."""

import pandas as pd

from polyextract.dataset.variants import clean_release, is_theoretical, measurement_variant


def test_variant_labels_from_headers():
    assert measurement_variant("Tg, DSC^{c} (°C)") == "DSC; note c"
    assert measurement_variant("Tg, rheo^{d} (°C)") == "rheology; note d"
    assert measurement_variant("Tg(1)d (°C)") == "transition 1"
    assert measurement_variant("Tg [°C] II ^{b}") == "transition 2; note b"
    assert measurement_variant("Tg^{PDMS} ^{(c)}(°C)") == "phase PDMS; note c"
    assert measurement_variant("Tg,HS (°C)") == "phase HS"
    assert measurement_variant("Td5% (°C)") == "5% mass loss"
    assert measurement_variant("Tg (°C) neat PMMA") == "context neat PMMA"
    assert measurement_variant("ρ/g·cm^{−3}") == "" and measurement_variant("Tg (°C)") == ""
    assert is_theoretical("Tg,theo^{d} (°C)") and is_theoretical("Tg (Fox) (°C)")
    assert not is_theoretical("Tg,exp^{c} (°C)")


def _row(rid, header, value, conf=0.9, sample="s1"):
    return {"record_id": rid, "doi": "10.1/x", "sample_id": sample, "property_name": "glass_transition_temperature",
            "property_reported_as": header, "value": value, "value_max": None, "unit": "K", "confidence": conf,
            "heating_rate_K_min": None, "temperature_K": None, "frequency": None}


def test_clean_release_merges_exact_duplicates_and_drops_theoretical():
    df = pd.DataFrame([
        _row("a", "Tg, DSC (°C)", 400.0, 0.8), _row("b", "Tg, DSC (°C)", 400.0, 0.95),   # exact duplicate
        _row("c", "Tg, rheo (°C)", 405.0),                                                 # different measurement
        _row("d", "Tg,theo (°C)", 398.0),                                                  # computed
        _row("e", "Tg, DSC (°C)", 400.0, sample="s2"),                                     # same value, other sample
    ])
    out, theo, n = clean_release(df)
    assert set(out.record_id) == {"b", "c", "e"} and list(theo.record_id) == ["d"]
    assert out.set_index("record_id").loc["b", "merged_record_ids"] == "a"  # higher confidence kept
    assert n == {"theoretical_excluded": 1, "exact_duplicates_merged": 1, "multi_value_groups": 1,
                 "multi_value_groups_without_distinguishing_variant": 0}
