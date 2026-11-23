"""LLM extraction (path 2 of 2). The LLM is a *reader*, never a source of values.

Design:
  * Targeted context: only tables that the deterministic locator flagged + experimental paragraphs
    mentioning GPC/SEC/DSC etc., each given a chunk id.
  * Structured output: JSON schema enforced by the API, then re-validated with Pydantic.
  * The model returns, for every value, the VERBATIM reported text and a verbatim quote plus chunk id.
    Our code checks the quote occurs in that chunk (else the value is dropped and logged), then our own
    parser/unit table normalizes it. Missing information must be omitted, never estimated.
  * Versioned prompt (PROMPT_VERSION), response cache keyed on (prompt version, model, context hash),
    token + cost logging per call, retries via the SDK, mock mode for tests (output = TEST_FIXTURE).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ValidationError

from polyextract.acquisition.document import DocumentModel
from polyextract.extraction.table_rules import (
    DESCRIPTOR_RULES,
    PROPERTY_RULES,
    ExtractionResult,
    _slug,
    classify_column,
    identify_family,
)
from polyextract.logging_setup import get_logger
from polyextract.normalization.units import DIMENSIONLESS, convert, parse_value_text
from polyextract.schemas import (
    DataOrigin,
    Evidence,
    ExtractionInfo,
    ExtractionMethod,
    LocationType,
    MolecularCharacteristicsSchema,
    PolymerSampleSchema,
    PropertyMeasurementSchema,
    PropertyName,
    Qualifier,
    Quantity,
)

log = get_logger("llm")


class LLMAccountError(RuntimeError):
    """Billing/auth/usage-limit failure: every further call would fail too, so the run must stop (not silently skip)."""


ACCOUNT_ERRORS = ("credit balance", "authentication", "invalid x-api-key", "permission", "not logged in",
                  "please run /login", "usage limit", "limit reached", "rate_limit_error")


def _strip_fences(text: str | None) -> str | None:
    """Local models sometimes wrap JSON in ```json fences despite constrained decoding."""
    if text is None:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    return m.group(1) if m else text.strip()


PROMPT_VERSION = "extract-v1"
DEFAULT_MODEL = "claude-opus-5-5"
# USD per 1M tokens (input, output) — Anthropic first-party list prices cached 2026-10-06; verify before budgeting.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50),
          "claude-fable-5-1": (10.0, 50.0)}

DESCRIPTOR_NAMES = [f for f, _, _ in DESCRIPTOR_RULES] + ["dpz"]
PROPERTY_NAMES = [p.value for p, _, _ in PROPERTY_RULES]

SYSTEM = """You extract experimentally REPORTED data about polymer samples from excerpts of one scientific paper.
Rules:
- Copy values exactly as printed (reported_text) and give a verbatim quote (8-200 characters) from the chunk containing it.
- Only report a value if it is explicitly stated for a specific sample. Never estimate, infer, average, convert, or fill gaps.
- Skip theoretical/target/feed values (e.g. "Mn,theo", "targeted DP") — only measured values.
- If a unit is given in a table header, put it in `unit`. If no unit is stated anywhere, leave unit empty.
- One sample per distinct material (row/label). Use the sample label exactly as the paper writes it.
- If nothing qualifies, return an empty samples list."""

SCHEMA = {
    "type": "object",
    "properties": {
        "samples": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sample_label": {"type": "string"},
                    "polymer_name": {"type": "string", "description": "as named in the paper"},
                    "identity_chunk_id": {"type": "string"},
                    "identity_quote": {"type": "string"},
                    "mw_technique": {"type": "string", "description": "e.g. 'SEC in THF vs PS standards', or ''"},
                    "descriptors": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "enum": DESCRIPTOR_NAMES},
                                "reported_text": {"type": "string"},
                                "unit": {"type": "string"},
                                "chunk_id": {"type": "string"},
                                "quote": {"type": "string"},
                            },
                            "required": ["name", "reported_text", "unit", "chunk_id", "quote"],
                            "additionalProperties": False,
                        },
                    },
                    "properties": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "enum": PROPERTY_NAMES},
                                "reported_name": {"type": "string"},
                                "reported_text": {"type": "string"},
                                "unit": {"type": "string"},
                                "technique": {"type": "string"},
                                "chunk_id": {"type": "string"},
                                "quote": {"type": "string"},
                            },
                            "required": ["name", "reported_name", "reported_text", "unit", "technique", "chunk_id", "quote"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["sample_label", "polymer_name", "identity_chunk_id", "identity_quote", "mw_technique",
                             "descriptors", "properties"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["samples"],
    "additionalProperties": False,
}


class _D(BaseModel):
    name: str
    reported_text: str
    unit: str
    chunk_id: str
    quote: str


class _P(_D):
    reported_name: str
    technique: str


class _S(BaseModel):
    sample_label: str
    polymer_name: str
    identity_chunk_id: str
    identity_quote: str
    mw_technique: str
    descriptors: list[_D]
    properties: list[_P]


class LLMOutput(BaseModel):
    samples: list[_S]


# ------------------------------------------------------------------------------------------------
@dataclass
class Chunk:
    chunk_id: str
    text: str
    location: LocationType
    table_id: str | None = None
    section: str | None = None


RELEVANT_TEXT = re.compile(r"\b(GPC|SEC|MALDI|DSC|DMA|TGA|rheolog|glass transition|T_?g\b|M_?n\b|M_?w\b|dispersity|Đ|"
                           r"molecular weight|molar mass|viscosity|tensile|modulus)", re.I)


def build_chunks(doc: DocumentModel, max_paragraphs: int = 12) -> list[Chunk]:
    """Deterministic targeting: relevant tables (any MW/property column) + matching paragraphs."""
    chunks: list[Chunk] = []
    for t in doc.tables:
        hdr = t.column_headers()
        roles = [classify_column(j, h) for j, h in enumerate(hdr)]
        if not any(r and r.role in ("descriptor", "property", "skipped") for r in roles):
            continue
        lines = [f"{t.label or t.table_id}: {t.caption}", " | ".join(hdr)] + [" | ".join(r) for r in t.rows] + t.footnotes
        chunks.append(Chunk(f"T{len(chunks) + 1}", "\n".join(lines),
                            LocationType.SUPPLEMENTARY_TABLE if t.is_supplementary else LocationType.TABLE,
                            table_id=t.label or t.table_id))
    paras = [p for p in doc.paragraphs if RELEVANT_TEXT.search(p.text) and p.section != "Abstract"]
    paras.sort(key=lambda p: -len(RELEVANT_TEXT.findall(p.text)))
    for p in paras[:max_paragraphs]:
        chunks.append(Chunk(f"P{len(chunks) + 1}", p.text, LocationType.TEXT, section=p.section))
    return chunks


def render_context(doc: DocumentModel, chunks: list[Chunk]) -> str:
    head = f"Paper title: {doc.title or ''}\n\n"
    return head + "\n\n".join(f"<chunk id=\"{c.chunk_id}\">\n{c.text}\n</chunk>" for c in chunks)


# ------------------------------------------------------------------------------------------------
@dataclass
class CallLog:
    model: str
    prompt_version: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cached: bool = False
    seconds: float = 0.0
    stop_reason: str | None = None
    error: str | None = None


PROVIDERS = ("anthropic", "claude_cli", "ollama", "mock")
DEFAULT_MODELS = {"anthropic": DEFAULT_MODEL, "claude_cli": "opus", "ollama": "qwen2.5:7b", "mock": "mock"}


@dataclass
class _Raw:
    """Provider-neutral result of one model call."""
    text: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    stop_reason: str | None = None


class LLMClient:
    """One reader interface, several backends (LLM_PROVIDER):
      anthropic  - official SDK, API credits (structured output enforced server-side)
      claude_cli - Claude Code `claude -p` under the user's Claude subscription login (usage-limited)
      ollama     - local open model via http://localhost:11434 (free, slower, CPU-friendly sizes)
      mock       - tests only; output is labelled test_fixture and can never be verified
    Every backend's output goes through the same Pydantic validation, quote anchoring and validators."""

    def __init__(self, provider: str, model: str | None, api_key: str | None, cache_dir: Path | None,
                 effort: str = "medium", use_fallbacks: bool = True, ollama_host: str = "http://localhost:11434",
                 timeout_s: float = 900.0, cache_only: bool = False):
        self.cache_only = cache_only  # reuse cached answers only; never call a model (offline re-scoring)
        self.provider = provider.lower()
        if self.provider not in PROVIDERS:
            raise ValueError(f"unsupported LLM_PROVIDER={provider!r} (use one of {PROVIDERS})")
        self.model = model or DEFAULT_MODELS[self.provider]
        self.cache_dir = cache_dir
        self.effort = effort
        self.use_fallbacks = use_fallbacks
        self.ollama_host = ollama_host.rstrip("/")
        self.timeout_s = timeout_s
        self.calls: list[CallLog] = []
        if self.provider == "anthropic":
            import anthropic

            self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        elif self.provider == "claude_cli":
            import shutil

            self._claude = shutil.which("claude")
            if self._claude and self._claude.lower().endswith((".cmd", ".ps1", ".bat")):
                # Windows npm shims are batch/PowerShell wrappers that truncate multi-line arguments:
                # call the real binary next to them instead.
                exe = Path(self._claude).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
                self._claude = str(exe) if exe.exists() else self._claude
            if not self._claude:
                raise ValueError("LLM_PROVIDER=claude_cli but the `claude` command is not on PATH")
        elif self.provider == "ollama":
            import httpx

            try:
                tags = httpx.get(f"{self.ollama_host}/api/tags", timeout=5).json()
            except Exception as e:
                raise ValueError(f"Ollama not reachable at {self.ollama_host} ({e}). Install it and run `ollama serve`.") from e
            have = {m["name"] for m in tags.get("models", [])}
            if self.model not in have and f"{self.model}:latest" not in have:
                raise ValueError(f"Ollama model {self.model!r} not pulled. Run: ollama pull {self.model}  (have: {sorted(have)})")

    # --- backends -----------------------------------------------------------------------------
    def _call(self, system: str, user: str, schema: dict | None) -> _Raw:
        if self.provider == "anthropic":
            resp = self._create(system, user) if schema else self._client.messages.create(
                model=self.model, max_tokens=4000, output_config={"effort": "low"}, system=system,
                messages=[{"role": "user", "content": user}])
            u = resp.usage
            pin, pout = PRICES.get(self.model, (0.0, 0.0))
            return _Raw(next((b.text for b in resp.content if b.type == "text"), None), u.input_tokens, u.output_tokens,
                        round((u.input_tokens * pin + u.output_tokens * pout) / 1e6, 6), resp.stop_reason)
        if self.provider == "claude_cli":
            return self._call_claude_cli(system, user, schema)
        return self._call_ollama(system, user, schema)

    def _call_claude_cli(self, system: str, user: str, schema: dict | None) -> _Raw:
        import os
        import subprocess

        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        if schema:  # the CLI does not force the schema on long inputs, so state the output contract explicitly
            system = (system + "\n\nOUTPUT CONTRACT: You are a non-interactive extraction function, not a chat assistant. "
                      "Deliver the result through the structured output tool as ONE JSON object that validates against "
                      "this JSON Schema. Never answer in prose, markdown, or tables. Do not ask questions.\n"
                      + json.dumps(schema))
            user = user + "\n\nReturn the JSON object now."
        import tempfile

        # neutral cwd: no project CLAUDE.md / memory leaks into the extraction context
        with tempfile.TemporaryDirectory(prefix="polyextract_llm_") as cwd:
            spf = Path(cwd) / "system.txt"
            spf.write_text(system, encoding="utf-8")
            cmd = [self._claude, "-p", "--output-format", "json", "--model", self.model,
                   "--system-prompt-file", str(spf), "--no-session-persistence", "--strict-mcp-config"]
            if schema:
                cmd += ["--json-schema", json.dumps(schema)]
            cmd += ["--tools", ""]
            p = subprocess.run(cmd, input=user, capture_output=True, text=True, encoding="utf-8", env=env,
                               timeout=self.timeout_s, cwd=cwd)
        out = (p.stdout or "").strip()
        try:
            j = json.loads(out.splitlines()[-1]) if out else {}
        except json.JSONDecodeError:
            j = {}
        if p.returncode != 0 or j.get("is_error"):
            raise RuntimeError(f"claude -p failed (rc={p.returncode}): {(j.get('result') or p.stderr or out)[:400]}")
        u = j.get("usage") or {}
        text = json.dumps(j["structured_output"]) if j.get("structured_output") is not None else j.get("result")
        return _Raw(text, int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0))
                    + int(u.get("cache_creation_input_tokens", 0)), int(u.get("output_tokens", 0)),
                    0.0,  # subscription usage: no per-call charge (CLI's notional total_cost_usd not billed)
                    j.get("stop_reason") or j.get("subtype"))

    def _call_ollama(self, system: str, user: str, schema: dict | None) -> _Raw:
        import httpx

        body = {"model": self.model, "stream": False,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "options": {"temperature": 0, "num_ctx": 16384}}
        if schema:
            body["format"] = schema  # Ollama structured outputs (JSON-schema constrained decoding)
        r = httpx.post(f"{self.ollama_host}/api/chat", json=body, timeout=self.timeout_s)
        r.raise_for_status()
        j = r.json()
        return _Raw(j.get("message", {}).get("content"), int(j.get("prompt_eval_count", 0)),
                    int(j.get("eval_count", 0)), 0.0, j.get("done_reason"))

    def _key(self, system: str, user: str) -> str:
        return hashlib.sha256(json.dumps([PROMPT_VERSION, self.model, system, user, SCHEMA]).encode()).hexdigest()

    def _create(self, system: str, user: str):
        import anthropic

        common = dict(model=self.model, max_tokens=16000, system=system,
                      messages=[{"role": "user", "content": user}],
                      output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": SCHEMA}})
        if self.use_fallbacks:
            try:
                return self._client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **common)
            except anthropic.BadRequestError as e:
                if "fallback" not in str(e).lower():
                    raise  # e.g. billing / invalid request: not a fallbacks problem
                log.warning("fallbacks parameter rejected (%s); retrying without it", e)
                self.use_fallbacks = False
        return self._client.messages.create(**common)

    def extract_json(self, system: str, user: str) -> tuple[dict | None, CallLog]:
        cl = CallLog(self.model, PROMPT_VERSION)
        key = self._key(system, user)
        cpath = self.cache_dir / f"{key}.json" if self.cache_dir else None
        if cpath and cpath.exists():
            cl.cached = True
            self.calls.append(cl)
            return json.loads(cpath.read_text(encoding="utf-8")), cl
        if self.cache_only:
            cl.error = "cache_miss"
            return None, cl
        if self.provider == "mock":
            self.calls.append(cl)
            return {"samples": []}, cl
        t0 = time.time()
        try:
            raw = self._call(system, user, SCHEMA)
        except Exception as e:  # SDK already retried 429/5xx; record and move on
            msg = str(e).lower()
            if any(s in msg for s in ACCOUNT_ERRORS):
                raise LLMAccountError(f"LLM account problem, stopping run: {e}") from e
            cl.error = f"{type(e).__name__}: {e}"[:300]
            self.calls.append(cl)
            return None, cl
        cl.seconds = round(time.time() - t0, 2)
        cl.stop_reason = raw.stop_reason
        cl.input_tokens, cl.output_tokens, cl.cost_usd = raw.input_tokens, raw.output_tokens, raw.cost_usd
        self.calls.append(cl)
        if raw.stop_reason in ("refusal", "max_tokens", "length", "error_max_turns"):
            cl.error = f"stop_reason={raw.stop_reason}"
            return None, cl
        text = _strip_fences(raw.text)
        try:
            data = LLMOutput.model_validate_json(text or "").model_dump()
        except ValidationError as e:
            cl.error = f"malformed output: {str(e)[:200]}"
            return None, cl
        if cpath:
            cpath.parent.mkdir(parents=True, exist_ok=True)
            cpath.write_text(json.dumps(data), encoding="utf-8")
        return data, cl

    def complete(self, prompt: str, purpose: str = "qa") -> str:
        if self.provider == "mock":
            return "[mock LLM answer — test mode]"
        raw = self._call("Answer using only the numbered evidence provided; cite it as [n].", prompt, None)
        self.calls.append(CallLog(self.model, purpose, raw.input_tokens, raw.output_tokens, raw.cost_usd,
                                  stop_reason=raw.stop_reason))
        return raw.text or ""


# ------------------------------------------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("−", "-")).strip().casefold()


def _anchor(chunks: dict[str, Chunk], chunk_id: str, quote: str, reported: str) -> Chunk | None:
    c = chunks.get(chunk_id)
    if c is None:
        return None
    src = _norm(c.text)
    if _norm(quote) in src and _norm(reported) in src:
        return c
    return None


def llm_to_records(doc: DocumentModel, data: dict, chunks: list[Chunk], *, model: str, provider: str,
                   run_id: str | None = None) -> tuple[ExtractionResult, list[str]]:
    """Convert validated LLM JSON into our schemas. Unanchored values are dropped and reported."""
    res = ExtractionResult()
    dropped: list[str] = []
    origin = DataOrigin.TEST_FIXTURE if provider == "mock" else DataOrigin.LITERATURE_EXTRACTION
    info = ExtractionInfo(method=ExtractionMethod.LLM, model=model, prompt_version=PROMPT_VERSION, run_id=run_id)
    cmap = {c.chunk_id: c for c in chunks}

    def ev(c: Chunk) -> Evidence:
        return Evidence(paper_id=doc.paper_id, doi=doc.doi, document_ref=doc.source_url, location_type=c.location,
                        table_id=c.table_id, section=c.section, source_text=c.text[:2000])

    for s in data.get("samples", []):
        sid = f"{doc.paper_id}#s:{_slug(s['sample_label'])}"
        idc = _anchor(cmap, s["identity_chunk_id"], s["identity_quote"], s["sample_label"])
        fam, _ = identify_family(s["polymer_name"])
        res.samples.append(PolymerSampleSchema(
            sample_id=sid, paper_id=doc.paper_id, sample_label=s["sample_label"], polymer_name=s["polymer_name"] or "unidentified",
            polymer_family=fam, identity_evidence=[ev(idc)] if idc else [], extraction=info, data_origin=origin))
        desc: dict[str, Quantity] = {}
        for d in s["descriptors"]:
            c = _anchor(cmap, d["chunk_id"], d["quote"], d["reported_text"])
            pv = parse_value_text(d["reported_text"])
            if c is None or not pv.ok or d["name"] in desc:
                dropped.append(f"{sid}:{d['name']}:{'unanchored' if c is None else 'unparseable_or_dup'}")
                continue
            q = _llm_quantity(pv, pv.unit or d["unit"] or None, d["name"] not in ("mn", "mw", "mz"),
                              d["reported_text"], ev(c))
            if q is None:
                dropped.append(f"{sid}:{d['name']}:invalid_quantity")
                continue
            desc[d["name"]] = q
        if desc:
            res.molecular.append(MolecularCharacteristicsSchema(
                mc_id=f"{sid}#mc", sample_id=sid, paper_id=doc.paper_id, technique_reported=s["mw_technique"] or None,
                extraction=info, data_origin=origin, **desc))
        for i, p in enumerate(s["properties"]):
            c = _anchor(cmap, p["chunk_id"], p["quote"], p["reported_text"])
            pv = parse_value_text(p["reported_text"])
            if c is None or not pv.ok:
                dropped.append(f"{sid}:{p['name']}:{'unanchored' if c is None else 'unparseable'}")
                continue
            q = _llm_quantity(pv, pv.unit or p["unit"] or None, False, p["reported_text"], ev(c))
            if q is None:
                dropped.append(f"{sid}:{p['name']}:invalid_quantity")
                continue
            res.measurements.append(PropertyMeasurementSchema(
                measurement_id=f"{sid}#llm:{i}", sample_id=sid, paper_id=doc.paper_id, property_name=PropertyName(p["name"]),
                property_name_reported=p["reported_name"] or p["name"], value=q, technique=p["technique"] or None,
                extraction=info, data_origin=origin))
    return res, dropped


def _llm_quantity(pv, unit: str | None, dimensionless: bool, reported_text: str, evidence: Evidence) -> Quantity | None:
    """Build a Quantity from an LLM-reported string exactly like the table path does: qualifier and range
    (value_max) are kept; never collapses '100-120' to '100'. Returns None if the schema rejects it."""
    if dimensionless:
        unit, nv, nmax, nu = None, pv.value, pv.value_max, DIMENSIONLESS
    else:
        nv, nu = convert(pv.value, unit) if unit else (None, None)
        nmax = convert(pv.value_max, unit)[0] if unit and pv.value_max is not None and nv is not None else None
    qual = Qualifier(pv.qualifier) if pv.qualifier in {q.value for q in Qualifier} else Qualifier.EQ
    try:
        return Quantity(value=pv.value, value_max=pv.value_max, qualifier=qual, uncertainty=pv.uncertainty, unit=unit,
                        reported_text=reported_text, normalized_value=nv, normalized_value_max=nmax,
                        normalized_unit=nu, evidence=[evidence])
    except ValidationError:
        return None


def run_llm_extraction(client: LLMClient, doc: DocumentModel, run_id: str | None = None):
    chunks = build_chunks(doc)
    if not chunks:
        return None, [], None
    data, call = client.extract_json(SYSTEM, render_context(doc, chunks))
    if data is None:
        return None, [], call
    res, dropped = llm_to_records(doc, data, chunks, model=client.model, provider=client.provider, run_id=run_id)
    return res, dropped, call
