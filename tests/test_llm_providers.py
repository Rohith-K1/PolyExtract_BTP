"""LLM backends (no network, no real CLI): output parsing, account-error handling, fence stripping."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from polyextract.extraction import llm as L


def _cli_client(monkeypatch, stdout: str, rc: int = 0):
    monkeypatch.setattr("shutil.which", lambda _: "C:/fake/claude.exe")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return SimpleNamespace(returncode=rc, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return L.LLMClient("claude_cli", "opus", None, None), seen


def test_claude_cli_structured_output_parsed(monkeypatch):
    out = {"samples": []}
    c, seen = _cli_client(monkeypatch, json.dumps({"is_error": False, "subtype": "success", "structured_output": out,
                                                   "usage": {"input_tokens": 5, "output_tokens": 7}}))
    data, call = c.extract_json(L.SYSTEM, "ctx")
    assert data == out and call.error is None and call.cost_usd == 0.0 and call.output_tokens == 7
    cmd = seen["cmd"]
    assert "--system-prompt-file" in cmd and "--json-schema" in cmd and cmd[cmd.index("--tools") + 1] == ""
    assert "ANTHROPIC_API_KEY" not in seen["kw"]["env"]  # always the subscription login, never API credits
    assert seen["kw"]["cwd"]  # neutral working dir, no project context


def test_claude_cli_prose_reply_is_rejected_not_parsed(monkeypatch):
    c, _ = _cli_client(monkeypatch, json.dumps({"is_error": False, "result": "I pulled out every value..."}))
    data, call = c.extract_json(L.SYSTEM, "ctx")
    assert data is None and call.error.startswith("malformed output")


def test_usage_limit_stops_the_run(monkeypatch):
    c, _ = _cli_client(monkeypatch, json.dumps({"is_error": True, "result": "Claude AI usage limit reached"}), rc=1)
    with pytest.raises(L.LLMAccountError):
        c.extract_json(L.SYSTEM, "ctx")


def test_cache_only_never_calls_the_model(monkeypatch, tmp_path):
    c, seen = _cli_client(monkeypatch, "{}")
    c.cache_dir, c.cache_only = tmp_path, True
    data, call = c.extract_json(L.SYSTEM, "ctx")
    assert data is None and call.error == "cache_miss" and "cmd" not in seen


def test_strip_fences():
    assert L._strip_fences('```json\n{"samples": []}\n```') == '{"samples": []}'
    assert L._strip_fences(' {"a": 1} ') == '{"a": 1}'


def test_unknown_provider_rejected():
    with pytest.raises(ValueError):
        L.LLMClient("openai", None, None, None)
