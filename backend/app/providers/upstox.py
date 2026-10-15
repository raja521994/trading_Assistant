"""Upstox market-data adapter (READ-ONLY, real-time) using the official REST API v2/v3.

Authentication: a free, read-only **Analytics Token** (1-year validity) generated at
https://account.upstox.com/developer/apps#analytics . It cannot place or modify orders, and no
order-related endpoint is called anywhere in this module. The token is read from the server-side
environment (UPSTOX_ACCESS_TOKEN) and is never sent to the browser or logged.

Endpoints used (https://upstox.com/developer/api-documentation):
- Instrument master (public BOD JSON): assets.upstox.com/market-quote/instruments/exchange/{NSE,BSE}.json.gz
- Historical candles V3:  GET /v3/historical-candle/{key}/{unit}/{interval}/{to}/{from}
- Intraday candles V3:    GET /v3/historical-candle/intraday/{key}/{unit}/{interval}
- LTP quotes V3:          GET /v3/market-quote/ltp?instrument_key=...  (up to 500 keys per call)
- Market holidays:        GET /v2/market/holidays
Rate limits (standard APIs): 50 req/s, 500 req/min, 2000 req/30 min -> throttled well below that here.
"""

from __future__ import annotations

import gzip
import json
import logging
import threading
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote as urlquote

import httpx
import pandas as pd

from ..config import BACKEND_DIR
from .base import DataStatus, InstrumentInfo, MarketDataProvider, ProviderCapabilities, ProviderHealth, Quote
from .calendar import BAR_MINUTES, IST, TradingCalendar

log = logging.getLogger(__name__)
API = "https://api.upstox.com"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/{ex}.json.gz"
CACHE_DIR = BACKEND_DIR / "var" / "upstox"

# Display symbol -> (exchange, instrument_key)
INDEX_KEYS: dict[str, tuple[str, str, str]] = {
    "NIFTY 50": ("NSE", "NSE_INDEX|Nifty 50", "NIFTY 50"),
    "NIFTY BANK": ("NSE", "NSE_INDEX|Nifty Bank", "NIFTY BANK"),
    "NIFTY FIN SERVICE": ("NSE", "NSE_INDEX|Nifty Fin Service", "NIFTY FINANCIAL SERVICES"),
    "SENSEX": ("BSE", "BSE_INDEX|SENSEX", "S&P BSE SENSEX"),
    "NIFTY IT": ("NSE", "NSE_INDEX|Nifty IT", "NIFTY IT"),
    "NIFTY PHARMA": ("NSE", "NSE_INDEX|Nifty Pharma", "NIFTY PHARMA"),
    "NIFTY AUTO": ("NSE", "NSE_INDEX|Nifty Auto", "NIFTY AUTO"),
    "NIFTY FMCG": ("NSE", "NSE_INDEX|Nifty FMCG", "NIFTY FMCG"),
    "NIFTY METAL": ("NSE", "NSE_INDEX|Nifty Metal", "NIFTY METAL"),
    "NIFTY ENERGY": ("NSE", "NSE_INDEX|Nifty Energy", "NIFTY ENERGY"),
}

