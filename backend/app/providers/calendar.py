"""NSE/BSE trading calendar helpers (IST). Holidays are supplied by the provider when available."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pandas as pd

IST = "Asia/Kolkata"
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
BAR_MINUTES = 5
BARS_PER_SESSION = 75


class TradingCalendar:
    def __init__(self, holidays: dict[date, str] | None = None) -> None:
        self.holidays = holidays or {}

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def prev_trading_day(self, d: date) -> date:
        d -= timedelta(days=1)
        while not self.is_trading_day(d):
            d -= timedelta(days=1)
        return d

    def last_trading_day_on_or_before(self, d: date) -> date:
        while not self.is_trading_day(d):
            d -= timedelta(days=1)
        return d

    def in_session(self, now: datetime) -> bool:
        return self.is_trading_day(now.date()) and SESSION_OPEN <= now.time() < SESSION_CLOSE

    def last_completed_session(self, now: datetime) -> date:
        d = now.date()
        if self.is_trading_day(d) and now.time() >= SESSION_CLOSE:
            return d
        return self.prev_trading_day(d)

    def expected_last_bar(self, timeframe: str, now: datetime) -> datetime | None:
        if timeframe == "1d":
            return pd.Timestamp(self.last_completed_session(now)).tz_localize(IST).to_pydatetime()
        if timeframe == "5m":
            if self.is_trading_day(now.date()):
                open_dt = pd.Timestamp(datetime.combine(now.date(), SESSION_OPEN)).tz_localize(IST)
                elapsed = (pd.Timestamp(now) - open_dt).total_seconds() / 60.0
                k = min(int(elapsed // BAR_MINUTES) - 1, BARS_PER_SESSION - 1)
                if k >= 0:
                    return (open_dt + pd.Timedelta(minutes=BAR_MINUTES * k)).to_pydatetime()
            d = self.prev_trading_day(now.date()) if self.is_trading_day(now.date()) else \
                self.last_trading_day_on_or_before(now.date())
            return (pd.Timestamp(datetime.combine(d, SESSION_OPEN)).tz_localize(IST)
                    + pd.Timedelta(minutes=BAR_MINUTES * (BARS_PER_SESSION - 1))).to_pydatetime()
        return None

    def session_state(self, now: datetime) -> str:
        d = now.date()
        if d in self.holidays:
            return f"CLOSED (holiday: {self.holidays[d]})"
        return market_session_state(now)


def market_session_state(now: datetime) -> str:
    if now.weekday() >= 5:
        return "CLOSED (weekend)"
    t = now.time()
    if t < time(9, 0):
        return "PRE-OPEN WINDOW NOT STARTED"
    if t < SESSION_OPEN:
        return "PRE-OPEN"
    if t < SESSION_CLOSE:
        return "OPEN"
    return "CLOSED"
