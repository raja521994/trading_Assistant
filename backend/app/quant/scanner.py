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
from ..providers.sample import now_ist
from ..risk.costs import product_for_mode, round_trip_costs
from ..risk.limits import RiskConfig, load_risk_config
from ..risk.sizing import position_size
from .regime import classify_regime
from .scoring import composite, load_weights, score_components
from .strategies.base import Setup, Strategy, all_strategy_ids, get_strategy
from ..realtime.service import service as realtime
from .universe import LIVE_TAIL, features, instrument_map, last_row, load_benchmark, load_universe, prepared_with_arrays
from ..signals.setup_fsm import (ACTIVE, EXPIRED, INVALIDATED, SETUP, TRIGGERED, Plan, on_bar_close, on_next_open,
                                 trigger_level)

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
    """Most recent signal within the setup window, advanced through the shared state machine over every closed bar
    after it (same rules as the backtester). Returns SETUP / TRIGGERED / ACTIVE, WATCH, or None if resolved."""
    n = len(a["close"])
    last = n - 1
    if last < strat.warmup + 1:
        return None
    window = strat.expiry_bars + LOOKBACK_BARS
    for j in range(last, max(last - window, strat.warmup), -1):
        setup = strat.entry(a, j)
        if setup is None:
            continue
        if strat.intraday and a["_date"][j] != a["_date"][last]:
            break
        long = setup.direction == "LONG"
        plan = Plan(setup.direction, trigger_level(strat.trigger_rule, setup.direction, a["high"][j], a["low"][j],
                                                   a["close"][j]), setup.stop, strat.expiry_bars)
        entry_i = trig_i = None
        target = None
        for k in range(j + 1, n):
            if plan.state == SETUP:
                if on_bar_close(plan, a["high"][k], a["low"][k], a["close"][k]) == TRIGGERED:
                    trig_i = k
            elif plan.state == TRIGGERED:
                if on_next_open(plan, a["open"][k]) == ACTIVE:
                    entry_i = k
                    risk = abs(plan.fill - setup.stop)
                    target = None if not setup.target_r else (
                        plan.fill + setup.target_r * risk if long else plan.fill - setup.target_r * risk)
            if plan.state == ACTIVE:
                if (long and a["low"][k] <= setup.stop) or (not long and a["high"][k] >= setup.stop):
                    return None  # stopped out -> resolved
                if target is not None and ((long and a["high"][k] >= target) or (not long and a["low"][k] <= target)):
                    return None  # target reached -> resolved
                if k < last and strat.exit(a, k, setup.direction):
                    return None
                if strat.intraday and k < last and a["_date"][k + 1] != a["_date"][k]:
                    return None
            if plan.state in (INVALIDATED, EXPIRED):
                return None
        return {"state": plan.state, "setup": setup, "signal_i": j, "plan": plan, "trigger_i": trig_i,
                "entry_i": entry_i, "entry_ref": plan.fill if plan.fill is not None else plan.trigger}
    w = strat.watch(a, last)
    if w:
        return {"state": "WATCH", "setup": None, "signal_i": last, "entry_ref": a["close"][last], "entry_i": None,
                "watch_reason": w, "plan": None, "trigger_i": None}
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
                         regime: dict[str, Any] | None, dq: dict[str, Any], now: datetime, data_label: str,
                         freshness: dict[str, Any] | None = None) -> dict[str, Any]:
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
    stop_info: dict[str, Any] | None = None
    target_info: dict[str, Any] | None = None
    if setup:
        stop = round(setup.stop, 2)
        risk_u = abs(entry_ref - setup.stop)
        stop_pct = risk_u / entry_ref * 100 if entry_ref else 0.0
        cap = cfg.max_stop_pct(strat.meta.modes[0])
        stop_info = {"stop_pct": round(stop_pct, 2), "cap_pct": cap, "atr14": None if atr != atr else round(atr, 2),
                     "atr_multiple": None if not atr or atr != atr else round(risk_u / atr, 2),
                     "within_cap": stop_pct <= cap,
                     "rule": f"{strat.meta.name} stop rule (ATR / structure); max {cap}% for {strat.meta.modes[0]} mode"}
        # Targets from the strategy's historical (walk-forward OOS) favourable-excursion distribution, not a fixed R.
        mfe = (evidence or {}).get("mfe_r_quantiles") or {}
        targets = []
        if mfe.get("p50") and mfe.get("n", 0) >= 20:
            for q in ("p50", "p75"):
                if mfe.get(q):
                    targets.append(round(entry_ref + mfe[q] * risk_u if long else entry_ref - mfe[q] * risk_u, 2))
            # R:R = reward to target 2 (75th percentile of favourable excursion). Trend strategies win through a minority
            # of large moves, so the median excursion (target 1) understates the reward; the edge itself is required
            # separately (positive out-of-sample expectancy via the evidence tier / edge gate).
            rr = round(float(mfe.get("p75") or mfe["p50"]), 2)
            target_info = {"method": "MFE quantiles of walk-forward OOS trades (target 1 = median, target 2 = 75th "
                                     "percentile; R:R measured to target 2)", "p50_r": mfe["p50"],
                           "p75_r": mfe.get("p75"), "n_trades": mfe["n"]}
        elif setup.target_r:
            targets = [round(entry_ref + setup.target_r * risk_u if long else entry_ref - setup.target_r * risk_u, 2)]
            rr = round(float(setup.target_r), 2)
            target_info = {"method": f"strategy rule {setup.target_r}R (MFE history not available yet)"}
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
            need = max(entry_ref / (cfg.max_alloc_per_instrument_pct / 100.0),
                       abs(entry_ref - setup.stop) / (cfg.risk_per_trade_pct / 100.0)) if setup else 0
            action_reasons.append(
                f"Position size rounds to 0 shares: account equity Rs {cfg.account_equity:,.0f} is too small for one "
                f"share at Rs {entry_ref:,.2f} ({sizing['limiting_factor']}); needs at least Rs {need:,.0f} - change "
                "'account equity' in Risk & Performance")
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
            if state == "ACTIVE" and setup:
                risk_u = abs(entry_ref - setup.stop)
                moved = (close - entry_ref) / risk_u if long else (entry_ref - close) / risk_u
                if moved > 1.0:
                    action = "WAIT"
                    action_reasons.append(f"Entry filled at {entry_ref:.2f}; price already {moved:.1f}R beyond it - "
                                          "late entry not advised")
            if stop_info and not stop_info["within_cap"]:
                action = "NO TRADE"
                action_reasons.append(f"SKIP: stop {stop_info['stop_pct']}% away exceeds the {stop_info['cap_pct']}% cap "
                                      f"for {strat.meta.modes[0]} mode")
        if not action_reasons:
            action_reasons.append("All entry rules met, risk checks on sizing/liquidity passed, no major conflicts")

    gates = evaluate_gates(strat, direction_code, evidence, regime, cfg)
    fr = freshness or {}
    gates["freshness"] = {"name": "Data freshness", "applies": True, "passed": not fr.get("stale", False),
                          "detail": (f"Live data stale: last tick {fr.get('tick_age_s')} s ago (> 5 s in market hours)"
                                     if fr.get("stale") else f"Last tick {fr.get('tick_age_s')} s ago"
                                     if fr.get("tick_age_s") is not None else "No live tick yet (outside session or REST data)")}
    ml = None
    if setup and not strat.intraday and state in ("SETUP", "TRIGGERED", "ACTIVE"):
        try:
            from ..ml.meta import score_setup

            ml = score_setup(strat.meta.id, p, p.index[cand["signal_i"]], entry_ref, setup.stop)
        except Exception:  # noqa: BLE001 - the model is optional; never break the scanner
            ml = None
    if ml is not None and ml["model_useful"] and cfg.evidence_policy != "aggressive":
        gates["ml"] = {"name": "ML win probability", "applies": True, "passed": bool(ml["above_threshold"]),
                       "detail": (f"Meta-label model P(win) {ml['p_win']:.0%} vs threshold {ml['threshold']:.0%} "
                                  f"(walk-forward OOS AUC {ml['oos_auc']})")}
    if action in ("BUY", "SELL"):
        failed = [g for g in gates.values() if g["applies"] and not g["passed"]]
        if failed:
            action = "WAIT"
            action_reasons = [g["detail"] for g in failed] + [r for r in action_reasons if not r.startswith("All entry rules")]

    sig_ts = p.index[cand["signal_i"]]
    last_ts = p.index[last]
    if strat.intraday:
        h, m = (int(x) for x in str(strat.params.get("exit_time", "15:15")).split(":"))
        expires = _epoch(last_ts.date(), h, m)
    elif state == "ACTIVE":
        entry_date = p.index[cand["entry_i"]].date()
        expires = _epoch(_add_weekdays(entry_date, int(setup.max_hold_bars) if setup else 10), 15, 30)
    else:
        expires = _epoch(_add_weekdays(last_ts.date(), strat.expiry_bars), 15, 30)

    plan = cand.get("plan")
    trig = plan.trigger if plan is not None else None
    if state == "SETUP":
        entry_note = (f"Waiting for a closed bar {'at/above' if long else 'at/below'} trigger {trig:.2f}; entry at the "
                      "following bar's open")
    elif state == "TRIGGERED":
        entry_note = (f"Trigger {trig:.2f} crossed on the close of {p.index[cand['trigger_i']].isoformat()}; entry at the "
                      "next bar's open")
    elif state == "ACTIVE":
        entry_note = (f"Filled at {plan.fill:.2f} (open of {p.index[cand['entry_i']].isoformat()}) after trigger "
                      f"{trig:.2f}; now {close:.2f}")
    else:
        entry_note = "No entry until the setup completes"
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
                      "market_session": active_provider().session_state(now)},
        "reference_price": round(close, 2),
        "direction": "bullish" if direction_code == "LONG" else "bearish" if direction_code == "SHORT" else "neutral",
        "direction_code": direction_code, "state": state, "action": action, "action_reasons": action_reasons,
        "entry": {"reference": round(entry_ref, 2), "zone": zone, "note": entry_note,
                  "trigger": None if trig is None else round(trig, 2),
                  "filled": None if plan is None or plan.fill is None else round(plan.fill, 2)},
        "stop_info": stop_info, "target_info": target_info,
        "stop_loss": stop, "invalidation": setup.invalidation if setup else "n/a (watch only)",
        "targets": targets or [], "target_note": (target_info or {}).get("method") if targets else (
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
        "expires_at_epoch": expires, "data_quality": dq, "gates": gates, "live": fr, "ml": ml,
        "evidence_tier": (gates.get("edge") or {}).get("tier"),
        **_final_score(comp["score"], action, gates, regime, direction_code, ml),
    }


