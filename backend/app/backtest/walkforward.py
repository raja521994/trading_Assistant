"""Walk-forward evaluation: anchored in-sample parameter selection on a small published grid, then
out-of-sample (OOS) evaluation on the next unseen segment. Only OOS results are used as evidence."""

from __future__ import annotations

import itertools
from typing import Any

import pandas as pd

from ..quant.strategies.base import get_strategy, load_specs
from ..quant.universe import features
from .engine import BTConfig, run_portfolio
from .metrics import compute_metrics


def param_combos(strategy_id: str) -> list[dict[str, Any]]:
    grid = load_specs()[strategy_id].get("param_grid") or {}
    if not grid:
        return [{}]
    keys = list(grid.keys())
    return [dict(zip(keys, vals, strict=True)) for vals in itertools.product(*[grid[k] for k in keys])]


def _session_dates(data: dict[tuple[str, str], pd.DataFrame]) -> list:
    dates: set = set()
    for df in data.values():
        dates.update(df.index.date)
    return sorted(dates)


def walk_forward(strategy_id: str, data: dict[tuple[str, str], pd.DataFrame], cfg: BTConfig, folds: int = 3,
                 benchmark: pd.Series | None = None, min_is_trades: int = 5) -> dict[str, Any]:
    dates = _session_dates(data)
    if len(dates) < (folds + 1) * 5:
        return {"error": "Not enough history for walk-forward", "folds": []}
    chunk = len(dates) // (folds + 1)
    bounds = [dates[k * chunk] for k in range(folds + 1)] + [dates[-1]]
    tz = next(iter(data.values())).index.tz
    combos = param_combos(strategy_id)
    prep_cache: dict[tuple, dict[tuple[str, str], pd.DataFrame]] = {}

    def prepared_for(params: dict[str, Any]):
        key = tuple(sorted(params.items()))
        if key not in prep_cache:
            strat = get_strategy(strategy_id, params)
            prep_cache[key] = {k: strat.prepare(df, features(k, df, strat.intraday)) for k, df in data.items()}
        return prep_cache[key]

    def ts(d, end=False) -> pd.Timestamp:
        t = pd.Timestamp(d).tz_localize(tz)
        return t + pd.Timedelta(hours=23, minutes=59) if end else t

    fold_results = []
    oos_trades = []
    oos_curve_parts: list[pd.Series] = []
    equity_level = cfg.capital
    oos_exposures = []
    for k in range(1, folds + 1):
        is_start, is_end = ts(bounds[0]), ts(bounds[k], end=True) - pd.Timedelta(days=1)
        oos_start, oos_end = ts(bounds[k]), ts(bounds[k + 1], end=True)
        best, best_obj, best_is = None, float("-inf"), None
        for params in combos:
            strat = get_strategy(strategy_id, params)
            c = cfg.with_window(cfg.capital, is_start, is_end)
            res = run_portfolio(strat, data, c, prepared_for(params))
            m = compute_metrics(res["equity"], res["trades"], cfg.capital, res["exposure"])
            obj = m.get("sharpe")
            obj = float("-inf") if obj is None else obj
            if m["num_trades"] < min_is_trades:
                obj -= 100.0
            if obj > best_obj or best is None:
                best, best_obj, best_is = params, obj, m
        strat = get_strategy(strategy_id, best)
        c = cfg.with_window(equity_level, oos_start, oos_end)
        res = run_portfolio(strat, data, c, prepared_for(best))
        m = compute_metrics(res["equity"], res["trades"], equity_level, res["exposure"], benchmark)
        if len(res["equity"]):
            oos_curve_parts.append(res["equity"])
            equity_level = float(res["equity"].iloc[-1])
        oos_trades.extend(res["trades"])
        oos_exposures.append(res["exposure"])
        fold_results.append({
            "fold": k, "in_sample": {"start": str(bounds[0]), "end": str(bounds[k])},
            "out_of_sample": {"start": str(bounds[k]), "end": str(bounds[k + 1])},
            "selected_params": best, "is_metrics": _brief(best_is), "oos_metrics": _brief(m),
        })
    oos_equity = pd.concat(oos_curve_parts) if oos_curve_parts else pd.Series(dtype=float)
    oos_equity = oos_equity[~oos_equity.index.duplicated(keep="last")]
    exposure = sum(oos_exposures) / len(oos_exposures) if oos_exposures else 0.0
    oos_metrics = compute_metrics(oos_equity, oos_trades, cfg.capital, exposure, benchmark)
    return {"folds": fold_results, "oos_metrics": oos_metrics, "oos_trades": oos_trades, "oos_equity": oos_equity,
            "grid": combos, "method": "Anchored walk-forward; params chosen by in-sample Sharpe on the spec grid; "
                                      "OOS segments stitched sequentially."}


def _brief(m: dict[str, Any] | None) -> dict[str, Any]:
    if not m:
        return {}
    keys = ["num_trades", "net_return_pct", "max_drawdown_pct", "sharpe", "win_rate_pct", "profit_factor",
            "expectancy_r", "benchmark_return_pct"]
    return {k: m.get(k) for k in keys}
