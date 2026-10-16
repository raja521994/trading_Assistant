"""Decision engine: "best stocks to buy today" from ONLY tournament-passed, regime-suitable strategies.

Cycle (Part 6 of the brief):
1. Freshness: if the live feed is stale during market hours, nothing is actionable.
2. Regime: bull / bear / sideways (NIFTY vs 50/200-DMA, live price) x low / high volatility (India VIX vs its
   1-year median) -> exposure budget (config/decision.yaml).
3. Universe: NIFTY 200 filtered by liquidity. Ban-list / results-date / circuit / surveillance data is NOT
   connected: the event gate is reported as UNKNOWN on every pick (never silently passed).
4. Signals from passed + regime-suitable + not-decayed strategies only.
5. Symbol score (editable weights) x regime multiplier x gate multiplier.
6. Portfolio construction: risk per trade, max positions, sector cap, correlation cap, daily-loss halt, liquidity cap.
7. TOP N <= 5, or exactly NO_TRADE_MSG with the nearest Watch Only candidates and what must change.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import BACKEND_DIR
from ..models import PaperTrade
from ..providers.registry import active_provider
from ..providers.sample import now_ist
from ..quant.library.catalog import BY_ID
from ..quant.library.context import load_context
from ..quant.regime import classify_regime
from ..quant.scanner import apply_live_benchmark, build_recommendation, evaluate_latest
from ..quant.scoring import load_weights
from ..quant.strategies.base import get_strategy
from ..quant.universe import (LIVE_TAIL, features, instrument_map, last_row, load_benchmark, load_universe,
                              prepared_with_arrays)
from ..realtime.service import service as realtime
from ..realtime.store import STALE_SECONDS, store
from ..risk.limits import load_risk_config
from ..validation.tournament import NO_TRADE_MSG, load_report

CONFIG_FILE = BACKEND_DIR / "config" / "decision.yaml"


def load_config() -> dict[str, Any]:
    return yaml.safe_load(Path(CONFIG_FILE).read_text(encoding="utf-8"))


def save_weights(weights: dict[str, float]) -> dict[str, Any]:
    conf = load_config()
    for k, v in weights.items():
        if k in conf["weights"]:
            conf["weights"][k] = float(v)
    CONFIG_FILE.write_text(yaml.safe_dump(conf, sort_keys=False), encoding="utf-8")
    return conf


# ------------------------------------------------------------------ regime

def regime_state(session: Session, now: datetime, conf: dict[str, Any]) -> dict[str, Any]:
    provider = active_provider()
    bench = load_benchmark(session)
    daily = load_universe(session, "1d", tail=LIVE_TAIL["1d"])
    reg = apply_live_benchmark(classify_regime(bench, daily) if bench is not None else None, provider, now)
    cur = (reg or {}).get("current") or {}
    f = cur.get("facts") or {}
    c, s50, s200 = f.get("close"), f.get("sma50"), f.get("sma200")
    if None in (c, s50, s200):
        trend = "unknown"
    elif c > s200 and s50 > s200:
        trend = "bull"
    elif c < s200 and s50 < s200:
        trend = "bear"
    else:
        trend = "sideways"
    idx = load_universe(session, "1d", "INDEX", tail=300)
    vix_df = idx.get(("NSE", "INDIA VIX"))
    vix_val = vix_med = None
    vix_src = "missing"
    if vix_df is not None and len(vix_df) > 60:
        vix_val, vix_med = float(vix_df["close"].iloc[-1]), float(vix_df["close"].iloc[-252:].median())
        vix_src = f"daily close {vix_df.index[-1].date()}"
        tick = store.get("NSE", "INDIA VIX")
        if tick is not None and tick.ltp:
            vix_val, vix_src = float(tick.ltp), "live tick"
    vol = "unknown" if vix_val is None else ("high" if vix_val > vix_med else "low")
    budget = float(conf["budget"].get(trend, conf["budget"]["unknown"]).get("high" if vol == "high" else "low", 0))
    nifty = idx.get(("NSE", "NIFTY 50"))
    sma100 = round(float(nifty["close"].iloc[-100:].mean()), 2) if nifty is not None and len(nifty) >= 100 else None
    inputs = [
        {"name": "NIFTY 50", "value": c, "detail": f.get("close_source"), "status": "ok" if c else "missing"},
        {"name": "50-DMA", "value": s50, "status": "ok" if s50 else "missing"},
        {"name": "100-DMA", "value": sma100, "status": "ok" if sma100 else "missing"},
        {"name": "200-DMA", "value": s200, "status": "ok" if s200 else "missing"},
        {"name": "Breadth (% above 50-DMA)", "value": cur.get("breadth_pct_above_sma50"), "status": "ok"},
        {"name": "India VIX", "value": None if vix_val is None else round(vix_val, 2),
         "detail": None if vix_med is None else f"1-year median {vix_med:.2f} ({vix_src})",
         "status": "ok" if vix_val is not None else "missing"},
        {"name": "FII/DII flows", "value": None, "status": "not connected"},
        {"name": "USD-INR, crude, US 10-year, GIFT Nifty", "value": None, "status": "not connected"},
        {"name": "Event calendar (results, RBI, Budget, F&O ban)", "value": None, "status": "not connected"},
    ]
    return {"trend": trend, "volatility": vol, "label": f"{trend.upper()} x {vol.upper()} volatility",
            "exposure_budget_pct": budget, "multiplier": budget / 100.0, "inputs": inputs,
            "as_of": cur.get("as_of_live") or cur.get("as_of")}


# ------------------------------------------------------------------ eligibility & decay

def decay_monitor(session: Session, report: dict[str, Any] | None, conf: dict[str, Any]) -> list[dict[str, Any]]:
    """Rolling paper performance of each PASSED strategy against its out-of-sample expectancy band."""
    if not report:
        return []
    trades = session.scalars(select(PaperTrade).where(PaperTrade.status == "CLOSED")).all()
    by: dict[str, list[float]] = {}
    for t in trades:
        if t.r_multiple is not None:
            by.setdefault(t.strategy, []).append(float(t.r_multiple))
    out = []
    for row in report["leaderboard"]:
        if row.get("result") != "PASS":
            continue
        ci = (report["details"].get(row["id"]) or {}).get("bootstrap", {}).get("expectancy_r_ci") or [None, None]
        rs = by.get(row["id"], [])
        n = len(rs)
        mean = float(np.mean(rs)) if rs else None
        if n < conf["decay"]["min_paper_trades"]:
            st = "insufficient paper trades"
        elif ci[0] is not None and mean is not None and mean < ci[0]:
            st = "BELOW expected band - disabled"
        elif ci[1] is not None and mean is not None and mean > ci[1]:
            st = "above expected band"
        else:
            st = "within expected band"
        out.append({"id": row["id"], "name": row["name"], "paper_trades": n,
                    "paper_expectancy_r": None if mean is None else round(mean, 3),
                    "backtest_expectancy_r": row.get("expectancy_r"), "expected_band": ci, "status": st,
                    "disabled": st.startswith("BELOW")})
    return out


def eligible_strategies(report: dict[str, Any] | None, regime: dict[str, Any], conf: dict[str, Any],
                        decay: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(eligible, rejected-with-reason) among tournament strategies."""
    if not report:
        return [], []
    disabled = {d["id"] for d in decay if d["disabled"]}
    rs = conf["regime_suitability"]
    eligible, rejected = [], []
    for row in report["leaderboard"]:
        det = report["details"].get(row["id"]) or {}
        if row.get("result") != "PASS":
            rejected.append({"id": row["id"], "name": row["name"], "reason":
                             f"did not pass the tournament (first failed: {row.get('first_failed')})"})
            continue
        if row["id"] in disabled:
            rejected.append({"id": row["id"], "name": row["name"], "reason": "decay monitor: paper results below band"})
            continue
        bucket = (det.get("regimes") or {}).get(f"trend:{regime['trend']}") or {}
        if (bucket.get("num_trades") or 0) < rs["min_trades"] or (bucket.get("profit_factor") or 0) < rs["min_profit_factor"]:
            rejected.append({"id": row["id"], "name": row["name"], "reason":
                             f"not suitable for a {regime['trend']} regime (OOS PF {bucket.get('profit_factor')} over "
                             f"{bucket.get('num_trades', 0)} trades in this regime)"})
            continue
        eligible.append({**row, "details": det, "regime_bucket": bucket})
    return eligible, rejected


