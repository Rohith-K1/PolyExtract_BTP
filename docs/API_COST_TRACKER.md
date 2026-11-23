# API Cost Tracker

| Date | Service | Calls | Tokens | Cost | Notes |
|---|---|---|---|---|---|
| 2026-11-05 | OpenAlex (anonymous) | ~10 | — | free | 7× HTTP 429; API key needed |
| 2026-11-05 | Crossref (public) | ~13 | — | free | |
| 2026-11-05 | DuckDuckGo (ddgs) | 6 | — | free | 2/3 queries blocked in run 2 |
| — | LLM | 0 | 0 | 0 | Phase 4 not started |

Per-run HTTP statistics (requests, cache hits, retries, rate limits) are in each `report.json` under `sources.*.http`. LLM token and cost logging will be automatic from Phase 4.
