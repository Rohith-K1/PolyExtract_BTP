"""Deterministic (rule-based) extraction from parsed tables. Extraction path 1 of 2 (the LLM is path 2).

Pipeline per document:
  classify every column header -> (role, field, unit, multiplier)
  each data row of a table with a sample-label column becomes a sample observation
  rows are linked across tables by identical sample label (flagged as cross-table linkage)
  every value carries Evidence = the verbatim table row (+ caption) and its reported cell text

Deliberately conservative: theoretical/target columns are skipped, ambiguous header multipliers
leave the value unnormalized (-> review), and unidentified polymers are flagged rather than guessed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field as dc_field

from polyextract.acquisition.document import DocumentModel, Table
from polyextract.normalization.units import DIMENSIONLESS, convert, parse_unit, parse_value_text
from polyextract.schemas import (
    AmorphousStatus,
    Evidence,
    ExperimentalConditions,
    ExtractionInfo,
    ExtractionMethod,
    LocationType,
    MolecularCharacteristicsSchema,
    MWTechnique,
    PolymerSampleSchema,
    PropertyMeasurementSchema,
    PropertyName,
    Qualifier,
    Quantity,
    TextualValue,
)

EXTRACTOR_VERSION = "table-rules-v1"

# ---- header classification ---------------------------------------------------------------------
# (field, regex, flags). Order matters: Mw/Mn ratio before Mw; plateau before storage modulus.
DESCRIPTOR_RULES: list[tuple[str, str, int]] = [
    ("dispersity", r"Đ|\bPDI\b|\bM\s*w\s*/\s*M\s*n\b|dispersity|polydispersity|\bD\s*M\b", re.I),
    ("mn", r"\bM\s*n\b|number[- ]average", 0),
    ("mw", r"\bM\s*w\b|weight[- ]average", 0),
    ("mz", r"\bM\s*z\b|z[- ]average", 0),
    ("dpn", r"\bDP\s*n?\b|\bX\s*n\b|degree of polymeri[sz]ation", 0),
    ("dpw", r"\bDP\s*w\b|\bX\s*w\b", 0),
    ("mwd_skewness", r"skewness", re.I),
    ("mwd_kurtosis", r"kurtosis", re.I),
]
PROPERTY_RULES: list[tuple[PropertyName, str, int]] = [
    (PropertyName.GLASS_TRANSITION_TEMPERATURE, r"\bT\s*g\b|glass transition", 0),
    (PropertyName.ZERO_SHEAR_VISCOSITY, r"η\s*_?\{?\s*0|zero[- ]shear", re.I),
    (PropertyName.INTRINSIC_VISCOSITY, r"\[\s*η\s*\]|intrinsic viscosity", re.I),
    (PropertyName.COMPLEX_VISCOSITY, r"η\s*\*|complex viscosity", re.I),
    (PropertyName.DECOMPOSITION_TEMPERATURE, r"\bT\s*d\b|\bT\s*d?\s*,?\s*(?:5|10)\s*%|decomposition temp", 0),
    (PropertyName.DENSITY, r"\bdensity\b|^\s*ρ\b", re.I),
    (PropertyName.TENSILE_STRENGTH, r"tensile strength|σ\s*b\b|σ\s*max|ultimate (?:tensile )?strength|stress at break", re.I),
    (PropertyName.ELONGATION_AT_BREAK, r"elongation|ε\s*b\b|strain at break", re.I),
    (PropertyName.YOUNGS_MODULUS, r"young|elastic modulus|tensile modulus", re.I),
    (PropertyName.PLATEAU_MODULUS, r"G\s*N\s*\^?\{?\s*0|plateau modulus", re.I),
    (PropertyName.STORAGE_MODULUS, r"G\s*['′]|storage modulus", re.I),
    (PropertyName.LOSS_MODULUS, r"G\s*(?:''|″)|loss modulus", re.I),
    (PropertyName.TOUGHNESS, r"toughness|impact strength", re.I),
]
NOT_MASS_DENSITY = re.compile(r"cross-?link|entangle|energy|power|charge|current|linear|areal|surface|number|grafting|"
                              r"optical|packing|defect|dislocation|flux|probability|spectral|state", re.I)
LABEL_HEADER = re.compile(r"sample|entry|polymer|code|run|name|^\s*#|^\s*no\.?\s*$|designation|material", re.I)
NOT_MEASURED = re.compile(r"theo|calc|target|\bth\b|,\s*th|feed|expected|design|\bexp\.?\s*$", re.I)
MW_TECHNIQUE_IN_HEADER = [
    (re.compile(r"MALS|LS\b|light scattering|abs", re.I), MWTechnique.SEC_MALS),
    (re.compile(r"NMR", re.I), MWTechnique.NMR_END_GROUP),
    (re.compile(r"MALDI", re.I), MWTechnique.MALDI_TOF),
    (re.compile(r"SEC|GPC", re.I), MWTechnique.SEC_CONVENTIONAL),
]

# Canonical family -> pattern (used to name the polymer from caption/title, with evidence)
FAMILIES: list[tuple[str, str]] = [
    ("poly(methyl methacrylate)", r"poly\s?\(methyl methacrylate\)|\bPMMA\b"),
    ("polystyrene", r"\bpolystyrene|\bPS\b"),
    ("polycarbonate", r"polycarbonate|\bBPA-PC\b"),
    ("poly(vinyl chloride)", r"poly\s?\(vinyl chloride\)|\bPVC\b"),
    ("polyisobutylene", r"polyisobutylene|\bPIB\b"),
    ("polyisoprene", r"polyisoprene|\bPI\b"),
    ("polybutadiene", r"polybutadiene|\bPB[dD]?\b"),
    ("poly(dimethylsiloxane)", r"poly\s?\(dimethylsiloxane\)|\bPDMS\b"),
    ("poly(lactic acid)", r"poly\s?\(lactic acid\)|polylactide|\bPLL?A\b|\bPDLLA\b"),
    ("poly(vinyl acetate)", r"poly\s?\(vinyl acetate\)|\bPVAc\b"),
    ("poly(n-butyl acrylate)", r"poly\s?\(n-butyl acrylate\)|\bPnBA\b"),
    ("poly(tert-butyl acrylate)", r"poly\s?\(tert-butyl acrylate\)|\bPtBA\b"),
    ("poly(methyl acrylate)", r"poly\s?\(methyl acrylate\)|\bPMA\b"),
    ("polysulfone", r"polysulfone|\bPSU\b"),
    ("poly(ethylene oxide)", r"poly\s?\(ethylene (?:oxide|glycol)\)|\bPEO\b|\bPEG\b"),
    ("poly(ε-caprolactone)", r"polycaprolactone|poly\s?\(ε-caprolactone\)|\bPCL\b"),
    ("polyethylene", r"\bpolyethylene\b|\bHDPE\b|\bLDPE\b"),
    ("polypropylene", r"\bpolypropylene\b|\biPP\b"),
]


@dataclass
class ColumnRole:
    index: int
    header: str
    role: str  # 'label' | 'descriptor' | 'property' | 'skipped'
    field: str | None = None  # descriptor name or PropertyName value
    unit: str | None = None
    multiplier: float = 1.0
    issues: list[str] = dc_field(default_factory=list)
    technique: MWTechnique | None = None


def split_header_unit(header: str) -> tuple[str, str | None, float, list[str]]:
    """'Mn × 10^{-3} (g/mol)' -> ('Mn', 'g/mol', 1e3, []);  'Tg/°C' -> ('Tg', '°C', 1, [])."""
    issues: list[str] = []
    h = header.replace("−", "-").strip()
    unit = None
    # Several bracket groups are common: 'Mw (g/mol) (SEC)', 'Tg (°C) (PMMA)', "G′(ω) (Pa)". Pick the group that
    # is a recognized unit (right to left); a group that is a technique, polymer, symbol or number is NOT a unit.
    groups = list(re.finditer(r"[\(\[]([^\(\)\[\]]*)[\)\]]", h))
    m = None
    for g in reversed(groups):
        inner = re.sub(r"(×\s*)?10\s*\^\s*\{?\s*[-+]?\d+\s*\}?", "", g.group(1)).strip(" ×x·")
        if inner and parse_unit(inner).recognized:
            m = g
            break
    if m is None and groups:
        last = groups[-1]
        inner = last.group(1).strip()
        not_unit = (re.fullmatch(r"[A-Za-zα-ωΑ-Ω]{1,2}|\d+(?:\.\d+)?|η|", inner)
                    or re.fullmatch(r"(?:SEC|GPC|NMR|MALDI|DSC|DMA|TGA|MALS|LS|RI|[A-Z]{2,6})", inner))
        if not not_unit and h[last.end():].strip(" ^{}abcdefgh*†‡§") == "":
            m = last
    if m is not None:
        unit, label = m.group(1).strip(), " ".join((h[: m.start()] + h[m.end():]).split())
    elif "/" in h and not re.search(r"M\s*w\s*/\s*M\s*n", h):
        label, unit = [s.strip() for s in h.split("/", 1)]
        unit = unit.strip("() ")
    elif "," in h:
        label, unit = [s.strip() for s in h.rsplit(",", 1)]
        if not parse_unit(unit).recognized and not re.search(r"10\s*\^", unit):
            label, unit = h, None
    else:
        label = h
    mult = 1.0
    if unit:
        um = re.search(r"(×\s*)?10\s*\^\s*\{?\s*([-+]?\d+)\s*\}?", unit)
        if um:
            exp = int(um.group(2))
            unit = (unit[: um.start()] + unit[um.end():]).strip(" ×x·")
            if exp > 0:
                mult = 10.0**exp
            else:
                issues.append("ambiguous_multiplier_sign_in_unit")
                mult = float("nan")
    lm = re.search(r"[×x]\s*10\s*\^\s*\{?\s*([-+]?\d+)\s*\}?", label)
    if lm:
        exp = int(lm.group(1))
        label = (label[: lm.start()]).strip()
        mult = 10.0 ** (-exp)  # 'Mn × 10^-3' = displayed value -> Mn = value × 10^3
        if exp > 0:
            issues.append("unusual_positive_multiplier_on_label")
    label = re.sub(r"\^\{[a-z*†‡§]{1,2}\}", "", label).strip()  # footnote markers
    return label, unit or None, mult, issues


def classify_column(j: int, header: str) -> ColumnRole | None:
    label, unit, mult, issues = split_header_unit(header)
    for fname, pat, flags in DESCRIPTOR_RULES:
        if re.search(pat, label, flags):
            if NOT_MEASURED.search(header):
                return ColumnRole(j, header, "skipped", fname, issues=["theoretical_or_target_value"])
            tech = next((t for rx, t in MW_TECHNIQUE_IN_HEADER if rx.search(header)), None)
            return ColumnRole(j, header, "descriptor", fname, unit, mult, issues, tech)
    for pname, pat, flags in PROPERTY_RULES:
        if re.search(pat, label, flags):
            if pname is PropertyName.DENSITY and NOT_MASS_DENSITY.search(label):
                return ColumnRole(j, header, "skipped", pname.value, issues=["not_mass_density"])
            return ColumnRole(j, header, "property", pname.value, unit, mult, issues)
    if LABEL_HEADER.search(label):
        return ColumnRole(j, header, "label")
    return None


# ---- context (technique, calibration, polymer identity) from caption / footnotes / text ---------

def _evidence(doc: DocumentModel, table: Table | None, text: str, loc: LocationType = LocationType.TABLE) -> Evidence:
    return Evidence(
        paper_id=doc.paper_id, doi=doc.doi, document_ref=doc.source_url, location_type=loc,
        table_id=(table.label or table.table_id) if table else None, page=table.page if table else None,
        source_text=text[:2000],
    )


def detect_mw_context(text: str) -> dict:
    out: dict = {}
    if re.search(r"MALS|multi-?angle|light scattering|absolute molecular", text, re.I):
        out["technique"] = MWTechnique.SEC_MALS
    elif re.search(r"\bSEC\b|\bGPC\b|size[- ]exclusion|gel permeation", text, re.I):
        out["technique"] = MWTechnique.SEC_CONVENTIONAL
    elif re.search(r"MALDI", text):
        out["technique"] = MWTechnique.MALDI_TOF
    m = re.search(r"((?:linear |narrow(?:ly)?[- ]dispersed? )?(?:polystyrene|PS|PMMA|poly\(methyl methacrylate\)|PEG|PEO)\s+(?:standards?|calibration))", text, re.I)
    if m:
        out["calibration"] = m.group(1)
    e = re.search(r"\b(?:in|eluent:?|using)\s+(THF|tetrahydrofuran|DMF|chloroform|CHCl3|toluene|TCB|trichlorobenzene|DMAc|HFIP|water)\b", text, re.I)
    if e:
        out["eluent"] = e.group(1)
    return out


def detect_property_context(text: str) -> dict:
    out: dict = {}
    t = re.search(r"\b(DSC|DMA|DMTA|TGA|rheometr\w*|oscillatory shear|tensile test\w*)\b", text, re.I)
    if t:
        out["technique"] = t.group(1)
    hr = re.search(r"(\d+(?:\.\d+)?)\s*(°C|K|℃)\s*(?:/|per\s*|·?\s*)min", text)
    if hr:
        out["heating_rate"] = (float(hr.group(1)), f"{hr.group(2)}/min", hr.group(0))
    d = re.search(r"\b(midpoint|onset|inflection|half[- ]height|tan\s*δ\s*peak|peak of tan)", text, re.I)
    if d:
        out["definition"] = d.group(1)
    return out


_CALIBRATION_PHRASE = re.compile(
    r"(?:relative to|against|vs\.?|versus|calibrated (?:with|using|against)|using)?\s*(?:linear |narrow\w* )?"
    r"(?:polystyrene|PS|PMMA|poly\s?\(methyl methacrylate\)|PEG|PEO)\s+(?:standards?|calibration|equivalents?)", re.I)


def identify_family(*texts: str) -> tuple[str | None, int]:
    """First family named in the texts, ignoring calibration phrases ('vs. PS standards')."""
    for i, t in enumerate(texts):
        t = _CALIBRATION_PHRASE.sub(" ", t or "")
        for fam, pat in FAMILIES:
            if t and family_regex(fam).search(t):
                return fam, i
    return None, -1


def family_regex(fam: str) -> re.Pattern:
    """Full names case-insensitive; upper-case abbreviations (\\bPS\\b) stay case-sensitive."""
    pat = dict(FAMILIES)[fam]
    parts = [p if re.match(r"\\b[A-Z]", p) else f"(?i:{p})" for p in pat.split("|")]
    return re.compile("|".join(parts))


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")[:40] or "x"


def _quantity(cell: str, col: ColumnRole, ev: Evidence, canonical_dimensionless: bool) -> tuple[Quantity | None, list[str]]:
    pv = parse_value_text(cell)
    issues = list(col.issues) + pv.notes
    if not pv.ok:
        return None, issues
    unit = pv.unit or col.unit
    nv = nmax = None
    nunit = None
    mult = col.multiplier
    if canonical_dimensionless:
        unit = None  # Đ, DP, moments: header text like '(Mw/Mn)' is a definition, not a unit
        if mult == mult:  # not NaN
            nv, nunit = pv.value * mult, DIMENSIONLESS
    elif mult == mult and unit:
        nv, nunit = convert(pv.value * mult, unit)
        if pv.value_max is not None and nv is not None:
            nmax, _ = convert(pv.value_max * mult, unit)
        if nv is None:
            issues.append(f"unrecognized_unit:{unit}")
    elif not unit:
        issues.append("unit_missing")
    qual = Qualifier(pv.qualifier) if pv.qualifier in {q.value for q in Qualifier} else Qualifier.EQ
    q = Quantity(
        value=pv.value, value_max=pv.value_max, qualifier=qual, uncertainty=pv.uncertainty, unit=unit,
        reported_text=cell.strip(), normalized_value=nv, normalized_value_max=nmax, normalized_unit=nunit,
        evidence=[ev],
    )
    if mult == mult and mult != 1.0:
        issues.append(f"header_multiplier_applied:{mult:g}")
    return q, issues


# ---- main ---------------------------------------------------------------------------------------

@dataclass
class ExtractionResult:
    samples: list[PolymerSampleSchema] = dc_field(default_factory=list)
    molecular: list[MolecularCharacteristicsSchema] = dc_field(default_factory=list)
    measurements: list[PropertyMeasurementSchema] = dc_field(default_factory=list)
    issues: dict[str, list[str]] = dc_field(default_factory=dict)  # record id -> issue codes
    tables_used: list[str] = dc_field(default_factory=list)
    tables_skipped: dict[str, str] = dc_field(default_factory=dict)


def extract_tables(doc: DocumentModel, run_id: str | None = None) -> ExtractionResult:
    res = ExtractionResult()
    info = ExtractionInfo(method=ExtractionMethod.RULE_BASED, model=EXTRACTOR_VERSION, run_id=run_id)
    body_text = doc.full_text()
    samples: dict[str, dict] = {}  # label key -> accumulated observation

    for t in doc.tables:
        headers = t.column_headers()
        if not headers or not t.rows:
            res.tables_skipped[t.table_id] = "no header or rows"
            continue
        roles = [classify_column(j, h) for j, h in enumerate(headers)]
        useful = [r for r in roles if r and r.role in ("descriptor", "property")]
        if not useful:
            res.tables_skipped[t.table_id] = "no molecular-weight or property columns"
            continue
        label_col = next((r.index for r in roles if r and r.role == "label"), None)
        if label_col is None and (roles[0] is None):
            label_col = 0  # first unclassified column is the conventional sample column
        if label_col is None:
            res.tables_skipped[t.table_id] = "no sample-label column (cannot link rows to samples)"
            continue
        res.tables_used.append(t.table_id)
        context = " ".join([t.caption, *t.footnotes])
        mwctx = detect_mw_context(context + " " + " ".join(headers))
        pctx = detect_property_context(context + " " + " ".join(headers))
        loc = LocationType.SUPPLEMENTARY_TABLE if t.is_supplementary else LocationType.TABLE
        for i, row in enumerate(t.rows):
            if label_col >= len(row) or not row[label_col].strip():
                continue
            label = re.sub(r"\^\{[^}]*\}", "", row[label_col]).strip()
            if not label or len(set(row)) == 1:  # spanning sub-header row
                continue
            ev = _evidence(doc, t, (t.caption[:300] + " || " if t.caption else "") + t.row_text(i), loc)
            key = label.casefold()
            if key in samples and t.table_id in samples[key]["tables"]:
                # same label twice in ONE table -> likely different samples (sub-blocks); never merge
                key = f"{key}@{t.table_id}#r{i}"
                label_disp = f"{label} [{t.label or t.table_id}, row {i + 1}]"
            else:
                label_disp = label
            s = samples.setdefault(key, {"label": label_disp, "desc": {}, "props": [], "tables": set(),
                                         "mwctx": {}, "ctx_ev": [], "caption": t.caption})
            if "@" in key:
                s.setdefault("skipped", set()).add("duplicate_label_in_table:repeated label disambiguated by row")
            s["tables"].add(t.table_id)
            for r in useful:
                if r.index >= len(row):
                    continue
                cell = row[r.index]
                if r.role == "descriptor":
                    q, iss = _quantity(cell, r, ev, canonical_dimensionless=r.field not in ("mn", "mw", "mz"))
                    if q is not None and r.field not in s["desc"]:
                        s["desc"][r.field] = (q, iss, r.technique)
                        if mwctx:
                            s["mwctx"] = {**mwctx, "ev": _evidence(doc, t, context or t.row_text(i), loc)}
                else:
                    q, iss = _quantity(cell, r, ev, canonical_dimensionless=False)
                    if q is not None:
                        s["props"].append((r, q, iss, pctx, t, i))
            for r in roles:
                if r and r.role == "skipped":
                    s.setdefault("skipped", set()).add(f"{r.header}:{','.join(r.issues)}")

    if not samples:
        return res

    fam, where = identify_family(*(s["caption"] for s in samples.values()), doc.title or "")
    fam_text = None
    if fam:
        fam_text = next((s["caption"] for s in samples.values() if family_regex(fam).search(s["caption"] or "")),
                        doc.title or "")
    amorph_ev = None
    if fam:
        for p in doc.paragraphs:
            if re.search(r"\bamorphous\b", p.text, re.I) and family_regex(fam).search(p.text):
                amorph_ev = Evidence(paper_id=doc.paper_id, doi=doc.doi, document_ref=doc.source_url,
                                     location_type=LocationType.TEXT, section=p.section, source_text=p.text[:1500])
                break

    used_ids: set[str] = set()
    for key, s in samples.items():
        sid = f"{doc.paper_id}#s:{_slug(s['label'])}"
        if sid in used_ids:  # distinct labels that slug identically ("PS-1" vs "PS 1") stay distinct samples
            sid += "-" + hashlib.sha1(key.encode()).hexdigest()[:6]
        used_ids.add(sid)
        issues: list[str] = []
        if len(s["tables"]) > 1:
            issues.append("cross_table_label_linkage")
        if re.fullmatch(r"\d{1,2}|[a-z]", s["label"], re.I):
            issues.append("generic_sample_label")
        if fam is None:
            issues.append("polymer_identity_unresolved")
        name_ev = []
        if fam and fam_text:
            name_ev = [_evidence(doc, None, fam_text, LocationType.TABLE if where < len(samples) else LocationType.METADATA)]
        sample = PolymerSampleSchema(
            sample_id=sid, paper_id=doc.paper_id, sample_label=s["label"],
            polymer_name=fam or f"unidentified ({s['label']})", polymer_family=fam,
            amorphous_status=AmorphousStatus.SUPPORTED_BY_EVIDENCE if amorph_ev else AmorphousStatus.UNKNOWN,
            amorphous_evidence=[amorph_ev] if amorph_ev else [], identity_evidence=name_ev, extraction=info,
        )
        res.samples.append(sample)
        res.issues[sid] = issues + [f"skipped_column:{x}" for x in sorted(s.get("skipped", []))]

        if s["desc"]:
            mwctx = s["mwctx"]
            techs = [tq for (_, _, tq) in s["desc"].values() if tq]
            tech = techs[0] if techs else mwctx.get("technique", MWTechnique.NOT_REPORTED)
            cev = mwctx.get("ev")
            mc = MolecularCharacteristicsSchema(
                mc_id=f"{sid}#mc", sample_id=sid, paper_id=doc.paper_id,
                **{f: q for f, (q, _, _) in s["desc"].items()},
                technique=tech,
                calibration_standard=TextualValue(text=mwctx["calibration"], evidence=[cev]) if "calibration" in mwctx and cev else None,
                eluent=TextualValue(text=mwctx["eluent"], evidence=[cev]) if "eluent" in mwctx and cev else None,
                extraction=info,
            )
            res.molecular.append(mc)
            res.issues[mc.mc_id] = sorted({i for (_, iss, _) in s["desc"].values() for i in iss})

        for n, (r, q, iss, pctx, t, ri) in enumerate(s["props"]):
            cond = ExperimentalConditions()
            cev = _evidence(doc, t, " ".join([t.caption, *t.footnotes]) or t.row_text(0))
            if "heating_rate" in pctx:
                v, u, txt = pctx["heating_rate"]
                nv, nu = convert(v, u)
                cond.heating_rate = Quantity(value=v, unit=u, reported_text=txt, normalized_value=nv,
                                             normalized_unit=nu, evidence=[cev])
            m = PropertyMeasurementSchema(
                measurement_id=f"{sid}#m:{_slug(t.table_id)}:r{ri}c{r.index}", sample_id=sid, paper_id=doc.paper_id,
                property_name=PropertyName(r.field), property_name_reported=r.header or r.field,
                property_definition=pctx.get("definition"), value=q, technique=pctx.get("technique"),
                conditions=cond, extraction=info,
            )
            res.measurements.append(m)
            res.issues[m.measurement_id] = iss
    return res
