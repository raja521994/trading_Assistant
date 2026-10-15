"""Kite Connect adapter tests with a mocked HTTP transport (no network, no credentials)."""

import hashlib
from datetime import date, datetime
from urllib.parse import parse_qs

import httpx
import pandas as pd
import pytest

from app.providers import kite as kt
from app.providers.base import DataStatus

IST = "Asia/Kolkata"
NSE_CSV = (
    "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n"
    "738561,2885,RELIANCE,RELIANCE INDUSTRIES,0,,,0.1,1,EQ,NSE,NSE\n"
    "2953217,11536,TCS,TATA CONSULTANCY SERV LT,0,,,0.1,1,EQ,NSE,NSE\n"
    "256265,1001,NIFTY 50,NIFTY 50,0,,,0,0,EQ,INDICES,NSE\n"
    "260105,1016,NIFTY BANK,NIFTY BANK,0,,,0,0,EQ,INDICES,NSE\n"
)
BSE_CSV = (
    "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n"
    "265,1,SENSEX,SENSEX,0,,,0,0,EQ,INDICES,BSE\n"
)


def k_rows(day: str, until: str) -> list[list]:
    idx = pd.date_range(f"{day} 09:15", f"{day} {until}", freq="5min", tz=IST)
    return [[t.strftime("%Y-%m-%dT%H:%M:%S+0530"), 100.0, 101.0, 99.0, 100.5, 1000] for t in idx]


def make_transport(calls: list, status: int = 200, error_type: str = "TokenException"):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, dict(request.url.params), request.headers.get("authorization")))
        p = request.url.path
        if status != 200:
            return httpx.Response(status, json={"status": "error", "message": "Incorrect `api_key` or `access_token`.",
                                                "error_type": error_type})
        if p == "/session/token":
            form = parse_qs(request.content.decode())
            expected = hashlib.sha256(b"KEY123" + form["request_token"][0].encode() + b"SECRET456").hexdigest()
            if form["checksum"][0] != expected:
                return httpx.Response(403, json={"status": "error", "message": "Invalid checksum", "error_type": "TokenException"})
            return httpx.Response(200, json={"status": "success", "data": {"access_token": "ACCESS789", "user_id": "AB1234",
                                                                            "user_name": "Test User"}})
        if p == "/instruments/NSE":
            return httpx.Response(200, content=NSE_CSV.encode())
        if p == "/instruments/BSE":
            return httpx.Response(200, content=BSE_CSV.encode())
        if p.startswith("/instruments/historical/") and p.endswith("/day"):
            days = pd.bdate_range("2025-06-02", "2025-06-27")
            rows = [[pd.Timestamp(d).strftime("%Y-%m-%dT00:00:00+0530"), 100, 102, 98, 101, 1e5] for d in days]
            return httpx.Response(200, json={"status": "success", "data": {"candles": rows}})
        if p.startswith("/instruments/historical/") and p.endswith("/5minute"):
            return httpx.Response(200, json={"status": "success", "data": {"candles": k_rows("2025-06-26", "15:25") + k_rows("2025-06-27", "11:00")}})
        if p == "/quote":
            return httpx.Response(200, json={"status": "success", "data": {
                "NSE:RELIANCE": {"instrument_token": 738561, "last_price": 1500.5, "ohlc": {"close": 1490.0},
                                 "last_trade_time": "2025-06-27 11:02:00", "timestamp": "2025-06-27 11:02:01"},
                "NSE:NIFTY 50": {"instrument_token": 256265, "last_price": 25000.1, "ohlc": {"close": 24900.0},
                                 "timestamp": "2025-06-27 11:02:01"},
            }})
        return httpx.Response(404, json={"status": "error", "message": "not found", "error_type": "GeneralException"})

    return httpx.MockTransport(handler)


def provider(now: str, calls: list, tmp_path, monkeypatch, status: int = 200, token: str | None = "ACCESS789",
             created: str | None = None, error_type: str = "TokenException"):
    monkeypatch.setattr(kt, "CACHE_DIR", tmp_path)
    clock = lambda: pd.Timestamp(now, tz=IST).to_pydatetime()  # noqa: E731
    created = created or pd.Timestamp(now, tz=IST).isoformat()
    return kt.KiteProvider("KEY123", token, created, ["RELIANCE", "TCS", "NOTREAL"], "2025-06-01", 30,
                           make_transport(calls, status, error_type), clock)


