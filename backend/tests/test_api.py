"""API integration tests: ingestion, scanner recommendations, backtest, paper trading, assistant."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.jobs import scheduler
from app.main import app

SPEC7_FIELDS = [
    "symbol", "exchange", "instrument_type", "mode", "strategy", "strategy_version", "generated_at", "data_timestamp",
    "freshness", "reference_price", "direction", "entry", "stop_loss", "invalidation", "targets", "risk_reward",
    "holding_period", "liquidity", "position_size", "indicators", "supporting_evidence", "conflicting_evidence",
    "event_risks", "oos_performance", "estimated_costs", "state", "action", "score", "research_only",
]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        scheduler.startup_sync()
        yield c


def test_health_and_meta(client):
    assert client.get("/api/health").json()["status"] == "ok"
    m = client.get("/api/meta").json()
    assert m["data_label"] == "SIMULATED"
    assert m["live_trading_enabled"] is False


def test_overview(client):
    o = client.get("/api/market/overview").json()
    names = [i["symbol"] for i in o["indices"]]
    for idx in ("NIFTY 50", "NIFTY BANK", "NIFTY FIN SERVICE", "SENSEX"):
        assert idx in names
    b = o["breadth"]
    assert b["advances"] + b["declines"] + b["unchanged"] == b["total"] == 42
    assert o["announcements"]["status"] == "NOT CONNECTED"
    assert o["regime"]["current"]["trend"]


def test_scanner_recommendations_are_complete(client):
    r = client.get("/api/scanner").json()
    assert r["data_label"] == "SIMULATED"
    recs = r["recommendations"]
    assert recs, "expected at least one candidate on sample data"
    for rec in recs:
        for f in SPEC7_FIELDS:
            assert f in rec, f
        assert rec["state"] in ("WATCH", "SETUP", "TRIGGERED")
        assert rec["action"] in ("BUY", "SELL", "WAIT", "NO TRADE")
        assert rec["executable"] is False and rec["research_only"] is True
        if rec["state"] != "WATCH":
            ps = rec["position_size"]
            assert rec["stop_loss"] is not None and ps["qty"] >= 0
            if rec["direction_code"] == "LONG":
                assert rec["stop_loss"] < rec["entry"]["reference"]
        assert 0 <= rec["score"] <= 100
    detail = client.get(f"/api/signals/{recs[0]['signal_id']}").json()
    assert detail["explanation"]["facts"] and detail["events"]


def test_scanner_filters(client):
    r = client.get("/api/scanner", params={"exchange": "BSE"}).json()
    assert all(x["exchange"] == "BSE" for x in r["recommendations"])
    r2 = client.get("/api/scanner", params={"state": "WATCH"}).json()
    assert all(x["action"] == "WAIT" for x in r2["recommendations"])


def test_chart_and_integrations(client):
    c = client.get("/api/chart/NSE/RELIANCE", params={"strategy": "donchian_breakout", "bars": 300}).json()
    assert len(c["candles"]) == 300 and c["indicators"]["ema20"] and c["quality"]["ok"]
    i = client.get("/api/integrations").json()
    kite = next(p for p in i["providers"] if p["name"] == "kite")
    assert kite["enabled"] is False and kite["missing_requirements"]
    assert i["execution"]["live_trading_enabled"] is False
    assert any(row["bars"] > 0 for row in i["coverage"])


def test_backtest_endpoint(client):
    r = client.post("/api/backtest", json={"strategy": "donchian_breakout", "symbols": ["RELIANCE", "TCS", "INFY"],
                                           "start": "2024-01-01"})
    assert r.status_code == 200, r.text
    b = r.json()
    m = b["metrics"]
    for k in ("net_return_pct", "max_drawdown_pct", "sharpe", "win_rate_pct", "profit_factor", "expectancy",
              "num_trades", "exposure_pct", "total_costs", "benchmark_return_pct"):
        assert k in m
    assert b["equity"] and b["trades"] is not None
    csv = client.get(f"/api/backtest/runs/{b['id']}/trades.csv")
    assert csv.status_code == 200


def _manual_order(client, key, stop_frac=0.97, target_frac=1.06, symbol="HDFCBANK"):
    c = client.get(f"/api/chart/NSE/{symbol}", params={"bars": 60}).json()
    px = c["candles"][-1]["close"]
    return client.post("/api/paper/orders", json={"symbol": symbol, "exchange": "NSE", "direction": "LONG",
                                                  "mode": "swing", "stop": round(px * stop_frac, 2),
                                                  "target": round(px * target_frac, 2), "idempotency_key": key})


def test_paper_trading_flow(client):
    key = f"test-{uuid.uuid4()}"
    r = _manual_order(client, key)
    assert r.status_code == 200, r.text
    t = r.json()["trade"]
    assert t["status"] == "OPEN" and t["qty"] > 0
    # idempotent: same key returns the same trade
    assert _manual_order(client, key).json()["trade"]["id"] == t["id"]
    # duplicate exposure in the same symbol is rejected by the risk engine
    dup = _manual_order(client, f"test-{uuid.uuid4()}")
    assert dup.status_code == 422 and "already exists" in dup.text
    closed = client.post(f"/api/paper/trades/{t['id']}/close").json()["trade"]
    assert closed["status"] == "CLOSED" and closed["net_pnl"] is not None
    perf = client.get("/api/performance").json()
    assert perf["risk_breaches"] and perf["strategy_attribution"]


def test_kill_switch_blocks_orders(client):
    client.post("/api/risk/kill-switch", json={"on": True})
    try:
        r = _manual_order(client, f"test-{uuid.uuid4()}", symbol="ICICIBANK")
        assert r.status_code == 422 and "Kill switch" in r.text
    finally:
        client.post("/api/risk/kill-switch", json={"on": False})


def test_assistant_uses_computed_facts(client):
    r = client.post("/api/assistant/ask", json={"question": "What changed in the current market regime?"}).json()
    assert r["intent"] == "regime" and r["answer"]["facts"] and r["provider"] == "template"
    rs = client.post("/api/assistant/ask", json={"question": "Which stocks show improving relative strength?"}).json()
    assert rs["intent"] == "relative_strength"
    sym = client.get("/api/scanner").json()["recommendations"][0]["symbol"]
    w = client.post("/api/assistant/ask", json={"question": f"Why is {sym} appearing in the scanner?"}).json()
    assert w["intent"] == "explain_signal" and w["answer"]["calculations"] is not None
