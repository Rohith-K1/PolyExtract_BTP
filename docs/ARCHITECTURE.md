# Architecture

The full diagram and rationale are in [AUDIT_AND_PLAN.md §4](AUDIT_AND_PLAN.md). This file tracks what exists in code.

## Modules (implemented)

| Module | Responsibility |
|---|---|
| `polyextract.config` | `.env`-backed settings; `redacted()` for safe logging |
| `polyextract.logging_setup` | Console logs plus a JSON-lines log file per run |
| `polyextract.exceptions` | `SourceError` / `RateLimitedError` / `SourceUnavailableError` / `ProvenanceError` |
| `polyextract.schemas` | The 4 core schemas plus `Quantity`, `Evidence`, `CalculationRecord`, `ExtractionInfo` |
| `polyextract.discovery.http` | Rate limiting, retry and backoff, Retry-After, body-level rate-limit detection, credential-free disk cache |
| `polyextract.discovery.{openalex,crossref,websearch}` | Source adapters behind the `SourceAdapter` interface |
| `polyextract.discovery.dedup` | DOI, then exact title, then fuzzy-title matching; per-field source attribution |
| `polyextract.discovery.filters` | Deterministic, explainable rules R0–R6 driven by `config/filters.yaml` |
| `polyextract.discovery.pipeline` | Orchestration, per-source failure isolation, reports |
| `polyextract.storage.sqlite` | `papers`, `discovery_runs`, `discovery_candidates` |
| `polyextract.cli` | `polyextract discover` |

## Key decisions

- **Modular monolith + SQLite.** The data volume is small (thousands of papers). One file DB is reproducible and easy to inspect, so there are no services and no ORM.
- **Adapters raise and the pipeline continues.** A source outage becomes a row in `report.errors` and never aborts the run.
- **Web hits are enriched, not trusted.** A DOI found in a web result triggers a Crossref lookup, which enters as its own attributable record.
- **Filter rules live in YAML, the logic in code.** Domain experts can tune terms without touching code, and the `version` string is stored with every decision.
