"""Zerodha Kite Connect adapter (READ-ONLY market data) using the official Kite Connect v3 REST API.

Login: the official redirect flow only - the user signs in on kite.zerodha.com, Zerodha redirects to the app's
registered redirect URL with a one-time `request_token`, and the backend exchanges it (with the server-side
api_secret checksum) for an `access_token`. This app never asks for the Kite password, PIN or TOTP.
Access tokens are flushed by Zerodha daily (~06:00 IST), so a fresh login is needed each trading day.

No order endpoints are called anywhere in this module. Docs: https://kite.trade/docs/connect/v3/
Rate limits: quote 1 req/s, historical 3 req/s, other 10 req/s (throttled below these here).
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import threading
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import pandas as pd

from ..config import BACKEND_DIR
from .base import DataStatus, InstrumentInfo, MarketDataProvider, ProviderCapabilities, ProviderHealth, Quote
from .calendar import BAR_MINUTES, IST, SESSION_CLOSE, SESSION_OPEN, TradingCalendar
from .upstox import DEFAULT_UNIVERSE

log = logging.getLogger(__name__)
API = "https://api.kite.trade"
LOGIN_URL = "https://kite.zerodha.com/connect/login"
CACHE_DIR = BACKEND_DIR / "var" / "kite"

# Display symbol -> (exchange, Kite tradingsymbol, name)
INDEX_SYMBOLS: dict[str, tuple[str, str, str]] = {
    "NIFTY 50": ("NSE", "NIFTY 50", "NIFTY 50"),
    "NIFTY BANK": ("NSE", "NIFTY BANK", "NIFTY BANK"),
    "NIFTY FIN SERVICE": ("NSE", "NIFTY FIN SERVICE", "NIFTY FINANCIAL SERVICES"),
    "SENSEX": ("BSE", "SENSEX", "S&P BSE SENSEX"),
    "NIFTY IT": ("NSE", "NIFTY IT", "NIFTY IT"),
    "NIFTY PHARMA": ("NSE", "NIFTY PHARMA", "NIFTY PHARMA"),
    "NIFTY AUTO": ("NSE", "NIFTY AUTO", "NIFTY AUTO"),
    "NIFTY FMCG": ("NSE", "NIFTY FMCG", "NIFTY FMCG"),
    "NIFTY METAL": ("NSE", "NIFTY METAL", "NIFTY METAL"),
    "NIFTY ENERGY": ("NSE", "NIFTY ENERGY", "NIFTY ENERGY"),
}
MAX_DAYS = {"day": 2000, "5minute": 100}


class KiteError(RuntimeError):
    def __init__(self, status: int, error_type: str, message: str) -> None:
        super().__init__(f"{error_type or 'HTTP ' + str(status)}: {message}")
        self.status, self.error_type, self.message = status, error_type, message


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


def last_token_flush(now: datetime) -> datetime:
    """Most recent 06:00 IST (Kite access tokens are invalidated daily around this time)."""
    six = pd.Timestamp(datetime.combine(now.date(), time(6, 0))).tz_localize(IST).to_pydatetime()
    return six if now >= six else six - timedelta(days=1)


def login_url(api_key: str) -> str:
    return f"{LOGIN_URL}?{urlencode({'v': 3, 'api_key': api_key})}"


def _parse_candles(rows: list[list[Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"], dtype=float)
    df = pd.DataFrame([r[:6] for r in rows], columns=["ts", "open", "high", "low", "close", "volume"])
    idx = pd.DatetimeIndex(pd.to_datetime(df["ts"], utc=True)).tz_convert(IST)
    out = df[["open", "high", "low", "close", "volume"]].astype(float)
    out.index = idx.rename("ts")
    return out[~out.index.duplicated(keep="last")].sort_index()


def _err_from(r: httpx.Response) -> KiteError:
    try:
        j = r.json()
        return KiteError(r.status_code, j.get("error_type", ""), j.get("message", r.text[:200]))
    except ValueError:
        return KiteError(r.status_code, "", r.text[:200])


def exchange_request_token(api_key: str, api_secret: str, request_token: str,
                           transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Official token exchange: POST /session/token with checksum = sha256(api_key + request_token + api_secret)."""
    checksum = hashlib.sha256(f"{api_key}{request_token}{api_secret}".encode()).hexdigest()
    with httpx.Client(transport=transport, timeout=20.0) as c:
        r = c.post(f"{API}/session/token", headers={"X-Kite-Version": "3"},
                   data={"api_key": api_key, "request_token": request_token, "checksum": checksum})
    if r.status_code != 200:
        raise _err_from(r)
    return r.json()["data"]