def regime_multiplier(regime: dict[str, Any] | None, direction: str) -> float:
    up = benchmark_trend_up(regime)
    if up is None:
        return 1.0
    if direction == "SHORT":
        return 1.0 if not up else 0.6
    return 1.0 if up else 0.5


def _final_score(score: float, action: str, gates: dict[str, Any], regime: dict[str, Any] | None,
                 direction: str, ml: dict[str, Any] | None = None) -> dict[str, Any]:
    """final = composite x regime multiplier x gate multiplier x ML multiplier (0 if any applicable gate fails or the
    setup is not actionable). The ML multiplier (P(win) / base rate, clipped 0.6-1.4) is applied only when the
    meta-label model showed out-of-sample lift."""
    blocked_by = [g["name"] for g in gates.values() if g.get("applies") and not g.get("passed")]
    gate_mult = 0.0 if blocked_by or action not in ("BUY", "SELL") else 1.0
    reg_mult = regime_multiplier(regime, direction)
    ml_mult = 1.0
    if ml and ml.get("model_useful") and ml.get("threshold"):
        ml_mult = round(min(1.4, max(0.6, ml["p_win"] / ml["threshold"])), 3)
    return {"final_score": round(score * reg_mult * gate_mult * ml_mult, 1), "regime_multiplier": reg_mult,
            "gate_multiplier": gate_mult, "ml_multiplier": ml_mult,
            "blocked": bool(blocked_by) or action not in ("BUY", "SELL"), "blocked_by": blocked_by}


