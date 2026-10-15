"""Application configuration (environment variables / .env files)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent
IST = "Asia/Kolkata"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(REPO_DIR / ".env"), str(BACKEND_DIR / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./var/trading.db"

    data_provider: str = "sample"
    sample_seed: int = 42
    sample_start_date: str = "2023-01-02"
    sample_intraday_sessions: int = 20

    kite_api_key: SecretStr | None = None
    kite_api_secret: SecretStr | None = None
    kite_access_token: SecretStr | None = None

    # Live execution is not implemented. Kept as an explicit, visible flag that must remain False.
    live_trading_enabled: bool = False

    llm_provider: str = "template"
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_timeout_seconds: float = 20.0

    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    background_jobs: bool = True
    quote_refresh_seconds: int = 5
    ingest_refresh_seconds: int = 60

    @field_validator("database_url")
    @classmethod
    def _resolve_sqlite_path(cls, v: str) -> str:
        prefix = "sqlite:///"
        if v.startswith(prefix) and ":memory:" not in v:
            raw = v[len(prefix):]
            p = Path(raw)
            if not p.is_absolute():
                p = (BACKEND_DIR / raw).resolve()
            return prefix + p.as_posix()
        return v

    def secret_values(self) -> list[str]:
        out: list[str] = []
        for s in (self.kite_api_key, self.kite_api_secret, self.kite_access_token, self.llm_api_key):
            if s is not None:
                val = s.get_secret_value()
                if val and len(val) >= 4:
                    out.append(val)
        return out

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()
