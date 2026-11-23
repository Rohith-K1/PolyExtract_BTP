"""Polite JSON HTTP client: per-source rate limiting, retry with backoff, Retry-After, disk cache.

The cache makes discovery runs reproducible and cheap to re-run. The cache key deliberately
excludes credentials (api_key), and credentials are never written to disk or logs.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from polyextract.exceptions import RateLimitedError, SourceError
from polyextract.logging_setup import get_logger

log = get_logger("http")

SECRET_PARAMS = frozenset({"api_key", "apikey", "key", "token"})
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass
class HttpStats:
    requests: int = 0
    cache_hits: int = 0
    retries: int = 0
    rate_limited: int = 0
    failures: int = 0
    status_counts: dict[int, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "status_counts": {str(k): v for k, v in self.status_counts.items()}}


BodyCheck = Callable[[Any], float | None]
"""Inspects a decoded JSON body; returns seconds-to-wait if the body signals rate limiting."""


class HttpClient:
    def __init__(
        self,
        source: str,
        *,
        min_interval_s: float = 0.5,
        max_retries: int = 4,
        backoff_base_s: float = 2.0,
        max_wait_s: float = 120.0,
        timeout_s: float = 30.0,
        user_agent: str = "polyextract/0.1 (academic research)",
        cache_dir: Path | None = None,
        body_rate_limit_check: BodyCheck | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.source = source
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.max_wait_s = max_wait_s
        self.cache_dir = cache_dir
        self.body_rate_limit_check = body_rate_limit_check
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None
        self.stats = HttpStats()
        self._client = httpx.Client(
            timeout=timeout_s, headers={"User-Agent": user_agent}, transport=transport, follow_redirects=True
        )

    # ---- cache -------------------------------------------------------------------------
    @staticmethod
    def cache_key(url: str, params: dict[str, Any] | None) -> str:
        public = {k: v for k, v in sorted((params or {}).items()) if k.lower() not in SECRET_PARAMS}
        return hashlib.sha256(json.dumps([url, public], sort_keys=True, default=str).encode()).hexdigest()

    def _cache_path(self, key: str) -> Path | None:
        return None if self.cache_dir is None else self.cache_dir / self.source / f"{key[:2]}/{key}.json"

    # ---- rate limiting -----------------------------------------------------------------
    def _throttle(self) -> None:
        if self._last_request is not None:
            wait = self.min_interval_s - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(max(retry_after, 0.0), self.max_wait_s)
        return min(self.backoff_base_s * (2**attempt), self.max_wait_s)

    @staticmethod
    def _parse_retry_after(resp: httpx.Response) -> float | None:
        v = resp.headers.get("Retry-After")
        if v is None:
            return None
        try:
            return float(v)
        except ValueError:
            return None

    # ---- main --------------------------------------------------------------------------
    def get_json(self, url: str, params: dict[str, Any] | None = None, *, use_cache: bool = True) -> Any:
        key = self.cache_key(url, params)
        cpath = self._cache_path(key) if use_cache else None
        if cpath is not None and cpath.exists():
            self.stats.cache_hits += 1
            return json.loads(cpath.read_text(encoding="utf-8"))

        last_error: str = ""
        for attempt in range(self.max_retries + 1):
            self._throttle()
            self.stats.requests += 1
            try:
                resp = self._client.get(url, params=params)
            except httpx.TransportError as e:
                last_error = f"transport error: {type(e).__name__}: {e}"
                log.warning("%s %s (attempt %d)", self.source, last_error, attempt + 1)
                if attempt < self.max_retries:
                    self.stats.retries += 1
                    self._sleep(self._backoff(attempt, None))
                    continue
                break

            self.stats.status_counts[resp.status_code] = self.stats.status_counts.get(resp.status_code, 0) + 1
            if resp.status_code in RETRY_STATUS:
                if resp.status_code == 429:
                    self.stats.rate_limited += 1
                last_error = f"HTTP {resp.status_code}"
                if attempt < self.max_retries:
                    self.stats.retries += 1
                    wait = self._backoff(attempt, self._parse_retry_after(resp))
                    log.warning("%s %s; retrying in %.1fs", self.source, last_error, wait)
                    self._sleep(wait)
                    continue
                break
            if resp.status_code >= 400:
                self.stats.failures += 1
                raise SourceError(self.source, f"HTTP {resp.status_code} for {resp.url}: {resp.text[:200]}")
            try:
                body = resp.json()
            except ValueError as e:
                self.stats.failures += 1
                raise SourceError(self.source, f"non-JSON response from {url}") from e

            # Some APIs (OpenAlex) signal throttling in a JSON body.
            wait_hint = self.body_rate_limit_check(body) if self.body_rate_limit_check else None
            if wait_hint is not None:
                self.stats.rate_limited += 1
                last_error = "rate limited (body)"
                if attempt < self.max_retries:
                    self.stats.retries += 1
                    wait = self._backoff(attempt, wait_hint)
                    log.warning("%s rate-limited by body; retrying in %.1fs", self.source, wait)
                    self._sleep(wait)
                    continue
                break

            if cpath is not None:
                cpath.parent.mkdir(parents=True, exist_ok=True)
                cpath.write_text(json.dumps(body), encoding="utf-8")
            return body

        self.stats.failures += 1
        if "rate" in last_error or "429" in last_error:
            raise RateLimitedError(self.source, f"rate limited after {self.max_retries + 1} attempts")
        raise SourceError(self.source, f"failed after {self.max_retries + 1} attempts: {last_error}")

    def close(self) -> None:
        self._client.close()