class KiteProvider(MarketDataProvider):
    name = "kite"
    display_name = "Zerodha Kite Connect (read-only)"

    def __init__(self, api_key: str | None, access_token: str | None, token_created: str | None = None,
                 universe: list[str] | None = None, history_start: str = "2023-01-01", intraday_days: int = 30,
                 transport: httpx.BaseTransport | None = None, clock=_now_ist) -> None:
        self._api_key = api_key
        self._token = access_token
        self._token_created = token_created
        self.universe = universe or list(DEFAULT_UNIVERSE.keys())
        self.history_start = date.fromisoformat(history_start)
        self.intraday_days = min(max(intraday_days, 1), 60)
        self.clock = clock
        self._client = httpx.Client(base_url=API, timeout=30.0, transport=transport)
        self._hist_rl = _RateLimiter(2.5)
        self._quote_rl = _RateLimiter(0.9)
        self._lock = threading.Lock()
        self._errors: list[str] = []
        self._session_invalid: str | None = None
        self._infos: list[InstrumentInfo] | None = None
        self._tokens: dict[tuple[str, str], int] = {}
        self._kite_keys: dict[tuple[str, str], str] = {}
        self._unresolved: list[str] = []
        self._holidays: dict[date, str] = {}
        self._quotes: dict[str, dict[str, Any]] = {}
        self._quotes_at = 0.0
        self._last_ok: datetime | None = None
        self._latency: float | None = None

    # ---------------- session ----------------
    def token_expired(self) -> bool:
        if not self._token:
            return True
        if not self._token_created:
            return False
        try:
            created = pd.Timestamp(self._token_created)
            created = created.tz_localize(IST) if created.tzinfo is None else created.tz_convert(IST)
        except ValueError:
            return False
        return created.to_pydatetime() < last_token_flush(self.clock())

    @property
    def enabled(self) -> bool:
        return bool(self._api_key and self._token and not self.token_expired() and not self._session_invalid)

    def missing_requirements(self) -> list[str]:
        out = []
        if not self._api_key:
            out.append("Kite API key / secret not saved - add them in Data & Integrations")
        elif not self._token:
            out.append("Not logged in - click 'Login with Zerodha' (required once per trading day)")
        elif self.token_expired():
            out.append("Zerodha session expired (tokens reset daily ~06:00 IST) - click 'Login with Zerodha'")
        elif self._session_invalid:
            out.append(f"Zerodha rejected the session ({self._session_invalid}) - click 'Login with Zerodha'")
        return out

    def _headers(self) -> dict[str, str]:
        return {"X-Kite-Version": "3", "Authorization": f"token {self._api_key}:{self._token}"}

    def _record_error(self, msg: str) -> None:
        self._errors = (self._errors + [f"{datetime.now().isoformat(timespec='seconds')} {msg}"])[-20:]
        log.warning("kite: %s", msg)

    def _get(self, path: str, params: Any = None, limiter: _RateLimiter | None = None, raw: bool = False,
             retries: int = 3) -> Any:
        if not (self._api_key and self._token):
            raise KiteError(403, "TokenException", "Not logged in to Zerodha")
        delay = 0.5
        for attempt in range(retries):
            (limiter or self._hist_rl).wait()
            t0 = _time.perf_counter()
            try:
                r = self._client.get(path, params=params, headers=self._headers())
            except httpx.HTTPError as exc:
                self._record_error(f"{type(exc).__name__} on {path.split('?')[0]} (attempt {attempt + 1})")
                if attempt == retries - 1:
                    raise KiteError(0, "NetworkException", type(exc).__name__) from exc
                _time.sleep(delay)
                delay *= 2
                continue
            if r.status_code == 200:
                self._last_ok, self._latency = datetime.now(), (_time.perf_counter() - t0) * 1000
                return r.content if raw else r.json().get("data")
            err = _err_from(r)
            if err.error_type == "TokenException":
                self._session_invalid = err.message
                self._record_error(f"TokenException: {err.message}")
                raise err
            if err.error_type == "PermissionException":
                self._record_error(f"PermissionException: {err.message} - your Kite Connect app plan may not "
                                   "include market data / historical data")
                raise err
            if (r.status_code == 429 or r.status_code >= 500) and attempt < retries - 1:
                self._record_error(f"HTTP {r.status_code} on {path.split('/')[1]}; retrying")
                _time.sleep(delay * (3 if r.status_code == 429 else 1))
                delay *= 2
                continue
            self._record_error(str(err))
            raise err
        raise KiteError(0, "NetworkException", "retries exhausted")

    # ---------------- instruments ----------------
    def _instrument_dump(self, exchange: str) -> list[dict[str, str]]:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = CACHE_DIR / f"{exchange}-{self.clock().date().isoformat()}.csv"
        if not path.exists():
            content = self._get(f"/instruments/{exchange}", limiter=_RateLimiter(5), raw=True)
            path.write_bytes(content)
            for old in CACHE_DIR.glob(f"{exchange}-*.csv"):
                if old != path:
                    old.unlink(missing_ok=True)
        return list(csv.DictReader(io.StringIO(Path(path).read_text(encoding="utf-8"))))

    def instruments(self) -> list[InstrumentInfo]:
        with self._lock:
            if self._infos is not None:
                return self._infos
        nse = self._instrument_dump("NSE")
        bse = self._instrument_dump("BSE")
        eq = {r["tradingsymbol"]: r for r in nse if r.get("segment") == "NSE" and r.get("instrument_type") == "EQ"}
        idx = {("NSE", r["tradingsymbol"]): r for r in nse if r.get("segment") == "INDICES"}
        idx.update({("BSE", r["tradingsymbol"]): r for r in bse if r.get("segment") == "INDICES"})
        infos, tokens, keys, unresolved = [], {}, {}, []
        for sym in self.universe:
            r = eq.get(sym)
            if r is None:
                unresolved.append(sym)
                continue
            tokens[("NSE", sym)] = int(r["instrument_token"])
            keys[("NSE", sym)] = f"NSE:{sym}"
            infos.append(InstrumentInfo(sym, "NSE", (r.get("name") or sym).title(), "EQ", DEFAULT_UNIVERSE.get(sym),
                                        int(float(r.get("lot_size") or 1)), float(r.get("tick_size") or 0.05),
                                        r["instrument_token"]))
        for sym, (exch, ts, name) in INDEX_SYMBOLS.items():
            r = idx.get((exch, ts))
            if r is None:
                unresolved.append(sym)
                continue
            tokens[(exch, sym)] = int(r["instrument_token"])
            keys[(exch, sym)] = f"{exch}:{ts}"
            infos.append(InstrumentInfo(sym, exch, name, "INDEX", None, 1, 0.05, r["instrument_token"]))
        with self._lock:
            self._infos, self._tokens, self._kite_keys, self._unresolved = infos, tokens, keys, unresolved
        if unresolved:
            log.warning("kite: not found in instrument master: %s", ", ".join(unresolved))
        return infos

    # ---------------- calendar ----------------
    def _calendar(self) -> TradingCalendar:
        return TradingCalendar(self._holidays)

    def session_state(self, now: datetime) -> str:
        return self._calendar().session_state(now)

    def expected_last_bar(self, timeframe: str, now: datetime | None = None) -> datetime | None:
        return self._calendar().expected_last_bar(timeframe, now or self.clock())

    def _infer_holiday(self) -> None:
        """Kite has no holiday API: if it's a weekday after 09:45 IST and NIFTY 50 hasn't traded today, it's a holiday."""
        now = self.clock()
        if now.weekday() >= 5 or now.time() < time(9, 45):
            return
        q = self._quotes.get("NSE:NIFTY 50")
        ts = (q or {}).get("last_trade_time") or (q or {}).get("timestamp")
        if ts:
            try:
                if pd.Timestamp(ts).date() < now.date():
                    self._holidays[now.date()] = "exchange closed (no trading today)"
            except ValueError:
                pass

    # ---------------- candles ----------------
    def _historical(self, token: int, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
        frames = []
        cur = start
        step = timedelta(days=MAX_DAYS[interval] - 1)
        while cur <= end:
            nxt = min(cur + step, end)
            data = self._get(f"/instruments/historical/{token}/{interval}",
                             params={"from": cur.strftime("%Y-%m-%d %H:%M:%S"), "to": nxt.strftime("%Y-%m-%d %H:%M:%S")})
            frames.append(_parse_candles((data or {}).get("candles") or []))
            cur = nxt + timedelta(seconds=1)
        return pd.concat(frames) if frames else _parse_candles([])

    def candles(self, symbol: str, exchange: str, timeframe: str, start: datetime | None = None,
                end: datetime | None = None) -> pd.DataFrame:
        if not self._tokens:
            self.instruments()
        token = self._tokens.get((exchange, symbol))
        if token is None:
            raise KeyError(f"{exchange}:{symbol} not found in the Kite instrument master")
        now = self.clock()
        today = now.date()
        naive_now = now.replace(tzinfo=None)
        if timeframe == "1d":
            s = pd.Timestamp(start).tz_convert(IST).date() if start is not None else self.history_start
            df = self._historical(token, "day", datetime.combine(s, time(0, 0)), naive_now)
            if len(df):
                df.index = pd.DatetimeIndex([pd.Timestamp(t.date()).tz_localize(IST) for t in df.index], name="ts")
                df = df[~df.index.duplicated(keep="last")]
        elif timeframe == "5m":
            default_start = datetime.combine(today - timedelta(days=self.intraday_days), time(9, 15))
            s = pd.Timestamp(start).tz_convert(IST).tz_localize(None).to_pydatetime() if start is not None \
                else default_start
            df = self._historical(token, "5minute", s, naive_now)
            if len(df):
                t = df.index.time
                df = df[(t >= SESSION_OPEN) & (t < SESSION_CLOSE)]
                df = df[df.index + pd.Timedelta(minutes=BAR_MINUTES) <= pd.Timestamp(now)]  # completed bars only
        else:
            raise ValueError(f"Unsupported timeframe {timeframe}")
        expected = self.expected_last_bar(timeframe, now)
        if expected is not None:
            df = df[df.index <= pd.Timestamp(expected)]
        if start is not None:
            df = df[df.index >= pd.Timestamp(start)]
        if end is not None:
            df = df[df.index <= pd.Timestamp(end)]
        return df.sort_index()

    # ---------------- quotes ----------------
    def _refresh_quotes(self) -> None:
        if not self._kite_keys:
            self.instruments()
        keys = list(self._kite_keys.values())
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(keys), 450):
            data = self._get("/quote", params=[("i", k) for k in keys[i:i + 450]], limiter=self._quote_rl) or {}
            out.update(data)
        self._quotes, self._quotes_at = out, _time.monotonic()
        self._infer_holiday()

    def quote(self, symbol: str, exchange: str) -> Quote | None:
        if not self.enabled:
            return None
        if _time.monotonic() - self._quotes_at > 3.0:
            try:
                self._refresh_quotes()
            except KiteError:
                if not self._quotes:
                    return None
        key = self._kite_keys.get((exchange, symbol))
        q = self._quotes.get(key) if key else None
        if not q or q.get("last_price") is None:
            return None
        now = self.clock()
        open_ = self._calendar().in_session(now)
        prev = float((q.get("ohlc") or {}).get("close") or 0.0)
        return Quote(symbol, exchange, float(q["last_price"]), prev, now,
                     DataStatus.LIVE if open_ else DataStatus.EOD, open_)

    # ---------------- status ----------------
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            timeframes=["1d", "5m"], data_status=DataStatus.LIVE if self.enabled else DataStatus.UNAVAILABLE,
            exchanges=["NSE", "BSE"], instrument_types=["EQ", "INDEX"], supports_quotes=True,
            supports_streaming=False, supports_depth=False, supports_oi=False, supports_option_chain=False,
            supports_fundamentals=False, supports_corporate_actions=False, supports_news=False, supports_orders=False,
            history_note=(f"Daily from {self.history_start.isoformat()}; 5-minute bars for the last "
                          f"{self.intraday_days} days plus today's completed bars; quotes polled every few seconds"),
            missing_fields=["market depth (not used)", "open interest", "implied volatility", "Greeks", "option chain",
                            "fundamentals", "corporate actions", "delivery statistics", "news / announcements"],
            requires=["Kite Connect developer app (API key + secret) with market-data access",
                      "Daily login via the official Zerodha redirect flow"],
            needs_verification=[
                "Your Kite Connect plan includes live quotes and historical candles (otherwise PermissionException)",
                "Universe is a static NIFTY 50-style list; verify against current index constituents",
                "Holidays are inferred (Kite has no holiday API); exchange calendar edge cases may need review",
                "KiteTicker WebSocket streaming is not wired yet (REST polling every few seconds)",
            ],
        )

    def health(self) -> ProviderHealth:
        if not self.enabled:
            return ProviderHealth(self.name, False, DataStatus.UNAVAILABLE,
                                  "Not connected: " + "; ".join(self.missing_requirements()), errors=list(self._errors))
        try:
            if _time.monotonic() - self._quotes_at > 10:
                self._refresh_quotes()
            msg = "Connected (official Kite Connect API, read-only)"
            if self._unresolved:
                msg += f"; not found in instrument master: {', '.join(self._unresolved)}"
            return ProviderHealth(self.name, True, DataStatus.LIVE, msg, last_update=self._last_ok,
                                  latency_ms=self._latency, errors=list(self._errors))
        except KiteError as exc:
            hint = ""
            if exc.error_type == "PermissionException":
                hint = " - your Kite Connect app plan may not include market data"
            elif exc.error_type == "TokenException":
                hint = " - login with Zerodha again"
            return ProviderHealth(self.name, False, DataStatus.UNAVAILABLE, f"Connection failed: {exc}{hint}",
                                  last_update=self._last_ok, errors=list(self._errors))
        except Exception as exc:  # noqa: BLE001
            self._record_error(f"{type(exc).__name__}: {exc}")
            return ProviderHealth(self.name, False, DataStatus.UNAVAILABLE, f"Connection failed: {type(exc).__name__}",
                                  last_update=self._last_ok, errors=list(self._errors))
