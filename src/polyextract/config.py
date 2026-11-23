"""Environment-based settings. Credentials come only from the environment / .env, never from code."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    openalex_email: str | None = Field(default=None, alias="OPENALEX_EMAIL")
    openalex_api_key: str | None = Field(default=None, alias="OPENALEX_API_KEY")
    crossref_mailto: str | None = Field(default=None, alias="CROSSREF_MAILTO")
    web_search_provider: str = Field(default="ddgs", alias="WEB_SEARCH_PROVIDER")
    web_search_api_key: str | None = Field(default=None, alias="WEB_SEARCH_API_KEY")

    llm_provider: str = Field(default="mock", alias="LLM_PROVIDER")
    llm_api_key: str | None = Field(default=None, alias="LLM_API_KEY")
    llm_model: str | None = Field(default=None, alias="LLM_MODEL")

    data_dir: Path = Field(default=Path("data"), alias="POLYEXTRACT_DATA_DIR")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def contact_email(self) -> str | None:
        return self.crossref_mailto or self.openalex_email

    def resolved_data_dir(self) -> Path:
        d = self.data_dir if self.data_dir.is_absolute() else PROJECT_ROOT / self.data_dir
        return d

    def redacted(self) -> dict:
        """Settings safe to log: secrets replaced by presence flags."""
        out = self.model_dump()
        for k in ("openalex_api_key", "web_search_api_key", "llm_api_key"):
            out[k] = "<set>" if out[k] else None
        out["data_dir"] = str(out["data_dir"])
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
