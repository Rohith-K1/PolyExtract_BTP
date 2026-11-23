"""ML protocol tests on GENERATED SOFTWARE-TEST NUMBERS (random, never stored, never scientific data)."""

import numpy as np
import pandas as pd

from polyextract.apps.workflows import forward_predict
from polyextract.ml.experiments import run


def fixture_df(n=60, papers=10, with_mz=True, seed=0):
    rng = np.random.default_rng(seed)
    mn = 10 ** rng.uniform(3.5, 5.5, n)
    d = rng.uniform(1.05, 2.0, n)
    return pd.DataFrame({
        "paper_id": [f"doi:10.5555/test-{i % papers}" for i in range(n)],
        "property_name": "glass_transition_temperature", "value": 380 - 1e5 / mn + rng.normal(0, 1, n), "unit": "K",
        "mn": mn, "mw": mn * d, "dispersity": d, "mz": mn * d * 1.4 if with_mz else np.nan,
        "dpn": np.nan, "dpw": np.nan, "dpz": np.nan, "mwd_skewness": np.nan, "mwd_kurtosis": np.nan,
        "polymer_family": "test-family", "heating_rate_K_min": 10.0,
    })


def test_grouped_cv_and_same_subset_comparison(tmp_path):
    rep = run(fixture_df(), "test", tmp_path / "r.json")
    e = rep.properties["glass_transition_temperature"]
    assert e["status"] == "ok" and e["A"]["ridge"]["folds"] == 5 and e["A"]["ridge"]["n_papers"] == 10
    assert "log_mz" in e["features_B"] and "log_mz" not in e["features_A"]
    assert set(e["same_subset"]) == {"A", "B"} and "log_mz" in e["ablation_drop_one"]
    assert (tmp_path / "r.json").exists()


def test_insufficient_data_reports_no_number():
    e = run(fixture_df(n=20, papers=3), "test").properties["glass_transition_temperature"]
    assert e["status"].startswith("insufficient") and "A" not in e


def test_b_not_run_without_mwd_descriptors():
    e = run(fixture_df(with_mz=False), "test").properties["glass_transition_temperature"]
    assert e["B"] == {} and "B_note" in e


def test_prediction_is_labelled_and_flags_extrapolation():
    p = forward_predict(fixture_df(), "glass_transition_temperature", polymer_family="other", mn=1e7, mw=2e7, dispersity=2.0)
    assert "not an experimental measurement" in p.label and p.predicted_value is not None
    assert any("outside training range" in x for x in p.limitations) and any("extrapolation" in x for x in p.limitations)