# ------------------------------------------------------------------ helpers

def _clip(x: float) -> float:
    return max(0.0, min(100.0, x))


def _family_score(n: int) -> float:
    return {0: 0.0, 1: 40.0, 2: 75.0}.get(n, 100.0)


def _wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round((c - h) * 100, 1), round((c + h) * 100, 1)]


# ------------------------------------------------------------------ main

def decide(session: Session, now: datetime | None = None, report: dict[str, Any] | None = None) -> dict[str, Any]:
    provider = active_provider()
    now = now or now_ist()
    conf = load_config()
    cfg = load_risk_config(session)
    label = provider.capabilities().data_status.value
    report = report if report is not None else load_report()
    load_context(session)
    health = realtime.health()
    market_open = provider.session_state(now) == "OPEN"
    feed_ok = (health.get("feed") or {}).get("state") in ("connected", "replaying")
    age = health.get("last_tick_age_s")
    stale = market_open and label != "SIMULATED" and (not feed_ok or age is None or age > STALE_SECONDS)
    regime = regime_state(session, now, conf)
    decay = decay_monitor(session, report, conf)
    eligible, rejected = eligible_strategies(report, regime, conf, decay)

    from ..paper.broker import portfolio_state

    pstate = portfolio_state(session, cfg, now)
    daily_loss_hit = pstate.realized_pnl_today <= -cfg.max_daily_loss_pct / 100 * cfg.account_equity
    gates = [
        {"name": "Data freshness", "passed": not stale,
         "detail": "STALE: no live tick for > 5 s during market hours - nothing is actionable" if stale else
         (f"live feed {(health.get('feed') or {}).get('state')}, last tick {age}s ago" if market_open else
          "market closed - plans are for the next session, using the last official prices")},
        {"name": "Tournament evidence", "passed": bool(report),
         "detail": f"run {report['run_id']}: {report['headline']}" if report else "No tournament run yet"},
        {"name": "Regime exposure budget", "passed": regime["exposure_budget_pct"] > 0,
         "detail": f"{regime['label']} -> {regime['exposure_budget_pct']:.0f}% of normal risk"},
        {"name": "Kill switch", "passed": not cfg.kill_switch, "detail": "on" if cfg.kill_switch else "off"},
        {"name": "Daily loss limit", "passed": not daily_loss_hit,
         "detail": f"realised today Rs {pstate.realized_pnl_today:,.0f} (limit -{cfg.max_daily_loss_pct}% of equity)"},
        {"name": "Event gate", "passed": None,
         "detail": "UNKNOWN - results dates, F&O ban list and circuit/surveillance lists are not connected; "
                   "check them manually before acting"},
    ]
    blocking = [g for g in gates if g["passed"] is False]
    candidates = _signals(session, eligible, cfg, now, label, market_open) if eligible and not stale else []
    picks, skipped = _construct(session, candidates, regime, cfg, conf, pstate) if not blocking else ([], [])
    out = {"generated_at": now.isoformat(timespec="seconds"), "data_label": label, "market_open": market_open,
           "regime": regime, "gates": gates,
           "eligible_strategies": [{"id": e["id"], "name": e["name"], "family": e["family"],
                                    "regime_pf": e["regime_bucket"].get("profit_factor"),
                                    "regime_trades": e["regime_bucket"].get("num_trades")} for e in eligible],
           "rejected_strategies": rejected, "decay_monitor": decay, "weights": conf["weights"],
           "tournament_run": report["run_id"] if report else None,
           "disclaimer": "Research and paper trading only. Not investment advice. Confidence is shown as a range."}
    if picks:
        out.update({"decision": "BUY", "message": f"TOP {len(picks)} BUYS", "picks": picks, "skipped": skipped,
                    "watch_only": []})
    else:
        out.update({"decision": "NO TRADE", "message": NO_TRADE_MSG, "picks": [], "skipped": skipped,
                    "watch_only": _watch_only(report, regime, rejected, blocking, skipped)})
    return out


