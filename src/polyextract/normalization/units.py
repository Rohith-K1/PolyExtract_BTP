"""Value-text and unit parsing and normalization for polymer data.

The unit table is our own (not `pint`) because the domain set is small and closed: molar masses
(g/mol, kg/mol, Da, kDa), temperatures, viscosities, moduli and stresses, and densities. Each rule
is explicit and tested. Unknown units are NEVER guessed: the normalized value stays None and
validation flags the record.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# canonical normalized units per dimension
G_PER_MOL = "g/mol"
KELVIN = "K"
PA_S = "Pa·s"
PA = "Pa"
G_PER_CM3 = "g/cm3"
PERCENT = "%"
DIMENSIONLESS = "dimensionless"
DL_PER_G = "dL/g"
J_PER_M2 = "J/m2"
MJ_PER_M3 = "MJ/m3"
RAD_PER_S = "rad/s"
HZ = "Hz"
PER_S = "1/s"
K_PER_MIN = "K/min"


def _clean_unit(u: str) -> str:
    u = u.strip().strip("()[]{} ,;")
    u = (u.replace("−", "-").replace("–", "-").replace("⁻¹", "-1").replace("⁻³", "-3").replace("³", "3")
         .replace("·", " ").replace("⋅", " ").replace("*", " ").replace("^", "").replace("{", "").replace("}", ""))
    u = re.sub(r"-\s+(\d)", r"-\1", u)  # 'g mol- 1' -> 'g mol-1'
    u = re.sub(r"\s+", " ", u).strip()
    return u.lower()


# (cleaned unit spelling) -> (canonical unit, multiplier, offset)
_UNIT_TABLE: dict[str, tuple[str, float, float]] = {}


def _reg(canon: str, mult: float, *spellings: str, offset: float = 0.0) -> None:
    for s in spellings:
        _UNIT_TABLE[_clean_unit(s)] = (canon, mult, offset)


_reg(G_PER_MOL, 1.0, "g/mol", "g mol-1", "g mol -1", "g.mol-1", "g/mole", "da", "dalton", "daltons", "g mol−1")
_reg(G_PER_MOL, 1e3, "kg/mol", "kg mol-1", "kg mol -1", "kg.mol-1", "kda", "kg/mole")
_reg(G_PER_MOL, 1e6, "mda")
_reg(KELVIN, 1.0, "k", "kelvin")
_reg(KELVIN, 1.0, "°c", "℃", "oc", "° c", "deg c", "degc", "celsius", offset=273.15)
_reg(PA_S, 1.0, "pa s", "pas", "pa.s", "pa-s")
_reg(PA_S, 1e-3, "mpa s", "mpas", "mpa.s", "cp", "centipoise")
_reg(PA_S, 1e3, "kpa s", "kpas", "kpa.s")
_reg(PA_S, 0.1, "p", "poise")
_reg(PA, 1.0, "pa")
_reg(PA, 1e3, "kpa")
_reg(PA, 1e6, "mpa", "n/mm2", "n mm-2")
_reg(PA, 1e9, "gpa")
_reg(G_PER_CM3, 1.0, "g/cm3", "g cm-3", "g/cc", "g/ml", "g ml-1", "g.cm-3")
_reg(G_PER_CM3, 1e-3, "kg/m3", "kg m-3")
_reg(PERCENT, 1.0, "%", "percent")
_reg(DL_PER_G, 1.0, "dl/g", "dl g-1")
_reg(DL_PER_G, 0.01, "ml/g", "ml g-1", "cm3/g", "cm3 g-1")
_reg(K_PER_MIN, 1.0, "k/min", "°c/min", "°c min-1", "k min-1", "℃/min")
_reg(RAD_PER_S, 1.0, "rad/s", "rad s-1")
_reg(HZ, 1.0, "hz")
_reg(PER_S, 1.0, "1/s", "s-1")
_reg(MJ_PER_M3, 1.0, "mj/m3", "mj m-3")
_reg(J_PER_M2, 1.0, "j/m2", "j m-2")
_reg(J_PER_M2, 1e3, "kj/m2", "kj m-2")

# NOTE: "mpa s" is milli-pascal-second in practice; "MPa" alone is mega-pascal. Lower-casing
# merges them, so the original case is checked first:
_CASE_SENSITIVE = {"MPa": (PA, 1e6), "mPa": (PA, 1e-3), "MDa": (G_PER_MOL, 1e6), "mPa s": (PA_S, 1e-3), "mPa·s": (PA_S, 1e-3), "MPa s": (PA_S, 1e6)}


@dataclass
class UnitResult:
    canonical: str | None
    multiplier: float = 1.0
    offset: float = 0.0
    recognized: bool = False


def parse_unit(unit: str | None) -> UnitResult:
    if not unit or not unit.strip():
        return UnitResult(None)
    raw = unit.strip().strip("()[]{} ,;/").replace("º", "°")  # ordinal º is a common typo for °
    if re.match(r"^%(\s|$)", raw):
        return UnitResult(PERCENT, 1.0, 0.0, True)  # '% Initial Length', '% strain'
    raw_sp = re.sub(r"\s+", " ", raw.replace("·", " ").replace("⋅", " "))
    for k, (canon, mult) in _CASE_SENSITIVE.items():
        if raw_sp == k.replace("·", " "):
            return UnitResult(canon, mult, 0.0, True)
    hit = _UNIT_TABLE.get(_clean_unit(raw.replace("∙", " ")))
    if hit:
        return UnitResult(hit[0], hit[1], hit[2], True)
    return UnitResult(None)


def convert(value: float, unit: str | None) -> tuple[float | None, str | None]:
    """Convert to the canonical unit. Returns (None, None) for unrecognized units — never guesses."""
    u = parse_unit(unit)
    if not u.recognized:
        return None, None
    return value * u.multiplier + u.offset, u.canonical


# ------------------------------------------------------------------------------------------------
# value text parsing
# ------------------------------------------------------------------------------------------------

MISSING_TOKENS = {"", "-", "–", "—", "n.d.", "nd", "n/a", "na", "n.a.", "—a", "/", "×", "nm", "n.m.", "*"}
_FOOTNOTE = re.compile(r"(?:\^\{?[a-z*†‡§¶#]{1,2}\}?|[*†‡§¶#]+)$", re.I)
_NUM = r"(?:[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\d,])|[-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?)"


@dataclass
class ParsedValue:
    value: float | None
    value_max: float | None = None
    qualifier: str = "="
    uncertainty: float | None = None
    unit: str | None = None
    raw: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.value is not None


def _num(s: str) -> float:
    if "," in s and "." not in s:
        # '35,000' / '1,168,000' are thousands groups; '1,5' / '12,75' are decimal commas
        s = s.replace(",", "") if re.fullmatch(r"[-+]?\d{1,3}(?:,\d{3})+", s) else s.replace(",", ".")
    else:
        s = s.replace(",", "")
    return float(s)


_FOOTNOTE_ANYWHERE = re.compile(r"\^\{\s*(?:[a-z]{1,2}|[*†‡§¶#]+|)\s*\}", re.I)
_DIGIT_GROUPS = re.compile(r"(?<![\d.])(\d{1,3})((?:[   \xa0]\d{3})+)(?![\d])")


def normalize_text(t: str) -> str:
    t = (t.replace("−", "-").replace(" ", " ").replace(" ", " ").replace("\xa0", " ")
         .replace(" x 10", " × 10").replace(" X 10", " × 10").replace("*10", "× 10"))
    t = _FOOTNOTE_ANYWHERE.sub("", t)  # footnote markers '^{a}', '^{*}', empty '^{}'
    t = _DIGIT_GROUPS.sub(lambda m: m.group(1) + re.sub(r"\s", "", m.group(2)), t)  # '20 307' -> '20307'
    return re.sub(r"\s+", " ", t).strip()


def parse_value_text(text: str) -> ParsedValue:
    """Parse cell/passage value text, e.g. '45.2', '45.2 ± 1.3', '1.2 × 10^5 g/mol', '100–105 °C', '<5'."""
    raw = text
    t = normalize_text(text)
    t = _FOOTNOTE.sub("", t).strip()
    if t.lower() in MISSING_TOKENS:
        return ParsedValue(None, raw=raw, notes=["missing_token"])
    qualifier = "="
    m = re.match(r"^(<=|>=|≤|≥|<|>|~|≈|ca\.)\s*", t)
    if m:
        qualifier = {"≤": "<=", "≥": ">=", "≈": "~", "ca.": "~"}.get(m.group(1), m.group(1))
        t = t[m.end():]
    # scientific notation 'a × 10^b' (also '10^{b}', '10 b' after lost superscripts is NOT accepted)
    sci = re.match(rf"^({_NUM})\s*[×x]\s*10\s*\^\s*\{{?\s*([-+]?\d+)\s*\}}?\s*(.*)$", t)
    if sci:
        val = _num(sci.group(1)) * 10 ** int(sci.group(2))
        return ParsedValue(val, qualifier=qualifier, unit=sci.group(3).strip() or None, raw=raw)
    if re.match(rf"^({_NUM})\s*[×x]\s*10\s*(\d+)\b", t):
        return ParsedValue(None, raw=raw, notes=["ambiguous_lost_superscript"])
    rng = re.match(rf"^({_NUM})\s*(?:–|-|to)\s*({_NUM})\s*(.*)$", t)
    if rng and not t.startswith("-"):
        a, b = _num(rng.group(1)), _num(rng.group(2))
        if b >= a:
            return ParsedValue(a, value_max=b, qualifier="range", unit=rng.group(3).strip() or None, raw=raw)
    pm = re.match(rf"^({_NUM})\s*(?:±|\+/-|\+-)\s*({_NUM})\s*(.*)$", t)
    if pm:
        return ParsedValue(_num(pm.group(1)), qualifier=qualifier, uncertainty=abs(_num(pm.group(2))),
                           unit=pm.group(3).strip() or None, raw=raw)
    plain = re.match(rf"^({_NUM})\s*(.*)$", t)
    if plain:
        rest = plain.group(2).strip()
        # paren uncertainty '45.2 (1.3)' is ambiguous (SD? n?) — keep value, note it
        notes = []
        if re.match(r"^[\(\[]\s*" + _NUM + r"\s*[\)\]]$", rest):
            notes.append("bracketed_secondary_number_ignored")  # e.g. 'Mn [Đ]' = '75.1 [1.09]'
            rest = ""
        return ParsedValue(_num(plain.group(1)), qualifier=qualifier, unit=rest or None, raw=raw, notes=notes)
    return ParsedValue(None, raw=raw, notes=["unparseable"])


def finite(x: float | None) -> bool:
    return x is not None and math.isfinite(x)
