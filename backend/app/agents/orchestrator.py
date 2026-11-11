"""Orchestrator (main) agent: collects market context, strategy candidates and per-stock risk assessment, then
hands a plan of instructions to the worker. It never places or closes trades itself."""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from datetime import time as dtime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..data.repository import get_instrument
from ..models import PaperTrade, Signal
from ..paper.broker import portfolio_state
from ..providers.registry import active_provider
from ..providers.sample import now_ist
from ..risk.limits import OpenPosition, RiskConfig, TradeProposal, check_trade, load_risk_config
from ..risk.sizing import position_size
from .state import KEY_PREFIX, AgentState, agent_trades, idempotency_key

log = logging.getLogger(__name__)

INTRADAY_ENTRY_CUTOFF = dtime(14, 45)
ACTIONABLE = ("SETUP", "TRIGGERED", "ACTIVE")
# Gates from the decision engine that stop all new entries (regime/tournament gates only scale or rank)
HARD_GATES = ("Data freshness", "Kill switch", "Daily loss limit")


@dataclass
class Instruction:
    kind: str  # ENTER | EXIT | TIGHTEN_STOP
    symbol: str
    exchange: str
    mode: str
    reason: str
    direction: str | None = None
    signal_id: int | None = None
    trade_id: int | None = None
    qty: int | None = None
    stop: float | None = None
    target: float | None = None
    strategy: str | None = None
    score: float | None = None
    order_key: str | None = None
    source: str | None = None  # decision_engine | scanner
    risk: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Plan:
    generated_at: str
    market_open: bool
    intel: dict[str, Any]
    portfolio: dict[str, Any]
    candidates_seen: int
    instructions: list[Instruction]
    rejected: list[dict[str, Any]]
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "instructions": [i.to_dict() for i in self.instructions]}


def market_intel(session: Session, now: datetime) -> dict[str, Any]:
    """Regime, exposure budget and gates from the decision engine; its TOP picks get priority."""
    try:
        from ..decision.engine import decide

        d = decide(session, now)
    except Exception as exc:  # noqa: BLE001
        log.exception("agent: decision engine failed")
        return {"decision": "UNAVAILABLE", "error": f"{type(exc).__name__}: {exc}", "regime_label": "unknown",
                "multiplier": 1.0, "blocking": [], "picks": []}
    reg = d.get("regime") or {}
    blocking = [f"{g['name']}: {g['detail']}" for g in d.get("gates", [])
                if g.get("passed") is False and g["name"] in HARD_GATES]
    return {"decision": d.get("decision"), "regime_label": reg.get("label"),
            "multiplier": float(reg.get("multiplier") or 0.0), "exposure_budget_pct": reg.get("exposure_budget_pct"),
            "blocking": blocking,
            "picks": [{k: p.get(k) for k in ("symbol", "exchange", "direction", "mode", "state", "entry_trigger",
                                              "stop", "targets", "final_score", "tier", "strategies", "evidence")}
                      for p in d.get("picks", [])]}


def _manage_positions(session: Session, st: AgentState, now: datetime) -> list[Instruction]:
    provider = active_provider()
    acfg = st.config
    out: list[Instruction] = []
    for t in agent_trades(session, status="OPEN"):
        q = provider.quote(t.symbol, t.exchange)
        px = q.last_price if q else t.last_price
        sig = session.get(Signal, t.signal_id) if t.signal_id else None
        base = {"symbol": t.symbol, "exchange": t.exchange, "mode": t.mode, "direction": t.direction,
                "trade_id": t.id, "strategy": t.strategy}
        if sig is not None and sig.state == "EXITED":
            out.append(Instruction("EXIT", reason=f"strategy exit signal ({sig.strategy})", **base))
            continue
        max_days = acfg.max_hold_days.get(t.mode)
        held_days = (now.timestamp() - t.entry_ts) / 86400
        if max_days and held_days >= max_days:
            out.append(Instruction("EXIT", reason=f"max holding period {max_days} days reached", **base))
            continue
        if px is None or acfg.breakeven_at_r <= 0:
            continue
        initial_stop = float(((t.details or {}).get("agent") or {}).get("initial_stop") or t.stop)
        r = abs(t.entry_price - initial_stop)
        long = t.direction == "LONG"
        move = (px - t.entry_price) if long else (t.entry_price - px)
        not_yet = t.stop < t.entry_price if long else t.stop > t.entry_price
        if r > 0 and not_yet and move >= acfg.breakeven_at_r * r:
            out.append(Instruction("TIGHTEN_STOP", stop=t.entry_price,
                                   reason=f"+{move / r:.2f}R in favour: stop to breakeven {t.entry_price:.2f}", **base))
    return out


def _mode_slots(cfg: RiskConfig, st: AgentState) -> dict[str, int]:
    alloc = st.config.mode_allocation_pct
    return {m: max(1, round(cfg.max_open_positions * alloc.get(m, 0.0) / 100.0))
            for m in st.config.modes if alloc.get(m, 0.0) > 0}