def _signals(session: Session, eligible: list[dict[str, Any]], cfg: Any, now: datetime, label: str,
             market_open: bool) -> list[dict[str, Any]]:
    imap = instrument_map(session)
    daily = load_universe(session, "1d", tail=LIVE_TAIL["1d"])
    intraday = load_universe(session, "5m", tail=LIVE_TAIL["5m"])
    bench = load_benchmark(session)
    bench_ret_63 = None
    if bench is not None and len(bench) > 64:
        bench_ret_63 = float(bench["close"].iloc[-1] / bench["close"].iloc[-64] - 1.0)
    weights = load_weights(session)
    out = []
    for e in eligible:
        det = e["details"]
        params = (det.get("stability") or {}).get("base_params") or {}
        strat = get_strategy(e["id"], params)
        if hasattr(strat, "run_custom"):
            continue  # rebalance / pairs strategies cannot pass without point-in-time constituents
        data = intraday if strat.intraday else daily
        if strat.intraday and (not market_open or not data):
            continue
        o = det.get("oos") or {}
        ev = {"status": "ready", "profit_factor": o.get("profit_factor"), "num_trades": o.get("num_trades"),
              "expectancy_r": o.get("expectancy_r"), "win_rate_pct": o.get("win_rate_pct"),
              "net_return_pct": o.get("net_return_pct"), "max_drawdown_pct": o.get("max_drawdown_pct"),
              "mfe_r_quantiles": det.get("mfe_r_quantiles") or {}, "data_label": label,
              "period_start": o.get("period_start"), "period_end": o.get("period_end")}
        for key, df in data.items():
            inst = imap.get(key)
            if inst is None or len(df) < strat.warmup + 5:
                continue
            p, a = prepared_with_arrays(strat, key, df)
            cand = evaluate_latest(strat, a)
            if cand is None or cand["state"] not in ("SETUP", "TRIGGERED"):
                continue
            snap = last_row(features(key, daily[key])) if key in daily else {}
            rec = build_recommendation(strat, key, inst, cand, p, a, snap, bench_ret_63, ev, cfg, weights, None,
                                       {"ok": True, "status": "OK"}, now, label, realtime.freshness(key[0], key[1]))
            rec["tournament"] = {"id": e["id"], "family": BY_ID[e["id"]].family_name,
                                 "score": (det.get("composite") or {}).get("score"),
                                 "expectancy_ci": (det.get("bootstrap") or {}).get("expectancy_r_ci"),
                                 "win_rate_pct": o.get("win_rate_pct"), "num_trades": o.get("num_trades"),
                                 "profit_factor": o.get("profit_factor"),
                                 "pf_ci": (det.get("bootstrap") or {}).get("pf_ci"),
                                 "regime_pf": e["regime_bucket"].get("profit_factor")}
            out.append(rec)
    return out


