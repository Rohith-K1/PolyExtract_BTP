# ML Experiments

Code: [ml/experiments.py](../src/polyextract/ml/experiments.py). Run: `polyextract ml --version v1` (verified records only).

**Status: implemented, not yet run on real data.** The verified set is still empty because human review is pending. No result is claimed until a run produces one; results are written to `data/processed/ml_results_<version>.json`.

## Protocol
- **One model per property.** Tg, η0, and the others are different physical targets and are never pooled.
- **Target transform:** log10 for viscosities and moduli; identity for temperatures.
- **Experiment A (conventional):** log Mn, log Mw, Đ, plus polymer-family one-hot and heating rate.
- **Experiment B (MWD-enriched):** A plus log Mz, log DPn/DPw/DPz, skewness, kurtosis.
- **Missing descriptors:** no values are invented. A median fill *plus a missingness indicator* is used inside each training fold only (scikit-learn `SimpleImputer(add_indicator=True)` in the pipeline, so there is no test-fold leakage). B is also compared with A **on the same rows where every B descriptor is actually reported**, so a "gain" can't come from a different subset.
- **Models:** Ridge, Random Forest, Gradient Boosting (XGBoost and neural networks only if the data size justifies them).
- **Splits:** GroupKFold by `paper_id`, so no publication appears in both train and test. Seed 42.
- **Metrics:** MAE, RMSE, R², with n rows, n papers, and folds.
- **Ablation:** drop one B descriptor at a time on the complete-B subset.
- **Minimum data:** 30 rows from 5 papers per property. Below that the run reports "insufficient data" and gives no number.

## Expected limitation (to verify, not assume)
Skewness and kurtosis are rarely reported, so the complete-B subset may be too small. If so, that **is the finding**: richer MWD descriptors are under-reported in the literature, and the hypothesis can't yet be tested at scale.
