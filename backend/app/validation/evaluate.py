"""Per-strategy honest validation (runs in a worker process).

Walk-forward: anchored in-sample windows ending `embargo_days` before each out-of-sample (OOS) segment; parameters
are picked from the strategy's FIXED grid by in-sample Sharpe; only the stitched OOS results are evidence.
In-sample positions are closed at the in-sample end (purge), so no trade spans the IS/OOS boundary.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ..backtest.engine import BTConfig, Trade, run_portfolio
from ..backtest.metrics import compute_metrics, daily_equity
from ..backtest.service import slippage_fn_for
from ..quant.library.context import set_context
from ..quant.strategies.base import get_strategy
from ..quant.universe import features
from ..risk.costs import CostModel
from . import stats


@dataclass
class Bundle:
    daily: dict[tuple[str, str], pd.DataFrame]
    intraday: dict[tuple[str, str], pd.DataFrame]
    benchmark: pd.Series | None
    tri: pd.Series | None
    tri_label: str
    regimes: pd.DataFrame | None
    index_closes: dict[str, pd.Series] = field(default_factory=dict)
    sector_of: dict[str, str | None] = field(default_factory=dict)
    max_stop_pct: dict[str, float] = field(default_factory=lambda: {"intraday": 1.5, "swing": 6.0, "positional": 10.0})
    label: str = "LIVE"


_BUNDLE: Bundle | None = None


def init_worker(bundle: Bundle) -> None:
    global _BUNDLE
    _BUNDLE = bundle
    set_context(bundle.benchmark, bundle.index_closes, bundle.sector_of)


def evaluate_in_worker(entry: dict[str, Any], conf: dict[str, Any]) -> dict[str, Any]:
    assert _BUNDLE is not None, "worker not initialised"
    try:
        return evaluate(entry, _BUNDLE, conf)
    except Exception as exc:  # noqa: BLE001
        import traceback

        return {"id": entry["id"], "status": "ERROR", "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-2000:]}


def _tz_ts(d: Any, tz: Any, end: bool = False) -> pd.Timestamp:
    t = pd.Timestamp(d).tz_localize(tz) if pd.Timestamp(d).tzinfo is None else pd.Timestamp(d)
    return t + pd.Timedelta(hours=23, minutes=59) if end else t


def _pf(trades: list[Trade]) -> float | None:
    if not trades:
        return None
    v = stats.profit_factor(np.array([t.net_pnl for t in trades]))
    return None if math.isnan(v) else (99.0 if math.isinf(v) else round(v, 3))


def _exp(trades: list[Trade]) -> float | None:
    return round(float(np.mean([t.r_multiple for t in trades])), 4) if trades else None


def _perturb(v: Any, pct: float, up: bool) -> Any:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = 1 + pct / 100 if up else 1 - pct / 100
    if isinstance(v, int):
        n = int(round(v * f))
        if n == v:
            n = v + (1 if up else -1)
        return max(n, 1)
    return round(v * f, 4)


def _bucket_stats(trades: list[Trade]) -> dict[str, Any]:
    return {"num_trades": len(trades), "profit_factor": _pf(trades), "expectancy_r": _exp(trades),
            "net_pnl": round(sum(t.net_pnl for t in trades), 2)}


def _regime_split(trades: list[Trade], regimes: pd.DataFrame | None) -> dict[str, dict[str, Any]]:
    if regimes is None or regimes.empty or not trades:
        return {}
    days = regimes.index
    groups: dict[str, list[Trade]] = {}
    for t in trades:
        d = pd.Timestamp(t.signal_ts or t.entry_ts).tz_convert(days.tz).normalize()
        pos = days.searchsorted(d, side="right") - 1
        if pos < 0:
            continue
        r = regimes.iloc[pos]
        groups.setdefault(f"trend:{r['trend']}", []).append(t)
        groups.setdefault("nifty:" + ("above_200dma" if r["above200"] else "below_200dma"), []).append(t)
        if isinstance(r.get("vix"), str):
            groups.setdefault(f"vix:{r['vix']}", []).append(t)
    return {k: _bucket_stats(v) for k, v in sorted(groups.items())}


def _subperiods(trades: list[Trade], conf: dict[str, Any], oos_start: pd.Timestamp, oos_end: pd.Timestamp
                ) -> dict[str, Any]:
    c = conf["criteria"]
    tz = oos_start.tz
    periods = []
    for s, e, label in c["subperiods"]:
        ps, pe = _tz_ts(s, tz), _tz_ts(e, tz, end=True)
        overlap = (min(pe, oos_end) - max(ps, oos_start)).days
        if overlap >= 60:
            periods.append((max(ps, oos_start), min(pe, oos_end), label))
    method = "calendar"
    if len(periods) < 3:  # short histories (e.g. one year of 5-minute data): five equal OOS chunks
        method = "equal_chunks"
        edges = pd.date_range(oos_start, oos_end, periods=6)
        periods = [(edges[k], edges[k + 1], f"chunk {k + 1}") for k in range(5)]
    out = []
    for ps, pe, label in periods:
        tr = [t for t in trades if ps <= pd.Timestamp(t.exit_ts) <= pe]
        net = sum(t.net_pnl for t in tr)
        works = len(tr) >= c["min_trades_per_subperiod"] and net > 0
        out.append({"label": label, "start": str(ps.date()), "end": str(pe.date()), "num_trades": len(tr),
                    "net_pnl": round(net, 2), "profit_factor": _pf(tr), "works": works})
    return {"method": method, "periods": out, "working": sum(p["works"] for p in out), "total": len(out)}


def _downsample(s: pd.Series, n: int = 300) -> list[list[Any]]:
    if s.empty:
        return []
    step = max(1, len(s) // n)
    s2 = s.iloc[::step]
    if s2.index[-1] != s.index[-1]:
        s2 = pd.concat([s2, s.iloc[-1:]])
    return [[str(pd.Timestamp(i).date()), round(float(v), 4)] for i, v in s2.items()]


def evaluate(entry: dict[str, Any], b: Bundle, conf: dict[str, Any]) -> dict[str, Any]:
    sid, tf, mode = entry["id"], entry["timeframe"], entry["modes"][0]
    data = b.intraday if tf != "1d" else b.daily
    base_out = {"id": sid, "no": entry["no"], "name": entry["name"], "family": entry["family"],
                "family_name": entry["family_name"], "kind": entry["kind"], "timeframe": tf, "mode": mode,
                "direction": entry["direction"], "data_label": b.label}
    if not data:
        return {**base_out, "status": "NO DATA", "checks": [{"name": "data", "passed": False,
                                                              "detail": f"No {tf} data available"}]}
    wf, bt, crit = conf["walk_forward"], conf["backtest"], conf["criteria"]
    slip_fn = slippage_fn_for(data)
    cfg = BTConfig(capital=float(bt["capital"]), risk_pct=float(bt["risk_pct"]), max_alloc_pct=float(bt["max_alloc_pct"]),
                   max_positions=int(bt["max_positions"]), costs=CostModel(),
                   max_stop_pct=b.max_stop_pct.get(mode), slippage_fn=slip_fn)
    combos = entry.get("grid") or [{}]
    custom = entry["kind"] in ("portfolio", "pairs")
    prep_cache: dict[tuple, Any] = {}

    def prepared_for(params: dict[str, Any]):
        key = tuple(sorted((k, str(v)) for k, v in params.items()))
        if custom:
            return None
        if key not in prep_cache:
            st = get_strategy(sid, params)
            prep_cache[key] = {k: st.prepare_keyed(k, df, features(k, df, st.intraday)) for k, df in data.items()}
        return prep_cache[key]

    def run(params: dict[str, Any], start, end, capital: float, c: BTConfig = cfg) -> dict[str, Any]:
        st = get_strategy(sid, params)
        return run_portfolio(st, data, c.with_window(capital, start, end), prepared_for(params))

    dates = sorted({d for df in data.values() for d in df.index.normalize().unique()})
    tz = dates[0].tz
    folds = int(wf["folds"])
    if len(dates) < (folds + 1) * 20:
        return {**base_out, "status": "NO DATA", "checks": [{"name": "data", "passed": False,
                                                              "detail": "Not enough history for walk-forward"}]}
    chunk = len(dates) // (folds + 1)
    bounds = [dates[k * chunk] for k in range(folds + 1)] + [dates[-1]]
    embargo = pd.Timedelta(days=int(wf["embargo_days"]))
    oos_trades: list[Trade] = []
    parts: list[pd.Series] = []
    level = cfg.capital
    fold_rows, selected, exposures = [], [], []
    for k in range(1, folds + 1):
        is_s, is_e = bounds[0], _tz_ts(bounds[k], tz) - embargo
        oos_s, oos_e = _tz_ts(bounds[k], tz), _tz_ts(bounds[k + 1], tz, end=True)
        best, best_obj, best_m = combos[0], float("-inf"), None
        if len(combos) > 1:
            for params in combos:
                r = run(params, is_s, is_e, cfg.capital)
                m = compute_metrics(r["equity"], r["trades"], cfg.capital, r["exposure"])
                obj = m.get("sharpe")
                obj = -1e9 if obj is None else obj
                if m["num_trades"] < 5:
                    obj -= 100
                if obj > best_obj:
                    best, best_obj, best_m = params, obj, m
        r = run(best, oos_s, oos_e, level)
        m = compute_metrics(r["equity"], r["trades"], level, r["exposure"])
        if len(r["equity"]):
            parts.append(r["equity"])
            level = float(r["equity"].iloc[-1])
        oos_trades += r["trades"]
        exposures.append(r["exposure"])
        selected.append(best)
        fold_rows.append({"fold": k, "in_sample": [str(pd.Timestamp(is_s).date()), str(is_e.date())],
                          "out_of_sample": [str(oos_s.date()), str(oos_e.date())], "params": best,
                          "is_sharpe": None if best_m is None else best_m.get("sharpe"),
                          "oos_trades": m["num_trades"], "oos_return_pct": m.get("net_return_pct"),
                          "oos_profit_factor": m.get("profit_factor")})
    equity = pd.concat(parts) if parts else pd.Series(dtype=float)
    equity = equity[~equity.index.duplicated(keep="last")]
    oos_start, oos_end = _tz_ts(bounds[1], tz), _tz_ts(bounds[-1], tz, end=True)
    m = compute_metrics(equity, oos_trades, cfg.capital, float(np.mean(exposures)) if exposures else 0.0)
    pnl = [t.net_pnl for t in oos_trades]
    rr = [t.r_multiple for t in oos_trades]
    boot = stats.bootstrap(pnl, rr, **{"samples": conf["bootstrap"]["samples"],
                                       "conf": conf["bootstrap"]["confidence"], "seed": conf["bootstrap"]["seed"]})
    deq = daily_equity(equity)
    rets = deq.pct_change().dropna().to_numpy() if len(deq) > 1 else np.array([])
    sh = stats.sharpe_stats(rets)

    # benchmark (NIFTY 50 total return) over the same OOS window
    bench = {}
    if b.tri is not None and len(deq):
        tri = b.tri[(b.tri.index >= oos_start.normalize()) & (b.tri.index <= oos_end)]
        if len(tri) > 1:
            bret = float(tri.iloc[-1] / tri.iloc[0] - 1) * 100
            bench = {"label": b.tri_label, "return_pct": round(bret, 2),
                     "max_drawdown_pct": round(stats.max_drawdown(tri.to_numpy()), 2),
                     "curve": _downsample(tri / tri.iloc[0])}
            bsh = stats.sharpe_stats(tri.pct_change().dropna().to_numpy())
            bench["sharpe"] = None if bsh["sr"] is None else round(bsh["sr"] * math.sqrt(252), 3)
    years = max((oos_end - oos_start).days / 365.25, 1e-9)
    net_ret = m.get("net_return_pct") or 0.0
    mdd = m.get("max_drawdown_pct") or 0.0
    ann = ((1 + net_ret / 100) ** (1 / years) - 1) * 100 if net_ret > -100 else -100.0
    calmar = round(ann / abs(mdd), 3) if mdd else None

    # 2x costs and slippage (trade-level recomputation, sizes held fixed)
    def slip_bps(t: Trade) -> float:
        return 10.0 if "/" in t.symbol else float(slip_fn(t.symbol, t.exchange))

    net2 = np.array([t.net_pnl - t.costs - (t.entry_value + t.exit_value) * slip_bps(t) / 1e4 for t in oos_trades])
    pf2 = stats.profit_factor(net2) if len(net2) else float("nan")
    double_costs = {"profit_factor": None if math.isnan(pf2) else (99.0 if math.isinf(pf2) else round(pf2, 3)),
                    "net_pnl": round(float(net2.sum()), 2) if len(net2) else 0.0,
                    "method": "each OOS trade's charges and slippage doubled (position sizes unchanged)"}

    # parameter stability: +/-pct on the declared parameters, base = most frequently selected params
    base_params = Counter(tuple(sorted(p.items())) for p in selected).most_common(1)[0][0] if selected else ()
    base_params = dict(base_params)
    defaults = get_strategy(sid, base_params).params
    pct = float(crit["stability_perturb_pct"])
    base_run = run(base_params, oos_start, oos_end, cfg.capital)
    base_pf, base_exp = _pf(base_run["trades"]), _exp(base_run["trades"])
    heat, variants = [], []
    for name in entry.get("stability_params") or []:
        v0 = defaults.get(name)
        row = {"param": name, "base_value": v0, "cells": []}
        for up in (False, True):
            v = _perturb(v0, pct, up)
            if v is None:
                continue
            rv = run({**base_params, name: v}, oos_start, oos_end, cfg.capital)
            cell = {"shift": f"{'+' if up else '-'}{pct:.0f}%", "value": v, "profit_factor": _pf(rv["trades"]),
                    "expectancy_r": _exp(rv["trades"]), "num_trades": len(rv["trades"])}
            row["cells"].append(cell)
            variants.append(cell)
        row["cells"].insert(1 if row["cells"] else 0, {"shift": "base", "value": v0, "profit_factor": base_pf,
                                                       "expectancy_r": base_exp, "num_trades": len(base_run["trades"])})
        heat.append(row)
    if variants and base_exp and base_exp > 0:
        ok_v = [(c["profit_factor"] or 0) >= crit["stability_min_pf"]
                and (c["expectancy_r"] or -1) >= crit["stability_min_expectancy_ratio"] * base_exp for c in variants]
        stab_pass = all(ok_v)
        stab_score = round(100 * float(np.mean([min(1.0, max(0.0, (c["expectancy_r"] or 0) / base_exp))
                                                 for c in variants])), 1)
    else:
        stab_pass, stab_score = False, 0.0
    stability = {"passed": stab_pass, "score": stab_score, "base_params": base_params, "heatmap": heat,
                 "base_profit_factor": base_pf, "base_expectancy_r": base_exp,
                 "note": None if variants else "No numeric parameters declared for perturbation"}

    sub = _subperiods(oos_trades, conf, oos_start, oos_end)
    regime = _regime_split(oos_trades, b.regimes)

    # survivorship sensitivity (daily event strategies): re-test on names that were ALREADY established in the first
    # year of data (listed in month 1 and in the top half by first-year traded value). Chosen with information
    # available at the time, so the result depends less on today's winners having joined the index later.
    surv = None
    if not custom and tf == "1d" and b.label != "RANDOM WALK (control)":
        first_day = dates[0]
        tv = {}
        for k, df in data.items():
            if df.index[0] <= first_day + pd.Timedelta(days=31):
                head = df.iloc[:250]
                tv[k] = float((head["close"] * head["volume"]).mean())
        est = sorted(tv, key=tv.get, reverse=True)[: max(len(tv) // 2, 1)]
        if est:
            stv = get_strategy(sid, base_params)
            prep = prepared_for(base_params)
            sres = run_portfolio(stv, {k: data[k] for k in est}, cfg.with_window(cfg.capital, oos_start, oos_end),
                                 {k: prep[k] for k in est})
            surv = {"symbols": len(est), "num_trades": len(sres["trades"]), "profit_factor": _pf(sres["trades"]),
                    "expectancy_r": _exp(sres["trades"]),
                    "net_pnl": round(sum(t.net_pnl for t in sres["trades"]), 2),
                    "method": "base parameters on names listed by month 1 and in the top half by first-year traded "
                              "value (ex-ante selection)"}

    # capacity: median 20-day traded value of the names traded (Rs crore) x 1% participation x max positions
    turn = []
    for t in oos_trades[-500:]:
        df = data.get((t.exchange, t.symbol))
        if df is not None:
            tv = (df["close"] * df["volume"]).groupby(df.index.date).sum().tail(20).mean() / 1e7
            turn.append(tv)
    capacity_cr = round(float(np.median(turn)) * 0.01 * cfg.max_positions, 2) if turn else None

    checks = []

    def chk(name: str, passed: bool, value: Any, threshold: Any, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "value": value, "threshold": threshold, "detail": detail})

    n = len(oos_trades)
    if entry["kind"] == "portfolio" and b.label != "RANDOM WALK (control)":
        ref = b.index_closes.get("NIFTY200 MOMENTUM 30")
        ref_txt = ""
        if ref is not None:
            rs = ref[(ref.index >= oos_start.normalize()) & (ref.index <= oos_end)]
            if len(rs) > 1:
                ref_txt = (f" Unbiased reference: the real NIFTY200 Momentum 30 index returned "
                           f"{(rs.iloc[-1] / rs.iloc[0] - 1) * 100:.0f}% over the same window vs {net_ret:.0f}% here.")
        chk("point_in_time_universe", False, None, None,
            "Cross-sectional ranking on TODAY's index constituents selects stocks already known to have survived and "
            "grown into the index (survivorship bias); point-in-time constituents are not available, so this result "
            "cannot be validated." + ref_txt)
    pf = m.get("profit_factor")
    pf_lo = boot["pf_ci"][0]
    exp_r = m.get("expectancy_r")
    exp_lo = boot["expectancy_r_ci"][0]
    chk("min_oos_trades", n >= crit["min_oos_trades"], n, crit["min_oos_trades"],
        f"{n} OOS trades (need >= {crit['min_oos_trades']}{'; low sample' if n < crit['preferred_oos_trades'] else ''})")
    chk("profit_factor", pf is not None and pf >= crit["min_profit_factor"] and pf_lo is not None
        and pf_lo > crit["pf_ci_lower_min"], pf, crit["min_profit_factor"],
        f"OOS PF {pf} (95% CI {boot['pf_ci'][0]}-{boot['pf_ci'][1]}); need >= {crit['min_profit_factor']} "
        f"and CI lower bound > {crit['pf_ci_lower_min']}")
    chk("expectancy", exp_r is not None and exp_r > 0 and exp_lo is not None and exp_lo > crit["expectancy_ci_lower_min"],
        exp_r, 0, f"Expectancy {exp_r}R (95% CI {boot['expectancy_r_ci'][0]} to {boot['expectancy_r_ci'][1]})")
    if bench:
        b_ret, b_dd = bench["return_pct"], bench["max_drawdown_pct"]
        beats = net_ret > b_ret or (net_ret > 0 and abs(mdd) <= (1 - crit["benchmark_dd_reduction"]) * abs(b_dd))
        chk("benchmark", beats, net_ret, b_ret, f"Net {net_ret:.1f}% vs NIFTY 50 {b_ret:.1f}% ({b.tri_label}); "
            f"max DD {mdd:.1f}% vs {b_dd:.1f}%")
    else:
        chk("benchmark", False, net_ret, None, "Benchmark data unavailable for the OOS window")
    chk("max_drawdown", abs(mdd) <= crit["max_drawdown_pct"], mdd, -crit["max_drawdown_pct"],
        f"Max drawdown {mdd:.1f}% (limit {crit['max_drawdown_pct']}%)")
    if surv is not None:
        min_pf = float(crit.get("survivorship_min_pf", 1.1))
        chk("survivorship_sensitivity", (surv["profit_factor"] or 0) >= min_pf and surv["net_pnl"] > 0,
            surv["profit_factor"], min_pf,
            f"On {surv['symbols']} names already established in the first year: PF {surv['profit_factor']} over "
            f"{surv['num_trades']} trades, net Rs {surv['net_pnl']:,.0f} (need PF >= {min_pf} and net > 0)")
    chk("parameter_stability", stab_pass, stab_score, None,
        f"+/-{pct:.0f}% on {', '.join(entry.get('stability_params') or []) or 'no parameters'}: "
        f"{'edge persisted' if stab_pass else 'edge did not persist (or base expectancy <= 0)'}")
    checks.append({"name": "multiple_testing", "passed": None, "value": None, "threshold": crit["min_deflated_sharpe"],
                   "detail": "Computed across all strategies after the tournament (deflated Sharpe)"})
    chk("double_costs", double_costs["profit_factor"] is not None
        and double_costs["profit_factor"] >= crit["double_costs_min_pf"] and double_costs["net_pnl"] > 0,
        double_costs["profit_factor"], crit["double_costs_min_pf"],
        f"With 2x costs/slippage: PF {double_costs['profit_factor']}, net Rs {double_costs['net_pnl']:,.0f}")
    chk("subperiods", sub["working"] >= crit["min_subperiods_working"], sub["working"], crit["min_subperiods_working"],
        f"Profitable (>= {crit['min_trades_per_subperiod']} trades, net > 0) in {sub['working']} of {sub['total']} "
        f"sub-periods ({sub['method']})")

    wins = [t.r_multiple for t in oos_trades if t.net_pnl > 0]
    losses = [t.r_multiple for t in oos_trades if t.net_pnl <= 0]
    mfe = np.array([t.mfe_r for t in oos_trades if t.mfe_r == t.mfe_r])
    mfe_q = ({"p50": round(float(np.percentile(mfe, 50)), 3), "p75": round(float(np.percentile(mfe, 75)), 3),
              "n": int(len(mfe))} if len(mfe) >= 20 and mfe.max() > 0 else {})
    return {**base_out, "status": "TESTED", "folds": fold_rows, "grid_size": len(combos), "mfe_r_quantiles": mfe_q,
            "oos": {**{k: m.get(k) for k in ("num_trades", "net_return_pct", "cagr_pct", "max_drawdown_pct", "sharpe",
                                               "sortino", "win_rate_pct", "profit_factor", "expectancy_r", "avg_win",
                                               "avg_loss", "turnover_x", "exposure_pct", "avg_holding_bars",
                                               "total_costs", "period_start", "period_end")},
                    "annualised_return_pct": round(ann, 3), "calmar": calmar,
                    "avg_win_r": round(float(np.mean(wins)), 3) if wins else None,
                    "avg_loss_r": round(float(np.mean(losses)), 3) if losses else None,
                    "avg_mfe_r": round(float(np.mean([t.mfe_r for t in oos_trades])), 3) if oos_trades else None,
                    "avg_mae_r": round(float(np.mean([t.mae_r for t in oos_trades])), 3) if oos_trades else None,
                    "capacity_cr": capacity_cr},
            "bootstrap": boot, "sharpe_stats": sh, "benchmark": bench, "double_costs": double_costs,
            "survivorship": surv,
            "stability": stability, "subperiods": sub, "regimes": regime, "checks": checks,
            "equity_curve": _downsample(equity / cfg.capital) if len(equity) else [],
            "drawdown_curve": _downsample((daily_equity(equity) / daily_equity(equity).cummax() - 1) * 100)
            if len(equity) else [],
            "exit_reasons": dict(Counter(t.exit_reason for t in oos_trades).most_common(8))}