def _select_entries(session: Session, st: AgentState, cfg: RiskConfig, intel: dict[str, Any], now: datetime,
                    rejected: list[dict[str, Any]]) -> tuple[list[Instruction], int]:
    provider = active_provider()
    acfg = st.config
    pstate = portfolio_state(session, cfg, now)
    open_all = session.scalars(select(PaperTrade).where(PaperTrade.status == "OPEN")).all()
    open_symbols = {t.symbol for t in open_all}
    per_mode = Counter(t.mode for t in agent_trades(session, status="OPEN"))
    slots = _mode_slots(cfg, st)
    traded = {t.signal_id for t in agent_trades(session, st.session_id)}
    now_epoch = int(now.timestamp())
    sigs = session.scalars(select(Signal).where(Signal.origin == "live_scan", Signal.provider == provider.name,
                                                Signal.state.in_(ACTIONABLE), Signal.mode.in_(list(slots)))).all()

    # Unified candidates: decision-engine TOP picks (tournament-validated) first, then scanner signals
    cands: list[dict[str, Any]] = []
    for p in intel.get("picks", []):
        ev = p.get("evidence") or []
        cands.append({
            "source": "decision_engine", "signal_id": None, "symbol": p["symbol"], "exchange": p["exchange"],
            "direction": p["direction"], "mode": p["mode"], "state": p["state"], "action": f"TOP PICK {p.get('tier')}",
            "strategy": ev[0]["strategy"] if ev else ", ".join(p.get("strategies") or []) or "decision",
            "score": float(p.get("final_score") or 0.0), "trigger": p.get("entry_trigger"), "stop": p.get("stop"),
            "target": (p.get("targets") or [None])[-1], "turnover": None, "rr": None, "validated": True,
            "order_key": f"{KEY_PREFIX}{st.session_id}-d{now:%Y%m%d}-{p['exchange']}-{p['symbol']}"[:96]})
    for s in sigs:
        if s.id in traded:
            continue
        p = s.payload or {}
        cands.append({
            "source": "scanner", "signal_id": s.id, "symbol": s.symbol, "exchange": s.exchange,
            "direction": s.direction, "mode": s.mode, "state": s.state, "action": s.action, "strategy": s.strategy,
            "score": float(s.score or 0.0), "trigger": (p.get("entry") or {}).get("trigger"),
            "stop": p.get("stop_loss"), "target": (p.get("targets") or [None])[-1],
            "turnover": (p.get("liquidity") or {}).get("avg_turnover_cr"), "rr": p.get("risk_reward"),
            "validated": s.action in ("BUY", "SELL"), "expires_at": s.expires_at,
            "order_key": idempotency_key(st.session_id or "manual", s.id)})

    def reject(c: dict[str, Any], why: str) -> None:
        rejected.append({"signal_id": c["signal_id"], "source": c["source"], "symbol": c["symbol"],
                         "mode": c["mode"], "strategy": c["strategy"], "direction": c["direction"],
                         "action": c["action"], "reason": why})

    used_keys = set(session.scalars(select(PaperTrade.idempotency_key).where(
        PaperTrade.idempotency_key.in_([c["order_key"] for c in cands]))))
    ranked: list[dict[str, Any]] = []
    for c in cands:
        if c["order_key"] in used_keys:
            continue
        if c["direction"] not in ("LONG", "SHORT"):
            reject(c, "no direction yet (setup not formed)")
        elif c["mode"] not in slots:
            reject(c, f"{c['mode']} trading is not enabled for the agent")
        elif c["symbol"] in open_symbols:
            reject(c, "position already open in this symbol")
        elif not c["validated"] and not acfg.allow_unvalidated:
            reject(c, f"scanner action is {c['action']} (evidence gates not passed; enable learning mode to trade it)")
        elif c["direction"] == "SHORT" and c["mode"] != "intraday":
            reject(c, "cash-market shorts are intraday only")
        elif c["mode"] == "intraday" and now.time() >= INTRADAY_ENTRY_CUTOFF:
            reject(c, f"intraday entries stop at {INTRADAY_ENTRY_CUTOFF.strftime('%H:%M')} IST")
        elif c.get("expires_at") and now_epoch > c["expires_at"]:
            reject(c, "signal window expired")
        else:
            ranked.append(c)
    ranked.sort(key=lambda c: (c["source"] != "decision_engine", -c["score"]))

    out: list[Instruction] = []
    mult = max(0.0, min(1.0, float(intel.get("multiplier", 1.0))))
    for c in ranked:
        if len(out) >= acfg.max_new_trades_per_cycle:
            break
        if c["symbol"] in open_symbols:
            reject(c, "already selected this cycle")
            continue
        mode = c["mode"]
        if per_mode[mode] >= slots.get(mode, 0):
            reject(c, f"{mode} slots full ({slots.get(mode, 0)})")
            continue
        long = c["direction"] == "LONG"
        eff = max(mult, 0.25) if mode == "intraday" else mult
        if eff <= 0:
            reject(c, f"regime {intel.get('regime_label')} has a 0% exposure budget for overnight trades")
            continue
        q = provider.quote(c["symbol"], c["exchange"])
        if q is None:
            reject(c, "no live quote")
            continue
        px = q.last_price
        trig = c["trigger"]
        if c["state"] == "SETUP" and (trig is None or (long and px < trig) or (not long and px > trig)):
            reject(c, f"waiting for price to cross trigger {trig} (last {px:.2f})")
            continue
        stop, target = c["stop"], c["target"]
        if stop is None or (long and stop >= px) or (not long and stop <= px):
            reject(c, f"stop {stop} already breached at {px:.2f}")
            continue
        if target is not None and ((long and target <= px) or (not long and target >= px)):
            reject(c, f"target {target} already reached at {px:.2f}")
            continue
        inst = get_instrument(session, c["symbol"], c["exchange"])
        if inst is None:
            reject(c, "unknown instrument")
            continue
        # 0.98: headroom so the slippage-adjusted fill still fits the broker's risk-per-trade check
        risk_pct = cfg.risk_per_trade_pct * eff * 0.98
        size = position_size(pstate.equity, px, float(stop), risk_pct, cfg.max_alloc_per_instrument_pct,
                             cash_available=pstate.cash, lot_size=inst.lot_size, max_leverage=cfg.max_leverage)
        if size.qty < 1:
            reject(c, f"position size rounds to 0 ({size.limiting_factor})")
            continue
        proposal = TradeProposal(c["symbol"], c["exchange"], inst.sector, c["direction"], mode, px, float(stop),
                                 None if target is None else float(target), size.qty, c["turnover"])
        dec = check_trade(proposal, pstate, cfg)
        if not dec.approved:
            reject(c, "risk: " + "; ".join(dec.violations))
            continue
        why = (f"{'decision-engine TOP pick, ' if c['source'] == 'decision_engine' else ''}{c['strategy']} "
               f"{c['state']} {c['action']} score {c['score']:.0f}" + (f", R:R {c['rr']}" if c["rr"] else ""))
        out.append(Instruction(
            "ENTER", c["symbol"], c["exchange"], mode, why, direction=c["direction"], signal_id=c["signal_id"],
            qty=size.qty, stop=float(stop), target=None if target is None else float(target), strategy=c["strategy"],
            score=c["score"], order_key=c["order_key"], source=c["source"],
            risk={"entry_estimate": px, "risk_amount": size.risk_amount, "capital": size.capital_required,
                  "allocation_pct": size.allocation_pct, "risk_pct": round(risk_pct, 3), "regime_multiplier": eff,
                  "limiting_factor": size.limiting_factor, "warnings": dec.warnings,
                  "unvalidated": not c["validated"]}))
        # reserve the capacity so later candidates in this cycle are checked against it
        pstate.open_positions.append(OpenPosition(c["symbol"], inst.sector, px * size.qty, size.risk_amount))
        if long:
            pstate.cash -= px * size.qty
        open_symbols.add(c["symbol"])
        per_mode[mode] += 1
    return out, len(cands)


