"""The three application workflows, all over VERIFIED records only.

1. forward_predict      — property prediction from polymer + molecular characteristics (MODEL OUTPUT)
2. retrieve             — property-conditioned structured retrieval + ranking (database query, not RAG)
3. answer_with_evidence — question -> retrieve verified records -> grounded answer citing record ids
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from polyextract.ml.experiments import BASE_FEATURES, LOG_TARGETS, MIN_GROUPS, MIN_ROWS, featurize, grouped_cv

# ------------------------------------------------------------------------------------------------
# 1. forward prediction
# ------------------------------------------------------------------------------------------------


@dataclass
class Prediction:
    property_name: str
    predicted_value: float | None
    unit: str | None
    label: str = "MODEL PREDICTION — not an experimental measurement"
    model: str = "random_forest (GroupKFold-evaluated)"
    n_training_records: int = 0
    n_training_papers: int = 0
    cv_metrics: dict = field(default_factory=dict)
    features_used: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


def forward_predict(verified: pd.DataFrame, property_name: str, *, polymer_family: str | None = None,
                    mn: float | None = None, mw: float | None = None, dispersity: float | None = None,
                    heating_rate_K_min: float | None = None) -> Prediction:
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import make_pipeline

    sub = verified[(verified["property_name"] == property_name) & verified["value"].notna()]
    unit = sub["unit"].iloc[0] if len(sub) else None
    pred = Prediction(property_name, None, unit, n_training_records=len(sub),
                      n_training_papers=int(sub["paper_id"].nunique()) if len(sub) else 0)
    if len(sub) < MIN_ROWS or pred.n_training_papers < MIN_GROUPS:
        pred.limitations.append(f"insufficient verified data ({len(sub)} rows, {pred.n_training_papers} papers); no prediction made")
        return pred
    X = featurize(sub)
    feats = [c for c in BASE_FEATURES if X[c].notna().any()] + [c for c in X if c.startswith("fam_")]
    if "heating_rate" in X and X["heating_rate"].notna().any():
        feats.append("heating_rate")
    y = sub["value"].astype(float).to_numpy()
    logt = property_name in LOG_TARGETS
    if logt:
        y = np.log10(y)
    mk = lambda: make_pipeline(SimpleImputer(strategy="median", add_indicator=True),  # noqa: E731
                               RandomForestRegressor(n_estimators=300, min_samples_leaf=2, random_state=42))
    pred.cv_metrics = grouped_cv(X[feats], y, sub["paper_id"].to_numpy(), mk)
    if logt:
        pred.cv_metrics["note"] = "metrics in log10 units"
    model = mk().fit(X[feats], y)
    q = pd.DataFrame([{"mn": mn, "mw": mw, "dispersity": dispersity, "polymer_family": polymer_family,
                       "heating_rate_K_min": heating_rate_K_min, "mz": None, "dpn": None, "dpw": None, "dpz": None,
                       "mwd_skewness": None, "mwd_kurtosis": None}])
    xq = featurize(q).reindex(columns=feats)
    for c in [c for c in feats if c.startswith("fam_")]:
        xq[c] = float(polymer_family is not None and c == f"fam_{polymer_family}")
    v = float(model.predict(xq)[0])
    pred.predicted_value = 10**v if logt else v
    pred.features_used = feats
    if polymer_family and f"fam_{polymer_family}" not in feats:
        pred.limitations.append(f"family '{polymer_family}' not represented in training data — extrapolation")
    for name, val in (("mn", mn), ("mw", mw)):
        if val:
            lo, hi = sub[name].min(), sub[name].max()
            if pd.notna(lo) and not (lo <= val <= hi):
                pred.limitations.append(f"{name}={val:g} outside training range [{lo:g}, {hi:g}]")
    pred.limitations.append("trained on literature data with heterogeneous measurement protocols")
    return pred


# ------------------------------------------------------------------------------------------------
# 2. property-conditioned retrieval
# ------------------------------------------------------------------------------------------------


def retrieve(verified: pd.DataFrame, *, property_name: str | None = None, value_min: float | None = None,
             value_max: float | None = None, target: float | None = None, polymer_family: str | None = None,
             mn_range: tuple[float, float] | None = None, mw_range: tuple[float, float] | None = None,
             dispersity_max: float | None = None, limit: int = 20) -> pd.DataFrame:
    d = verified
    if property_name:
        d = d[d["property_name"] == property_name]
    if value_min is not None:
        d = d[d["value"] >= value_min]
    if value_max is not None:
        d = d[d["value"] <= value_max]
    if polymer_family:
        d = d[d["polymer_family"].fillna("").str.contains(polymer_family, case=False, regex=False)]
    if mn_range:
        d = d[d["mn"].between(*mn_range)]
    if mw_range:
        d = d[d["mw"].between(*mw_range)]
    if dispersity_max is not None:
        d = d[d["dispersity"] <= dispersity_max]
    d = d.copy()
    # rank: closeness to target value (if given), then confidence
    d["_dist"] = (d["value"] - target).abs() if target is not None else 0.0
    d = d.sort_values(["_dist", "confidence"], ascending=[True, False]).drop(columns="_dist")
    cols = ["record_id", "polymer_name", "sample_label", "property_name", "value", "unit", "value_reported",
            "mn", "mw", "dispersity", "mw_technique", "property_technique", "heating_rate_K_min", "doi",
            "evidence_table", "evidence_text", "confidence"]
    return d[[c for c in cols if c in d]].head(limit)


# ------------------------------------------------------------------------------------------------
# 3. evidence-backed answers
# ------------------------------------------------------------------------------------------------

PROPERTY_WORDS = {
    "glass_transition_temperature": r"\bT_?g\b|glass[- ]transition",
    "zero_shear_viscosity": r"zero[- ]shear|η0|viscosit",
    "decomposition_temperature": r"decomposition|thermal stability|\bT_?d\b",
    "tensile_strength": r"tensile strength",
    "youngs_modulus": r"young|modulus",
    "elongation_at_break": r"elongation",
    "density": r"density",
}


@dataclass
class Answer:
    question: str
    answer: str
    evidence: pd.DataFrame
    interpretation_label: str = "Interpretation (generated from the cited records; not itself evidence)"
    generator: str = "deterministic-summary"


def _parse_question(q: str, families: list[str]) -> dict:
    out: dict = {}
    for prop, pat in PROPERTY_WORDS.items():
        if re.search(pat, q, re.I):
            out["property_name"] = prop
            break
    for f in families:
        if f and (f.lower() in q.lower() or re.sub(r"[()]", "", f.lower()) in q.lower()):
            out["polymer_family"] = f
            break
    m = re.search(r"(?:above|greater than|>)\s*(\d+(?:\.\d+)?)", q, re.I)
    if m:
        out["value_min"] = float(m.group(1))
    m = re.search(r"(?:below|less than|<)\s*(\d+(?:\.\d+)?)", q, re.I)
    if m:
        out["value_max"] = float(m.group(1))
    return out


def answer_with_evidence(verified: pd.DataFrame, question: str, llm=None, limit: int = 12) -> Answer:
    families = sorted(verified["polymer_family"].dropna().unique().tolist(), key=len, reverse=True)
    params = _parse_question(question, families)
    ev = retrieve(verified, limit=limit, **params)
    if ev.empty:
        return Answer(question, "No verified records match this question. The dataset contains no evidence to answer it.", ev)
    cite = [f"[{i + 1}] {r.polymer_name} {r.sample_label}: {r.property_name}={r.value:g} {r.unit} "
            f"(reported '{r.value_reported}'; Mn={r.mn if r.mn == r.mn else 'n/a'}, Mw={r.mw if r.mw == r.mw else 'n/a'}, "
            f"Đ={r.dispersity if r.dispersity == r.dispersity else 'n/a'}; doi:{r.doi}, {r.evidence_table})"
            for i, r in enumerate(ev.itertuples())]
    if llm is not None:
        prompt = ("Answer the question using ONLY the numbered evidence records. Cite records as [n] after every "
                  "claim. If the evidence is insufficient, say so. Separate 'Evidence:' (what records show) from "
                  "'Interpretation:' (your reasoning).\n\nQuestion: " + question + "\n\nEvidence records:\n" + "\n".join(cite))
        text = llm.complete(prompt, purpose="qa")
        return Answer(question, text, ev, generator=getattr(llm, "model", "llm"))
    vals = ev["value"].astype(float)
    unit = ev["unit"].iloc[0]
    summ = (f"Evidence: {len(ev)} verified record(s) from {ev['doi'].nunique()} paper(s) match "
            f"({', '.join(f'{k}={v}' for k, v in params.items()) or 'no structured constraints parsed'}). "
            f"Reported values range {vals.min():g}–{vals.max():g} {unit} (median {vals.median():g}).\n"
            + "\n".join(cite))
    if {"mn", "value"} <= set(ev) and ev["mn"].notna().sum() >= 3 and params.get("property_name"):
        r = np.corrcoef(np.log10(ev["mn"].dropna()), ev.loc[ev["mn"].notna(), "value"])[0, 1]
        if math.isfinite(r):
            summ += f"\nInterpretation: within these records, corr(log10 Mn, value) = {r:.2f} (n={int(ev['mn'].notna().sum())}); descriptive only."
    return Answer(question, summ, ev)
