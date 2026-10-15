"""Provider registry: one active provider for analytics, all providers listed for status/coverage."""

from __future__ import annotations

from functools import lru_cache

from ..config import get_settings
from .base import MarketDataProvider
from .kite import KiteProvider
from .sample import SampleProvider
from .upstox import UpstoxProvider
from .yahoo import YahooProvider


@lru_cache
def all_providers() -> dict[str, MarketDataProvider]:
    s = get_settings()
    raw = s.universe or s.upstox_universe
    universe = [x.strip().upper() for x in raw.split(",") if x.strip()] if raw else None
    return {
        "yahoo": YahooProvider(universe, s.yahoo_history_start, s.yahoo_intraday_days),
        "upstox": UpstoxProvider(
            s.upstox_access_token.get_secret_value() if s.upstox_access_token else None,
            universe, s.upstox_history_start, s.upstox_intraday_days,
        ),
        "kite": KiteProvider(
            s.kite_api_key.get_secret_value() if s.kite_api_key else None,
            s.kite_access_token.get_secret_value() if s.kite_access_token else None,
            s.kite_token_created, universe, s.yahoo_history_start, s.yahoo_intraday_days,
        ),
        "sample": SampleProvider(s.sample_seed, s.sample_start_date, s.sample_intraday_sessions),
    }


def reset_providers() -> None:
    """Re-read settings and rebuild providers (used after credentials change)."""
    get_settings.cache_clear()
    all_providers.cache_clear()


def listed_providers() -> dict[str, MarketDataProvider]:
    """Providers shown in status/coverage: Zerodha, sample, and whichever source is active (avoids network health
    checks against sources the user is not using)."""
    act = active_provider().name
    return {k: v for k, v in all_providers().items() if k in ("kite", "sample") or k == act}


def active_provider() -> MarketDataProvider:
    s = get_settings()
    providers = all_providers()
    p = providers.get(s.data_provider)
    if p is None or not p.enabled:
        # Never silently pretend: the UI shows the configured provider is unavailable and which one is active.
        return providers["sample"]
    return p


def provider_fallback_reason() -> str | None:
    s = get_settings()
    p = all_providers().get(s.data_provider)
    if p is None:
        return f"Unknown DATA_PROVIDER '{s.data_provider}'; showing SIMULATED sample data."
    if not p.enabled:
        why = "; ".join(getattr(p, "missing_requirements", lambda: [])()) or "needs credentials"
        return f"{p.display_name} is not connected ({why}). Showing SIMULATED sample data until it is."
    return None
