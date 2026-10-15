"""SampleProvider: offline, deterministic SIMULATED data. Always labelled DataStatus.SIMULATED.

Simulated 'live' behaviour: during weekday session hours (09:15-15:30 IST) today's 5-minute bars are
revealed as they complete and the quote follows today's pre-generated intraday path, so intraday
quotes, 5m candles and the (later revealed) daily bar are mutually consistent.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, time, timedelta

import pandas as pd

from ..data import sample_generator as gen
from .base import (
    DataStatus,
    InstrumentInfo,
    MarketDataProvider,
    ProviderCapabilities,
    ProviderHealth,
    Quote,
)

IST = gen.IST
TIMEFRAMES = ["1d", "5m"]


def now_ist() -> datetime:
    return pd.Timestamp.now(tz=IST).to_pydatetime()


class SampleProvider(MarketDataProvider):
    name = "sample"
    display_name = "Sample data (SIMULATED)"

    def __init__(self, seed: int = 42, start_date: str = "2023-01-02", intraday_sessions: int = 20,
                 clock=now_ist) -> None:
        self.seed = seed
        self.start = date.fromisoformat(start_date)
        self.intraday_sessions = intraday_sessions
        self.clock = clock
        self._lock = threading.Lock()
        self._daily_end: date | None = None
        self._daily: dict[tuple[str, str], pd.DataFrame] = {}
        self._intraday: dict[tuple[str, str, date], pd.DataFrame] = {}
        self._last_update: datetime | None = None

    # ---------- internals ----------
    def _now(self) -> datetime:
        n = self.clock()
        return n if n.tzinfo else pd.Timestamp(n).tz_localize(IST).to_pydatetime()

    def _ensure_daily(self, now: datetime) -> None:
        end = gen.last_weekday_on_or_before(now.date())
        with self._lock:
            if self._daily_end != end:
                self._daily = gen.generate_daily(self.seed, self.start, end)
                self._daily_end = end
                self._intraday.clear()
                self._last_update = now

    @staticmethod
    def _session_done(now: datetime) -> bool:
        return now.time() >= gen.SESSION_CLOSE

    def _last_daily_date(self, now: datetime) -> date:
        d = gen.last_weekday_on_or_before(now.date())
        if d == now.date() and not self._session_done(now):
            d = gen.last_weekday_on_or_before(d - timedelta(days=1))
        return d

    def _intraday_session(self, exchange: str, symbol: str, d: date) -> pd.DataFrame:
        key = (exchange, symbol, d)
        with self._lock:
            cached = self._intraday.get(key)
        if cached is not None:
            return cached
        daily = self._daily[(exchange, symbol)]
        ts = pd.Timestamp(d).tz_localize(IST)
        if ts not in daily.index:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        r = daily.loc[ts]
        df = gen.generate_intraday_session(self.seed, symbol, d, float(r["open"]), float(r["high"]),
                                           float(r["low"]), float(r["close"]), float(r["volume"]))
        with self._lock:
            self._intraday[key] = df
        return df

    def _intraday_dates(self, now: datetime) -> list[date]:
        today = gen.last_weekday_on_or_before(now.date())
        days = gen.trading_days(today - timedelta(days=self.intraday_sessions * 2 + 10), today)
        out = [d.date() for d in days[-self.intraday_sessions:]]
        if out and out[-1] == now.date() and now.time() < gen.SESSION_OPEN:
            out = out[:-1]
        return out

    # ---------- interface ----------
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            timeframes=TIMEFRAMES,
            data_status=DataStatus.SIMULATED,
            exchanges=["NSE", "BSE"],
            instrument_types=["EQ", "INDEX"],
            supports_quotes=True,
            supports_streaming=False,
            supports_depth=False,
            supports_oi=False,
            supports_option_chain=False,
            supports_fundamentals=False,
            supports_corporate_actions=False,
            supports_news=False,
            supports_orders=False,
            history_note=(f"Daily from {self.start.isoformat()} (weekday calendar, no holidays); "
                          f"5-minute bars for the last {self.intraday_sessions} sessions"),
            missing_fields=["market depth", "open interest", "implied volatility", "Greeks", "option chain",
                            "fundamentals", "corporate actions", "delivery statistics", "news / announcements"],
            requires=[],
            needs_verification=[],
        )

    def health(self) -> ProviderHealth:
        now = self._now()
        self._ensure_daily(now)
        return ProviderHealth(name=self.name, connected=True, status=DataStatus.SIMULATED,
                              message="Synthetic deterministic data generated locally. Not real market data.",
                              last_update=self._last_update, latency_ms=0.0)

    def instruments(self) -> list[InstrumentInfo]:
        out = [InstrumentInfo(e.symbol, e.exchange, e.name, "EQ", e.sector, 1, 0.05, f"SIM-{e.exchange}-{e.symbol}")
               for e in gen.EQUITIES]
        out += [InstrumentInfo(i["symbol"], i["exchange"], i["name"], "INDEX", None, 1, 0.05,
                               f"SIM-{i['exchange']}-{i['symbol']}") for i in gen.INDICES]
        return out

    def expected_last_bar(self, timeframe: str, now: datetime | None = None) -> datetime | None:
        now = now or self._now()
        if timeframe == "1d":
            return pd.Timestamp(self._last_daily_date(now)).tz_localize(IST).to_pydatetime()
        if timeframe == "5m":
            if now.weekday() < 5:
                open_dt = pd.Timestamp(datetime.combine(now.date(), gen.SESSION_OPEN)).tz_localize(IST)
                elapsed = (pd.Timestamp(now) - open_dt).total_seconds() / 60.0
                k = min(int(elapsed // gen.BAR_MINUTES) - 1, gen.BARS_PER_SESSION - 1)
                if k >= 0:
                    return (open_dt + pd.Timedelta(minutes=gen.BAR_MINUTES * k)).to_pydatetime()
                d = gen.last_weekday_on_or_before(now.date() - timedelta(days=1))
            else:
                d = gen.last_weekday_on_or_before(now.date())
            return gen.session_bar_starts(d)[-1].to_pydatetime()
        return None

    def candles(self, symbol: str, exchange: str, timeframe: str, start: datetime | None = None,
                end: datetime | None = None) -> pd.DataFrame:
        now = self._now()
        self._ensure_daily(now)
        if (exchange, symbol) not in self._daily:
            raise KeyError(f"Unknown instrument {exchange}:{symbol}")
        last = pd.Timestamp(self.expected_last_bar(timeframe, now))
        if timeframe == "1d":
            df = self._daily[(exchange, symbol)]
        elif timeframe == "5m":
            parts = [self._intraday_session(exchange, symbol, d) for d in self._intraday_dates(now)]
            parts = [p for p in parts if len(p)]
            df = pd.concat(parts) if parts else pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        else:
            raise ValueError(f"Unsupported timeframe {timeframe}")
        df = df[df.index <= last]
        if start is not None:
            df = df[df.index >= pd.Timestamp(start)]
        if end is not None:
            df = df[df.index <= pd.Timestamp(end)]
        return df.copy()

    def quote(self, symbol: str, exchange: str) -> Quote | None:
        now = self._now()
        self._ensure_daily(now)
        daily = self._daily.get((exchange, symbol))
        if daily is None:
            return None
        today = now.date()
        in_session = today.weekday() < 5 and gen.SESSION_OPEN <= now.time() < gen.SESSION_CLOSE
        if in_session:
            ts = pd.Timestamp(today).tz_localize(IST)
            pos = daily.index.get_loc(ts)
            prev_close = float(daily["close"].iloc[pos - 1]) if pos > 0 else float(daily["open"].iloc[pos])
            bars = self._intraday_session(exchange, symbol, today)
            open_dt = pd.Timestamp(datetime.combine(today, gen.SESSION_OPEN)).tz_localize(IST)
            elapsed = (pd.Timestamp(now) - open_dt).total_seconds() / 60.0
            k = min(int(elapsed // gen.BAR_MINUTES), gen.BARS_PER_SESSION - 1)
            frac = min(max((elapsed - k * gen.BAR_MINUTES) / gen.BAR_MINUTES, 0.0), 1.0)
            o, c = float(bars["open"].iloc[k]), float(bars["close"].iloc[k])
            price = round(round((o + (c - o) * frac) / 0.05) * 0.05, 2)
            return Quote(symbol, exchange, price, prev_close, now, DataStatus.SIMULATED, True)
        last_d = pd.Timestamp(self._last_daily_date(now)).tz_localize(IST)
        hist = daily[daily.index <= last_d]
        if len(hist) < 2:
            return None
        return Quote(symbol, exchange, float(hist["close"].iloc[-1]), float(hist["close"].iloc[-2]),
                     now, DataStatus.SIMULATED, False)


def market_session_state(now: datetime) -> str:
    if now.weekday() >= 5:
        return "CLOSED (weekend)"
    t = now.time()
    if t < time(9, 0):
        return "PRE-OPEN WINDOW NOT STARTED"
    if t < gen.SESSION_OPEN:
        return "PRE-OPEN"
    if t < gen.SESSION_CLOSE:
        return "OPEN"
    return "CLOSED"
