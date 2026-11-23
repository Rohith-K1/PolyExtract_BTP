"""Rule-based table extraction on a JATS TEST FIXTURE shaped like real polymer characterization tables."""

from polyextract.acquisition.jats import parse_jats
from polyextract.extraction.table_rules import classify_column, extract_tables, identify_family, split_header_unit
from polyextract.schemas import AmorphousStatus, MWTechnique, PropertyName

JATS = """<?xml version="1.0"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink"><front><article-meta>
<title-group><article-title>TEST FIXTURE: Poly(methyl methacrylate) of varying M<sub>n</sub></article-title></title-group>
<permissions><license xlink:href="http://creativecommons.org/licenses/by/4.0/"/></permissions>
<abstract><p>TEST FIXTURE abstract.</p></abstract></article-meta></front>
<body><sec><title>Results</title><p>The PMMA samples are amorphous and show a single glass transition.</p>
<table-wrap id="tab1"><label>Table 1</label>
<caption><p>Molecular characteristics of PMMA determined by SEC in THF relative to polystyrene standards; T<sub>g</sub> by DSC (midpoint, 10 °C/min).</p></caption>
<table><thead><tr><th>Sample</th><th>M<sub>n,theo</sub> (kg/mol)</th><th>M<sub>n</sub> (kg/mol)</th><th>M<sub>w</sub> (kg/mol)</th><th>Đ</th><th>T<sub>g</sub> (°C)</th></tr></thead>
<tbody>
<tr><td>P1</td><td>10</td><td>9.8</td><td>10.6</td><td>1.08</td><td>112.4<sup>a</sup></td></tr>
<tr><td>P2</td><td>50</td><td>48,500</td><td>55.0</td><td>1.13</td><td>121.0</td></tr>
<tr><td>P3</td><td>100</td><td>n.d.</td><td>–</td><td>–</td><td>123.5</td></tr>
</tbody></table>
<table-wrap-foot><fn><p><sup>a</sup> second heating.</p></fn></table-wrap-foot></table-wrap>
</sec></body></article>""".encode()


def doc():
    return parse_jats(JATS, paper_id="doi:10.5555/test-fixture-jats", doi="10.5555/test-fixture-jats")


def test_jats_parsing_keeps_structure():
    d = doc()
    assert d.title.startswith("TEST FIXTURE: Poly(methyl methacrylate) of varying Mn")
    t = d.tables[0]
    assert t.table_id == "tab1" and t.label == "Table 1"
    assert t.column_headers()[2] == "Mn (kg/mol)" and t.rows[0][5] == "112.4^{a}"
    assert d.license.endswith("by/4.0/") and "second heating" in t.footnotes[0]


def test_header_parsing():
    assert split_header_unit("Mn × 10^{-3} (g/mol)") == ("Mn", "g/mol", 1e3, [])
    assert split_header_unit("Tg/°C")[:2] == ("Tg", "°C")
    assert split_header_unit("Mn (10^{4} g/mol)")[1:3] == ("g/mol", 1e4)
    assert classify_column(0, "Mn,theo (kg/mol)").role == "skipped"
    assert classify_column(0, "Mn,NMR (g/mol)").technique is MWTechnique.NMR_END_GROUP
    assert classify_column(0, "Đ (Mw/Mn)").field == "dispersity"


def test_header_unit_choice_from_live_tables():
    # LIVE 2026-11-05: last bracket group was a technique / polymer / symbol, not the unit
    assert split_header_unit("Mw (g/mol) (SEC)")[:2] == ("Mw (SEC)", "g/mol")
    assert split_header_unit("Tg (°C) (PMMA)")[:2] == ("Tg (PMMA)", "°C")
    assert classify_column(0, "Mw (g/mol) (SEC)").technique is MWTechnique.SEC_CONVENTIONAL
    assert split_header_unit("G′(ω) (Pa)")[1] == "Pa"
    assert split_header_unit("Mn (GPC)")[1] is None
    assert split_header_unit("G′(ω)")[1] is None
    assert classify_column(0, "Crosslink density (mol∙cm^{-3})").role == "skipped"
    assert classify_column(0, "Density (g/cm^{3})").field == "density"


def test_calibration_phrase_does_not_name_the_polymer():
    assert identify_family("PMMA by SEC relative to polystyrene standards")[0] == "poly(methyl methacrylate)"
    assert identify_family("SEC vs. PS standards", "TEST FIXTURE: Poly(methyl methacrylate)")[0] == "poly(methyl methacrylate)"


def test_extraction_end_to_end():
    r = extract_tables(doc())
    assert [s.sample_label for s in r.samples] == ["P1", "P2", "P3"]
    p1 = r.samples[0]
    assert p1.polymer_family == "poly(methyl methacrylate)"
    assert p1.amorphous_status is AmorphousStatus.SUPPORTED_BY_EVIDENCE and p1.amorphous_evidence
    mc1 = next(m for m in r.molecular if m.sample_id == p1.sample_id)
    assert mc1.mn.normalized_value == 9800 and mc1.mn.reported_text == "9.8"
    assert mc1.dispersity.normalized_value == 1.08 and mc1.dispersity.unit is None
    assert mc1.technique is MWTechnique.SEC_CONVENTIONAL
    assert mc1.calibration_standard.text.lower().startswith("polystyrene standards")
    assert mc1.eluent.text == "THF"
    # theoretical column skipped and recorded
    assert any("theoretical" in i for i in r.issues[p1.sample_id])
    # evidence is the verbatim row and contains the reported text
    ev = mc1.mn.evidence[0]
    assert ev.table_id == "Table 1" and "9.8" in ev.source_text
    tg = next(m for m in r.measurements if m.sample_id == p1.sample_id)
    assert tg.property_name is PropertyName.GLASS_TRANSITION_TEMPERATURE
    assert tg.value.value == 112.4 and abs(tg.value.normalized_value - 385.55) < 1e-9
    assert tg.technique == "DSC" and tg.property_definition == "midpoint"
    assert tg.conditions.heating_rate.normalized_value == 10
    # '48,500' with header kg/mol -> 48 500 000 g/mol: implausible, must be caught by validation, not hidden
    mc2 = next(m for m in r.molecular if m.sample_id.endswith("P2"))
    assert mc2.mn.normalized_value == 48_500_000
    # P3 has no molecular data -> no MolecularCharacteristics, but Tg measurement still kept
    assert not any(m.sample_id.endswith("P3") for m in r.molecular)
    assert any(m.sample_id.endswith("P3") for m in r.measurements)
