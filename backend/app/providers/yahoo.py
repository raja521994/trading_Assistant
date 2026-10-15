"""Yahoo Finance adapter (READ-ONLY) via the open-source `yfinance` library.

IMPORTANT DISCLOSURES (also shown in the UI):
- Yahoo Finance has no official public API. `yfinance` uses Yahoo's publicly reachable but undocumented web
  endpoints; Yahoo's terms restrict use to personal, non-commercial purposes. No Yahoo login is used or needed.
- NSE/BSE quotes from Yahoo may be delayed (exchange-dependent, up to ~15 minutes) -> labelled DELAYED.
- Prices are not adjusted for corporate actions here (raw OHLC, auto_adjust=False).

Efficiency: requests are batched across the whole universe (one download per timeframe) and cached briefly,
so per-instrument calls from the ingestion layer don't multiply HTTP traffic.
"""

from __future__ import annotations

import logging
import threading
import time as _time
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd

from .base import DataStatus, InstrumentInfo, MarketDataProvider, ProviderCapabilities, ProviderHealth, Quote
from .calendar import BAR_MINUTES, IST, SESSION_CLOSE, SESSION_OPEN, TradingCalendar
from .upstox import DEFAULT_UNIVERSE

log = logging.getLogger(__name__)

INDEX_TICKERS: dict[str, tuple[str, str, str]] = {
    "NIFTY 50": ("NSE", "^NSEI", "NIFTY 50"),
    "NIFTY BANK": ("NSE", "^NSEBANK", "NIFTY BANK"),
    "NIFTY FIN SERVICE": ("NSE", "NIFTY_FIN_SERVICE.NS", "NIFTY FINANCIAL SERVICES"),
    "SENSEX": ("BSE", "^BSESN", "S&P BSE SENSEX"),
    "NIFTY IT": ("NSE", "^CNXIT", "NIFTY IT"),
    "NIFTY PHARMA": ("NSE", "^CNXPHARMA", "NIFTY PHARMA"),
    "NIFTY AUTO": ("NSE", "^CNXAUTO", "NIFTY AUTO"),
    "NIFTY FMCG": ("NSE", "^CNXFMCG", "NIFTY FMCG"),
    "NIFTY METAL": ("NSE", "^CNXMETAL", "NIFTY METAL"),
    "NIFTY ENERGY": ("NSE", "^CNXENERGY", "NIFTY ENERGY"),
}
MAX_INTRADAY_DAYS = 59  # Yahoo serves 5-minute bars for roughly the last 60 days


def _now_ist() -> datetime:
    return pd.Timestamp.now(tz=IST).to_pydatetime()


def _default_download(tickers: list[str], **kw: Any) -> pd.DataFrame:
    import yfinance as yf

    return yf.download(tickers, group_by="ticker", auto_adjust=False, progress=False, threads=True, **kw)


def _split(raw: pd.DataFrame, tickers: list[str]) -> dict[str, pd.DataFrame]:
    """yfinance batch frame -> {ticker: OHLCV frame with IST index}."""
    out: dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return out
    multi = isinstance(raw.columns, pd.MultiIndex)
    for t in tickers:
        try:
            sub = raw[t] if multi else raw
        except KeyError:
            continue
        cols = {c: str(c).lower() for c in sub.columns}
        sub = sub.rename(columns=cols)
        if not {"open", "high", "low", "close"}.issubset(sub.columns):
            continue
        df = sub[["open", "high", "low", "close"]].copy()
        df["volume"] = sub["volume"] if "volume" in sub.columns else 0.0
        df = df.dropna(subset=["open", "high", "low", "close"]).astype(float)
        df["volume"] = df["volume"].fillna(0.0)
        idx = pd.DatetimeIndex(df.index)
        idx = idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)
        df.index = idx.rename("ts")
        out[t] = df[~df.index.duplicated(keep="last")].sort_index()
    return out


def _aggregate_daily(m: pd.DataFrame | None) -> pd.DataFrame | None:
    """5-minute bars -> daily OHLCV (session bars only)."""
    if m is None or m.empty:
        return None
    t = m.index.time
    m = m[(t >= SESSION_OPEN) & (t < SESSION_CLOSE)]
    if m.empty:
        return None
    g = m.groupby(m.index.date)
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "volume": g["volume"].sum()})
    out.index = pd.DatetimeIndex([pd.Timestamp(d).tz_localize(IST) for d in out.index], name="ts")
    return out


