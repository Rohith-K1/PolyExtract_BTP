"""Source adapters against mocked HTTP payloads shaped like the real APIs (TEST FIXTURE content)."""

import httpx
import pytest

from polyextract.discovery.crossref import CrossrefAdapter
from polyextract.discovery.http import HttpClient
from polyextract.discovery.openalex import OpenAlexAdapter, reconstruct_abstract
from polyextract.discovery.websearch import WebSearchAdapter
from polyextract.exceptions import SourceError


def http(handler):
    return HttpClient("t", transport=httpx.MockTransport(handler), sleep=lambda s: None, min_interval_s=0)


def oa_work(i):
    return {
        "id": f"https://openalex.org/W{i}",
        "doi": f"https://doi.org/10.5555/TEST-FIXTURE-{i}",
        "title": f"TEST FIXTURE {i}: Effect of M<sub>w</sub> on glass transition",
        "publication_year": 2001,
        "type": "article",
        "authorships": [{"author": {"display_name": "A. Tester", "orcid": None}}],
        "primary_location": {"source": {"display_name": "Fixture Journal"}, "landing_page_url": "https://x.test"},
        "abstract_inverted_index": {"Polystyrene": [0], "samples": [1], "were": [2], "studied": [3]},
        "open_access": {"is_oa": True, "oa_url": "https://x.test/pdf", "oa_status": "gold"},
        "best_oa_location": {"license": "cc-by"},
    }


def test_reconstruct_abstract():
    assert reconstruct_abstract({"b": [1], "a": [0], "c": [2, 3]}) == "a b c c"
    assert reconstruct_abstract(None) is None


def test_openalex_paginates_with_cursor_and_maps_fields():
    seen = []

    def h(req):
        seen.append(dict(req.url.params))
        cursor = req.url.params["cursor"]
        if cursor == "*":
            return httpx.Response(200, json={"meta": {"next_cursor": "c2"}, "results": [oa_work(1), oa_work(2)]})
        return httpx.Response(200, json={"meta": {"next_cursor": None}, "results": [oa_work(3)]})

    a = OpenAlexAdapter(http(h), email="me@x.test", api_key="K", per_page=2)
    recs = a.search("q", max_results=10, year_from=1990, year_to=2020)
    assert [r.rank for r in recs] == [1, 2, 3]
    r = recs[0]
    assert r.doi == "10.5555/test-fixture-1" and r.source_id == "W1"
    assert r.title.startswith("TEST FIXTURE 1: Effect of Mw")
    assert r.abstract == "Polystyrene samples were studied"
    assert r.oa_url == "https://x.test/pdf" and r.journal == "Fixture Journal"
    assert seen[0]["filter"] == "publication_year:1990-2020,type:article"
    assert seen[0]["mailto"] == "me@x.test" and seen[0]["api_key"] == "K"


def test_openalex_respects_max_results():
    h = lambda req: httpx.Response(200, json={"meta": {"next_cursor": "n"}, "results": [oa_work(i) for i in range(5)]})
    assert len(OpenAlexAdapter(http(h), per_page=5).search("q", max_results=3)) == 3


def test_openalex_malformed_payload():
    with pytest.raises(SourceError):
        OpenAlexAdapter(http(lambda r: httpx.Response(200, json={"oops": 1}))).search("q", max_results=5)


def cr_item(i):
    return {
        "DOI": f"10.5555/TEST-FIXTURE-{i}",
        "title": [f"TEST FIXTURE {i}"],
        "author": [{"given": "A.", "family": "Tester"}],
        "container-title": ["Fixture Journal"],
        "issued": {"date-parts": [[2001, 5]]},
        "abstract": "<jats:p>Dispersity <jats:italic>Đ</jats:italic> and T<jats:sub>g</jats:sub>.</jats:p>",
        "type": "journal-article",
        "license": [{"URL": "http://creativecommons.org/licenses/by/4.0/", "content-version": "vor"}],
        "link": [{"URL": "https://x.test/full.pdf", "content-type": "application/pdf", "intended-application": "text-mining"}],
    }


def test_crossref_maps_and_strips_jats():
    def h(req):
        # Regression: cursor pagination needs explicit relevance sort or ranking is lost.
        assert req.url.params["sort"] == "relevance" and req.url.params["order"] == "desc"
        if req.url.params["cursor"] == "*":
            return httpx.Response(200, json={"message": {"items": [cr_item(1)], "next-cursor": "n"}})
        return httpx.Response(200, json={"message": {"items": [], "next-cursor": "n"}})

    recs = CrossrefAdapter(http(h), mailto="me@x.test").search("q", max_results=10, year_from=1990)
    assert len(recs) == 1
    r = recs[0]
    assert r.doi == "10.5555/test-fixture-1" and r.publication_year == 2001
    assert r.abstract == "Dispersity Đ and Tg." and r.authors[0].family == "Tester"
    assert r.extra["fulltext_links"][0]["intended"] == "text-mining"


def test_crossref_lookup_404_returns_none():
    a = CrossrefAdapter(http(lambda r: httpx.Response(404, text="Resource not found.")))
    assert a.lookup_doi("10.5555/none") is None


class FakeProvider:
    name = "fake"

    def __init__(self, hits=None, fail=False):
        self.hits, self.fail = hits or [], fail

    def search(self, query, max_results):
        if self.fail:
            raise SourceError("websearch", "blocked")
        return self.hits[:max_results]


def test_websearch_extracts_doi_and_keeps_snippet_separate():
    p = FakeProvider([
        {"title": "TEST FIXTURE hit", "url": "https://pubs.x.test/doi/10.5555/TEST-1", "snippet": "Tg vs Mn ..."},
        {"title": "No DOI hit", "url": "https://blog.x.test/post", "snippet": "polymer"},
    ])
    recs = WebSearchAdapter(p, min_interval_s=0).search("q", max_results=5)
    assert recs[0].doi == "10.5555/test-1" and recs[0].abstract is None and recs[0].snippet == "Tg vs Mn ..."
    assert recs[1].doi is None
    assert recs[0].extra["provider"] == "fake"


def test_websearch_failure_propagates_as_source_error():
    a = WebSearchAdapter(FakeProvider(fail=True), min_interval_s=0)
    with pytest.raises(SourceError):
        a.search("q", max_results=5)
    assert a.stats()["failures"] == 1


@pytest.mark.parametrize("url,doi,rule", [
    ("https://www.nature.com/articles/s41428-020-00443-1", "10.1038/s41428-020-00443-1", "nature"),
    ("https://arxiv.org/pdf/2507.20935v2", "10.48550/arxiv.2507.20935", "arxiv"),
    ("https://arxiv.org/abs/2409.18315", "10.48550/arxiv.2409.18315", "arxiv"),
    ("https://en.wikipedia.org/wiki/Dispersity", None, None),
])
def test_doi_from_url_rules(url, doi, rule):
    from polyextract.discovery.websearch import doi_from_url
    assert doi_from_url(url) == (doi, rule)


def test_websearch_records_doi_derivation():
    p = FakeProvider([{"title": "t", "url": "https://arxiv.org/abs/2409.18315", "snippet": ""}])
    r = WebSearchAdapter(p, min_interval_s=0).search("q", max_results=1)[0]
    assert r.doi == "10.48550/arxiv.2409.18315" and r.extra["doi_derivation"] == "url_rule:arxiv"