def add_confluence(recs: list[dict[str, Any]]) -> None:
    """Group by symbol: how many independent strategies agree, and flag direction conflicts."""
    by_sym: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in recs:
        by_sym.setdefault((r["exchange"], r["symbol"]), []).append(r)
    for rs in by_sym.values():
        active = [r for r in rs if r["state"] != "WATCH"]
        dirs = {r["direction_code"] for r in active}
        for r in rs:
            agree = [x["strategy"] for x in active if x["direction_code"] == r["direction_code"]]
            r["confluence"] = {"count": len(agree), "strategies": agree, "all_strategies": [x["strategy"] for x in rs],
                               "conflict": len(dirs) > 1}


def apply_live_benchmark(regime: dict[str, Any] | None, provider: Any, now: datetime) -> dict[str, Any] | None:
    """Use the live NIFTY 50 price (same store as the header) for the regime close; DMAs stay from daily history."""
    cur = (regime or {}).get("current")
    if not cur:
        return regime
    q = provider.quote("NIFTY 50", "NSE")
    if q is None or not q.last_price:
        cur["facts"]["close_source"] = f"daily close as of {cur.get('as_of')}"
        return regime
    f = cur["facts"]
    f["daily_close"] = f.get("close")
    f["close"] = round(float(q.last_price), 2)
    f["close_source"] = f"live {'tick' if q.market_open else 'last price'} as of {q.ts.isoformat(timespec='seconds')}"
    c, s50, s200, adx = f["close"], f.get("sma50"), f.get("sma200"), f.get("adx14")
    if s50 is not None and s200 is not None and adx is not None:
        trend = ("UPTREND" if c > s50 > s200 and adx >= 20 else
                 "DOWNTREND" if c < s50 < s200 and adx >= 20 else "RANGE / TRANSITION")
        if trend != cur["trend"]:
            cur["label"] = cur["label"].replace(cur["trend"].title(), trend.title())
            cur["trend"] = trend
    cur["as_of_live"] = q.ts.isoformat(timespec="seconds")
    return regime


