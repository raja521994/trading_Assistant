"""Backtest engine execution-model and metrics tests on hand-built bars."""

from datetime import date

import pandas as pd
import pytest

from app.backtest.engine import BTConfig, run_portfolio
from app.backtest.metrics import compute_metrics
from app.backtest.walkforward import walk_forward
from app.data.sample_generator import generate_daily
from app.quant.strategies.base import Setup, Strategy
from app.risk.costs import CostModel

ZERO_COSTS = CostModel(stt_delivery_pct=0, exchange_txn_pct_nse=0, exchange_txn_pct_bse=0, sebi_fee_per_crore=0,
                       stamp_delivery_buy_pct=0, gst_pct=0, dp_charge_per_sell=0)
SPEC = {"id": "test", "name": "Test", "version": "0", "category": "test", "modes": ["swing"], "timeframe": "1d",
        "direction": "long_only", "params": {}}


class OneShot(Strategy):
    """Signals LONG on the close of bar `at` with a fixed stop distance and 2R target."""

    def __init__(self, at: int, stop_dist: float = 2.0, target_r: float | None = 2.0, exit_at: int | None = None):
        super().__init__(SPEC)
        self.at, self.stop_dist, self.target_r, self.exit_at = at, stop_dist, target_r, exit_at

    @property
    def warmup(self) -> int:
        return 1

    def prepare(self, df, base=None):
        return df.copy()

    def entry(self, a, i):
        if i == self.at:
            return Setup("LONG", a["close"][i] - self.stop_dist, self.target_r, ["test"], "test", 50)
        return None

    def exit(self, a, i, direction):
        return "strategy exit" if self.exit_at is not None and i == self.exit_at else None


def bars(n=20, overrides=None):
    idx = pd.bdate_range("2024-01-01", periods=n).tz_localize("Asia/Kolkata")
    df = pd.DataFrame({"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1e5}, index=idx)
    for i, vals in (overrides or {}).items():
        for k, v in vals.items():
            df.iloc[i, df.columns.get_loc(k)] = v
    return df


def run(strat, df, **cfg):
    c = BTConfig(slippage_bps=0, costs=ZERO_COSTS, **cfg)
    return run_portfolio(strat, {("NSE", "TEST"): df}, c)


def test_entry_fills_at_next_open_and_target_exit():
    df = bars(overrides={6: {"open": 100.2}, 8: {"high": 104.5}})
    res = run(OneShot(at=5), df)
    t = res["trades"][0]
    assert t.entry_ts == df.index[6].isoformat()
    assert t.entry_price == pytest.approx(100.2)
    # risk/share = 100.2 - 98 = 2.2 -> target 104.6 not reached at 104.5; ensure no exit at bar 8
    assert t.exit_reason == "End of test period"
    df2 = bars(overrides={8: {"high": 104.5}})
    t2 = run(OneShot(at=5), df2)["trades"][0]
    assert t2.exit_reason == "Target" and t2.exit_price == pytest.approx(104.0)
    # qty capped by 15% allocation: 150,000 / 100 = 1,500
    assert t2.qty == 1500 and t2.gross_pnl == pytest.approx(6000.0)
    assert t2.r_multiple == pytest.approx(2.0)


def test_gap_through_stop_exits_at_open():
    df = bars(overrides={7: {"open": 97.0, "low": 96.5, "high": 97.5, "close": 97.0}})
    t = run(OneShot(at=5), df)["trades"][0]
    assert t.exit_reason == "Stop (gap through)" and t.exit_price == pytest.approx(97.0)
    assert t.r_multiple == pytest.approx(-1.5)


def test_stop_assumed_before_target_in_same_bar():
    df = bars(overrides={7: {"high": 105.0, "low": 97.0}})
    t = run(OneShot(at=5), df)["trades"][0]
    assert t.exit_reason == "Stop loss" and t.exit_price == pytest.approx(98.0)


def test_strategy_exit_fills_next_open():
    df = bars(overrides={10: {"open": 101.0}})
    t = run(OneShot(at=5, target_r=None, exit_at=9), df)["trades"][0]
    assert t.exit_reason == "strategy exit"
    assert t.exit_ts == df.index[10].isoformat() and t.exit_price == pytest.approx(101.0)


def test_costs_and_slippage_reduce_pnl():
    df = bars(overrides={8: {"high": 104.5}})
    gross = run(OneShot(at=5), df)["trades"][0]
    c = BTConfig(slippage_bps=10, costs=CostModel())
    net = run_portfolio(OneShot(at=5), {("NSE", "TEST"): df}, c)["trades"][0]
    assert net.entry_price == pytest.approx(100.1)
    assert net.costs > 0 and net.net_pnl < gross.net_pnl


def test_no_entry_on_last_bar_and_window_respected():
    df = bars()
    assert run(OneShot(at=19), df)["trades"] == []
    res = run(OneShot(at=5), df, start=df.index[8])
    assert res["trades"] == []


def test_equity_marks_to_market():
    df = bars(overrides={7: {"close": 101.0}})
    res = run(OneShot(at=5), df)
    assert res["equity"].iloc[0] == pytest.approx(1_000_000.0)
    assert res["equity"].loc[df.index[7]] == pytest.approx(1_000_000.0 + 1500 * 1.0)


def test_metrics_drawdown_and_return():
    idx = pd.bdate_range("2024-01-01", periods=4).tz_localize("Asia/Kolkata")
    eq = pd.Series([100.0, 120.0, 90.0, 135.0], index=idx)
    m = compute_metrics(eq, [], 100.0, 0.5)
    assert m["net_return_pct"] == pytest.approx(35.0)
    assert m["max_drawdown_pct"] == pytest.approx(-25.0)
    assert m["cagr_pct"] is None  # < 1 year
    assert m["sharpe"] is None  # < 20 observations
    assert m["exposure_pct"] == pytest.approx(50.0)


def test_walk_forward_reports_only_oos_segments():
    d = generate_daily(7, date(2022, 1, 3), date(2024, 12, 31))
    data = {k: v for k, v in list(d.items())[:6]}
    wf = walk_forward("ema_cross", data, BTConfig(), folds=3)
    assert len(wf["folds"]) == 3
    first_oos = pd.Timestamp(wf["folds"][0]["out_of_sample"]["start"]).date()
    for t in wf["oos_trades"]:
        assert pd.Timestamp(t.entry_ts).date() >= first_oos
    for f in wf["folds"]:
        assert f["in_sample"]["end"] == f["out_of_sample"]["start"]
