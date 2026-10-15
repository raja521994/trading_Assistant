"""Market-wide scanner: evaluates every strategy on every instrument with validated data and builds
complete, explainable recommendations (spec section 7). Vectorised indicators; no per-symbol LLM calls."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from ..backtest.service import latest_evidence
from ..data.validation import validate_candles
from ..providers.registry import active_provider
from ..providers.sample import market_session_state, now_ist
from ..risk.costs import product_for_mode, round_trip_costs
from ..risk.limits import RiskConfig, load_risk_config
from ..risk.sizing import position_size
from .regime import classify_regime
from .scoring import composite, load_weights, score_components
from .strategies.base import Setup, Strategy, all_strategy_ids, get_strategy
from .universe import features, instrument_map, last_row, load_benchmark, load_universe, prepared_with_arrays

log = logging.getLogger(__name__)
IST = "Asia/Kolkata"
LOOKBACK_BARS = 3
CONFLICT_COMPONENTS = ("trend", "momentum", "relative_strength")


def _add_weekdays(d: date, k: int) -> date:
    while k > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            k -= 1
    return d


def _epoch(d: date, hh: int, mm: int) -> int:
    return int(pd.Timestamp(datetime(d.year, d.month, d.day, hh, mm)).tz_localize(IST).timestamp())


def evaluate_latest(strat: Strategy, a: dict[str, list]) -> dict[str, Any] | None:
    n = len(a["close"])
    last = n - 1
    if last < strat.warmup + 1:
        return None
    for j in range(last, max(last - LOOKBACK_BARS, strat.warmup), -1):
        setup = strat.entry(a, j)
        if setup is None:
            continue
        if strat.intraday and a["_date"][j] != a["_date"][last]:
            break
        if j == last:
            return {"state": "SETUP", "setup": setup, "signal_i": j, "entry_ref": a["close"][last], "entry_i": None}
        long = setup.direction == "LONG"
        fill = a["open"][j + 1]
        if (long and fill <= setup.stop) or (not long and fill >= setup.stop):
            return None
        risk = abs(fill - setup.stop)
        target = None if not setup.target_r else (fill + setup.target_r * risk if long else fill - setup.target_r * risk)
        for k in range(j + 1, n):
            if (long and a["low"][k] <= setup.stop) or (not long and a["high"][k] >= setup.stop):
                return None
            if target is not None and ((long and a["high"][k] >= target) or (not long and a["low"][k] <= target)):
                return None
            if k < last and strat.exit(a, k, setup.direction):
                return None
        return {"state": "TRIGGERED", "setup": setup, "signal_i": j, "entry_ref": fill, "entry_i": j + 1}
    w = strat.watch(a, last)
    if w:
        return {"state": "WATCH", "setup": None, "signal_i": last, "entry_ref": a["close"][last], "entry_i": None,
                "watch_reason": w}
    return None


def _liquidity(daily: dict[str, Any], cfg: RiskConfig) -> dict[str, Any]:
    t20 = daily.get("turnover20")
    if t20 is None:
        return {"rating": "UNKNOWN", "avg_turnover_cr": None, "note": "No volume data"}
    cr = t20 / 1e7
    rating = "HIGH" if cr >= 100 else "MEDIUM" if cr >= 20 else "LOW"
    return {"rating": rating, "avg_turnover_cr": round(cr, 2), "min_required_cr": cfg.min_avg_turnover_cr,
            "spread": "Unavailable (no market-depth data from the active provider)"}


def _holding(strat: Strategy, setup: Setup | None) -> str:
    mh = setup.max_hold_bars if setup else strat.params.get("max_hold_bars")
    mode = strat.meta.modes[0]
    if mode == "intraday":
        return f"Same session only; flat by {strat.params.get('exit_time', '15:15')} IST"
    if mode == "positional":
        return f"Several weeks (trailing exit); maximum {mh} sessions"
    return f"Typically 3-15 sessions; maximum {mh} sessions"


def build_recommendation(strat: Strategy, key: tuple[str, str], inst: Any, cand: dict[str, Any], p: pd.DataFrame,
                         a: dict[str, list], daily_snap: dict[str, Any], bench_ret_63: float | None,
                         evidence: dict[str, Any] | None, cfg: RiskConfig, weights: dict[str, float],
                         regime: dict[str, Any] | None, dq: dict[str, Any], now: datetime, data_label: str
                         ) -> dict[str, Any]:
    exch, sym = key
    last = len(a["close"]) - 1
    setup: Setup | None = cand["setup"]
    state = cand["state"]
    close = a["close"][last]
    atr = a["atr14"][last]
    entry_ref = float(cand["entry_ref"])
    if setup:
        direction_code = setup.direction
    else:
        direction_code = "LONG" if strat.meta.direction == "long_only" else "NEUTRAL"
    long = direction_code != "SHORT"
    snap = strat.snapshot(a, last)
    comps = score_components("SHORT" if direction_code == "SHORT" else "LONG", snap, daily_snap, bench_ret_63,
                             evidence, cfg.min_avg_turnover_cr, strat.intraday, strat.meta.contrarian_components)
    comp = composite(comps, weights)

    stop = targets = rr = None
    sizing = costs = None
    risk_level = "N/A"
    if setup:
        stop = round(setup.stop, 2)
        risk_u = abs(entry_ref - setup.stop)
        targets = []
        if setup.target_r:
            targets = [round(entry_ref + setup.target_r * risk_u if long else entry_ref - setup.target_r * risk_u, 2)]
            rr = round(float(setup.target_r), 2)
        sz = position_size(cfg.account_equity, entry_ref, setup.stop, cfg.risk_per_trade_pct,
                           cfg.max_alloc_per_instrument_pct, lot_size=inst.lot_size if inst else 1,
                           max_leverage=cfg.max_leverage)
        sizing = sz.to_dict()
        ref_exit = targets[0] if targets else (entry_ref + 2 * risk_u if long else entry_ref - 2 * risk_u)
        costs = round_trip_costs(direction_code, entry_ref, ref_exit, max(sz.qty, 1),
                                 product_for_mode(strat.meta.modes[0]), exch, cfg.costs)
        costs["basis"] = (f"Round trip for {max(sz.qty, 1)} shares, entry {entry_ref:.2f} -> "
                          f"{'target' if targets else 'reference 2R exit'} {ref_exit:.2f}; {cfg.costs.note}")
        stop_pct = risk_u / entry_ref * 100
        risk_level = "LOW" if stop_pct < 2 else "MEDIUM" if stop_pct < 5 else "HIGH"

    liq = _liquidity(daily_snap, cfg)
    supporting = list(setup.reasons) if setup else ([cand["watch_reason"]] if cand.get("watch_reason") else [])
    conflicting: list[str] = []
    for name, c in comps.items():
        v = c["value"]
        if v is None:
            continue
        if v >= 0.5:
            supporting.append(f"{name.replace('_', ' ').title()}: {c['detail']}")
        elif v <= -0.5:
            conflicting.append(f"{name.replace('_', ' ').title()}: {c['detail']}")
    hard_conflicts = [k for k in CONFLICT_COMPONENTS if comps[k]["value"] is not None and comps[k]["value"] <= -0.5]
    cur_regime = (regime or {}).get("current") or {}
    if cur_regime.get("trend") == "DOWNTREND" and long and setup:
        conflicting.append("Market regime: benchmark in a confirmed downtrend")
    if cur_regime.get("trend") == "UPTREND" and direction_code == "SHORT":
        conflicting.append("Market regime: benchmark in a confirmed uptrend")
    if cur_regime.get("volatility") == "HIGH":
        conflicting.append("Market regime: high volatility - gap and slippage risk elevated")
    conflicting += [f"Data quality: {i}" for i in dq.get("issues", [])]

    action_reasons: list[str] = []
    if state == "WATCH":
        action = "WAIT"
        action_reasons.append("Watch state: entry conditions are not complete")
    else:
        action = "BUY" if long else "SELL"
        if sizing and sizing["qty"] < 1:
            action = "NO TRADE"
            action_reasons.append(f"Position size rounds to 0 shares under current risk limits ({sizing['limiting_factor']})")
        if liq["avg_turnover_cr"] is not None and liq["avg_turnover_cr"] < cfg.min_avg_turnover_cr:
            action = "NO TRADE"
            action_reasons.append(f"Liquidity below minimum (Rs {liq['avg_turnover_cr']} cr < Rs {cfg.min_avg_turnover_cr} cr)")
        if action != "NO TRADE":
            if len(hard_conflicts) >= 2:
                action = "WAIT"
                action_reasons.append(f"Conflicting evidence in {', '.join(hard_conflicts)}")
            if comp["score"] < cfg.min_score:
                action = "WAIT"
                action_reasons.append(f"Composite score {comp['score']} below minimum {cfg.min_score}")
            if rr is not None and rr < cfg.min_risk_reward:
                action = "WAIT"
                action_reasons.append(f"Risk-reward {rr} below minimum {cfg.min_risk_reward}")
            if state == "TRIGGERED" and setup:
                risk_u = abs(entry_ref - setup.stop)
                moved = (close - entry_ref) / risk_u if long else (entry_ref - close) / risk_u
                if moved > 1.0:
                    action = "WAIT"
                    action_reasons.append(f"Price already {moved:.1f}R beyond the entry reference - late entry not advised")
        if not action_reasons:
            action_reasons.append("All entry rules met, risk checks on sizing/liquidity passed, no major conflicts")

    sig_ts = p.index[cand["signal_i"]]
    last_ts = p.index[last]
    if strat.intraday:
        h, m = (int(x) for x in str(strat.params.get("exit_time", "15:15")).split(":"))
        expires = _epoch(last_ts.date(), h, m)
    elif state == "TRIGGERED":
        entry_date = p.index[cand["entry_i"]].date()
        expires = _epoch(_add_weekdays(entry_date, int(setup.max_hold_bars) if setup else 10), 15, 30)
    else:
        expires = _epoch(_add_weekdays(last_ts.date(), 1), 15, 30)

    entry_note = ("Signal on the latest completed bar; entry reference is the NEXT bar's open"
                  if state == "SETUP" else
                  f"Entry reference filled at the open of {p.index[cand['entry_i']].isoformat()}"
                  if state == "TRIGGERED" else "No entry until the setup completes")
    zone = None
    if setup and atr == atr:
        zone = ([round(entry_ref - 0.1 * atr, 2), round(entry_ref + 0.25 * atr, 2)] if long
                else [round(entry_ref - 0.25 * atr, 2), round(entry_ref + 0.1 * atr, 2)])

    return {
        "symbol": sym, "exchange": exch, "name": getattr(inst, "name", sym), "sector": getattr(inst, "sector", None),
        "instrument_type": getattr(inst, "instrument_type", "EQ"), "mode": strat.meta.modes[0],
        "modes": strat.meta.modes, "timeframe": strat.meta.timeframe,
        "strategy": strat.meta.id, "strategy_name": strat.meta.name, "strategy_version": strat.meta.version,
        "generated_at": now.isoformat(), "data_timestamp": last_ts.isoformat(),
        "data_ts_epoch": int(last_ts.timestamp()), "signal_bar_ts": sig_ts.isoformat(),
        "freshness": {"status": dq.get("freshness"), "data_label": data_label, "quality": dq.get("status"),
                      "market_session": market_session_state(now)},
        "reference_price": round(close, 2),
        "direction": "bullish" if direction_code == "LONG" else "bearish" if direction_code == "SHORT" else "neutral",
        "direction_code": direction_code, "state": state, "action": action, "action_reasons": action_reasons,
        "entry": {"reference": round(entry_ref, 2), "zone": zone, "note": entry_note},
        "stop_loss": stop, "invalidation": setup.invalidation if setup else "n/a (watch only)",
        "targets": targets or [], "target_note": None if targets else (
            "Trailing / rule-based exit - no fixed target" if setup else None),
        "risk_reward": rr, "holding_period": _holding(strat, setup), "liquidity": liq,
        "position_size": sizing, "indicators": snap,
        "supporting_evidence": supporting, "conflicting_evidence": conflicting,
        "event_risks": ["Earnings / corporate-action calendar not connected - event risk UNKNOWN",
                        "News & announcements feed not connected"],
        "oos_performance": evidence or {"status": "pending"},
        "estimated_costs": costs, "score": comp["score"],
        "score_breakdown": {"components": comps, "weights": weights, **comp},
        "risk_level": risk_level,
        "regime": cur_regime.get("label"),
        "research_only": True,
        "research_only_reason": ("SIMULATED data - research and paper trading only" if data_label == "SIMULATED"
                                 else "Live order execution is disabled in this build - research and paper trading only"),
        "executable": False,
        "expires_at": pd.Timestamp(expires, unit="s", tz="UTC").tz_convert(IST).isoformat(),
        "expires_at_epoch": expires, "data_quality": dq,
    }


def _blocked(key: tuple[str, str], inst: Any, tf: str, dq: dict[str, Any], strategies: list[str]) -> dict[str, Any]:
    return {"symbol": key[1], "exchange": key[0], "name": getattr(inst, "name", key[1]), "timeframe": tf,
            "action": "NO TRADE", "strategies": strategies, "reason": f"Data quality {dq['status']}",
            "issues": dq["issues"], "data_quality": dq}


def intraday_session_active(now: datetime, last_bar_ts: pd.Timestamp, exit_time: str = "15:15") -> bool:
    h, m = (int(x) for x in exit_time.split(":"))
    return last_bar_ts.date() == now.date() and now.weekday() < 5 and now.time() < datetime(2000, 1, 1, h, m).time()


def scan(session: Session, now: datetime | None = None) -> dict[str, Any]:
    t0 = time.perf_counter()
    provider = active_provider()
    now = now or now_ist()
    label = provider.capabilities().data_status.value
    cfg = load_risk_config(session)
    weights = load_weights(session)
    evidence = latest_evidence(session)
    imap = instrument_map(session)
    daily = load_universe(session, "1d")
    intraday = load_universe(session, "5m")
    bench = load_benchmark(session)
    bench_ret_63 = None
    if bench is not None and len(bench) > 64:
        bench_ret_63 = float(bench["close"].iloc[-1] / bench["close"].iloc[-64] - 1.0)
    regime = classify_regime(bench, daily) if bench is not None else None
    exp = {"1d": provider.expected_last_bar("1d", now), "5m": provider.expected_last_bar("5m", now)}

    dq_cache: dict[tuple[str, tuple[str, str]], dict[str, Any]] = {}
    daily_snaps = {k: last_row(features(k, df)) for k, df in daily.items()}
    recs: list[dict[str, Any]] = []
    blocked: dict[tuple[str, tuple[str, str]], dict[str, Any]] = {}
    notes: list[str] = []
    for sid in all_strategy_ids():
        strat = get_strategy(sid)
        tf = strat.meta.timeframe
        data = intraday if strat.intraday else daily
        if strat.intraday and data:
            any_df = next(iter(data.values()))
            if not intraday_session_active(now, any_df.index[-1], str(strat.params.get("exit_time", "15:15"))):
                notes.append(f"{strat.meta.name}: intraday scanner inactive outside the trading session "
                             f"(market {market_session_state(now)}); no intraday setups are generated.")
                continue
        for key, df in data.items():
            ck = (tf, key)
            if ck not in dq_cache:
                dq_cache[ck] = validate_candles(df, tf, exp[tf], now, min_bars=60).to_dict()
            dq = dq_cache[ck]
            if not dq["ok"]:
                b = blocked.setdefault(ck, _blocked(key, imap.get(key), tf, dq, []))
                b["strategies"].append(sid)
                continue
            if len(df) < strat.warmup + 5:
                continue
            p, a = prepared_with_arrays(strat, key, df)
            cand = evaluate_latest(strat, a)
            if cand is None:
                continue
            recs.append(build_recommendation(strat, key, imap.get(key), cand, p, a, daily_snaps.get(key, {}),
                                             bench_ret_63, evidence.get(sid), cfg, weights, regime, dq, now, label))
    order = {"BUY": 0, "SELL": 0, "WAIT": 1, "NO TRADE": 2}
    recs.sort(key=lambda r: (order.get(r["action"], 3), {"TRIGGERED": 0, "SETUP": 1, "WATCH": 2}[r["state"]],
                             -r["score"]))
    return {"generated_at": now.isoformat(), "data_label": label, "provider": provider.name,
            "recommendations": recs, "blocked": list(blocked.values()), "notes": notes, "regime": regime,
            "expected_last_bar": {k: (v.isoformat() if v else None) for k, v in exp.items()},
            "seconds": round(time.perf_counter() - t0, 2)}

