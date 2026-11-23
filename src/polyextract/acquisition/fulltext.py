"""Legal open-access full-text location and acquisition.

Order of preference (best structure first):
  1. Europe PMC open-access JATS XML (tables as real cells, explicit licence).
  2. Unpaywall best OA location (recorded; PDF parsing is a later fallback).
Not-open-access papers are recorded as such and never fetched. No paywall is bypassed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from polyextract.acquisition.document import DocumentModel
from polyextract.acquisition.jats import parse_jats
from polyextract.discovery.http import HttpClient
from polyextract.exceptions import SourceError
from polyextract.logging_setup import get_logger

log = get_logger("fulltext")

EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EPMC_FULLTEXT = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
UNPAYWALL = "https://api.unpaywall.org/v2/{doi}"


@dataclass
class Location:
    status: str  # 'jats_available' | 'oa_other' | 'not_open_access' | 'unknown'
    pmcid: str | None = None
    url: str | None = None
    license: str | None = None
    oa_status: str | None = None
    host: str | None = None
    note: str = ""


class FullTextLocator:
    def __init__(self, http: HttpClient, raw_http_get=None, email: str | None = None):
        self.http = http
        self.email = email
        # raw (non-JSON) GET for XML; injectable for tests
        self._raw_get = raw_http_get or (lambda url: http._client.get(url, timeout=60))

    def europepmc_lookup(self, doi: str) -> Location | None:
        body = self.http.get_json(EPMC_SEARCH, {"query": f'DOI:"{doi}"', "format": "json", "resultType": "core"})
        results = (body.get("resultList") or {}).get("result") or []
        for r in results:
            if (r.get("doi") or "").lower() != doi.lower():
                continue
            pmcid = r.get("pmcid")
            is_oa = r.get("isOpenAccess") == "Y"
            in_ft = r.get("inEPMC") == "Y" or r.get("hasTextMinedTerms") == "Y"
            if pmcid and is_oa:
                return Location("jats_available", pmcid=pmcid, license=r.get("license"), host="europepmc")
            if pmcid and in_ft:
                return Location("oa_other", pmcid=pmcid, license=r.get("license"), host="europepmc",
                                note="in PMC but not in the OA subset (no mining licence)")
        return None

    def unpaywall_lookup(self, doi: str) -> Location:
        if not self.email:
            return Location("unknown", note="Unpaywall needs a contact email (OPENALEX_EMAIL)")
        try:
            body = self.http.get_json(UNPAYWALL.format(doi=quote(doi, safe="/")), {"email": self.email})
        except SourceError as e:
            return Location("unknown", note=str(e)[:200])
        if not body.get("is_oa"):
            return Location("not_open_access", oa_status=body.get("oa_status"))
        best = body.get("best_oa_location") or {}
        return Location("oa_other", url=best.get("url_for_pdf") or best.get("url"), license=best.get("license"),
                        oa_status=body.get("oa_status"), host=best.get("host_type"))

    def locate(self, doi: str) -> Location:
        try:
            loc = self.europepmc_lookup(doi)
        except SourceError as e:
            log.warning("europepmc lookup failed for %s: %s", doi, e)
            loc = None
        if loc and loc.status == "jats_available":
            return loc
        upw = self.unpaywall_lookup(doi)
        if loc and upw.status in ("unknown",):
            return loc
        return upw

    def fetch_jats(self, loc: Location, *, paper_id: str, doi: str, cache_dir: Path) -> DocumentModel:
        assert loc.pmcid
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", loc.pmcid)
        path = cache_dir / f"{safe}.xml"
        if path.exists():
            data = path.read_bytes()
        else:
            resp = self._raw_get(EPMC_FULLTEXT.format(pmcid=loc.pmcid))
            if resp.status_code != 200 or not resp.content.lstrip().startswith(b"<"):
                raise SourceError("europepmc", f"fullTextXML {loc.pmcid}: HTTP {resp.status_code}")
            data = resp.content
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return parse_jats(data, paper_id=paper_id, doi=doi,
                          source_url=EPMC_FULLTEXT.format(pmcid=loc.pmcid), license=loc.license)
