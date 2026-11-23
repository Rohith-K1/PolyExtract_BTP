"""Release-time row hygiene: say WHICH measurement a row is, drop computed values, merge exact duplicates.

A paper often reports the same property of the same sample more than once, legitimately: Tg by DSC and by
rheology, Tg1 and Tg2 of a block copolymer, Tg of the hard and soft segment, intrinsic viscosity under two
footnoted conditions. Those are different measurements, not duplicates. `measurement_variant` makes the
difference explicit from the reported column header, so no information is lost.

Theoretical / calculated values (Tg,theo, Fox-equation Tg, predicted, simulated) are not measurements and
are excluded from a release. Exact duplicates (same paper, sample, property, variant, value and conditions)
are merged into one row, keeping the highest-confidence record and listing the merged record ids.
"""

from __future__ import annotations

import re

import pandas as pd

_THEORETICAL = re.compile(r"theo|calc|predict|simul|estimat|\bfox\b|\bmodel(?:led)?\b", re.I)
_METHODS = [  # (regex on header, label) — first match wins
    (re.compile(r"\bDMT?A\b", re.I), "DMA"),
    (re.compile(r"rheo", re.I), "rheology"),
    (re.compile(r"\bDSC\b", re.I), "DSC"),
    (re.compile(r"\bTGA\b", re.I), "TGA"),
    (re.compile(r"\bTMA\b", re.I), "TMA"),
    (re.compile(r"\bGPC\b|\bSEC\b", re.I), "SEC"),
]
_SCAN = re.compile(r"(1st|2nd|first|second|third|3rd)\s*(?:heating\s*)?(scan|heating|run)", re.I)
_INDEX = re.compile(r"T\s*g\s*\(?\s*([12])\s*\)?(?![0-9])|T\s*g\s*,?\s*\[[^\]]*\]\s*(I{1,3})\b|\bT\s*g\s*(I{1,3})\b")
_PHASE = re.compile(r"T\s*g\s*(?:\^\{([A-Za-z0-9]+)\}|,\s*([A-Z]{1,4})\b)")
_FOOTNOTE = re.compile(r"\^\{\(?([0-9a-z*†‡§]{1,2})\)?\}")
_MASSLOSS = re.compile(r"T\s*d?\s*[-,]?\s*(\d{1,2})\s*%", re.I)
_CONTEXT = re.compile(r"\)\s*([A-Za-z0-9][^()^]*?)\s*$")  # text after the unit: "Tg (°C) neat PMMA"
_FOOTNOTE_IGNORE = {"−3", "3", "0"}  # superscripts that are units or symbols (cm^{-3}, GN^{0}), not notes


def measurement_variant(header: str | None) -> str:
    """Compact label of what distinguishes this measurement, from the paper's own column header.
    Empty string = plain measurement with nothing to distinguish."""
    if not isinstance(header, str) or not header.strip():
        return ""
    h = header
    parts: list[str] = []
    if _THEORETICAL.search(h):
        parts.append("theoretical")
    elif re.search(r"\bexp\b", h, re.I):
        parts.append("experimental")
    for rx, label in _METHODS:
        if rx.search(h):
            parts.append(label)
            break
    if m := _SCAN.search(h):
        parts.append(f"{m.group(1).lower()} {m.group(2).lower()}")
    if m := _INDEX.search(h):
        idx = m.group(1) or m.group(2) or m.group(3)
        parts.append(f"transition {({'I': '1', 'II': '2', 'III': '3'}).get(idx, idx)}")
    if m := _PHASE.search(h):
        ph = m.group(1) or m.group(2)
        if ph and not ph.isdigit() and ph.lower() not in {"exp", "theo", "dsc", "c", "d", "a", "b"}:
            parts.append(f"phase {ph}")
    if m := _MASSLOSS.search(h):
        parts.append(f"{m.group(1)}% mass loss")
    if m := _CONTEXT.search(h):
        parts.append(f"context {m.group(1).strip()}")
    notes = [n for n in _FOOTNOTE.findall(h) if n not in _FOOTNOTE_IGNORE and not h.strip().startswith("GN")]
    if notes:
        parts.append("note " + ",".join(dict.fromkeys(notes)))
    return "; ".join(dict.fromkeys(parts))


def is_theoretical(header: str | None) -> bool:
    return isinstance(header, str) and bool(_THEORETICAL.search(header))


DUP_KEY = ["doi", "sample_id", "property_name", "measurement_variant", "value", "value_max", "unit",
           "heating_rate_K_min", "temperature_K", "frequency"]


def clean_release(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Returns (release rows, excluded theoretical rows, counts). Adds measurement_variant,
    merged_record_ids, n_values_same_sample_property."""
    if df.empty:
        return df, df, {"theoretical_excluded": 0, "exact_duplicates_merged": 0, "multi_value_groups": 0}
    df = df.copy()
    df["measurement_variant"] = df["property_reported_as"].map(measurement_variant)
    theo = df["property_reported_as"].map(is_theoretical)
    excluded, df = df[theo].copy(), df[~theo].copy()

    df = df.sort_values("confidence", ascending=False, kind="stable")
    key = df[DUP_KEY].apply(lambda c: c.map(lambda v: "" if pd.isna(v) else str(v))).agg("|".join, axis=1)
    merged = df.groupby(key)["record_id"].apply(lambda s: ";".join(s.iloc[1:]))
    df["merged_record_ids"] = key.map(merged)
    n_before = len(df)
    df = df[~key.duplicated()].copy()

    grp = ["doi", "sample_id", "property_name"]
    df["n_values_same_sample_property"] = df.groupby(grp)["record_id"].transform("size")
    unresolved = df[df["n_values_same_sample_property"] > 1].groupby(grp)["measurement_variant"].nunique()
    sizes = df[df["n_values_same_sample_property"] > 1].groupby(grp).size()
    counts = {
        "theoretical_excluded": int(len(excluded)),
        "exact_duplicates_merged": int(n_before - len(df)),
        "multi_value_groups": int(len(sizes)),
        "multi_value_groups_without_distinguishing_variant": int((unresolved < sizes).sum()),
    }
    return df.sort_index(), excluded, counts
