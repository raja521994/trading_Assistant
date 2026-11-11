"""Agent lifecycle (start/stop), one orchestrator->worker cycle, and the profit report for a run."""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuditLog, PaperTrade
from ..paper.broker import audit, portfolio_state
from ..providers.sample import now_ist
from ..risk.limits import load_risk_config
from . import orchestrator, worker
from .state import MODES, AgentConfig, AgentState, agent_trades, load_state, save_state

IST = "Asia/Kolkata"
ORCH_ACTOR = "agent-orchestrator"


def _ist(epoch: int | None) -> str | None:
    return pd.Timestamp(epoch, unit="s", tz="UTC").tz_convert(IST).isoformat() if epoch else None


def start(session: Session, config: AgentConfig | None = None) -> AgentState:
    st = load_state(session)
    if st.running:
        if config is not None:
            st.config = config
            save_state(session, st)
        return st
    cfg = load_risk_config(session)
    st = AgentState(running=True, session_id=uuid.uuid4().hex[:12], started_at=int(time.time()),
                    starting_equity=round(portfolio_state(session, cfg).equity, 2), config=config or st.config)
    save_state(session, st)
    audit(session, "agent_started", {"session_id": st.session_id, "config": st.config.model_dump(),
                                     "starting_equity": st.starting_equity}, actor=ORCH_ACTOR)
    return st


def stop(session: Session) -> AgentState:
    st = load_state(session)
    if st.running:
        st.running = False
        st.stopped_at = int(time.time())
        save_state(session, st)
        audit(session, "agent_stopped", {"session_id": st.session_id}, actor=ORCH_ACTOR)
    return st


def run_cycle(session: Session, now: datetime | None = None, force: bool = False) -> dict[str, Any] | None:
    """Orchestrator plans, worker executes. Open positions are left as they are when the agent is stopped."""
    st = load_state(session)
    if not st.running and not force:
        return None
    if st.session_id is None:
        st.session_id = uuid.uuid4().hex[:12]
    p = orchestrator.plan(session, st, now)
    results = worker.execute(session, st, p.instructions)
    summary = {"at": int(time.time()), "generated_at": p.generated_at, "market_open": p.market_open,
               "regime": p.intel.get("regime_label"), "decision": p.intel.get("decision"),
               "regime_multiplier": p.intel.get("multiplier"), "candidates_seen": p.candidates_seen,
               "instructions": len(p.instructions), "executed": results, "notes": p.notes,
               "rejected": p.rejected[:15], "portfolio": p.portfolio}
    st = load_state(session)  # the worker may have taken a while; keep any start/stop that happened meanwhile
    st.cycles += 1
    st.last_cycle = summary
    save_state(session, st)
    if p.instructions:
        audit(session, "agent_plan", {"session_id": st.session_id, "notes": p.notes,
                                      "instructions": [i.to_dict() for i in p.instructions]}, actor=ORCH_ACTOR)
    return summary


def _unrealized(t: PaperTrade) -> float:
    if t.status != "OPEN" or t.last_price is None:
        return 0.0
    sign = 1 if t.direction == "LONG" else -1
    return (t.last_price - t.entry_price) * t.qty * sign - t.entry_costs


def _bucket(trades: list[PaperTrade]) -> dict[str, Any]:
    closed = [t for t in trades if t.status == "CLOSED"]
    wins = [t for t in closed if (t.net_pnl or 0) > 0]
    realized = sum(t.net_pnl or 0 for t in closed)
    unreal = sum(_unrealized(t) for t in trades)
    return {"trades": len(trades), "open": len(trades) - len(closed), "closed": len(closed), "wins": len(wins),
            "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
            "realized_pnl": round(realized, 2), "unrealized_pnl": round(unreal, 2),
            "total_pnl": round(realized + unreal, 2),
            "avg_r": round(sum(t.r_multiple or 0 for t in closed) / len(closed), 3) if closed else None}


def _row(t: PaperTrade) -> dict[str, Any]:
    return {"id": t.id, "symbol": t.symbol, "exchange": t.exchange, "strategy": t.strategy, "mode": t.mode,
            "direction": t.direction, "qty": t.qty, "entry_price": t.entry_price, "entry_time": _ist(t.entry_ts),
            "stop": t.stop, "target": t.target, "status": t.status, "last_price": t.last_price,
            "exit_price": t.exit_price, "exit_time": _ist(t.exit_ts), "exit_reason": t.exit_reason,
            "net_pnl": t.net_pnl, "unrealized_pnl": round(_unrealized(t), 2) if t.status == "OPEN" else None,
            "r_multiple": t.r_multiple,
            "rationale": ((t.details or {}).get("agent") or {}).get("rationale"),
            "unvalidated": bool((t.details or {}).get("unvalidated"))}


def report(session: Session, session_id: str | None = None, all_runs: bool = False) -> dict[str, Any]:
    st = load_state(session)
    sid = None if all_runs else (session_id or st.session_id)
    trades = agent_trades(session, sid) if (sid or all_runs) else []
    totals = _bucket(trades)
    start_eq = st.starting_equity if sid == st.session_id and st.starting_equity else load_risk_config(session).account_equity
    daily: dict[str, float] = defaultdict(float)
    for t in trades:
        if t.status == "CLOSED" and t.exit_ts:
            daily[_ist(t.exit_ts)[:10]] += t.net_pnl or 0.0
    cum, curve = 0.0, []
    for d in sorted(daily):
        cum += daily[d]
        curve.append({"date": d, "pnl": round(daily[d], 2), "cumulative": round(cum, 2)})
    by_strategy: dict[str, list[PaperTrade]] = defaultdict(list)
    for t in trades:
        by_strategy[t.strategy].append(t)
    closed = [t for t in trades if t.status == "CLOSED"]
    started = st.started_at if sid == st.session_id else (trades[0].entry_ts if trades else None)
    end = (st.stopped_at if not st.running else None) or int(time.time())
    return {
        "session_id": sid, "all_runs": all_runs, "running": st.running, "started_at": _ist(started),
        "days_running": round((end - started) / 86400, 2) if started else 0.0,
        "starting_equity": start_eq, "totals": totals,
        "return_pct": round(totals["total_pnl"] / start_eq * 100, 3) if start_eq else None,
        "by_mode": {m: _bucket([t for t in trades if t.mode == m]) for m in MODES},
        "by_strategy": sorted(({"strategy": k, **_bucket(v)} for k, v in by_strategy.items()),
                              key=lambda x: -x["total_pnl"]),
        "daily": curve,
        "best_trade": _row(max(closed, key=lambda t: t.net_pnl or 0)) if closed else None,
        "worst_trade": _row(min(closed, key=lambda t: t.net_pnl or 0)) if closed else None,
        "trades": [_row(t) for t in reversed(trades[-200:])],
        "note": "Paper trading only - simulated fills with slippage and Indian transaction costs; no broker orders.",
    }


def activity(session: Session, limit: int = 100) -> list[dict[str, Any]]:
    rows = session.scalars(select(AuditLog).where(AuditLog.actor.in_([ORCH_ACTOR, worker.ACTOR]))
                           .order_by(AuditLog.id.desc()).limit(limit)).all()
    return [{"id": r.id, "time": _ist(r.ts), "actor": r.actor, "action": r.action, "details": r.details} for r in rows]


def status(session: Session) -> dict[str, Any]:
    st = load_state(session)
    return {**st.model_dump(), "started_at_ist": _ist(st.started_at), "stopped_at_ist": _ist(st.stopped_at),
            "now": now_ist().isoformat(timespec="seconds")}
