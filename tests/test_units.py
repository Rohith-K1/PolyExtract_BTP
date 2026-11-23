"""Value/unit parsing. Cases marked LIVE are exact strings observed in real OA tables (2026-11-05)."""

import math

import pytest

from polyextract.normalization.units import convert, parse_unit, parse_value_text


@pytest.mark.parametrize("text,value,vmax,qual,unc,unit", [
    ("45.2", 45.2, None, "=", None, None),
    ("35,000", 35000, None, "=", None, None),  # LIVE: thousands comma (was parsed as 35.0)
    ("1,168,000", 1168000, None, "=", None, None),
    ("12,5", 12.5, None, "=", None, None),  # decimal comma
    ("20 307 ± 422", 20307, None, "=", 422, None),  # LIVE: space thousands separator
    ("1 168 000", 1168000, None, "=", None, None),  # LIVE
    ("45 ± 4.7", 45, None, "=", 4.7, None),
    ("67 ^{*} MPa s", 67, None, "=", None, "MPa s"),  # LIVE: footnote marker mid-string
    ("1.60^{}", 1.60, None, "=", None, None),  # LIVE: empty superscript
    ("75.1 [1.09]", 75.1, None, "=", None, None),  # LIVE: bracketed Đ is not a unit
    ("1.2 × 10^{5} g/mol", 1.2e5, None, "=", None, "g/mol"),
    ("100–105 °C", 100, 105, "range", None, "°C"),
    ("−9", -9, None, "=", None, None),  # LIVE: unicode minus
    ("<5", 5, None, "<", None, None),
    ("89.6^{a}", 89.6, None, "=", None, None),
])
def test_parse_value(text, value, vmax, qual, unc, unit):
    p = parse_value_text(text)
    assert p.value == pytest.approx(value)
    assert p.value_max == vmax and p.qualifier == qual and p.uncertainty == unc and p.unit == unit


@pytest.mark.parametrize("text", ["–", "n.d.", "", "-"])
def test_missing_tokens(text):
    assert parse_value_text(text).value is None


def test_lost_superscript_is_not_guessed():
    # '1.2 × 105' (superscript lost upstream) must not become 1.2e105 or 126
    assert parse_value_text("1.2 × 105").value is None


@pytest.mark.parametrize("unit,value,out,canon", [
    ("g/mol", 5, 5, "g/mol"), ("kg/mol", 5, 5000, "g/mol"), ("kDa", 100, 1e5, "g/mol"),
    ("g mol^{-1}", 1, 1, "g/mol"),  # LIVE
    ("g mol–^{1}", 1, 1, "g/mol"),  # LIVE (en dash + superscript)
    ("kg mol−1", 2, 2000, "g/mol"),
    ("°C", 100, 373.15, "K"), ("K", 300, 300, "K"),
    ("Pa s", 1, 1, "Pa·s"), ("mPa s", 1, 1e-3, "Pa·s"), ("MPa s", 67, 6.7e7, "Pa·s"), ("cP", 1, 1e-3, "Pa·s"),
    ("MPa", 45, 4.5e7, "Pa"), ("GPa", 3, 3e9, "Pa"), ("kPa", 130, 1.3e5, "Pa"), ("mPa", 1, 1e-3, "Pa"),
    ("g/cm3", 1.05, 1.05, "g/cm3"), ("kg/m3", 1050, 1.05, "g/cm3"), ("%", 5, 5, "%"),
    ("ºC", 100, 373.15, "K"), ("% Initial Length", 5, 5, "%"), ("/g/mol", 3, 3, "g/mol"),  # LIVE
])
def test_convert(unit, value, out, canon):
    v, c = convert(value, unit)
    assert c == canon and math.isclose(v, out, rel_tol=1e-9)


def test_unknown_unit_never_guessed():
    assert convert(5, "furlongs") == (None, None)
    assert not parse_unit(None).recognized
