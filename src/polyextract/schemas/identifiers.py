"""DOI and ID helpers (pure functions, used by schemas, dedup and validation)."""

from __future__ import annotations

import hashlib
import re
import unicodedata

# Crossref's recommended pattern covers ~99.3% of modern DOIs.
DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9<>\[\]]+")
_DOI_PREFIXES = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*|urn:doi:)", re.IGNORECASE)
# Inline tags vanish ('M<sub>w</sub>' -> 'Mw'); any other tag becomes a space. Shared with adapters.
INLINE_TAGS = re.compile(r"</?(?:jats:)?(?:sub|sup|i|b|em|strong|italic|bold|sc)\b[^>]*>", re.IGNORECASE)
ANY_TAG = re.compile(r"<[^>]+>")


def normalize_doi(raw: str | None) -> str | None:
    """Lower-case bare DOI ('10.xxxx/...') or None if no DOI can be recognised."""
    if not raw:
        return None
    s = _DOI_PREFIXES.sub("", raw.strip())
    m = DOI_PATTERN.search(s)
    if not m:
        return None
    doi = m.group(0).rstrip(".,;:)]")
    # Balanced-parenthesis fix: keep ')' if the DOI itself opened one, e.g. 10.1016/0032-3861(93)90001-x
    if doi.count("(") > doi.count(")") and s[m.end(0) - 1 : m.end(0)] == ")":
        doi += ")"
    return doi.lower()


def extract_dois(text: str) -> list[str]:
    seen: list[str] = []
    for m in DOI_PATTERN.finditer(text or ""):
        d = normalize_doi(m.group(0))
        if d and d not in seen:
            seen.append(d)
    return seen


def normalize_title(title: str | None) -> str:
    """Casefolded alphanumeric-only title used for fallback duplicate matching."""
    if not title:
        return ""
    t = ANY_TAG.sub(" ", INLINE_TAGS.sub("", title))
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = re.sub(r"[^0-9a-z]+", " ", t.casefold())
    return " ".join(t.split())


def make_paper_id(doi: str | None, title: str | None, year: int | None) -> str:
    """Stable paper ID: DOI-based when available, else hash of normalized title + year."""
    if doi:
        return f"doi:{doi}"
    h = hashlib.sha1(f"{normalize_title(title)}|{year}".encode()).hexdigest()[:16]
    return f"title:{h}"