def test_login_url_and_checksum_exchange():
    assert kt.login_url("KEY123") == "https://kite.zerodha.com/connect/login?v=3&api_key=KEY123"
    calls: list = []
    data = kt.exchange_request_token("KEY123", "SECRET456", "REQTOKEN", make_transport(calls))
    assert data["access_token"] == "ACCESS789" and data["user_id"] == "AB1234"
    with pytest.raises(kt.KiteError) as ei:
        kt.exchange_request_token("KEY123", "WRONGSECRET", "REQTOKEN", make_transport([]))
    assert ei.value.error_type == "TokenException"


def test_instruments_and_auth_header(tmp_path, monkeypatch):
    calls: list = []
    p = provider("2025-06-27 11:02:30", calls, tmp_path, monkeypatch)
    infos = {(i.exchange, i.symbol): i for i in p.instruments()}
    assert infos[("NSE", "RELIANCE")].provider_token == "738561"
    assert ("BSE", "SENSEX") in infos and ("NSE", "NIFTY 50") in infos
    assert "NOTREAL" in p._unresolved and "NIFTY IT" in p._unresolved
    assert calls[0][3] == "token KEY123:ACCESS789"


def test_candles_completed_only(tmp_path, monkeypatch):
    calls: list = []
    p = provider("2025-06-27 11:02:30", calls, tmp_path, monkeypatch)
    d = p.candles("RELIANCE", "NSE", "1d")
    assert d.index[-1] == pd.Timestamp("2025-06-26", tz=IST)  # today's daily candle not final before 15:30
    m = p.candles("RELIANCE", "NSE", "5m")
    assert m.index[-1] == pd.Timestamp("2025-06-27 10:55", tz=IST)
    hist = [c for c in calls if "/historical/" in c[1]]
    assert hist and all(" " in c[2]["from"] and ":" in c[2]["to"] for c in hist)  # yyyy-mm-dd hh:mm:ss


def test_quotes_live(tmp_path, monkeypatch):
    p = provider("2025-06-27 11:02:30", [], tmp_path, monkeypatch)
    q = p.quote("RELIANCE", "NSE")
    assert q.last_price == 1500.5 and q.prev_close == 1490.0
    assert q.status == DataStatus.LIVE and q.market_open
    assert p.health().connected


def test_token_expiry_daily_flush(tmp_path, monkeypatch):
    # Logged in yesterday afternoon -> expired after today's 06:00 IST flush
    p = provider("2025-06-27 09:30", [], tmp_path, monkeypatch, created="2025-06-26T16:00:00+05:30")
    assert p.token_expired() and not p.enabled
    assert any("expired" in m for m in p.missing_requirements())
    # Logged in today after 06:00 -> valid
    p2 = provider("2025-06-27 09:30", [], tmp_path, monkeypatch, created="2025-06-27T08:00:00+05:30")
    assert not p2.token_expired() and p2.enabled
    assert kt.last_token_flush(datetime(2025, 6, 27, 5, 0, tzinfo=pd.Timestamp("2025-06-27", tz=IST).tzinfo)).date() == date(2025, 6, 26)


def test_rejected_session_disables_provider(tmp_path, monkeypatch):
    p = provider("2025-06-27 11:02:30", [], tmp_path, monkeypatch, status=403)
    h = p.health()
    assert not h.connected
    assert not p.enabled and any("rejected" in m for m in p.missing_requirements())


def test_permission_error_explained(tmp_path, monkeypatch):
    p = provider("2025-06-27 11:02:30", [], tmp_path, monkeypatch, status=403, error_type="PermissionException")
    h = p.health()
    assert not h.connected and "plan may not include market data" in h.message


def test_no_session_without_login():
    p = kt.KiteProvider("KEY123", None)
    assert not p.enabled and "Login with Zerodha" in p.missing_requirements()[0]
    p2 = kt.KiteProvider(None, None)
    assert "API key" in p2.missing_requirements()[0]
