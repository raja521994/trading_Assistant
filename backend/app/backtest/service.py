"""Backtest orchestration: request handling, persisted runs, walk-forward evidence and signal replay."""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BacktestRun
from ..providers.registry import active_provider
from ..quant.strategies.base import all_strategy_ids, get_strategy
from ..quant.universe import load_benchmark, load_universe, prepared
from ..risk.costs import CostModel
from ..risk.limits import load_risk_config
from .engine import BTConfig, run_portfolio
from .metrics import METRIC_ASSUMPTIONS, compute_metrics, drawdown_series, period_returns
from .walkforward import walk_forward

log = logging.getLogger(__name__)
IST = "Asia/Kolkata"
ENGINE_ASSUMPTIONS = [
    "Signals evaluated on bar close; fills at next bar open with adverse slippage.",
    "Stop fills at stop (or at the open on a gap through it); stop assumed before target within the same bar.",
    "Shared capital pool, fixed-fractional risk sizing on current equity, no leverage, max open positions cap.",
    "Round-trip transaction costs from config/charges.yaml (Zerodha-style estimates, see as_of) deducted per trade.",
    "Slippage per side from a liquidity model: tier by 20-day traded value (3-40 bps) unless a fixed bps is chosen.",
    "Setups whose stop exceeds the per-mode cap (intraday 1.5%, swing 6%, positional 10% by default) are skipped.",
    "Universe = instruments currently available from the active provider (survivorship bias: delisted symbols "
    "are not included).",
    "Intraday strategies are always flat by the session's last bar.",
]


class BacktestRequest(BaseModel):
    strategy: str
    symbols: list[str] | None = None
    start: date | None = None
    end: date | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    capital: float = Field(1_000_000.0, gt=0)
    risk_pct: float = Field(1.0, gt=0, le=5)
    max_alloc_pct: float = Field(15.0, gt=0, le=100)
    max_positions: int = Field(10, ge=1, le=100)
    slippage_bps: float = Field(5.0, ge=0, le=100)
    walk_forward: bool = False
    folds: int = Field(3, ge=2, le=6)
    slippage_model: str = Field("liquidity", pattern="^(liquidity|fixed)$")


def _select(data: dict[tuple[str, str], pd.DataFrame], symbols: list[str] | None) -> dict[tuple[str, str], pd.DataFrame]:
    if not symbols:
        return data
    wanted = {s.upper() for s in symbols}
    return {k: v for k, v in data.items() if k[1].upper() in wanted or f"{k[0]}:{k[1]}".upper() in wanted}


