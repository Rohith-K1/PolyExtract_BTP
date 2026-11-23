"""End-to-end Stage 1 with fake adapters: failure isolation, enrichment, persistence, report."""

import json

from polyextract.discovery.base import SourceAdapter
from polyextract.discovery.crossref import CrossrefAdapter
from polyextract.discovery.pipeline import DiscoveryConfig, run_discovery
from polyextract.exceptions import SourceError
from polyextract.storage.sqlite import Repository
from tests.conftest import make_record

ABS = "Polystyrene with controlled dispersity; Mn from SEC; glass transition by DSC."


class FakeAdapter(SourceAdapter):
    def __init__(self, name, records=None, fail=False):
        self.name, self._records, self.fail = name, records or [], fail

    def search(self, query, *, max_results, year_from=None, year_to=None):
        if self.fail:
            raise SourceError(self.name, "simulated outage")
        return [r.model_copy(update={"query": query}) for r in self._records[:max_results]]


class FakeCrossref(CrossrefAdapter):
    def __init__(self, records):
        self.name, self._records = "crossref", records

    def search(self, query, *, max_results, year_from=None, year_to=None):
        return list(self._records)

    def lookup_doi(self, doi, *, query_label="doi_lookup"):
        return make_record(source="crossref", doi=doi, title="TEST FIXTURE web paper polymer", abstract=ABS,
                           publication_year=2010, query=query_label)

    def stats(self):
        return {}


def test_pipeline_end_to_end(tmp_path, filter_engine):
    oa = FakeAdapter("openalex", [make_record(source="openalex", doi="10.5555/a", title="TEST FIXTURE A polymer",
                                              abstract=ABS, publication_year=2000)])
    cr = FakeCrossref([make_record(source="crossref", doi="10.5555/a", title="TEST FIXTURE A polymer",
                                   publication_year=2000)])
    web = FakeAdapter("websearch", [make_record(source="websearch", doi="10.5555/web", title="web hit")])
    broken = FakeAdapter("brokensource", fail=True)
    repo = Repository(tmp_path / "db.sqlite")
    cfg = DiscoveryConfig(queries=["q1", "q2"], max_results_per_query=10)

    report = run_discovery(cfg, [oa, cr, web, broken], filter_engine, repo, tmp_path / "run", run_id="r1")

    # failure of one source is recorded, others continue
    assert report["sources"]["brokensource"]["queries_failed"] == 2
    assert len([e for e in report["errors"] if e["source"] == "brokensource"]) == 2
    # web-only DOI enriched once via Crossref lookup
    assert report["sources"]["crossref"]["doi_lookups_added"] == 1
    # 2 unique papers: 10.5555/a (oa+crossref, both queries) and 10.5555/web (web+crossref lookup)
    assert report["unique_papers"] == 2
    assert report["dedup"]["source_overlap"] == {"crossref+openalex": 1, "crossref+websearch": 1}
    assert sum(report["filter_decisions"].values()) == 2
    # persistence
    assert sum(repo.candidate_counts("r1").values()) == 2
    p = repo.get_paper("doi:10.5555/a")
    assert p is not None and p.retrieval_sources == ["crossref", "openalex"]
    # artifacts
    for f in ("raw_records.jsonl", "candidates.jsonl", "report.json", "report.md"):
        assert (tmp_path / "run" / f).exists()
    assert json.loads((tmp_path / "run" / "report.json").read_text())["run_id"] == "r1"
    repo.close()