# Default universe: a NIFTY 50-style list (static; not auto-synced with index rebalances). Sector labels are ours.
DEFAULT_UNIVERSE: dict[str, str] = {
    "ADANIENT": "Diversified", "ADANIPORTS": "Infrastructure", "APOLLOHOSP": "Healthcare", "ASIANPAINT": "Consumer",
    "AXISBANK": "Banks", "BAJAJ-AUTO": "Auto", "BAJFINANCE": "Financial Services", "BAJAJFINSV": "Financial Services",
    "BEL": "Capital Goods", "BHARTIARTL": "Telecom", "CIPLA": "Pharma", "COALINDIA": "Metals", "DRREDDY": "Pharma",
    "EICHERMOT": "Auto", "ETERNAL": "Consumer", "GRASIM": "Cement", "HCLTECH": "IT", "HDFCBANK": "Banks",
    "HDFCLIFE": "Financial Services", "HINDALCO": "Metals", "HINDUNILVR": "FMCG", "ICICIBANK": "Banks",
    "INDIGO": "Services", "INFY": "IT", "ITC": "FMCG", "JIOFIN": "Financial Services", "JSWSTEEL": "Metals",
    "KOTAKBANK": "Banks", "LT": "Capital Goods", "M&M": "Auto", "MARUTI": "Auto", "MAXHEALTH": "Healthcare",
    "NESTLEIND": "FMCG", "NTPC": "Power", "ONGC": "Energy", "POWERGRID": "Power", "RELIANCE": "Energy",
    "SBILIFE": "Financial Services", "SBIN": "Banks", "SHRIRAMFIN": "Financial Services", "SUNPHARMA": "Pharma",
    "TATACONSUM": "FMCG", "TMPV": "Auto", "TATASTEEL": "Metals", "TCS": "IT", "TECHM": "IT", "TITAN": "Consumer",
    "TRENT": "Consumer", "ULTRACEMCO": "Cement", "WIPRO": "IT",
}


class UpstoxError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


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
                now = _time.monotonic()
            self._next = max(now, self._next) + self.interval


def _now_ist() -> datetime:
    return pd.Timestamp.now(tz=IST).to_pydatetime()