def _curve(s: pd.Series, max_points: int = 1500) -> list[dict[str, Any]]:
    if s.empty:
        return []
    d = s.groupby(s.index.date).last() if isinstance(s.index, pd.DatetimeIndex) else s
    if len(d) > max_points:
        d = d.iloc[:: max(1, len(d) // max_points)]
    return [{"time": str(k), "value": round(float(v), 2)} for k, v in d.items()]


def slippage_fn_for(data: dict[tuple[str, str], pd.DataFrame]):  # noqa: ANN201
    """Per-symbol liquidity-based slippage (bps per side) from the median 20-day traded value in the data."""
    from ..risk.costs import liquidity_slippage_bps

    turnover = {}
    for (ex, sym), df in data.items():
        tv = (df["close"] * df["volume"]).tail(250)
        turnover[(ex, sym)] = float(tv.median()) / 1e7 if len(tv) and tv.median() > 0 else None
    return lambda sym, ex: liquidity_slippage_bps(turnover.get((ex, sym)))


def data_label() -> str:
    return active_provider().capabilities().data_status.value


def run_backtest(session: Session, req: BacktestRequest, persist: bool = True) -> dict[str, Any]:
    strat = get_strategy(req.strategy, req.params)
    tf = strat.meta.timeframe
    data = _select(load_universe(session, tf), req.symbols)
    if not data:
        raise ValueError("No data for the selected universe")
    bench_df = load_benchmark(session)
    bench = bench_df["close"] if bench_df is not None else None
    costs = load_risk_config(session).costs
    tz = next(iter(data.values())).index.tz
    start = pd.Timestamp(req.start).tz_localize(tz) if req.start else None
    end = pd.Timestamp(req.end).tz_localize(tz) + pd.Timedelta(hours=23, minutes=59) if req.end else None
    risk_cfg = load_risk_config(session)
    cfg = BTConfig(req.capital, req.risk_pct, req.max_alloc_pct, req.max_positions, req.slippage_bps, costs, start, end,
                   risk_cfg.max_stop_pct(strat.meta.modes[0]),
                   slippage_fn_for(data) if req.slippage_model == "liquidity" else None)
    t0 = time.perf_counter()
    res = run_portfolio(strat, data, cfg, {k: prepared(strat, k, df) for k, df in data.items()})
    m = compute_metrics(res["equity"], res["trades"], req.capital, res["exposure"], bench)
    eq = res["equity"]
    bench_curve: list[dict[str, Any]] = []
    if bench is not None and len(eq):
        b = bench[(bench.index >= eq.index[0].normalize()) & (bench.index <= eq.index[-1])]
        if len(b):
            bench_curve = _curve(b / b.iloc[0] * req.capital)
    out: dict[str, Any] = {
        "strategy": strat.meta.id, "strategy_name": strat.meta.name, "version": strat.meta.version,
        "timeframe": tf, "params": strat.params, "universe": [f"{k[0]}:{k[1]}" for k in data],
        "config": {**req.model_dump(mode="json"), "costs": costs.model_dump()},
        "metrics": m, "equity": _curve(eq), "drawdown": _curve(drawdown_series(eq)) if len(eq) else [],
        "benchmark": bench_curve, "periods": period_returns(eq, req.capital),
        "trades": [t.to_dict() for t in res["trades"]][-2000:], "skipped_entries": res["skipped_entries"],
        "assumptions": ENGINE_ASSUMPTIONS + METRIC_ASSUMPTIONS, "data_label": data_label(),
        "warnings": _warnings(m),
    }
    if req.walk_forward:
        wf = walk_forward(strat.meta.id, data, cfg.with_window(req.capital, start, end), req.folds, bench)
        out["walk_forward"] = {"folds": wf.get("folds", []), "oos_metrics": wf.get("oos_metrics"),
                               "oos_equity": _curve(wf.get("oos_equity", pd.Series(dtype=float))),
                               "method": wf.get("method"), "grid": wf.get("grid"), "error": wf.get("error")}
    out["seconds"] = round(time.perf_counter() - t0, 2)
    if persist:
        run = BacktestRun(created_at=int(time.time()), kind="user", strategy=strat.meta.id,
                          params=out["config"], summary=m, result=out)
        session.add(run)
        session.flush()
        out["id"] = run.id
    return out


def _warnings(m: dict[str, Any]) -> list[str]:
    w = ["A single backtest is not evidence of future profitability; check walk-forward OOS results and paper-trade first."]
    if m.get("low_sample_warning"):
        w.append(f"Low sample size ({m.get('num_trades')} trades) - statistics are unreliable.")
    if data_label() == "SIMULATED":
        w.append("Results are on SIMULATED data and say nothing about real market performance.")
    return w


def compute_evidence(session: Session, strategy_ids: list[str] | None = None) -> dict[str, Any]:
    bench_df = load_benchmark(session)
    bench = bench_df["close"] if bench_df is not None else None
    costs = load_risk_config(session).costs
    out = {}
    for sid in strategy_ids or all_strategy_ids():
        t0 = time.perf_counter()
        strat = get_strategy(sid)
        data = load_universe(session, strat.meta.timeframe)
        if not data:
            continue
        cfg = BTConfig(costs=costs, max_stop_pct=load_risk_config(session).max_stop_pct(strat.meta.modes[0]),
                       slippage_fn=slippage_fn_for(data))
        wf = walk_forward(sid, data, cfg, folds=3, benchmark=bench if not strat.intraday else None)
        m = wf.get("oos_metrics") or {}
        last_bar = max(df.index[-1] for df in data.values())
        regime_split = _regime_split(wf.get("oos_trades", []), bench) if bench is not None else {}
        summary = {
            "status": "ready" if "error" not in wf else "error", "strategy": sid, "version": strat.meta.version,
            "num_trades": m.get("num_trades", 0), "profit_factor": m.get("profit_factor"),
            "win_rate_pct": m.get("win_rate_pct"), "expectancy_r": m.get("expectancy_r"),
            "net_return_pct": m.get("net_return_pct"), "max_drawdown_pct": m.get("max_drawdown_pct"),
            "sharpe": m.get("sharpe"), "benchmark_return_pct": m.get("benchmark_return_pct"),
            "period_start": m.get("period_start"), "period_end": m.get("period_end"),
            "low_sample": (m.get("num_trades") or 0) < 30, "data_label": data_label(), "provider": active_provider().name,
            "data_last_bar": last_bar.isoformat(), "folds": [
                {"fold": f["fold"], "params": f["selected_params"], "oos": f["oos_metrics"], "is": f["is_metrics"]}
                for f in wf.get("folds", [])],
            "error": wf.get("error"), "computed_at": int(time.time()), "method": wf.get("method"),
            "regime_split": regime_split,
            "mfe_r_quantiles": _mfe_quantiles(wf.get("oos_trades", [])),
        }
        result = {"oos_equity": _curve(wf.get("oos_equity", pd.Series(dtype=float))),
                  "oos_trades": [t.to_dict() for t in wf.get("oos_trades", [])][-1000:]}
        session.add(BacktestRun(created_at=int(time.time()), kind="evidence", strategy=sid,
                                params={"grid": wf.get("grid")}, summary=summary, result=result))
        session.flush()
        out[sid] = summary
        log.info("walk-forward evidence %s: %s trades OOS in %.1fs", sid, summary["num_trades"],
                 time.perf_counter() - t0)
    return out


def _mfe_quantiles(trades: list) -> dict[str, Any]:
    """Distribution of maximum favourable excursion (in R) of OOS trades -> realistic target levels."""
    import numpy as np

    v = [t.mfe_r for t in trades if t.mfe_r is not None]
    if not v:
        return {"n": 0}
    return {"n": len(v), "p50": round(float(np.quantile(v, 0.5)), 2), "p75": round(float(np.quantile(v, 0.75)), 2),
            "p90": round(float(np.quantile(v, 0.9)), 2)}


def _regime_split(trades: list, bench: pd.Series) -> dict[str, Any]:
    """OOS trade statistics split by market regime at entry: NIFTY 50 above ('up') / below ('down') its 200-DMA."""
    from ..quant import indicators as ind

    sma = ind.sma(bench, 200)
    up = (bench > sma).where(sma.notna())
    out: dict[str, Any] = {}
    buckets: dict[str, list] = {"up": [], "down": []}
    for t in trades:
        d = pd.Timestamp(t.entry_ts).normalize()
        flag = up.asof(d) if len(up) and d >= up.index[0] else None
        if flag is None or pd.isna(flag):
            continue
        buckets["up" if flag else "down"].append(t)
    for k, ts in buckets.items():
        pnl = [t.net_pnl for t in ts]
        gains = sum(p for p in pnl if p > 0)
        losses = -sum(p for p in pnl if p <= 0)
        out[k] = {"num_trades": len(ts), "profit_factor": round(gains / losses, 3) if losses > 0 else None,
                  "expectancy_r": round(sum(t.r_multiple for t in ts) / len(ts), 3) if ts else None,
                  "net_pnl": round(sum(pnl), 2)}
    return out


def latest_evidence(session: Session) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    prov = active_provider().name
    rows = session.scalars(select(BacktestRun).where(BacktestRun.kind == "evidence")
                           .order_by(BacktestRun.created_at.desc(), BacktestRun.id.desc())).all()
    for r in rows:
        if r.summary.get("provider", "sample") == prov:
            out.setdefault(r.strategy, r.summary)
    return out


def evidence_is_current(session: Session) -> bool:
    ev = latest_evidence(session)
    if set(ev) != set(all_strategy_ids()):
        return False
    for sid, s in ev.items():
        if "regime_split" not in s or "mfe_r_quantiles" not in s:
            return False  # computed by an older version
        data = load_universe(session, get_strategy(sid).meta.timeframe)
        if not data:
            continue
        last = max(df.index[-1] for df in data.values())
        if s.get("data_last_bar", "")[:10] < last.isoformat()[:10]:
            return False
    return True


def replay_recent_signals(session: Session, sessions: int = 60) -> int:
    """Record what each daily strategy would have signalled over the recent past, with outcomes
    (origin='historical_replay'). Clearly separated from live-scan signals in the UI."""
    from ..signals.state_machine import record_replay_signal

    costs = load_risk_config(session).costs
    created = 0
    for sid in all_strategy_ids():
        strat = get_strategy(sid)
        if strat.intraday:
            continue
        data = load_universe(session, "1d")
        if not data:
            continue
        last = max(df.index[-1] for df in data.values())
        dates = sorted({d for df in data.values() for d in df.index if d <= last})
        start = dates[-sessions] if len(dates) > sessions else dates[0]
        res = run_portfolio(strat, data, BTConfig(costs=costs, start=start))
        for t in res["trades"]:
            created += record_replay_signal(session, strat, t, active_provider().name)
    session.flush()
    return created
