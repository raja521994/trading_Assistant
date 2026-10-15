"""Position sizing, cost model and pre-trade risk-constraint tests."""

import pytest

from app.risk.costs import CostModel, order_costs, round_trip_costs
from app.risk.limits import OpenPosition, PortfolioState, RiskConfig, TradeProposal, check_trade
from app.risk.sizing import position_size


def test_size_limited_by_allocation():
    r = position_size(1_000_000, 100.0, 98.0, risk_pct=1.0, max_alloc_pct=15.0)
    # risk budget 10,000 / 2 = 5,000 shares; allocation 150,000 / 100 = 1,500 shares
    assert r.qty == 1500
    assert r.limiting_factor == "max allocation"
    assert r.risk_amount == pytest.approx(3000.0)


def test_size_limited_by_risk_budget():
    r = position_size(1_000_000, 100.0, 90.0, risk_pct=1.0, max_alloc_pct=50.0)
    assert r.qty == 1000
    assert r.limiting_factor == "risk budget"
    assert r.risk_amount == pytest.approx(10_000.0)


def test_size_respects_cash_and_lot_size():
    r = position_size(1_000_000, 100.0, 99.0, risk_pct=1.0, max_alloc_pct=100.0, cash_available=50_000, lot_size=25)
    assert r.qty == 500 and r.limiting_factor == "available cash"
    r2 = position_size(1_000_000, 100.0, 90.1, risk_pct=1.0, max_alloc_pct=100.0, lot_size=25)
    assert r2.qty % 25 == 0 and r2.qty == 1000


def test_size_invalid_stop_returns_zero():
    assert position_size(1_000_000, 100.0, 100.0, 1.0, 15.0).qty == 0
    assert position_size(0, 100.0, 95.0, 1.0, 15.0).qty == 0


def test_delivery_buy_costs():
    c = order_costs("BUY", 1000.0, 100, "CNC", "NSE", CostModel())
    # STT 0.1% = 100, stamp 0.015% = 15, txn 0.00297% = 2.97, SEBI 0.10, GST 18% of (2.97 + 0.10)
    assert c["stt"] == pytest.approx(100.0)
    assert c["stamp_duty"] == pytest.approx(15.0)
    assert c["total"] == pytest.approx(100 + 15 + 2.97 + 0.1 + 0.18 * 3.07, abs=1e-3)


def test_intraday_sell_costs_with_brokerage_cap():
    c = order_costs("SELL", 1000.0, 100, "MIS", "NSE", CostModel())
    assert c["brokerage"] == pytest.approx(20.0)  # min(0.03% of 100k = 30, 20)
    assert c["stt"] == pytest.approx(25.0)
    assert c["total"] == pytest.approx(20 + 25 + 2.97 + 0.1 + 0.18 * 23.07, abs=1e-3)


def test_round_trip_short_assigns_sides_correctly():
    rt = round_trip_costs("SHORT", 100.0, 95.0, 100, "MIS", "NSE", CostModel())
    sell = order_costs("SELL", 100.0, 100, "MIS", "NSE", CostModel())["total"]
    buy = order_costs("BUY", 95.0, 100, "MIS", "NSE", CostModel())["total"]
    assert rt["total"] == pytest.approx(round(sell + buy, 2))


def _state(**kw):
    base = dict(equity=1_000_000.0, cash=1_000_000.0, peak_equity=1_000_000.0, realized_pnl_today=0.0,
                unrealized_pnl=0.0, open_positions=[])
    base.update(kw)
    return PortfolioState(**base)


def _prop(**kw):
    base = dict(symbol="ABC", exchange="NSE", sector="IT", direction="LONG", mode="swing", entry=100.0, stop=98.0,
                target=104.0, qty=1000, avg_turnover_cr=100.0)
    base.update(kw)
    return TradeProposal(**base)


def test_valid_trade_is_approved():
    d = check_trade(_prop(), _state(), RiskConfig())
    assert d.approved, d.violations
    assert any("event risk UNKNOWN" in w for w in d.warnings)


@pytest.mark.parametrize("prop_kw,state_kw,cfg_kw,expected", [
    ({}, {}, {"kill_switch": True}, "Kill switch"),
    ({"qty": 6000}, {}, {}, "Risk Rs"),
    ({"qty": 1600}, {}, {}, "Position value"),
    ({"stop": 101.0}, {}, {}, "Stop 101.00 must be below"),
    ({"direction": "SHORT", "stop": 102.0, "target": 96.0}, {}, {}, "Short selling is only allowed intraday"),
    ({"avg_turnover_cr": 1.0}, {}, {}, "turnover"),
    ({"target": 101.0}, {}, {}, "Risk-reward"),
    ({}, {"open_positions": [OpenPosition(f"S{i}", "X", 10_000, 100) for i in range(8)]}, {}, "Open positions 8"),
    ({}, {"open_positions": [OpenPosition("ABC", "IT", 10_000, 100)]}, {}, "already exists"),
    ({}, {"open_positions": [OpenPosition("XYZ", "IT", 250_000, 100)]}, {}, "IT exposure"),
    ({}, {"realized_pnl_today": -25_000.0}, {}, "Today's loss"),
    ({}, {"equity": 880_000.0, "peak_equity": 1_000_000.0}, {}, "Drawdown"),
    ({}, {"cash": 50_000.0}, {}, "available cash"),
])
def test_risk_constraints_reject(prop_kw, state_kw, cfg_kw, expected):
    d = check_trade(_prop(**prop_kw), _state(**state_kw), RiskConfig(**cfg_kw))
    assert not d.approved
    assert any(expected in v for v in d.violations), d.violations


def test_intraday_short_allowed_when_enabled():
    d = check_trade(_prop(direction="SHORT", mode="intraday", stop=102.0, target=96.0), _state(), RiskConfig())
    assert d.approved, d.violations
    d2 = check_trade(_prop(direction="SHORT", mode="intraday", stop=102.0, target=96.0), _state(),
                     RiskConfig(allow_intraday_short=False))
    assert not d2.approved
