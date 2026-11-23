from polyextract.discovery.models import Decision, DiscoveryCandidate
from polyextract.schemas.paper import PaperMetadataSchema


def cand(title, abstract=None, dtype="article", year=2005, snippets=()):
    return DiscoveryCandidate(
        paper=PaperMetadataSchema(paper_id="doi:10.5555/f", title=title, abstract=abstract,
                                  document_type=dtype, publication_year=year),
        snippets=list(snippets),
    )


def test_accept_core_signals(filter_engine):
    d = filter_engine.evaluate(cand(
        "TEST FIXTURE: polystyrene",
        "Narrow-dispersity polystyrene samples (Mn 2–200 kg/mol) were characterised by SEC; Tg was measured by DSC.",
    ))
    assert d.decision is Decision.ACCEPT
    assert {"polymer", "molecular_weight", "mwd", "property", "amorphous"} <= set(d.groups_matched)
    assert any(r.rule_id == "R4" and r.passed for r in d.rules)
    assert d.reasons and d.reasons[0].startswith("Accepted")


def test_case_sensitive_abbreviations(filter_engine):
    # 'mn'/'tg' inside ordinary words must not match; 'Mn' / 'Tg' tokens must.
    m = filter_engine.match_terms(cand("TEST FIXTURE: column tgif polymer"))
    assert not any(x.term == r"\bM[nwz]\b" or x.term == r"\bT_?g\b" for x in m)
    m2 = filter_engine.match_terms(cand("TEST FIXTURE: Mn and Tg of a polymer"))
    assert {x.group for x in m2} >= {"molecular_weight", "property"}


def test_reject_document_type(filter_engine):
    d = filter_engine.evaluate(cand("Erratum: molecular weight and Tg of polystyrene", dtype="erratum"))
    assert d.decision is Decision.REJECT and d.rules[-1].rule_id == "R1" and not d.rules[-1].passed


def test_reject_year(filter_engine):
    d = filter_engine.evaluate(cand("Polymer Mn Tg", "polymer Mn Tg", year=1900))
    assert d.decision is Decision.REJECT and "year" in d.reasons[0]


def test_reject_non_polymer(filter_engine):
    d = filter_engine.evaluate(cand("TEST FIXTURE: steel fatigue", "Tensile strength of steel alloys."))
    assert d.decision is Decision.REJECT and "no polymer" in d.reasons[0]


def test_review_partial(filter_engine):
    d = filter_engine.evaluate(cand("TEST FIXTURE: polymer synthesis", "A new copolymer with high molecular weight."))
    assert d.decision is Decision.REVIEW and d.needs_review
    assert "missing property" in d.reasons[0]


def test_review_when_abstract_missing(filter_engine):
    d = filter_engine.evaluate(cand("TEST FIXTURE: a polymer study"))
    assert d.decision is Decision.REVIEW
    assert any("abstract unavailable" in r for r in d.reasons)


def test_review_simulation_soft_flag(filter_engine):
    d = filter_engine.evaluate(cand(
        "TEST FIXTURE", "Molecular dynamics simulation of polystyrene: molecular weight dependence of Tg."
    ))
    assert d.decision is Decision.REVIEW and "simulation" in d.reasons[0]


def test_semicrystalline_without_amorphous_goes_to_review(filter_engine):
    d = filter_engine.evaluate(cand("TEST FIXTURE", "Polyethylene molecular weight and tensile strength."))
    assert d.decision is Decision.REVIEW


def test_snippet_counts_but_is_attributed(filter_engine):
    d = filter_engine.evaluate(cand("TEST FIXTURE polymer", snippets=["dispersity and viscosity of the melt"]))
    assert any(m.field == "snippet" for m in d.matches)


def test_decision_is_deterministic(filter_engine):
    c = cand("TEST FIXTURE: PMMA", "PMMA Mw 100 kg/mol, glass transition.")
    assert filter_engine.evaluate(c).model_dump() == filter_engine.evaluate(c).model_dump()


def web_cand(title, abstract, url, doi=None):
    return DiscoveryCandidate(paper=PaperMetadataSchema(
        paper_id="title:x", title=title, abstract=abstract, doi=doi, source_url=url, retrieval_sources=["websearch"]))


def test_r0_rejects_non_scholarly_web_page(filter_engine):
    # Regression from live run 2026-11-05: a Wikipedia page matched all term groups and was ACCEPTED.
    d = filter_engine.evaluate(web_cand("Flory-Fox equation", "polymer Mn glass transition Tg",
                                        "https://en.wikipedia.org/wiki/Flory-Fox_equation"))
    assert d.decision is Decision.REJECT and "non-scholarly" in d.reasons[0]


def test_r0_caps_doi_less_web_hit_at_review(filter_engine):
    d = filter_engine.evaluate(web_cand("TEST FIXTURE polymer", "polystyrene Mn dispersity Tg DSC",
                                        "https://journal.x.test/article/123"))
    assert d.decision is Decision.REVIEW
    assert not next(r for r in d.rules if r.rule_id == "R0").passed
    assert not next(r for r in d.rules if r.rule_id == "R4").passed  # explanation consistent with decision


def test_r0_passes_with_doi(filter_engine):
    d = filter_engine.evaluate(web_cand("TEST FIXTURE polymer", "polystyrene Mn dispersity Tg DSC",
                                        "https://journal.x.test/a", doi="10.5555/z"))
    assert d.decision is Decision.ACCEPT