class YahooProvider(MarketDataProvider):
    name = "yahoo"
    display_name = "Yahoo Finance (unofficial, may be delayed)"

    def __init__(self, universe: list[str] | None = None, history_start: str = "2023-01-01",
                 intraday_days: int = 30, download=None, clock=_now_ist) -> None:
        self.universe = universe or list(DEFAULT_UNIVERSE.keys())
        self.history_start = date.fromisoformat(history_start)
        self.intraday_days = min(max(intraday_days, 1), MAX_INTRADAY_DAYS)
        self._download = download
        self.clock = clock
        self._lock = threading.Lock()
        self._batch: dict[tuple, tuple[float, dict[str, pd.DataFrame]]] = {}
        self._quote_batch: tuple[float, dict[str, pd.DataFrame]] | None = None
        self._holidays: dict[date, str] = {}
        self._errors: list[str] = []
        self._last_ok: datetime | None = None
        self._latency: float | None = None
        self._tickers: dict[tuple[str, str], str] = {}
        self.derived_daily: set[str] = set()
        for sym in self.universe:
            self._tickers[("NSE", sym)] = f"{sym}.NS"
        for sym, (exch, tk, _) in INDEX_TICKERS.items():
            self._tickers[(exch, sym)] = tk

    # ---------------- helpers ----------------
    def _record_error(self, msg: str) -> None:
        self._errors = (self._errors + [f"{datetime.now().isoformat(timespec='seconds')} {msg}"])[-20:]
        log.warning("yahoo: %s", msg)

    def _fetch(self, key: tuple, ttl: float, tickers: list[str] | None = None, **kw: Any) -> dict[str, pd.DataFrame]:
        with self._lock:
            hit = self._batch.get(key)
            if hit and _time.monotonic() - hit[0] < ttl:
                return hit[1]
        tickers = tickers or list(self._tickers.values())
        t0 = _time.perf_counter()
        try:
            raw = (self._download or _default_download)(tickers, **kw)
        except Exception as exc:  # noqa: BLE001 - network / Yahoo errors
            self._record_error(f"download failed: {type(exc).__name__}: {exc}")
            raise
        data = _split(raw, tickers)
        if not data:
            self._record_error("download returned no data (rate-limited or network issue)")
        else:
            self._last_ok, self._latency = datetime.now(), (_time.perf_counter() - t0) * 1000
        with self._lock:
            self._batch[key] = (_time.monotonic(), data)
            if len(self._batch) > 20:
                oldest = sorted(self._batch.items(), key=lambda kv: kv[1][0])[:10]
                for k, _ in oldest:
                    self._batch.pop(k, None)
        return data

    def _calendar(self) -> TradingCalendar:
        return TradingCalendar(self._holidays)

    def _infer_holiday(self, daily: dict[str, pd.DataFrame], now: datetime) -> None:
        """If it's a weekday past 09:45 IST and the benchmark has no bar for today, treat today as a holiday."""
        nifty = daily.get("^NSEI")
        if nifty is None or nifty.empty:
            return
        today = now.date()
        if today.weekday() < 5 and now.time() >= time(9, 45) and nifty.index[-1].date() < today:
            self._holidays[today] = "exchange closed (no trading data today)"

    # ---------------- interface ----------------
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            timeframes=["1d", "5m"], data_status=DataStatus.DELAYED, exchanges=["NSE", "BSE"],
            instrument_types=["EQ", "INDEX"], supports_quotes=True, supports_streaming=False, supports_depth=False,
            supports_oi=False, supports_option_chain=False, supports_fundamentals=False,
            supports_corporate_actions=False, supports_news=False, supports_orders=False,
            history_note=(f"Daily from {self.history_start.isoformat()}; 5-minute bars for the last "
                          f"{self.intraday_days} days (Yahoo keeps ~60); quotes refreshed every ~30 s"),
            missing_fields=["market depth", "open interest", "implied volatility", "Greeks", "option chain",
                            "fundamentals", "corporate actions", "delivery statistics", "news / announcements"],
            requires=[],
            needs_verification=[
                "Yahoo Finance has no official API; yfinance uses undocumented endpoints - personal, non-commercial use only",
                "NSE/BSE data on Yahoo may be delayed (up to ~15 minutes) and can be rate-limited or change without notice",
                "Universe is a static NIFTY 50-style list; verify against current index constituents",
                "Prices are raw (not adjusted for splits/bonuses/dividends)",
            ],
        )

    def health(self) -> ProviderHealth:
        try:
            self._quotes()
            msg = "Connected (Yahoo Finance via yfinance; no login; data may be delayed up to ~15 min)"
            if self.derived_daily:
                msg += ("; daily bars derived from 5-minute data (~60 days only) for: "
                        + ", ".join(sorted(self.derived_daily)))
            return ProviderHealth(self.name, True, DataStatus.DELAYED, msg,
                                  last_update=self._last_ok, latency_ms=self._latency, errors=list(self._errors))
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(self.name, False, DataStatus.UNAVAILABLE,
                                  f"Yahoo Finance unreachable: {type(exc).__name__}", last_update=self._last_ok,
                                  errors=list(self._errors))

    def instruments(self) -> list[InstrumentInfo]:
        out = [InstrumentInfo(sym, "NSE", sym, "EQ", DEFAULT_UNIVERSE.get(sym), 1, 0.05, f"{sym}.NS")
               for sym in self.universe]
        out += [InstrumentInfo(sym, exch, name, "INDEX", None, 1, 0.05, tk)
                for sym, (exch, tk, name) in INDEX_TICKERS.items()]
        return out

    def session_state(self, now: datetime) -> str:
        return self._calendar().session_state(now)

    def expected_last_bar(self, timeframe: str, now: datetime | None = None) -> datetime | None:
        return self._calendar().expected_last_bar(timeframe, now or self.clock())

    def _daily_from_intraday(self, tk: str, today: date) -> pd.DataFrame | None:
        """Some NSE sector indices have no daily history on Yahoo; aggregate their 5-minute bars instead
        (only ~60 days available). Disclosed in the coverage matrix."""
        s = today - timedelta(days=MAX_INTRADAY_DAYS)
        data = self._fetch(("5m", s), 120.0, start=s.isoformat(), end=(today + timedelta(days=1)).isoformat(),
                           interval="5m")
        return _aggregate_daily(data.get(tk))

    def _recent_index_daily(self, tk: str) -> pd.DataFrame | None:
        tks = [t for (_, s), t in self._tickers.items() if s in INDEX_TICKERS]
        data = self._fetch(("q5m",), 30.0, tickers=tks, period="5d", interval="5m")
        return _aggregate_daily(data.get(tk))

    def candles(self, symbol: str, exchange: str, timeframe: str, start: datetime | None = None,
                end: datetime | None = None) -> pd.DataFrame:
        tk = self._tickers.get((exchange, symbol))
        if tk is None:
            raise KeyError(f"Unknown instrument {exchange}:{symbol}")
        now = self.clock()
        today = now.date()
        start_d = pd.Timestamp(start).tz_convert(IST).date() if start is not None else None
        if timeframe == "1d":
            s = start_d or self.history_start
            data = self._fetch(("1d", s), 60.0, start=s.isoformat(), end=(today + timedelta(days=1)).isoformat(),
                               interval="1d")
            self._infer_holiday(data, now)
            df = data.get(tk)
            if (df is None or len(df) < 5) and symbol in INDEX_TICKERS:
                df = self._daily_from_intraday(tk, today)
                self.derived_daily.add(symbol)
            if df is None or df.empty:
                return pd.DataFrame(columns=["open", "high", "low", "close", "volume"], dtype=float)
            df = df.copy()
            df.index = pd.DatetimeIndex([pd.Timestamp(t.date()).tz_localize(IST) for t in df.index], name="ts")
            df = df[~df.index.duplicated(keep="last")]
        elif timeframe == "5m":
            earliest = today - timedelta(days=MAX_INTRADAY_DAYS)
            s = max(start_d or (today - timedelta(days=self.intraday_days)), earliest)
            data = self._fetch(("5m", s), 20.0, start=s.isoformat(), end=(today + timedelta(days=1)).isoformat(),
                               interval="5m")
            df = data.get(tk)
            if df is None:
                return pd.DataFrame(columns=["open", "high", "low", "close", "volume"], dtype=float)
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
        return df

    def _quotes(self) -> dict[str, pd.DataFrame]:
        data = self._fetch(("quotes",), 30.0, period="5d", interval="1d")
        self._infer_holiday(data, self.clock())
        return data

    def quote(self, symbol: str, exchange: str) -> Quote | None:
        tk = self._tickers.get((exchange, symbol))
        if tk is None:
            return None
        try:
            df = self._quotes().get(tk)
            if (df is None or len(df) < 2) and symbol in INDEX_TICKERS:
                df = self._recent_index_daily(tk)
        except Exception:  # noqa: BLE001
            return None
        if df is None or df.empty:
            return None
        now = self.clock()
        cal = self._calendar()
        last_date = df.index[-1].date()
        if last_date == now.date() and len(df) >= 2:
            last, prev = float(df["close"].iloc[-1]), float(df["close"].iloc[-2])
        else:
            last = float(df["close"].iloc[-1])
            prev = float(df["close"].iloc[-2]) if len(df) >= 2 else last
        open_ = cal.in_session(now)
        return Quote(symbol, exchange, last, prev, now, DataStatus.DELAYED if open_ else DataStatus.EOD, open_)