def benchmark_trend_up(regime: dict[str, Any] | None) -> bool | None:
    facts = ((regime or {}).get("current") or {}).get("facts") or {}
    c, s200 = facts.get("close"), facts.get("sma200")
    return None if c is None or s200 is None else c > s200


def evaluate_gates(strat: Strategy, direction: str, evidence: dict[str, Any] | None, regime: dict[str, Any] | None,
                   cfg: RiskConfig) -> dict[str, dict[str, Any]]:
    """Evidence-based gates. A BUY/SELL is only shown when (a) the market regime is one in which the strategy type
    has worked and (b) the strategy has a walk-forward out-of-sample edge (in the current regime when enough trades)."""
    up = benchmark_trend_up(regime)
    facts = ((regime or {}).get("current") or {}).get("facts") or {}
    from ..decision import policy as pol

    q = pol.qualify(strat.meta.id, pol.trend_from_facts(facts), cfg.evidence_policy)
    if q is not None:
        return {"edge": {"name": "Validated edge", "applies": True, "passed": bool(q["ok"]), "detail": q["detail"],
                         "tier": q["tier"], "policy": cfg.evidence_policy, "regime_pf": q["regime_pf"],
                         "regime_trades": q["regime_trades"], "profit_factor": q["profit_factor"],
                         "trades": q["oos_trades"], "source": f"tournament {q['run_id']}"}}
    long_daily = direction == "LONG" and not strat.intraday
    regime_gate = {
        "name": "Market regime", "applies": bool(cfg.regime_filter and long_daily),
        "passed": up is not False,
        "detail": (f"Market-regime filter: NIFTY 50 ({facts.get('close')}) is below its 200-day average "
                   f"({facts.get('sma200')}); long setups are not taken in this regime (historically they lost money)")
        if up is False else "NIFTY 50 above its 200-day average (or regime unknown)",
    }
    ev = evidence or {}
    if not cfg.require_oos_edge:
        edge = {"name": "Out-of-sample edge", "applies": False, "passed": True, "detail": "Gate disabled in settings"}
    elif ev.get("status") != "ready":
        edge = {"name": "Out-of-sample edge", "applies": True, "passed": False,
                "detail": "Walk-forward evidence for this strategy is still being computed on the current data"}
    else:
        bucket = (ev.get("regime_split") or {}).get("up" if up else "down") if up is not None else None
        if bucket and (bucket.get("num_trades") or 0) >= 20:
            pf, n, scope = bucket.get("profit_factor"), bucket.get("num_trades"), \
                f"when NIFTY is {'above' if up else 'below'} its 200-DMA"
        else:
            pf, n, scope = ev.get("profit_factor"), ev.get("num_trades") or 0, "overall"
        ok = pf is not None and pf >= cfg.min_oos_profit_factor and n >= cfg.min_oos_trades
        edge = {"name": "Out-of-sample edge", "applies": True, "passed": ok, "profit_factor": pf, "trades": n,
                "scope": scope,
                "detail": (f"Out-of-sample edge {scope}: profit factor {pf} over {n} trades "
                           f"(needs >= {cfg.min_oos_profit_factor} over >= {cfg.min_oos_trades})")
                if ok else
                (f"No proven out-of-sample edge {scope}: profit factor {pf} over {n} walk-forward trades "
                 f"(needs >= {cfg.min_oos_profit_factor} over >= {cfg.min_oos_trades})")}
    return {"regime": regime_gate, "edge": edge}


