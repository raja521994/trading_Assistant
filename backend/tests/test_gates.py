"""Evidence-based gates: regime filter and out-of-sample edge requirement."""

from app.quant.scanner import build_playbook, evaluate_gates
from app.quant.strategies.base import get_strategy
from app.risk.limits import RiskConfig


def regime(close, sma200):
    return {"current": {"facts": {"close": close, "sma200": sma200}}}


EV_GOOD = {"status": "ready", "num_trades": 120, "profit_factor": 1.3,
           "regime_split": {"up": {"num_trades": 80, "profit_factor": 1.6}, "down": {"num_trades": 40, "profit_factor": 0.5}}}


def test_regime_gate_blocks_longs_below_200dma():
    s = get_strategy("donchian_breakout")
    g = evaluate_gates(s, "LONG", EV_GOOD, regime(100, 110), RiskConfig())
    assert g["regime"]["applies"] and not g["regime"]["passed"]
    assert "below its 200-day average" in g["regime"]["detail"]
    g2 = evaluate_gates(s, "LONG", EV_GOOD, regime(120, 110), RiskConfig())
    assert g2["regime"]["passed"]
    off = evaluate_gates(s, "LONG", EV_GOOD, regime(100, 110), RiskConfig(regime_filter=False))
    assert not off["regime"]["applies"]


def test_edge_gate_uses_current_regime_bucket():
    s = get_strategy("donchian_breakout")
    up = evaluate_gates(s, "LONG", EV_GOOD, regime(120, 110), RiskConfig())
    assert up["edge"]["passed"] and up["edge"]["profit_factor"] == 1.6
    down = evaluate_gates(s, "LONG", EV_GOOD, regime(100, 110), RiskConfig())
    assert not down["edge"]["passed"] and down["edge"]["profit_factor"] == 0.5
    weak = {**EV_GOOD, "profit_factor": 1.05, "regime_split": {}}
    assert not evaluate_gates(s, "LONG", weak, regime(120, 110), RiskConfig())["edge"]["passed"]
    pending = evaluate_gates(s, "LONG", {"status": "pending"}, regime(120, 110), RiskConfig())
    assert not pending["edge"]["passed"]
    disabled = evaluate_gates(s, "LONG", None, regime(120, 110), RiskConfig(require_oos_edge=False))
    assert not disabled["edge"]["applies"]


def test_intraday_strategy_not_regime_gated():
    g = evaluate_gates(get_strategy("orb_vwap"), "SHORT", EV_GOOD, regime(100, 110), RiskConfig())
    assert not g["regime"]["applies"]


def test_playbook_headline_defensive_below_200dma():
    pb = build_playbook(regime(100, 110), {"donchian_breakout": EV_GOOD}, RiskConfig(), "LIVE")
    assert pb["benchmark_above_200dma"] is False and "setups are WAIT" in pb["headline"]
    assert all(not (r["regime_gate"] and r["edge_gate"]) for r in pb["strategies"])
    assert len(pb["strategies"]) == 5
