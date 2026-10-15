"""Performance metrics with documented assumptions."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from .engine import Trade

METRIC_ASSUMPTIONS = [
    "Returns are computed on end-of-day portfolio equity (intraday curves are sampled at each session's last bar).",
    "Sharpe = mean(daily excess return) / stdev(daily return) x sqrt(252); risk-free rate default 0 "
    "(configurable) because idle cash is assumed to earn nothing in the simulation.",
    "Sortino uses downside deviation of daily excess returns below 0, annualised with sqrt(252).",
    "CAGR is reported only for test periods of at least one year.",
    "Profit factor = gross winning P&L / gross losing P&L (net of costs per trade).",
    "Turnover = total traded value (entries + exits) / average equity.",
    "Benchmark = buy-and-hold NIFTY 50 index level over the same window (no dividends, no costs).",
]


def _f(x: float | None, nd: int = 4) -> float | None:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), nd)


def daily_equity(equity: pd.Series) -> pd.Series:
    if equity.empty:
        return equity
    return equity.groupby(equity.index.date).last()


def compute_metrics(equity: pd.Series, trades: list[Trade], capital: float, exposure: float,
                    benchmark: pd.Series | None = None, rf_annual: float = 0.0) -> dict[str, Any]:
    out: dict[str, Any] = {"num_trades": len(trades)}
    if equity.empty:
        return {**out, "net_return_pct": None, "note": "No bars in the selected window"}
    d = daily_equity(equity)
    start_eq = capital
    end_eq = float(d.iloc[-1])
    net_ret = end_eq / start_eq - 1.0
    days = (pd.Timestamp(d.index[-1]) - pd.Timestamp(d.index[0])).days
    years = days / 365.25
    rets = d.pct_change().dropna()
    rets = pd.concat([pd.Series([d.iloc[0] / start_eq - 1.0]), rets]) if len(d) else rets
    rf_d = (1 + rf_annual) ** (1 / 252) - 1
    ex = rets - rf_d
    sharpe = None
    if len(rets) >= 20 and rets.std(ddof=1) > 0:
        sharpe = ex.mean() / rets.std(ddof=1) * math.sqrt(252)
    sortino = None
    downside = np.sqrt(np.mean(np.minimum(ex.to_numpy(), 0.0) ** 2)) if len(ex) else 0.0
    if len(rets) >= 20 and downside > 0:
        sortino = ex.mean() / downside * math.sqrt(252)
    curve = d / d.cummax() - 1.0
    mdd = float(min(curve.min(), 0.0, d.iloc[0] / start_eq - 1.0))

    pnl = np.array([t.net_pnl for t in trades])
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    gross_win = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(-losses.sum()) if len(losses) else 0.0
    avg_eq = float(d.mean())
    traded_value = sum(t.entry_value + t.exit_value for t in trades)

    out.update({
        "start_equity": round(start_eq, 2),
        "end_equity": round(end_eq, 2),
        "net_return_pct": _f(net_ret * 100, 3),
        "cagr_pct": _f(((end_eq / start_eq) ** (1 / years) - 1) * 100, 3) if years >= 1 and end_eq > 0 else None,
        "max_drawdown_pct": _f(mdd * 100, 3),
        "sharpe": _f(sharpe, 3),
        "sortino": _f(sortino, 3),
        "win_rate_pct": _f(len(wins) / len(trades) * 100, 2) if trades else None,
        "profit_factor": _f(gross_win / gross_loss, 3) if gross_loss > 0 else None,
        "expectancy": _f(float(pnl.mean()), 2) if trades else None,
        "expectancy_r": _f(float(np.mean([t.r_multiple for t in trades])), 3) if trades else None,
        "avg_win": _f(float(wins.mean()), 2) if len(wins) else None,
        "avg_loss": _f(float(losses.mean()), 2) if len(losses) else None,
        "turnover_x": _f(traded_value / avg_eq, 3) if avg_eq else None,
        "exposure_pct": _f(exposure * 100, 2),
        "avg_holding_bars": _f(float(np.mean([t.bars_held for t in trades])), 2) if trades else None,
        "total_costs": round(float(sum(t.costs for t in trades)), 2),
        "period_start": str(d.index[0]),
        "period_end": str(d.index[-1]),
        "period_days": days,
        "risk_free_annual": rf_annual,
    })
    if benchmark is not None and len(benchmark) > 1:
        b = benchmark[(benchmark.index >= equity.index[0].normalize()) & (benchmark.index <= equity.index[-1])]
        if len(b) > 1:
            b_ret = float(b.iloc[-1] / b.iloc[0] - 1.0)
            out["benchmark_return_pct"] = _f(b_ret * 100, 3)
            out["excess_return_pct"] = _f((net_ret - b_ret) * 100, 3)
    out["low_sample_warning"] = len(trades) < 30
    return out


def period_returns(equity: pd.Series, capital: float) -> dict[str, list[dict[str, Any]]]:
    d = daily_equity(equity)
    if d.empty:
        return {"monthly": [], "yearly": []}
    s = pd.Series(d.values, index=pd.DatetimeIndex(pd.to_datetime(list(d.index))))
    out: dict[str, list[dict[str, Any]]] = {}
    for key, freq, fmt in (("monthly", "ME", "%Y-%m"), ("yearly", "YE", "%Y")):
        last = s.resample(freq).last().dropna()
        prev = last.shift(1)
        prev.iloc[0] = capital
        r = last / prev - 1.0
        out[key] = [{"period": ix.strftime(fmt), "return_pct": round(float(v) * 100, 3)} for ix, v in r.items()]
    return out


def drawdown_series(equity: pd.Series) -> pd.Series:
    d = daily_equity(equity)
    return (d / d.cummax() - 1.0) * 100.0
