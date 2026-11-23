import httpx
import pytest

from polyextract.discovery.http import HttpClient
from polyextract.discovery.openalex import openalex_rate_limit_check
from polyextract.exceptions import RateLimitedError, SourceError


def client(handler, **kw):
    sleeps: list[float] = []
    c = HttpClient("test", transport=httpx.MockTransport(handler), sleep=sleeps.append, min_interval_s=0, **kw)
    return c, sleeps


def test_retries_on_503_then_succeeds():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, json={"ok": True})

    c, sleeps = client(h, backoff_base_s=1)
    assert c.get_json("https://x.test/a") == {"ok": True}
    assert c.stats.retries == 2 and sleeps == [1, 2]


def test_respects_retry_after():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "7"}) if calls["n"] == 1 else httpx.Response(200, json={})

    c, sleeps = client(h)
    c.get_json("https://x.test/a")
    assert sleeps == [7.0] and c.stats.rate_limited == 1


def test_rate_limit_exhaustion_raises():
    c, _ = client(lambda r: httpx.Response(429), max_retries=2)
    with pytest.raises(RateLimitedError):
        c.get_json("https://x.test/a")


def test_body_level_rate_limit_detected():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"error": "Rate limit exceeded", "retryAfter": 5})
        return httpx.Response(200, json={"results": []})

    c, sleeps = client(h, body_rate_limit_check=openalex_rate_limit_check)
    assert c.get_json("https://x.test/a") == {"results": []}
    assert sleeps == [5.0]


def test_4xx_not_retried():
    c, _ = client(lambda r: httpx.Response(404, text="nope"))
    with pytest.raises(SourceError, match="HTTP 404"):
        c.get_json("https://x.test/a")
    assert c.stats.requests == 1


def test_cache_hit_and_secret_excluded(tmp_path):
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        return httpx.Response(200, json={"v": 1})

    c, _ = client(h, cache_dir=tmp_path)
    c.get_json("https://x.test/a", {"q": "x", "api_key": "SECRET1"})
    c.get_json("https://x.test/a", {"q": "x", "api_key": "SECRET2"})
    assert calls["n"] == 1 and c.stats.cache_hits == 1
    for f in tmp_path.rglob("*.json"):
        assert "SECRET" not in f.read_text()
