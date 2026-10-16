"""Decision engine: NO TRADE output, eligibility gates, portfolio construction and pick completeness."""

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.decision import engine as de
from app.jobs import scheduler
from app.main import app
from app.quant.library.catalog import CATALOG
from app.validation.tournament import NO_TRADE_MSG

PICK_FIELDS = ["symbol", "strategies", "families", "mode", "entry_trigger", "stop", "targets", "risk_reward", "qty",
               "capital_used", "max_loss", "holding_period", "score_breakdown", "evidence", "invalidation",
               "confidence", "reference_price", "final_score"]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        scheduler.startup_sync()
        yield c


def _bull(*_a, **_k):
    return {"trend": "bull", "volatility": "low", "label": "BULL x LOW volatility", "exposure_budget_pct": 100.0,
            "multiplier": 1.0, "inputs": [], "as_of": None}


def _fake_report(passed: set[str]):
    rows, details = [], {}
    for e in CATALOG:
        if not e.testable:
            continue
        ok = e.id in passed
        rows.append({"id": e.id, "name": e.name, "family": e.family_name, "result": "PASS" if ok else "FAIL",
                     "first_failed": None if ok else "benchmark", "expectancy_r": 0.3})
        details[e.id] = {"result": "PASS" if ok else "FAIL", "stability": {"base_params": {}},
                         "regimes": {f"trend:{t}": {"num_trades": 60, "profit_factor": 1.8} for t in
                                     ("bull", "bear", "sideways")},
                         "oos": {"num_trades": 300, "profit_factor": 1.6, "expectancy_r": 0.3, "win_rate_pct": 45},
                         "bootstrap": {"expectancy_r_ci": [0.1, 0.5], "pf_ci": [1.2, 2.0]},
                         "composite": {"score": 70}, "mfe_r_quantiles": {"p50": 1.5, "p75": 2.5, "n": 200}}
    return {"run_id": "test", "headline": "test", "leaderboard": rows, "details": details}


def test_no_report_gives_exact_no_trade(client, monkeypatch):
    monkeypatch.setattr(de, "load_report", lambda: None)
    r = client.get("/api/decision").json()
    assert r["decision"] == "NO TRADE" and r["message"] == NO_TRADE_MSG
    assert r["message"] == "NO TRADE: no setup has a validated edge in the current regime"
    assert next(g for g in r["gates"] if g["name"] == "Tournament evidence")["passed"] is False
    assert next(g for g in r["gates"] if g["name"] == "Event gate")["passed"] is None  # UNKNOWN, never silently passed
    assert isinstance(r["watch_only"], list)
    for w in r["watch_only"]:
        assert w["what_must_change"]
    names = {i["name"] for i in r["regime"]["inputs"]}
    assert "FII/DII flows" in names


def test_failed_strategies_never_produce_buys(client, monkeypatch):
    monkeypatch.setattr(de, "regime_state", _bull)
    with SessionLocal() as s:
        r = de.decide(s, report=_fake_report(set()))
    assert r["decision"] == "NO TRADE" and r["picks"] == [] and r["eligible_strategies"] == []
    assert all("did not pass" in x["reason"] for x in r["rejected_strategies"])


def test_bear_budget_blocks_everything(client, monkeypatch):
    def bear(*_a, **_k):
        return {**_bull(), "trend": "bear", "label": "BEAR", "exposure_budget_pct": 0.0, "multiplier": 0.0}

    monkeypatch.setattr(de, "regime_state", bear)
    daily = {e.id for e in CATALOG if e.kind == "event" and e.timeframe == "1d"}
    with SessionLocal() as s:
        r = de.decide(s, report=_fake_report(daily))
    assert r["decision"] == "NO TRADE"
    assert next(g for g in r["gates"] if g["name"] == "Regime exposure budget")["passed"] is False


def test_passed_strategies_produce_complete_capped_picks(client, monkeypatch):
    monkeypatch.setattr(de, "regime_state", _bull)
    daily = {e.id for e in CATALOG if e.kind == "event" and e.timeframe == "1d"}
    with SessionLocal() as s:
        r = de.decide(s, report=_fake_report(daily))
    assert len(r["eligible_strategies"]) == len(daily)
    assert r["decision"] == "BUY" and 1 <= len(r["picks"]) <= 5
    from app.risk.limits import load_risk_config

    with SessionLocal() as s:
        cfg = load_risk_config(s)
    budget = cfg.account_equity * cfg.risk_per_trade_pct / 100
    seen = set()
    for p in r["picks"]:
        for f in PICK_FIELDS:
            assert f in p, f
        assert p["symbol"] not in seen
        seen.add(p["symbol"])
        assert p["max_loss"] <= budget * 1.001
        assert p["capital_used"] <= cfg.account_equity * cfg.max_alloc_per_instrument_pct / 100 + 1
        assert p["direction"] == "LONG" and p["stop"] < p["entry_trigger"]
        lo, hi = p["confidence"]["expectancy_r_range"]
        assert lo < hi
    if r["decision"] == "NO TRADE":
        assert r["message"] == NO_TRADE_MSG
