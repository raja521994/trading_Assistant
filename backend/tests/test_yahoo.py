"""Yahoo adapter tests with a fake yfinance download (no network)."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.providers.base import DataStatus
from app.providers.yahoo import YahooProvider

IST = "Asia/Kolkata"


def fake_download_factory(calls: list[dict], daily_last: str = "2025-06-27"):
    def dl(tickers, **kw):
        calls.append(kw)
        if kw.get("interval") == "5m":
            idx = pd.date_range("2025-06-26 09:15", "2025-06-26 15:25", freq="5min", tz=IST).append(
                pd.date_range("2025-06-27 09:15", "2025-06-27 11:00", freq="5min", tz=IST))
        else:
            idx = pd.DatetimeIndex(pd.bdate_range("2025-06-02", daily_last))
        frames = {}
        for t in tickers:
            if t == "^CNXAUTO" and kw.get("interval") == "1d":
                continue  # Yahoo has no daily history for some sector indices
            base = 100.0 + len(t) + np.arange(len(idx))
            frames[t] = pd.DataFrame({"Open": base, "High": base + 1, "Low": base - 1, "Close": base + 0.5,
                                      "Adj Close": base + 0.5, "Volume": 1000.0}, index=idx)
        return pd.concat(frames, axis=1)
    return dl


def make(now: str, calls: list[dict], **kw) -> YahooProvider:
    clock = lambda: pd.Timestamp(now, tz=IST).to_pydatetime()  # noqa: E731
    return YahooProvider(["RELIANCE", "TCS"], "2025-06-01", 30, fake_download_factory(calls, **kw), clock)


def test_instruments_and_tickers():
    p = make("2025-06-27 11:02:30", [])
    infos = {(i.exchange, i.symbol): i for i in p.instruments()}
    assert infos[("NSE", "RELIANCE")].provider_token == "RELIANCE.NS"
    assert infos[("NSE", "NIFTY 50")].provider_token == "^NSEI"
    assert infos[("BSE", "SENSEX")].provider_token == "^BSESN"


def test_daily_excludes_incomplete_today_and_batches_requests():
    calls: list[dict] = []
    p = make("2025-06-27 11:02:30", calls)
    a = p.candles("RELIANCE", "NSE", "1d")
    b = p.candles("TCS", "NSE", "1d")
    assert a.index[-1] == pd.Timestamp("2025-06-26", tz=IST)  # today's bar is still forming
    assert b.index[-1] == pd.Timestamp("2025-06-26", tz=IST)
    assert len([c for c in calls if c.get("interval") == "1d" and "start" in c]) == 1  # one batched download
    after = make("2025-06-27 16:00", [])
    assert after.candles("RELIANCE", "NSE", "1d").index[-1] == pd.Timestamp("2025-06-27", tz=IST)


def test_intraday_completed_bars_only():
    p = make("2025-06-27 11:02:30", [])
    df = p.candles("RELIANCE", "NSE", "5m")
    assert df.index[-1] == pd.Timestamp("2025-06-27 10:55", tz=IST)
    assert df.index[0] == pd.Timestamp("2025-06-26 09:15", tz=IST)
    assert ((df.index.time >= pd.Timestamp("09:15").time()) & (df.index.time < pd.Timestamp("15:30").time())).all()


def test_quote_delayed_label_and_prev_close():
    p = make("2025-06-27 11:02:30", [])
    q = p.quote("RELIANCE", "NSE")
    daily = fake_download_factory([])(["RELIANCE.NS"], interval="1d")["RELIANCE.NS"]
    assert q.last_price == pytest.approx(float(daily["Close"].iloc[-1]))
    assert q.prev_close == pytest.approx(float(daily["Close"].iloc[-2]))
    assert q.market_open and q.status == DataStatus.DELAYED
    assert make("2025-06-27 18:00", []).quote("RELIANCE", "NSE").status == DataStatus.EOD


def test_holiday_inferred_when_no_bar_today():
    p = make("2025-06-27 10:30", [], daily_last="2025-06-26")
    p.quote("RELIANCE", "NSE")
    assert p.session_state(pd.Timestamp("2025-06-27 10:30", tz=IST).to_pydatetime()).startswith("CLOSED (holiday")
    assert p.expected_last_bar("1d").date() == date(2025, 6, 26)


def test_health_reports_unreachable():
    def boom(*a, **k):
        raise ConnectionError("offline")
    p = YahooProvider(["RELIANCE"], download=boom)
    h = p.health()
    assert not h.connected and h.status == DataStatus.UNAVAILABLE and h.errors


def test_index_without_daily_history_is_derived_from_5m():
    p = make("2025-06-27 16:00", [])
    df = p.candles("NIFTY AUTO", "NSE", "1d")
    assert [d.date() for d in df.index] == [date(2025, 6, 26), date(2025, 6, 27)]
    assert "NIFTY AUTO" in p.derived_daily
    q = p.quote("NIFTY AUTO", "NSE")
    assert q is not None and q.last_price == pytest.approx(float(df["close"].iloc[-1]))
    assert "derived from 5-minute" in p.health().message
