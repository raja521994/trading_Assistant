"""Zerodha Kite Connect adapter (READ-ONLY).

Implemented against the official `kiteconnect` Python SDK interface (https://kite.trade/docs/pykiteconnect/v4/).
It is DISABLED unless the SDK is installed (`uv sync --extra kite`) and KITE_API_KEY + KITE_ACCESS_TOKEN are
configured. The access token must be obtained via the official redirect login flow; this app never asks
for or stores a Kite password, PIN or TOTP. No order placement / modification / cancellation methods exist.

Capabilities marked `needs_verification` must be confirmed against the user's actual Kite Connect
subscription before relying on them. This adapter has NOT been tested against the live API in this build.
"""

from __future__ import annotations

import logging
import threading
import time as _time
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from .base import (
    DataStatus,
    InstrumentInfo,
    MarketDataProvider,
    ProviderCapabilities,
    ProviderHealth,
    Quote,
)

log = logging.getLogger(__name__)
IST = "Asia/Kolkata"
_INTERVALS = {"1d": "day", "5m": "5minute", "15m": "15minute", "1m": "minute", "3m": "3minute",
              "10m": "10minute", "30m": "30minute", "60m": "60minute"}
# Documented per-request lookback limits for historical data (verify against current docs).
_MAX_DAYS_PER_REQUEST = {"day": 2000, "minute": 60, "3minute": 100, "5minute": 100, "10minute": 100,
                         "15minute": 200, "30minute": 200, "60minute": 400}


class _RateLimiter:
    def __init__(self, per_second: float) -> None:
        self.interval = 1.0 / per_second
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = _time.monotonic()
            if now < self._next:
                _time.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval


