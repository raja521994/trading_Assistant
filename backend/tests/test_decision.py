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
    assert all("Evidence NONE" in x["reason"] for x in r["rejected_strategies"])


def test_bear_budget_blocks_everything(client, monkeypatch):
    def bear(*_a, **_k):
        return {**_bull(), "trend": "bear", "label": "BEAR", "exposure_budget_pct": 0.0, "multiplier": 0.0}

    monkeypatch.setattr(de, "regime_state", bear)
    daily = {e.id for e in CATALOG if e.kind == "event" and e.timeframe == "1d"}
    with SessionLocal() as s:
        from app.risk.limits import load_risk_config, save_risk_config

        cfg = load_risk_config(s)
        cfg.evidence_policy = "strict"  # strict keeps the regime's 0% budget in a bear market
        save_risk_config(s, cfg)
        s.commit()
        r = de.decide(s, report=_fake_report(daily))
        cfg.evidence_policy = "balanced"
        save_risk_config(s, cfg)
        s.commit()
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


def _report_with(checks_ok: dict, pf=1.5, exp=0.3, n=200, regime=(60, 1.6), result="FAIL", first="multiple_testing",
                 kind="event"):
    from app.quant.library.catalog import BY_ID

    e = BY_ID["darvas_box"]
    names = ["min_oos_trades", "profit_factor", "expectancy", "benchmark", "max_drawdown", "survivorship_sensitivity",
             "parameter_stability", "multiple_testing", "double_costs", "subperiods"]
    checks = [{"name": k, "passed": checks_ok.get(k, True), "detail": k} for k in names]
    row = {"id": e.id, "name": e.name, "family": e.family_name, "kind": kind, "result": result, "first_failed": first,
           "expectancy_r": exp}
    det = {"checks": checks, "oos": {"num_trades": n, "profit_factor": pf, "expectancy_r": exp},
           "regimes": {"trend:bear": {"num_trades": regime[0], "profit_factor": regime[1]}},
           "bootstrap": {"pf_ci": [1.1, 2.0], "expectancy_r_ci": [0.05, 0.5]}}
    return {"run_id": "t", "leaderboard": [row], "details": {e.id: det}}


def test_policy_tiers_and_acceptance():
    from app.decision import policy as pol

    prov = _report_with({"multiple_testing": False})
    q = pol.evaluate("darvas_box", "bear", prov)
    assert q["tier"] == "PROVISIONAL"
    assert not pol.qualify("darvas_box", "bear", "strict", prov)["ok"]
    assert pol.qualify("darvas_box", "bear", "balanced", prov)["ok"]
    assert pol.qualify("darvas_box", "bear", "aggressive", prov)["ok"]
    # lost money in the current regime -> never accepted, under any policy
    bad_regime = _report_with({"multiple_testing": False}, regime=(30, 0.6))
    assert pol.evaluate("darvas_box", "bear", bad_regime)["tier"] == "NONE"
    # weak edge (expectancy CI includes 0) -> SPECULATIVE: aggressive only
    weak = _report_with({"multiple_testing": False, "expectancy": False}, pf=1.15, regime=(8, 1.1))
    assert pol.evaluate("darvas_box", "bear", weak)["tier"] == "SPECULATIVE"
    assert not pol.qualify("darvas_box", "bear", "balanced", weak)["ok"]
    assert pol.qualify("darvas_box", "bear", "aggressive", weak)["ok"]
    # not profitable with 2x costs -> NONE
    assert pol.evaluate("darvas_box", "bear", _report_with({"double_costs": False}))["tier"] == "NONE"
    # cross-sectional strategy without point-in-time constituents -> NONE
    pit = _report_with({"point_in_time_universe": False}, kind="portfolio")
    assert pol.evaluate("darvas_box", "bear", pit)["tier"] == "NONE"
    # passed everything and works in the regime -> VALIDATED (strict accepts)
    full = _report_with({}, result="PASS", first=None)
    assert pol.qualify("darvas_box", "bear", "strict", full)["tier"] == "VALIDATED"


def test_balanced_policy_trades_bear_market_at_reduced_size(client, monkeypatch):
    def bear(*_a, **_k):
        return {**_bull(), "trend": "bear", "label": "BEAR", "exposure_budget_pct": 0.0, "multiplier": 0.0}

    monkeypatch.setattr(de, "regime_state", bear)
    daily = {e.id for e in CATALOG if e.kind == "event" and e.timeframe == "1d"}
    rep = _fake_report(daily)  # PASS + PF 1.8 in every regime -> VALIDATED
    with SessionLocal() as s:
        from app.risk.limits import load_risk_config, save_risk_config

        cfg = load_risk_config(s)
        cfg.evidence_policy = "balanced"
        save_risk_config(s, cfg)
        s.commit()
        r = de.decide(s, report=rep)
    assert r["regime"]["exposure_budget_pct"] == 50.0 and "raised" in r["regime"]["budget_note"]
    assert r["eligible_strategies"]
    budget = cfg.account_equity * cfg.risk_per_trade_pct / 100 * 0.5
    for p in r["picks"]:
        assert p["max_loss"] <= budget * 1.001 and p["tier"] == "VALIDATED"