def _parse_candles(rows: list[list[Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"], dtype=float)
    df = pd.DataFrame([r[:6] for r in rows], columns=["ts", "open", "high", "low", "close", "volume"])
    idx = pd.DatetimeIndex(pd.to_datetime(df["ts"], utc=True)).tz_convert(IST)
    out = df[["open", "high", "low", "close", "volume"]].astype(float)
    out.index = idx.rename("ts")
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


class UpstoxProvider(MarketDataProvider):
    name = "upstox"
    display_name = "Upstox (real-time, read-only)"

    def __init__(self, access_token: str | None, universe: list[str] | None = None, history_start: str = "2023-01-01",
                 intraday_days: int = 30, transport: httpx.BaseTransport | None = None, clock=_now_ist) -> None:
        self._token = access_token
        self.universe = universe or list(DEFAULT_UNIVERSE.keys())
        self.history_start = date.fromisoformat(history_start)
        self.intraday_days = min(max(intraday_days, 1), 30)
        self.clock = clock
        self._client = httpx.Client(base_url=API, timeout=20.0, transport=transport,
                                    headers={"Accept": "application/json"})
        self._public = httpx.Client(timeout=60.0, transport=transport, follow_redirects=True)
        self._rl = _RateLimiter(8.0)
        self._lock = threading.Lock()
        self._errors: list[str] = []
        self._keys: dict[tuple[str, str], str] = {}
        self._infos: list[InstrumentInfo] | None = None
        self._unresolved: list[str] = []
        self._holidays: dict[date, str] | None = None
        self._holidays_loaded: date | None = None
        self._quotes: dict[str, dict[str, Any]] = {}
        self._quotes_at = 0.0
        self._last_ok: datetime | None = None
        self._last_latency: float | None = None

    # ---------------- HTTP ----------------
    @property
    def enabled(self) -> bool:
        return bool(self._token)

    def missing_requirements(self) -> list[str]:
        return [] if self._token else [
            "UPSTOX_ACCESS_TOKEN not set - generate a free read-only Analytics Token at "
            "https://account.upstox.com/developer/apps#analytics and paste it in Data & Integrations"]

    def _record_error(self, msg: str) -> None:
        self._errors = (self._errors + [f"{datetime.now().isoformat(timespec='seconds')} {msg}"])[-20:]
        log.warning("upstox: %s", msg)

    def _get(self, path: str, params: dict[str, Any] | None = None, retries: int = 3) -> Any:
        if not self._token:
            raise UpstoxError(401, "No Upstox access token configured")
        delay = 0.5
        for attempt in range(retries):
            self._rl.wait()
            t0 = _time.perf_counter()
            try:
                r = self._client.get(path, params=params, headers={"Authorization": f"Bearer {self._token}"})
            except httpx.HTTPError as exc:
                self._record_error(f"{type(exc).__name__} on {path.split('?')[0]} (attempt {attempt + 1})")
                if attempt == retries - 1:
                    raise UpstoxError(0, f"Network error: {type(exc).__name__}") from exc
                _time.sleep(delay)
                delay *= 2
                continue
            if r.status_code == 200:
                self._last_ok = datetime.now()
                self._last_latency = (_time.perf_counter() - t0) * 1000
                return r.json().get("data")
            msg = r.text[:300]
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                self._record_error(f"HTTP {r.status_code} on {path.split('/')[2] if path.count('/') > 1 else path}; retrying")
                _time.sleep(delay * (4 if r.status_code == 429 else 1))
                delay *= 2
                continue
            self._record_error(f"HTTP {r.status_code}: {msg}")
            raise UpstoxError(r.status_code, msg)
        raise UpstoxError(0, "retries exhausted")

    # ---------------- instruments ----------------
    def _instrument_master(self, exchange: str) -> list[dict[str, Any]]:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = CACHE_DIR / f"{exchange}-{date.today().isoformat()}.json.gz"
        if not path.exists():
            r = self._public.get(INSTRUMENTS_URL.format(ex=exchange))
            r.raise_for_status()
            path.write_bytes(r.content)
            for old in CACHE_DIR.glob(f"{exchange}-*.json.gz"):
                if old != path:
                    old.unlink(missing_ok=True)
        return json.loads(gzip.decompress(Path(path).read_bytes()))

    def instruments(self) -> list[InstrumentInfo]:
        with self._lock:
            if self._infos is not None:
                return self._infos
        master = self._instrument_master("NSE")
        eq = {d["trading_symbol"]: d for d in master if d.get("segment") == "NSE_EQ" and d.get("instrument_type") == "EQ"}
        infos: list[InstrumentInfo] = []
        keys: dict[tuple[str, str], str] = {}
        unresolved = []
        for sym in self.universe:
            d = eq.get(sym)
            if d is None:
                unresolved.append(sym)
                continue
            keys[("NSE", sym)] = d["instrument_key"]
            infos.append(InstrumentInfo(sym, "NSE", str(d.get("name") or sym).title(), "EQ",
                                        DEFAULT_UNIVERSE.get(sym), int(d.get("lot_size") or 1),
                                        float(d.get("tick_size") or 5) / 100.0, d["instrument_key"]))
        for sym, (exch, key, name) in INDEX_KEYS.items():
            keys[(exch, sym)] = key
            infos.append(InstrumentInfo(sym, exch, name, "INDEX", None, 1, 0.05, key))
        with self._lock:
            self._keys, self._infos, self._unresolved = keys, infos, unresolved
        if unresolved:
            log.warning("upstox: symbols not found in instrument master: %s", ", ".join(unresolved))
        return infos

    def key_for(self, symbol: str, exchange: str) -> str:
        if not self._keys:
            self.instruments()
        return self._keys[(exchange, symbol)]

    # ---------------- calendar ----------------
    def calendar(self) -> TradingCalendar:
        today = self.clock().date()
        if self._holidays is None or self._holidays_loaded != today:
            hol: dict[date, str] = {}
            if self._token:
                try:
                    for h in self._get("/v2/market/holidays") or []:
                        if h.get("holiday_type") == "TRADING_HOLIDAY" and "NSE" in (h.get("closed_exchanges") or []):
                            hol[date.fromisoformat(h["date"])] = h.get("description", "holiday")
                except UpstoxError:
                    pass  # falls back to weekday calendar; staleness checks still apply
            self._holidays, self._holidays_loaded = hol, today
        return TradingCalendar(self._holidays)

    def session_state(self, now: datetime) -> str:
        return self.calendar().session_state(now)

    def expected_last_bar(self, timeframe: str, now: datetime | None = None) -> datetime | None:
        return self.calendar().expected_last_bar(timeframe, now or self.clock())

    # ---------------- candles ----------------
    def _historical(self, key: str, unit: str, interval: int, start: date, end: date) -> pd.DataFrame:
        if end < start:
            return _parse_candles([])
        k = urlquote(key, safe="")
        data = self._get(f"/v3/historical-candle/{k}/{unit}/{interval}/{end.isoformat()}/{start.isoformat()}")
        return _parse_candles((data or {}).get("candles") or [])

    def _intraday(self, key: str, unit: str, interval: int) -> pd.DataFrame:
        k = urlquote(key, safe="")
        data = self._get(f"/v3/historical-candle/intraday/{k}/{unit}/{interval}")
        return _parse_candles((data or {}).get("candles") or [])

    def candles(self, symbol: str, exchange: str, timeframe: str, start: datetime | None = None,
                end: datetime | None = None) -> pd.DataFrame:
        key = self.key_for(symbol, exchange)
        now = self.clock()
        cal = self.calendar()
        expected = cal.expected_last_bar(timeframe, now)
        today = now.date()
        start_d = pd.Timestamp(start).tz_convert(IST).date() if start is not None else None
        frames = []
        if timeframe == "1d":
            s = start_d or self.history_start
            if s < today:
                frames.append(self._historical(key, "days", 1, s, today - timedelta(days=1)))
            if cal.is_trading_day(today) and expected is not None and expected.date() == today:
                frames.append(self._intraday(key, "days", 1))  # today's completed session (after 15:30 IST)
            df = pd.concat(frames) if frames else _parse_candles([])
            if len(df):
                df.index = pd.DatetimeIndex([pd.Timestamp(t.date()).tz_localize(IST) for t in df.index], name="ts")
                df = df[~df.index.duplicated(keep="first")]
        elif timeframe == "5m":
            s = start_d or (today - timedelta(days=self.intraday_days))
            s = max(s, today - timedelta(days=30))
            if s < today:
                frames.append(self._historical(key, "minutes", BAR_MINUTES, s, today - timedelta(days=1)))
            if cal.is_trading_day(today) and now.time() >= datetime(2000, 1, 1, 9, 15).time():
                frames.append(self._intraday(key, "minutes", BAR_MINUTES))
            df = pd.concat(frames) if frames else _parse_candles([])
            if len(df):
                df = df[~df.index.duplicated(keep="last")]
                # only bars that have fully completed
                df = df[df.index + pd.Timedelta(minutes=BAR_MINUTES) <= pd.Timestamp(now)]
        else:
            raise ValueError(f"Unsupported timeframe {timeframe}")
        df = df.sort_index()
        if expected is not None:
            df = df[df.index <= pd.Timestamp(expected)]
        if start is not None:
            df = df[df.index >= pd.Timestamp(start)]
        if end is not None:
            df = df[df.index <= pd.Timestamp(end)]
        return df

    # ---------------- quotes ----------------
    def _refresh_quotes(self) -> None:
        if not self._keys:
            self.instruments()
        keys = list(self._keys.values())
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(keys), 450):
            data = self._get("/v3/market-quote/ltp", params={"instrument_key": ",".join(keys[i:i + 450])}) or {}
            for v in data.values():
                tok = v.get("instrument_token")
                if tok:
                    out[tok] = v
        self._quotes, self._quotes_at = out, _time.monotonic()

    def quote(self, symbol: str, exchange: str) -> Quote | None:
        if not self._token:
            return None
        with self._lock:
            stale = _time.monotonic() - self._quotes_at > 2.0
        if stale:
            try:
                self._refresh_quotes()
            except UpstoxError:
                if not self._quotes:
                    return None
        try:
            key = self.key_for(symbol, exchange)
        except KeyError:
            return None
        q = self._quotes.get(key)
        if not q or q.get("last_price") is None:
            return None
        now = self.clock()
        open_ = self.calendar().in_session(now)
        return Quote(symbol, exchange, float(q["last_price"]), float(q.get("cp") or 0.0), now,
                     DataStatus.LIVE if open_ else DataStatus.EOD, open_)

    # ---------------- status ----------------
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            timeframes=["1d", "5m"],
            data_status=DataStatus.LIVE if self.enabled else DataStatus.UNAVAILABLE,
            exchanges=["NSE", "BSE"],
            instrument_types=["EQ", "INDEX"],
            supports_quotes=True, supports_streaming=False, supports_depth=False, supports_oi=False,
            supports_option_chain=False, supports_fundamentals=False, supports_corporate_actions=False,
            supports_news=False, supports_orders=False,
            history_note=(f"Daily from {self.history_start.isoformat()}; 5-minute bars for the last "
                          f"{self.intraday_days} days plus today's completed bars; LTP quotes polled every few seconds"),
            missing_fields=["market depth", "open interest", "implied volatility", "Greeks", "option chain",
                            "fundamentals", "corporate actions", "delivery statistics", "news / announcements"],
            requires=["Upstox account", "Free read-only Analytics Token (1-year validity)"],
            needs_verification=[
                "Universe is a static NIFTY 50-style list; verify against current index constituents",
                "Historical candles are as provided by Upstox (not adjusted here for splits/bonuses)",
                "Option chain, fundamentals, news and WebSocket feeds are available from Upstox but not wired in this build",
                "Exchange data-redistribution terms if the dashboard is shared beyond personal use",
            ],
        )

    def health(self) -> ProviderHealth:
        if not self._token:
            return ProviderHealth(self.name, False, DataStatus.UNAVAILABLE,
                                  "Not connected: " + "; ".join(self.missing_requirements()), errors=list(self._errors))
        try:
            t0 = _time.perf_counter()
            if _time.monotonic() - self._quotes_at > 10:
                self._refresh_quotes()
            latency = self._last_latency if self._last_latency is not None else (_time.perf_counter() - t0) * 1000
            msg = "Connected (read-only Analytics Token)"
            if self._unresolved:
                msg += f"; not found in instrument master: {', '.join(self._unresolved)}"
            return ProviderHealth(self.name, True, DataStatus.LIVE, msg, last_update=self._last_ok,
                                  latency_ms=latency, errors=list(self._errors))
        except UpstoxError as exc:
            hint = " (token invalid or expired - generate a new Analytics Token)" if exc.status == 401 else ""
            return ProviderHealth(self.name, False, DataStatus.UNAVAILABLE, f"Connection failed: {exc}{hint}",
                                  last_update=self._last_ok, errors=list(self._errors))
        except Exception as exc:  # noqa: BLE001 - e.g. instrument master download failure
            self._record_error(f"{type(exc).__name__}: {exc}")
            return ProviderHealth(self.name, False, DataStatus.UNAVAILABLE,
                                  f"Connection failed: {type(exc).__name__}", last_update=self._last_ok,
                                  errors=list(self._errors))


def validate_token(token: str, transport: httpx.BaseTransport | None = None) -> tuple[bool, str]:
    """Check a token with one read-only LTP call (NIFTY 50). Returns (ok, message)."""
    try:
        with httpx.Client(transport=transport, timeout=15.0) as client:
            r = client.get(f"{API}/v3/market-quote/ltp", params={"instrument_key": "NSE_INDEX|Nifty 50"},
                           headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    except httpx.HTTPError as exc:
        return False, f"Could not reach Upstox ({type(exc).__name__})"
    if r.status_code == 200:
        data = (r.json() or {}).get("data") or {}
        entry = next((v for v in data.values() if v.get("instrument_token") == "NSE_INDEX|Nifty 50"),
                     next(iter(data.values()), {}))
        return True, f"Token valid - NIFTY 50 last price {entry.get('last_price')}"
    if r.status_code == 401:
        return False, "Upstox rejected the token (401). Generate a new Analytics Token and try again."
    return False, f"Upstox returned HTTP {r.status_code}"