class KiteProvider(MarketDataProvider):
    name = "kite"
    display_name = "Zerodha Kite Connect (read-only)"

    def __init__(self, api_key: str | None, access_token: str | None) -> None:
        self._api_key = api_key
        self._access_token = access_token
        self._kite: Any = None
        self._sdk_error: str | None = None
        self._errors: list[str] = []
        self._hist_rl = _RateLimiter(3.0)
        self._quote_rl = _RateLimiter(1.0)
        self._token_map: dict[tuple[str, str], int] = {}
        self._health_cache: tuple[float, ProviderHealth] | None = None
        try:
            from kiteconnect import KiteConnect  # type: ignore[import-not-found]

            if api_key:
                self._kite = KiteConnect(api_key=api_key)
                if access_token:
                    self._kite.set_access_token(access_token)
        except ImportError:
            self._sdk_error = "kiteconnect SDK not installed (run: uv sync --extra kite)"

    @property
    def enabled(self) -> bool:
        return bool(self._kite is not None and self._api_key and self._access_token)

    def missing_requirements(self) -> list[str]:
        out = []
        if self._sdk_error:
            out.append(self._sdk_error)
        if not self._api_key:
            out.append("KITE_API_KEY not set (requires a paid Kite Connect developer app)")
        if not self._access_token:
            out.append("KITE_ACCESS_TOKEN not set (obtain daily via the official Kite login redirect flow)")
        return out

    def _record_error(self, msg: str) -> None:
        self._errors = (self._errors + [f"{datetime.now().isoformat(timespec='seconds')} {msg}"])[-20:]
        log.warning("kite provider error: %s", msg)

    def _call(self, fn, *args, retries: int = 3, **kwargs):  # noqa: ANN001
        if not self.enabled:
            raise RuntimeError("Kite provider disabled: " + "; ".join(self.missing_requirements()))
        delay = 0.5
        for attempt in range(retries):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # SDK raises kiteconnect.exceptions.* types
                name = type(exc).__name__
                if name in ("TokenException", "PermissionException", "InputException"):
                    self._record_error(f"{name}: {exc}")
                    raise
                self._record_error(f"{name} (attempt {attempt + 1}/{retries}): {exc}")
                if attempt == retries - 1:
                    raise
                _time.sleep(delay)
                delay *= 2

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            timeframes=list(_INTERVALS.keys()),
            data_status=DataStatus.LIVE if self.enabled else DataStatus.UNAVAILABLE,
            exchanges=["NSE", "BSE", "NFO", "BFO", "MCX", "CDS"],
            instrument_types=["EQ", "INDEX", "FUT", "CE", "PE", "ETF"],
            supports_quotes=True,
            supports_streaming=False,  # KiteTicker WebSocket not wired in this build
            supports_depth=False,
            supports_oi=False,
            supports_option_chain=False,
            supports_fundamentals=False,
            supports_corporate_actions=False,
            supports_news=False,
            supports_orders=False,
            history_note="Historical candles require the Kite Connect historical-data add-on; per-request lookback limits apply.",
            missing_fields=["fundamentals", "corporate actions", "news", "delivery statistics"],
            requires=["Kite Connect developer app (paid)", "Historical data add-on", "Daily access token via official login"],
            needs_verification=[
                "Historical data add-on active on the account and lookback limits per interval",
                "Instrument master coverage for BSE equities and indices",
                "Quote fields (OHLC, volume, OI) and rate limits (quote ~1 req/s, historical ~3 req/s)",
                "WebSocket (KiteTicker) subscription limits and market-depth permissions",
                "Exchange data-redistribution terms for displaying data in this dashboard",
            ],
        )

    def health(self) -> ProviderHealth:
        if not self.enabled:
            return ProviderHealth(self.name, False, DataStatus.UNAVAILABLE,
                                  "Disabled: " + "; ".join(self.missing_requirements()), errors=list(self._errors))
        if self._health_cache and _time.monotonic() - self._health_cache[0] < 60:
            return self._health_cache[1]
        t0 = _time.perf_counter()
        try:
            self._call(self._kite.profile, retries=1)
            h = ProviderHealth(self.name, True, DataStatus.LIVE, "Connected (read-only)",
                               last_update=datetime.now(), latency_ms=(_time.perf_counter() - t0) * 1000,
                               errors=list(self._errors))
        except Exception as exc:  # noqa: BLE001
            h = ProviderHealth(self.name, False, DataStatus.UNAVAILABLE, f"Connection failed: {type(exc).__name__}",
                               errors=list(self._errors))
        self._health_cache = (_time.monotonic(), h)
        return h

    def instruments(self) -> list[InstrumentInfo]:
        out: list[InstrumentInfo] = []
        for exch in ("NSE", "BSE"):
            rows = self._call(self._kite.instruments, exch)
            for r in rows:
                itype = "INDEX" if r.get("segment") == "INDICES" else r.get("instrument_type", "EQ")
                if itype not in ("EQ", "INDEX"):
                    continue
                self._token_map[(exch, r["tradingsymbol"])] = int(r["instrument_token"])
                out.append(InstrumentInfo(r["tradingsymbol"], exch, r.get("name") or r["tradingsymbol"], itype, None,
                                          int(r.get("lot_size") or 1), float(r.get("tick_size") or 0.05),
                                          str(r["instrument_token"])))
        return out

    def candles(self, symbol: str, exchange: str, timeframe: str, start: datetime | None = None,
                end: datetime | None = None) -> pd.DataFrame:
        interval = _INTERVALS[timeframe]
        token = self._token_map.get((exchange, symbol))
        if token is None:
            self.instruments()
            token = self._token_map[(exchange, symbol)]
        end = end or datetime.now()
        start = start or end - timedelta(days=_MAX_DAYS_PER_REQUEST[interval])
        frames = []
        chunk = timedelta(days=_MAX_DAYS_PER_REQUEST[interval])
        cur = start
        while cur < end:
            nxt = min(cur + chunk, end)
            self._hist_rl.wait()
            rows = self._call(self._kite.historical_data, token, cur, nxt, interval)
            if rows:
                frames.append(pd.DataFrame(rows))
            cur = nxt + timedelta(seconds=1)
        if not frames:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.concat(frames).drop_duplicates(subset="date")
        idx = pd.DatetimeIndex(pd.to_datetime(df["date"]))
        idx = idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)
        out = df[["open", "high", "low", "close", "volume"]].astype(float)
        out.index = idx.rename("ts")
        return out.sort_index()

    def quote(self, symbol: str, exchange: str) -> Quote | None:
        self._quote_rl.wait()
        key = f"{exchange}:{symbol}"
        data = self._call(self._kite.quote, [key]).get(key)
        if not data:
            return None
        ts = data.get("timestamp") or datetime.now()
        return Quote(symbol, exchange, float(data["last_price"]), float(data.get("ohlc", {}).get("close") or 0.0),
                     ts, DataStatus.LIVE, True)

    def expected_last_bar(self, timeframe: str, now: datetime | None = None) -> datetime | None:
        return None  # determined from exchange calendar once verified; staleness falls back to age checks
