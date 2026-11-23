"""Experiment A (conventional Mn/Mw/Đ) vs Experiment B (MWD-enriched) on the VERIFIED dataset.

Protocol (identical for A and B):
  * one property at a time (Tg, η0, ... are different physical targets; never pooled)
  * target transforms: log10 for viscosity/modulus-like targets, identity for temperatures
  * descriptor features use log10 for molar masses; missing descriptors are NOT imputed with
    values — a missingness indicator + neutral fill is used, and B is also evaluated on the
    "complete-B subset" where every B feature is actually reported, against A on the SAME rows
  * GroupKFold by paper_id (no publication leakage); fixed seeds
  * metrics MAE, RMSE, R²; ablation = drop one B descriptor at a time
Results are written with dataset version, features, seeds and fold sizes. If too few rows exist,
the run says so instead of reporting a number.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

LOG_TARGETS = {"zero_shear_viscosity", "complex_viscosity", "shear_viscosity", "storage_modulus", "loss_modulus",
               "plateau_modulus", "youngs_modulus", "tensile_strength"}
BASE_FEATURES = ["log_mn", "log_mw", "dispersity"]
MWD_FEATURES = ["log_mz", "log_dpn", "log_dpw", "log_dpz", "mwd_skewness", "mwd_kurtosis"]
MIN_ROWS, MIN_GROUPS = 30, 5
SEED = 42


def featurize(df: pd.DataFrame) -> pd.DataFrame:
    X = pd.DataFrame(index=df.index)
    for f in ("mn", "mw", "mz", "dpn", "dpw", "dpz"):
        X[f"log_{f}"] = np.log10(df[f].astype(float)) if f in df else np.nan
    for f in ("dispersity", "mwd_skewness", "mwd_kurtosis"):
        X[f] = df[f].astype(float) if f in df else np.nan
    # Đ computed from Mw/Mn ONLY where both reported and Đ missing would be a calculated value; we do
    # not do it here silently — dispersity stays as reported.
    fam = df.get("polymer_family", pd.Series(index=df.index, dtype=object)).fillna("unidentified")
    top = fam.value_counts().index[:15]
    for f in top:
        X[f"fam_{f}"] = (fam == f).astype(float)
    if "heating_rate_K_min" in df:
        X["heating_rate"] = df["heating_rate_K_min"].astype(float)
    return X


def _models():
    from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    imp = lambda: SimpleImputer(strategy="median", add_indicator=True)  # noqa: E731  (fit on train folds only)
    return {
        "ridge": lambda: make_pipeline(imp(), StandardScaler(), Ridge(alpha=1.0)),
        "random_forest": lambda: make_pipeline(imp(), RandomForestRegressor(n_estimators=300, min_samples_leaf=2, random_state=SEED)),
        "gradient_boosting": lambda: make_pipeline(imp(), GradientBoostingRegressor(random_state=SEED)),
    }


def grouped_cv(X: pd.DataFrame, y: np.ndarray, groups: np.ndarray, make_model, n_splits: int = 5) -> dict:
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
    from sklearn.model_selection import GroupKFold

    k = min(n_splits, len(np.unique(groups)))
    preds = np.full(len(y), np.nan)
    for tr, te in GroupKFold(n_splits=k).split(X, y, groups):
        m = make_model()
        m.fit(X.iloc[tr], y[tr])
        preds[te] = m.predict(X.iloc[te])
    return {"MAE": float(mean_absolute_error(y, preds)), "RMSE": float(math.sqrt(mean_squared_error(y, preds))),
            "R2": float(r2_score(y, preds)), "n": int(len(y)), "n_papers": int(len(np.unique(groups))), "folds": k}


@dataclass
class ExperimentReport:
    dataset_version: str
    seed: int = SEED
    properties: dict = field(default_factory=dict)


def run(df: pd.DataFrame, dataset_version: str, out_path: Path | None = None) -> ExperimentReport:
    rep = ExperimentReport(dataset_version)
    for prop, sub in df.groupby("property_name"):
        sub = sub[sub["value"].notna()].copy()
        entry: dict = {"n_rows": len(sub), "n_papers": int(sub["paper_id"].nunique())}
        if len(sub) < MIN_ROWS or sub["paper_id"].nunique() < MIN_GROUPS:
            entry["status"] = f"insufficient data (need ≥{MIN_ROWS} rows and ≥{MIN_GROUPS} papers)"
            rep.properties[prop] = entry
            continue
        y = sub["value"].astype(float).to_numpy()
        if prop in LOG_TARGETS:
            y = np.log10(y)
            entry["target_transform"] = "log10"
        X = featurize(sub)
        groups = sub["paper_id"].to_numpy()
        fam_cols = [c for c in X if c.startswith("fam_")] + (["heating_rate"] if "heating_rate" in X else [])
        feats_a = [c for c in BASE_FEATURES if X[c].notna().any()] + fam_cols
        feats_b = feats_a + [c for c in MWD_FEATURES if X[c].notna().any()]
        entry["features_A"], entry["features_B"] = feats_a, feats_b
        entry["descriptor_coverage_pct"] = {c: round(100 * X[c].notna().mean(), 1) for c in BASE_FEATURES + MWD_FEATURES}
        entry["A"], entry["B"] = {}, {}
        for name, mk in _models().items():
            entry["A"][name] = grouped_cv(X[feats_a], y, groups, mk)
            if feats_b != feats_a:
                entry["B"][name] = grouped_cv(X[feats_b], y, groups, mk)
        extra = [c for c in feats_b if c not in feats_a]
        if not extra:
            entry["B_note"] = "no MWD-enriched descriptors present in verified data for this property; B not run"
        else:
            complete = X[extra].notna().all(axis=1)
            entry["complete_B_subset_rows"] = int(complete.sum())
            if complete.sum() >= MIN_ROWS and len(np.unique(groups[complete])) >= MIN_GROUPS:
                Xc, yc, gc = X[complete], y[complete.to_numpy()], groups[complete.to_numpy()]
                mk = _models()["random_forest"]
                entry["same_subset"] = {"A": grouped_cv(Xc[feats_a], yc, gc, mk), "B": grouped_cv(Xc[feats_b], yc, gc, mk)}
                entry["ablation_drop_one"] = {
                    c: grouped_cv(Xc[[f for f in feats_b if f != c]], yc, gc, mk) for c in extra
                }
            else:
                entry["same_subset"] = "too few rows where all B descriptors are reported; A-vs-B comparison not meaningful"
        entry["status"] = "ok"
        rep.properties[prop] = entry
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(rep.__dict__, indent=2, default=str), encoding="utf-8")
    return rep
