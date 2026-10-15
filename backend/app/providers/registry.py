"""Provider registry: one active provider for analytics, all providers listed for status/coverage."""

from __future__ import annotations

from functools import lru_cache

from ..config import get_settings
from .base import MarketDataProvider
from .kite import KiteProvider
from .sample import SampleProvider
from .upstox import UpstoxProvider


@lru_cache
def all_providers() -> dict[str, MarketDataProvider]:
    s = get_settings()
    universe = [x.strip().upper() for x in s.upstox_universe.split(",") if x.strip()] if s.upstox_universe else None
    return {
        "upstox": UpstoxProvider(
            s.upstox_access_token.get_secret_value() if s.upstox_access_token else None,
            universe, s.upstox_history_start, s.upstox_intraday_days,
        ),
        "kite": KiteProvider(
            s.kite_api_key.get_secret_value() if s.kite_api_key else None,
            s.kite_access_token.get_secret_value() if s.kite_access_token else None,
        ),
        "sample": SampleProvider(s.sample_seed, s.sample_start_date, s.sample_intraday_sessions),
    }


def reset_providers() -> None:
    """Re-read settings and rebuild providers (used after credentials change)."""
    get_settings.cache_clear()
    all_providers.cache_clear()


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
        return (f"Live data is not connected yet ({p.display_name} needs credentials) - showing SIMULATED sample "
                "data. Connect it in Data & Integrations.")
    return None