def _construct(session: Session, recs: list[dict[str, Any]], regime: dict[str, Any], cfg: Any, conf: dict[str, Any],
               pstate: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not recs:
        return [], []
    w = conf["weights"]
    wsum = sum(w.values()) or 1
    by_sym: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in recs:
        by_sym.setdefault((r["exchange"], r["symbol"]), []).append(r)
    daily = load_universe(session, "1d", tail=LIVE_TAIL["1d"])
    bench = load_benchmark(session)
    b63 = float(bench["close"].iloc[-1] / bench["close"].iloc[-64] - 1) * 100 if bench is not None and len(bench) > 64 \
        else 0.0
    scored = []
    for key, rs in by_sym.items():
        dirs = {r["direction_code"] for r in rs}
        r0 = max(rs, key=lambda r: (r["tournament"]["score"] or 0))
        fams = {r["tournament"]["family"] for r in rs if r["direction_code"] == r0["direction_code"]}
        df = daily.get(key)
        s63 = float(df["close"].iloc[-1] / df["close"].iloc[-64] - 1) * 100 if df is not None and len(df) > 64 else 0.0
        rr = r0.get("risk_reward") or 0
        vr = (r0.get("indicators") or {}).get("vol_ratio") or 1.0
        comp = {
            "validated_edge": _clip(float(np.mean([r["tournament"]["score"] or 0 for r in rs]))),
            "confluence": 0.0 if len(dirs) > 1 else _family_score(len(fams)),
            "relative_strength": _clip(50 + 5 * (s63 - b63)),
            "risk_reward": 0.0 if not (r0.get("stop_info") or {}).get("within_cap", True) else _clip(rr / 3 * 100),
            "participation": _clip(vr / 2 * 100),
            "fundamentals_events": 50.0,
        }
        score = sum(comp[k] * w.get(k, 0) for k in comp) / wsum
        gate_fail = []
        if len(dirs) > 1:
            gate_fail.append("strategies disagree on direction")
        if (r0.get("freshness") or {}).get("stale"):
            gate_fail.append("symbol price is STALE (> 5 s without a tick)")
        if (r0.get("liquidity") or {}).get("rating") == "LOW":
            gate_fail.append("liquidity below the minimum")
        if not (r0.get("stop_info") or {}).get("within_cap", True):
            gate_fail.append("stop wider than the mode cap")
        final = score * regime["multiplier"] * (0.0 if gate_fail else 1.0)
        scored.append({"key": key, "recs": rs, "primary": r0, "score": round(score, 1), "components": comp,
                       "final": round(final, 1), "gate_fail": gate_fail, "families": sorted(fams)})
    scored.sort(key=lambda x: -x["final"])
    slots = max(0, min(int(conf["top_n"]), cfg.max_open_positions - len(pstate.open_positions)))
    picks: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    sector_val: dict[str, float] = {}
    chosen_rets: list[pd.Series] = []
    risk_amt = cfg.account_equity * cfg.risk_per_trade_pct / 100 * regime["multiplier"]
    for s in scored:
        r = s["primary"]
        why = list(s["gate_fail"])
        if not why and s["final"] < conf["min_final_score"]:
            why.append(f"final score {s['final']} below {conf['min_final_score']}")
        df = daily.get(s["key"])
        rets = df["close"].pct_change().tail(60) if df is not None else None
        if not why and rets is not None:
            for cr in chosen_rets:
                c = rets.corr(cr)
                if c == c and c > conf["max_correlation"]:
                    why.append(f"60-day correlation {c:.2f} with an already selected position")
                    break
        entry = r["entry"].get("trigger") or r["entry"]["reference"]
        stop = r["stop_loss"]
        per_share = abs(entry - stop) if stop else 0
        qty = int(risk_amt // per_share) if per_share > 0 else 0
        qty = min(qty, int(cfg.account_equity * cfg.max_alloc_per_instrument_pct / 100 // entry)) if entry else 0
        adv = (r.get("liquidity") or {}).get("avg_turnover_cr")
        if adv:
            qty = min(qty, int(adv * 1e7 * conf["liquidity_participation_pct"] / 100 // entry))
        sector = r.get("sector") or "Unknown"
        cap_val = qty * entry
        if not why and (sector_val.get(sector, 0) + cap_val) / cfg.account_equity * 100 > cfg.max_sector_exposure_pct:
            why.append(f"sector {sector} exposure would exceed {cfg.max_sector_exposure_pct}%")
        if not why and qty < 1:
            why.append("position size rounds to zero at the current risk budget")
        if why or len(picks) >= slots:
            skipped.append({"symbol": s["key"][1], "score": s["score"], "final": s["final"],
                            "reasons": why or ["no free position slots"]})
            continue
        sector_val[sector] = sector_val.get(sector, 0) + cap_val
        if rets is not None:
            chosen_rets.append(rets)
        t = r["tournament"]
        n_tr = t.get("num_trades") or 0
        picks.append({
            "symbol": s["key"][1], "exchange": s["key"][0], "name": r.get("name"), "sector": sector,
            "strategies": [x["strategy_name"] for x in s["recs"]], "families": s["families"],
            "mode": r["mode"], "direction": r["direction_code"], "timeframe": r["timeframe"],
            "entry_trigger": entry, "entry_note": r["entry"].get("note"), "stop": stop, "targets": r["targets"],
            "target_info": r.get("target_info"), "risk_reward": r.get("risk_reward"), "qty": qty,
            "capital_used": round(qty * entry, 2), "max_loss": round(qty * per_share, 2),
            "holding_period": r.get("holding_period"), "invalidation": r.get("invalidation"),
            "score": s["score"], "score_breakdown": {k: round(v, 1) for k, v in s["components"].items()},
            "regime_multiplier": regime["multiplier"], "final_score": s["final"],
            "evidence": [{"strategy": x["tournament"]["id"], "profit_factor": x["tournament"]["profit_factor"],
                          "pf_ci": x["tournament"]["pf_ci"], "expectancy_ci": x["tournament"]["expectancy_ci"],
                          "oos_trades": x["tournament"]["num_trades"], "regime_pf": x["tournament"]["regime_pf"]}
                         for x in s["recs"]],
            "confidence": {"expectancy_r_range": t.get("expectancy_ci"),
                           "win_rate_range_pct": _wilson(int(round((t.get("win_rate_pct") or 0) / 100 * n_tr)), n_tr),
                           "note": "95% ranges from out-of-sample trades; past results do not guarantee future ones"},
            "signal_id": r.get("signal_id"), "state": r["state"], "reference_price": r["reference_price"],
            "freshness": r.get("freshness"),
            "what_to_check": "Event risk is UNKNOWN (results date, F&O ban, circuit limits not connected) - verify first.",
        })
    return picks, skipped


def _watch_only(report: dict[str, Any] | None, regime: dict[str, Any], rejected: list[dict[str, Any]],
                blocking: list[dict[str, Any]], skipped: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Nearest candidates from the live scanner, with exactly what must change for each to qualify."""
    from ..signals.engine import last_scan

    scan = last_scan() or {}
    rej = {r["id"]: r["reason"] for r in rejected}
    out: list[dict[str, Any]] = []
    for s in skipped[:5]:
        out.append({"symbol": s["symbol"], "strategy": "validated strategy", "state": "SETUP", "score": s["final"],
                    "what_must_change": s["reasons"]})
    recs = [r for r in scan.get("recommendations", []) if r.get("state") in ("SETUP", "TRIGGERED", "ACTIVE", "WATCH")]
    recs.sort(key=lambda r: (r.get("state") == "WATCH", -(r.get("score") or 0)))
    seen = {(o["symbol"], o["strategy"]) for o in out}
    s200 = next((x["value"] for x in regime.get("inputs", []) if x["name"] == "200-DMA"), None)
    for r in recs:
        if len(out) >= 10:
            break
        if (r["symbol"], r["strategy"]) in seen:
            continue
        need = []
        if r["strategy"] in rej:
            need.append(f"{r.get('strategy_name')} {rej[r['strategy']]}")
        elif not report:
            need.append("run the strategy tournament")
        else:
            need.append(f"{r.get('strategy_name')} was not part of the latest tournament")
        if regime["exposure_budget_pct"] <= 0:
            need.append(f"regime must improve from {regime['label']} (NIFTY 50 back above its 200-DMA"
                        f"{f' {s200:,.0f}' if s200 else ''} with the 50-DMA above it)")
        for g in blocking:
            if g["name"] not in ("Regime exposure budget", "Tournament evidence"):
                need.append(f"{g['name']}: {g['detail']}")
        trig = (r.get("entry") or {}).get("trigger")
        if r.get("state") == "SETUP" and trig:
            need.append(f"a closed bar must cross the trigger {trig:.2f}")
        if r.get("state") == "WATCH":
            need.append("entry conditions are incomplete (watch only)")
        out.append({"symbol": r["symbol"], "exchange": r["exchange"], "strategy": r["strategy"],
                    "strategy_name": r.get("strategy_name"), "state": r["state"], "score": r.get("score"),
                    "reference_price": r.get("reference_price"), "signal_id": r.get("signal_id"),
                    "timeframe": r.get("timeframe"), "what_must_change": need})
        seen.add((r["symbol"], r["strategy"]))
    return out
