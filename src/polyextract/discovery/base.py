"""Source adapter interface. Each literature source implements `search`."""

from __future__ import annotations

from abc import ABC, abstractmethod

from polyextract.discovery.models import SourceRecord
from polyextract.schemas.identifiers import ANY_TAG as _TAG
from polyextract.schemas.identifiers import INLINE_TAGS as _INLINE_TAGS


def strip_markup(text: str | None) -> str | None:
    """Remove JATS/HTML markup. Inline tags vanish ('M<sub>w</sub>' -> 'Mw'); block tags become spaces."""
    if text is None:
        return None
    t = _INLINE_TAGS.sub("", text)
    t = _TAG.sub(" ", t)
    t = " ".join(t.split())
    return t or None


class SourceAdapter(ABC):
    name: str

    @abstractmethod
    def search(self, query: str, *, max_results: int, year_from: int | None, year_to: int | None) -> list[SourceRecord]:
        """Return up to max_results records. Raise SourceError on failure (caller continues)."""

    def stats(self) -> dict:
        return {}

    def close(self) -> None:  # pragma: no cover - trivial
        pass
