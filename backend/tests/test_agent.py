"""Orchestrator/worker paper-trading agent."""

import pytest
from fastapi.testclient import TestClient

from app.agents import orchestrator, service
from app.agents.state import AgentConfig, agent_trades
from app.db import session_scope
from app.jobs import scheduler
from app.main import app
from app.models import PaperTrade


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        scheduler.startup_sync()
        yield c


def test_agent_lifecycle_and_report(client):
    cfg = AgentConfig(require_market_open=False, allow_unvalidated=True).model_dump()
    st = client.post("/api/agent/start", json=cfg).json()
    assert st["running"] and st["session_id"] and st["starting_equity"] > 0
    r = client.post("/api/agent/run-once").json()
    cyc = r["cycle"]
    assert cyc["candidates_seen"] >= 0 and isinstance(cyc["executed"], list)
    for e in cyc["executed"]:
        assert e["status"] in ("FILLED", "REJECTED", "CLOSED", "MODIFIED", "SKIPPED", "FAILED")
    rep = client.get("/api/agent/report").json()
    assert set(rep["by_mode"]) == {"intraday", "swing", "positional"}
    filled = [e for e in cyc["executed"] if e["status"] == "FILLED"]
    assert rep["totals"]["trades"] == len(filled)
    for t in rep["trades"]:
        assert not (t["direction"] == "SHORT" and t["mode"] != "intraday")
    assert client.post("/api/agent/stop").json()["running"] is False
    assert isinstance(client.get("/api/agent/activity").json(), list)
    for t in rep["trades"]:
        if t["status"] == "OPEN":
            client.post(f"/api/paper/trades/{t['id']}/close")


def test_breakeven_and_max_hold_instructions(client):
    with session_scope() as s:
        st = service.start(s, AgentConfig(require_market_open=False))
        t = PaperTrade(idempotency_key=f"agent-{st.session_id}-999999", symbol="ZZTEST", exchange="NSE",
                       strategy="test", mode="swing", direction="LONG", qty=10, entry_price=100.0, entry_ts=0,
                       stop=95.0, target=120.0, status="OPEN", entry_costs=0.0, exit_costs=0.0, last_price=110.0,
                       details={"agent": {"initial_stop": 95.0}})
        s.add(t)
        s.flush()
        ins = orchestrator._manage_positions(s, st, orchestrator.now_ist())
        mine = [i for i in ins if i.trade_id == t.id]
        assert mine and mine[0].kind == "EXIT" and "max holding" in mine[0].reason
        t.entry_ts = int(orchestrator.now_ist().timestamp())
        s.flush()
        ins = [i for i in orchestrator._manage_positions(s, st, orchestrator.now_ist()) if i.trade_id == t.id]
        assert ins and ins[0].kind == "TIGHTEN_STOP" and ins[0].stop == 100.0
        assert t in agent_trades(s, st.session_id)
        s.delete(t)
        service.stop(s)
