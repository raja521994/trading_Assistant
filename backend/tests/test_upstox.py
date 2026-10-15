"""Upstox adapter tests with a mocked HTTP transport (no network, no credentials)."""

import gzip
import json
from datetime import date, datetime

import httpx
import pandas as pd
import pytest

from app.providers import upstox as up
from app.providers.base import DataStatus

IST = "Asia/Kolkata"
MASTER = [
    {"segment": "NSE_EQ", "instrument_type": "EQ", "trading_symbol": "RELIANCE", "name": "RELIANCE INDUSTRIES LTD",
     "instrument_key": "NSE_EQ|INE002A01018", "lot_size": 1, "tick_size": 10.0},
    {"segment": "NSE_EQ", "instrument_type": "EQ", "trading_symbol": "TCS", "name": "TATA CONSULTANCY SERV LT",
     "instrument_key": "NSE_EQ|INE467B01029", "lot_size": 1, "tick_size": 10.0},
]


def intraday_rows(day: str, until: str) -> list[list]:
    idx = pd.date_range(f"{day} 09:15", f"{day} {until}", freq="5min", tz=IST)
    # Upstox returns newest first
    return [[t.isoformat(), 100.0, 101.0, 99.0, 100.5, 1000, 0] for t in reversed(idx)]


def make_transport(calls: list[str], status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if status != 200:
            return httpx.Response(status, json={"status": "error", "errors": [{"message": "Invalid token"}]})
        if path.endswith("NSE.json.gz"):
            return httpx.Response(200, content=gzip.compress(json.dumps(MASTER).encode()))
        if path == "/v2/market/holidays":
            return httpx.Response(200, json={"status": "success", "data": [
                {"date": "2025-06-25", "description": "Test Holiday", "holiday_type": "TRADING_HOLIDAY",
                 "closed_exchanges": ["NSE", "BSE"], "open_exchanges": []}]})
        if path == "/v3/market-quote/ltp":
            return httpx.Response(200, json={"status": "success", "data": {
                "NSE_EQ:RELIANCE": {"last_price": 1500.5, "instrument_token": "NSE_EQ|INE002A01018", "cp": 1490.0},
                "NSE_INDEX:Nifty 50": {"last_price": 25000.1, "instrument_token": "NSE_INDEX|Nifty 50", "cp": 24900.0},
            }})
        if "/intraday/" in path and path.endswith("/days/1"):
            return httpx.Response(200, json={"status": "success", "data": {"candles": [
                ["2025-06-27T00:00:00+05:30", 100, 110, 95, 105, 50000, 0]]}})
        if "/intraday/" in path:
            return httpx.Response(200, json={"status": "success", "data": {"candles": intraday_rows("2025-06-27", "11:00")}})
        if "/days/1/" in path:
            days = pd.bdate_range("2025-06-02", "2025-06-26")
            rows = [[pd.Timestamp(d).tz_localize(IST).isoformat(), 100, 102, 98, 101, 1e5, 0] for d in days
                    if d.date() != date(2025, 6, 25)]
            return httpx.Response(200, json={"status": "success", "data": {"candles": list(reversed(rows))}})
        if "/minutes/5/" in path:
            return httpx.Response(200, json={"status": "success", "data": {"candles": intraday_rows("2025-06-26", "15:25")}})
        return httpx.Response(404, json={"status": "error"})

    return httpx.MockTransport(handler)


def provider(now: str, calls: list[str], tmp_path, monkeypatch, status: int = 200, token: str | None = "tok"):
    monkeypatch.setattr(up, "CACHE_DIR", tmp_path)
    clock = lambda: pd.Timestamp(now, tz=IST).to_pydatetime()  # noqa: E731
    return up.UpstoxProvider(token, ["RELIANCE", "TCS", "NOTREAL"], "2025-06-01", 30, make_transport(calls, status),
                             clock)


def test_instruments_resolve_keys_and_report_unresolved(tmp_path, monkeypatch):
    p = provider("2025-06-27 11:02:30", [], tmp_path, monkeypatch)
    infos = p.instruments()
    syms = {(i.exchange, i.symbol): i for i in infos}
    assert syms[("NSE", "RELIANCE")].provider_token == "NSE_EQ|INE002A01018"
    assert syms[("NSE", "RELIANCE")].tick_size == pytest.approx(0.10)
    assert ("NSE", "NIFTY 50") in syms and ("BSE", "SENSEX") in syms
    assert p._unresolved == ["NOTREAL"]


def test_intraday_only_completed_bars(tmp_path, monkeypatch):
    p = provider("2025-06-27 11:02:30", [], tmp_path, monkeypatch)
    df = p.candles("RELIANCE", "NSE", "5m")
    assert df.index.is_monotonic_increasing
    assert df.index[-1] == pd.Timestamp("2025-06-27 10:55", tz=IST)  # 11:00 bar still forming
    assert df.index[0].date() == date(2025, 6, 26)
    assert p.expected_last_bar("5m") == df.index[-1].to_pydatetime()


def test_daily_excludes_today_until_close_and_respects_holidays(tmp_path, monkeypatch):
    calls: list[str] = []
    p = provider("2025-06-27 11:02:30", calls, tmp_path, monkeypatch)
    df = p.candles("RELIANCE", "NSE", "1d")
    assert df.index[-1].date() == date(2025, 6, 26)
    assert not any("/intraday/" in c for c in calls)
    p2 = provider("2025-06-27 16:00", [], tmp_path, monkeypatch)
    df2 = p2.candles("RELIANCE", "NSE", "1d")
    assert df2.index[-1] == pd.Timestamp("2025-06-27", tz=IST)
    assert df2["close"].iloc[-1] == 105
    cal = p.calendar()
    assert not cal.is_trading_day(date(2025, 6, 25))
    assert p.session_state(datetime(2025, 6, 25, 11, 0)) == "CLOSED (holiday: Test Holiday)"
    # Day after a holiday: expected daily bar before the close is the last trading day before the holiday
    assert cal.expected_last_bar("1d", pd.Timestamp("2025-06-26 10:00", tz=IST).to_pydatetime()).date() == date(2025, 6, 24)


def test_quotes_are_live_and_mapped_by_instrument_key(tmp_path, monkeypatch):
    calls: list[str] = []
    p = provider("2025-06-27 11:02:30", calls, tmp_path, monkeypatch)
    q = p.quote("RELIANCE", "NSE")
    assert q.last_price == 1500.5 and q.prev_close == 1490.0
    assert q.market_open and q.status == DataStatus.LIVE
    n = sum(1 for c in calls if c == "/v3/market-quote/ltp")
    p.quote("NIFTY 50", "NSE")
    assert sum(1 for c in calls if c == "/v3/market-quote/ltp") == n  # batched + cached
    closed = provider("2025-06-27 18:00", [], tmp_path, monkeypatch).quote("RELIANCE", "NSE")
    assert not closed.market_open and closed.status == DataStatus.EOD


def test_disabled_without_token_and_errors_surface(tmp_path, monkeypatch):
    p = provider("2025-06-27 11:02:30", [], tmp_path, monkeypatch, token=None)
    assert not p.enabled and p.missing_requirements()
    assert p.health().status == DataStatus.UNAVAILABLE
    bad = provider("2025-06-27 11:02:30", [], tmp_path, monkeypatch, status=401)
    h = bad.health()
    assert not h.connected and "Connection failed" in h.message and h.errors


def test_validate_token(monkeypatch):
    ok, msg = up.validate_token("x" * 30, make_transport([]))
    assert ok and "25000.1" in msg
    bad, msg2 = up.validate_token("x" * 30, make_transport([], status=401))
    assert not bad and "401" in msg2


def test_env_file_update(tmp_path):
    from app.config import update_env_file

    f = tmp_path / ".env"
    f.write_text("A=1\nUPSTOX_ACCESS_TOKEN=old\n", encoding="utf-8")
    update_env_file("UPSTOX_ACCESS_TOKEN", "new", f)
    assert f.read_text(encoding="utf-8").splitlines() == ["A=1", "UPSTOX_ACCESS_TOKEN=new"]
    update_env_file("UPSTOX_ACCESS_TOKEN", None, f)
    assert f.read_text(encoding="utf-8").splitlines() == ["A=1"]
