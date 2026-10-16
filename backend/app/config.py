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

    # "kite" (default; Zerodha Kite Connect, official, real-time, daily login), "yahoo" (unofficial, may be
    # delayed), "upstox" (real-time via free read-only Analytics Token), or "sample" (SIMULATED). If the configured provider is not usable,
    # the app falls back to SIMULATED sample data and says so.
    data_provider: str = "kite"
    universe: str | None = None  # comma-separated NSE symbols for live providers; default = NIFTY 50-style list
    yahoo_history_start: str = "2023-01-01"
    yahoo_intraday_days: int = 30
    sample_seed: int = 42
    sample_start_date: str = "2023-01-02"
    sample_intraday_sessions: int = 20

    upstox_access_token: SecretStr | None = None
    upstox_universe: str | None = None  # comma-separated NSE symbols; default = NIFTY 50-style list
    upstox_history_start: str = "2023-01-01"
    upstox_intraday_days: int = 30

    kite_api_key: SecretStr | None = None
    kite_api_secret: SecretStr | None = None
    kite_access_token: SecretStr | None = None
    kite_token_created: str | None = None  # ISO timestamp of the last successful Zerodha login
    # Universe for live providers: an NSE index list (nifty50 | nifty100 | nifty200 | nifty500), refreshed weekly
    # from niftyindices.com and cached in config/universe/. UNIVERSE (explicit symbols) overrides it.
    universe_index: str = "nifty200"
    kite_history_start: str = "2018-01-01"
    kite_intraday_days: int = 30
    # Research backfill of 5-minute history (strategy tournament) for the most liquid names (NIFTY 50 list).
    research_intraday_days: int = 365

    # Live execution is not implemented. Kept as an explicit, visible flag that must remain False.
    live_trading_enabled: bool = False

    llm_provider: str = "template"
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_timeout_seconds: float = 20.0

    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    background_jobs: bool = True
    quote_refresh_seconds: int = 2
    ingest_refresh_seconds: int = 60
    # Realtime feed: "auto" (Kite WebSocket when logged in, simulated ticks for sample data),
    # "replay" (replay stored 5-minute bars as ticks - for testing outside market hours) or "off".
    realtime_mode: str = "auto"
    replay_date: str | None = None  # YYYY-MM-DD; default = last stored session
    replay_speed: float = 60.0

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
        for s in (self.kite_api_key, self.kite_api_secret, self.kite_access_token, self.llm_api_key,
                  self.upstox_access_token):
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


def update_env_file(key: str, value: str | None, path: Path | None = None) -> Path:
    """Set (or remove when value is None) KEY=value in backend/.env (server-side only; git-ignored)."""
    path = path or BACKEND_DIR / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [ln for ln in lines if not ln.strip().startswith(f"{key}=")]
    if value is not None:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path