def plan(session: Session, st: AgentState, now: datetime | None = None) -> Plan:
    now = now or now_ist()
    cfg = load_risk_config(session)
    market_open = active_provider().session_state(now) == "OPEN"
    notes: list[str] = []
    rejected: list[dict[str, Any]] = []
    intel = market_intel(session, now)
    instructions = _manage_positions(session, st, now)
    seen = 0
    if intel.get("picks"):
        notes.append("Decision engine TOP picks: " + ", ".join(
            f"{p['symbol']} ({p['mode']} {p['direction']}, {p['state']})" for p in intel["picks"]))
    if st.config.require_market_open and not market_open:
        notes.append("Market closed - managing open positions only, no new entries")
    elif intel["blocking"]:
        notes.extend(f"No new entries - {b}" for b in intel["blocking"])
    else:
        entries, seen = _select_entries(session, st, cfg, intel, now, rejected)
        instructions += entries
        if not entries:
            notes.append("No candidate passed strategy, trigger and risk checks this cycle")
    ps = portfolio_state(session, cfg, now)
    return Plan(generated_at=now.isoformat(timespec="seconds"), market_open=market_open, intel=intel,
                portfolio={"equity": round(ps.equity, 2), "cash": round(ps.cash, 2),
                           "open_positions": len(ps.open_positions), "realized_today": round(ps.realized_pnl_today, 2),
                           "slots": _mode_slots(cfg, st), "at": int(time.time())},
                candidates_seen=seen, instructions=instructions, rejected=rejected[:50], notes=notes)
