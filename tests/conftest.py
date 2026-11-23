"""Shared fixtures.

All fixture data here is SOFTWARE TEST DATA. DOIs use Crossref's reserved test prefix 10.5555 and
titles start with 'TEST FIXTURE'. None of it is scientific data or ever enters the curated dataset.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from polyextract.discovery.filters import FilterEngine
from polyextract.discovery.models import SourceRecord

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def filter_engine() -> FilterEngine:
    return FilterEngine.from_yaml(ROOT / "config" / "filters.yaml")


def make_record(**kw) -> SourceRecord:
    base = dict(source="openalex", query="q", rank=1)
    base.update(kw)
    return SourceRecord(**base)


@pytest.fixture
def record_factory():
    return make_record