def build_playbook(regime: dict[str, Any] | None, evidence: dict[str, dict[str, Any]], cfg: RiskConfig,
                   data_label: str, extra: list[str] | None = None) -> dict[str, Any]:
    from ..decision import policy as pol

    up = benchmark_trend_up(regime)
    facts = ((regime or {}).get("current") or {}).get("facts") or {}
    trend = pol.trend_from_facts(facts)
    rows = []
    for sid in all_strategy_ids() + list(extra or []):
        strat = get_strategy(sid)
        g = evaluate_gates(strat, "LONG", evidence.get(sid), regime, cfg)
        q = pol.evaluate(sid, trend)
        ev = evidence.get(sid) or {}
        edge = g["edge"]
        reg = g.get("regime", {"passed": True, "applies": False})
        rows.append({"strategy": sid, "name": strat.meta.name, "timeframe": strat.meta.timeframe,
                     "oos_trades": (q or {}).get("oos_trades", ev.get("num_trades")),
                     "oos_profit_factor": (q or {}).get("profit_factor", ev.get("profit_factor")),
                     "oos_net_return_pct": ev.get("net_return_pct"), "benchmark_return_pct": ev.get("benchmark_return_pct"),
                     "regime_split": ev.get("regime_split"), "regime_trades": (q or {}).get("regime_trades"),
                     "regime_pf": (q or {}).get("regime_pf"), "tier": (q or {}).get("tier"),
                     "regime_gate": reg["passed"] or not reg["applies"],
                     "edge_gate": edge["passed"] or not edge["applies"], "edge_detail": edge["detail"]})
    tradable = [r for r in rows if r["regime_gate"] and r["edge_gate"]]
    pol_name = cfg.evidence_policy
    if tradable:
        headline = (f"{trend.upper()} regime, policy '{pol_name}': {len(tradable)} strateg"
                    f"{'y' if len(tradable) == 1 else 'ies'} with enough out-of-sample evidence - "
                    + ", ".join(f"{r['name']} [{r['tier'] or 'legacy'}]" for r in tradable))
    else:
        headline = (f"{trend.upper()} regime (NIFTY 50 {facts.get('close')} vs 200-DMA {facts.get('sma200')}), policy "
                    f"'{pol_name}': no strategy has enough out-of-sample evidence in this regime - setups are WAIT."
                    + (" Try the 'balanced' or 'aggressive' policy to see weaker evidence." if pol_name == "strict" else ""))
    return {"benchmark_above_200dma": up, "headline": headline, "strategies": rows, "data_label": data_label,
            "trend": trend, "policy": pol_name,
            "settings": {"regime_filter": cfg.regime_filter, "require_oos_edge": cfg.require_oos_edge,
                         "min_oos_profit_factor": cfg.min_oos_profit_factor, "min_oos_trades": cfg.min_oos_trades,
                         "evidence_policy": pol_name},
            "note": "Evidence tiers come from the latest strategy tournament (walk-forward out-of-sample, after costs); "
                    "they reduce, not remove, risk."}


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
    daily = load_universe(session, "1d", tail=LIVE_TAIL["1d"])
    intraday = load_universe(session, "5m", tail=LIVE_TAIL["5m"])
    bench = load_benchmark(session)
    bench_ret_63 = None
    if bench is not None and len(bench) > 64:
        bench_ret_63 = float(bench["close"].iloc[-1] / bench["close"].iloc[-64] - 1.0)
    regime = classify_regime(bench, daily) if bench is not None else None
    regime = apply_live_benchmark(regime, provider, now)
    calendar = set(bench.index.date) if bench is not None and len(bench) else None
    exp = {"1d": provider.expected_last_bar("1d", now), "5m": provider.expected_last_bar("5m", now)}

    dq_cache: dict[tuple[str, tuple[str, str]], dict[str, Any]] = {}
    daily_snaps = {k: last_row(features(k, df)) for k, df in daily.items()}
    recs: list[dict[str, Any]] = []
    blocked: dict[tuple[str, tuple[str, str]], dict[str, Any]] = {}
    notes: list[str] = []
    from ..decision import policy as pol
    from ..quant.library.context import load_context

    load_context(session)
    trend = pol.trend_from_facts(((regime or {}).get("current") or {}).get("facts"))
    extra = [x for x in pol.qualifying_library_ids(trend, cfg.evidence_policy) if x not in all_strategy_ids()]
    for sid in all_strategy_ids() + extra:
        strat = get_strategy(sid)
        tf = strat.meta.timeframe
        data = intraday if strat.intraday else daily
        if strat.intraday and data:
            any_df = next(iter(data.values()))
            if not intraday_session_active(now, any_df.index[-1], str(strat.params.get("exit_time", "15:15"))):
                notes.append(f"{strat.meta.name}: intraday scanner inactive outside the trading session "
                             f"(market {provider.session_state(now)}); no intraday setups are generated.")
                continue
        for key, df in data.items():
            ck = (tf, key)
            if ck not in dq_cache:
                dq_cache[ck] = validate_candles(df, tf, exp[tf], now, min_bars=60,
                                                calendar=calendar if tf == "1d" else None).to_dict()
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
            rec = build_recommendation(strat, key, imap.get(key), cand, p, a, daily_snaps.get(key, {}),
                                       bench_ret_63, pol.tournament_evidence(sid, label) or evidence.get(sid), cfg,
                                       weights, regime, dq, now, label,
                                       realtime.freshness(key[0], key[1]))
            rec["provider"] = provider.name
            recs.append(rec)
    add_confluence(recs)
    recs.sort(key=lambda r: (r["blocked"], -r["final_score"], -r["score"]))
    return {"generated_at": now.isoformat(), "data_label": label, "provider": provider.name,
            "recommendations": recs, "blocked": list(blocked.values()), "notes": notes, "regime": regime,
            "playbook": build_playbook(regime, evidence, cfg, label, extra),
            "policy": {"name": cfg.evidence_policy, "trend": trend, "library_strategies": extra,
                       "info": pol.POLICY_INFO[cfg.evidence_policy], "min_tier": pol.POLICY_MIN_TIER[cfg.evidence_policy]},
            "expected_last_bar": {k: (v.isoformat() if v else None) for k, v in exp.items()},
            "seconds": round(time.perf_counter() - t0, 2)}

