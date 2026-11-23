"""Project exception hierarchy. Catch `PolyExtractError` at pipeline boundaries."""


class PolyExtractError(Exception):
    """Base class for all project errors."""


class ConfigError(PolyExtractError):
    """Invalid or missing configuration."""


class SourceError(PolyExtractError):
    """A literature source failed (network, HTTP error, malformed payload)."""

    def __init__(self, source: str, message: str):
        super().__init__(f"[{source}] {message}")
        self.source = source


class RateLimitedError(SourceError):
    """Source signalled rate limiting and retries were exhausted."""


class SourceUnavailableError(SourceError):
    """Source cannot be used in this environment (missing optional dependency, blocked, etc.)."""


class ProvenanceError(PolyExtractError):
    """A record violates provenance rules (e.g. test fixture entering curated data)."""
