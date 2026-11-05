"""Validation engine & tournament: statistics, pass logic (first failed criterion), random-walk control, reports."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.data.sample_generator import generate_daily
from app.quant.library.catalog import BY_ID, CATALOG, testable
from app.validation import stats
from app.validation.evaluate import Bundle, evaluate
from app.validation.tournament import (assemble, finalize, leaderboard_csv, load_config, random_walk_bundle,
                                       regime_frame, total_return_index)


def test_catalog_covers_all_52_strategies_plus_additions():
    assert [e.no for e in CATALOG] == list(range(1, 56))
    kinds = {e.kind for e in CATALOG}
    assert kinds == {"event", "portfolio", "pairs", "context", "not_testable"}
    for e in CATALOG:
        if e.kind == "not_testable":
            assert e.missing_data, e.id
        if e.testable:
            assert e.combos(), e.id
    assert {e.no for e in CATALOG if e.family == "I"} == {50, 51, 52}
    assert len(testable()) == 37  # 52 - 15 NOT TESTABLE - 3 context inputs + 3 additional published methods


def test_bootstrap_and_deflated_sharpe():
    rng = np.random.default_rng(1)
    good = rng.normal(0.4, 1.0, 300)
    b = stats.bootstrap(good * 1000, good)
    assert b["expectancy_r_ci"][0] > 0 and b["pf_ci"][0] > 1 and b["p_value"] < 0.01
    noise = rng.normal(0.0, 1.0, 300)
    bn = stats.bootstrap(noise * 1000, noise)
    assert bn["expectancy_r_ci"][0] < 0 < bn["expectancy_r_ci"][1]
    sr0 = stats.expected_max_sharpe(50, 0.01)
    assert sr0 > 0
    st_strong = stats.sharpe_stats(rng.normal(0.003, 0.01, 1000))
    st_noise = stats.sharpe_stats(rng.normal(0.0, 0.01, 1000))
    assert stats.deflated_sharpe(st_strong, sr0) > stats.deflated_sharpe(st_noise, sr0)
    assert stats.benjamini_hochberg({"a": 0.001, "b": 0.04, "c": 0.5}, 0.05) == {"a"}


def test_total_return_index_handles_yearly_reset():
    idx = pd.bdate_range("2024-12-27", periods=6).tz_localize("Asia/Kolkata")
    close = pd.Series(100.0, index=idx)
    dp = pd.Series([10.0, 11.0, 12.0, 0.5, 0.5, 1.5], index=idx)  # reset to a new year after the 3rd day
    tri, label = total_return_index(close, dp.reindex(pd.bdate_range("2024-06-03", periods=60).tz_localize(
        "Asia/Kolkata").union(idx)).fillna(10.0))
    assert "Dividend" in label
    assert tri.iloc[-1] > 1.0


def _fake_result(sid: str, **overrides):
    checks = [{"name": n, "passed": True, "value": 1, "threshold": 1, "detail": n} for n in
              ("min_oos_trades", "profit_factor", "expectancy", "benchmark", "max_drawdown", "parameter_stability")]
    checks.append({"name": "multiple_testing", "passed": None, "value": None, "threshold": 0.95, "detail": ""})
    checks += [{"name": n, "passed": True, "value": 1, "threshold": 1, "detail": n} for n in ("double_costs", "subperiods")]
    for c in checks:
        if c["name"] in overrides.get("fail", []):
            c["passed"] = False
    e = BY_ID[sid]
    rng = np.random.default_rng(abs(hash(sid)) % 1000)
    r = rng.normal(overrides.get("mu", 0.002), 0.01, 1500)
    return {"id": sid, "no": e.no, "name": e.name, "family": e.family, "family_name": e.family_name, "kind": e.kind,
            "timeframe": e.timeframe, "mode": e.modes[0], "direction": e.direction, "status": "TESTED", "grid_size": 2,
            "checks": checks, "sharpe_stats": stats.sharpe_stats(r), "bootstrap": {"p_value": 0.001, "pf_ci": [1.3, 2]},
            "oos": {"num_trades": 200, "profit_factor": 1.6, "expectancy_r": 0.3, "net_return_pct": 80,
                    "max_drawdown_pct": -12, "calmar": 1.5, "annualised_return_pct": 15, "capacity_cr": 50,
                    "period_start": "2020-01-01", "period_end": "2026-01-01"},
            "benchmark": {"return_pct": 60}, "stability": {"score": 90, "passed": True},
            "subperiods": {"working": 4, "total": 5}, "regimes": {}}


def test_pass_logic_reports_first_failed_criterion_and_never_pads():
    conf = load_config()
    res = [_fake_result("ema_cross", mu=0.004), _fake_result("donchian_breakout", mu=0.004, fail=["benchmark", "subperiods"]),
           _fake_result("rsi2_meanrev", mu=0.0)]
    meta = finalize(res, conf)
    by = {r["id"]: r for r in res}
    assert by["ema_cross"]["result"] == "PASS"
    assert by["donchian_breakout"]["result"] == "FAIL" and by["donchian_breakout"]["first_failed"] == "benchmark"
    assert by["rsi2_meanrev"]["result"] == "FAIL" and by["rsi2_meanrev"]["first_failed"] == "multiple_testing"
    assert meta["n_trials"] == 6


@pytest.fixture(scope="module")
def rw_bundle():
    d = {k: v for k, v in generate_daily(9, date(2018, 1, 1), date(2024, 12, 31)).items() if v["volume"].sum() > 0}
    keys = list(d)[:14]
    real = Bundle({k: d[k] for k in keys}, {}, None, None, "n/a", None)
    return random_walk_bundle(real, 14, seed=3)


def test_random_walk_control_nothing_passes(rw_bundle):
    """If any strategy passes on zero-drift random walks, the validation is broken."""
    conf = load_config()
    results = []
    for sid in ("donchian_breakout", "rsi2_meanrev", "ema_pullback", "xs_momentum"):
        results.append(evaluate(BY_ID[sid].as_dict(), rw_bundle, conf))
    assert all(r["status"] == "TESTED" for r in results), [r.get("error") for r in results]
    meta = finalize(results, conf)
    assert [r["id"] for r in results if r["result"] == "PASS"] == []
    for r in results:
        assert r["first_failed"] and r["folds"] and len(r["folds"]) == conf["walk_forward"]["folds"]
        # walk-forward hygiene: OOS segments are disjoint, ordered and after the embargo
        for f in r["folds"]:
            assert pd.Timestamp(f["in_sample"][1]) < pd.Timestamp(f["out_of_sample"][0])
    rep = assemble(results, meta, None, conf, rw_bundle, "test", 1.0)
    assert rep["top10"] == [] and "Only 0 of 10" in rep["headline"]
    assert len(rep["not_testable"]) == sum(e.kind == "not_testable" for e in CATALOG)
    csv_text = leaderboard_csv(rep)
    assert "NOT TESTABLE" in csv_text and "first_failed" in csv_text
    from app.validation.report_html import render

    html = render(rep)
    assert "No strategy passed every criterion" in html and "<svg" in html


def test_regime_frame_labels():
    idx = pd.bdate_range("2020-01-01", periods=400).tz_localize("Asia/Kolkata")
    up = pd.Series(np.linspace(100, 200, 400), index=idx)
    rf = regime_frame(up, None)
    assert rf["trend"].iloc[-1] == "bull" and bool(rf["above200"].iloc[-1])
    assert rf["trend"].iloc[10] == "unknown"
