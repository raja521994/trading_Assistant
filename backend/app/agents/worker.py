"""Worker agent: executes the orchestrator's instructions against the PAPER broker only (no broker API exists)."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from ..models import PaperTrade
from ..paper.broker import PaperOrderRejected, PaperOrderRequest, audit, close_paper_trade, place_paper_order
from ..providers.registry import active_provider
from .orchestrator import Instruction
from .state import AgentState, idempotency_key

log = logging.getLogger(__name__)
ACTOR = "agent-worker"


def _enter(session: Session, st: AgentState, ins: Instruction) -> dict[str, Any]:
    key = ins.order_key or idempotency_key(st.session_id or "manual", ins.signal_id or 0)
    if ins.signal_id is not None:
        req = PaperOrderRequest(signal_id=ins.signal_id, qty=ins.qty, idempotency_key=key, note=f"Agent: {ins.reason}",
                                acknowledge_unvalidated=st.config.allow_unvalidated)
    else:
        req = PaperOrderRequest(symbol=ins.symbol, exchange=ins.exchange, direction=ins.direction, mode=ins.mode,
                                stop=ins.stop, target=ins.target, qty=ins.qty, idempotency_key=key,
                                strategy=(ins.strategy or "decision")[:64], note=f"Agent: {ins.reason}")
    t = place_paper_order(session, req)
    t.details = {**(t.details or {}), "signal_action": t.details.get("signal_action") if ins.signal_id else "TOP PICK",
                 "agent": {"session_id": st.session_id, "initial_stop": t.stop, "source": ins.source,
                           "rationale": ins.reason, "risk": ins.risk}}
    return {"status": "FILLED", "trade_id": t.id, "fill": t.entry_price, "qty": t.qty}


def _exit(session: Session, ins: Instruction) -> dict[str, Any]:
    t = session.get(PaperTrade, ins.trade_id)
    if t is None or t.status != "OPEN":
        return {"status": "SKIPPED", "detail": "trade no longer open"}
    q = active_provider().quote(t.symbol, t.exchange)
    if q is None:
        return {"status": "FAILED", "detail": "no quote to exit at"}
    close_paper_trade(session, t, q.last_price, f"Agent: {ins.reason}")
    return {"status": "CLOSED", "trade_id": t.id, "exit": t.exit_price, "net_pnl": t.net_pnl}


def _tighten(session: Session, ins: Instruction) -> dict[str, Any]:
    t = session.get(PaperTrade, ins.trade_id)
    if t is None or t.status != "OPEN" or ins.stop is None:
        return {"status": "SKIPPED", "detail": "trade no longer open"}
    old = t.stop
    t.stop = float(ins.stop)
    return {"status": "MODIFIED", "trade_id": t.id, "old_stop": old, "new_stop": t.stop}


def execute(session: Session, st: AgentState, instructions: list[Instruction]) -> list[dict[str, Any]]:
    results = []
    for ins in instructions:
        try:
            if ins.kind == "ENTER":
                res = _enter(session, st, ins)
            elif ins.kind == "EXIT":
                res = _exit(session, ins)
            elif ins.kind == "TIGHTEN_STOP":
                res = _tighten(session, ins)
            else:
                res = {"status": "FAILED", "detail": f"unknown instruction {ins.kind}"}
        except PaperOrderRejected as exc:
            res = {"status": "REJECTED", "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001
            log.exception("agent worker failed on %s %s", ins.kind, ins.symbol)
            res = {"status": "FAILED", "detail": f"{type(exc).__name__}: {exc}"}
        row = {"kind": ins.kind, "symbol": ins.symbol, "mode": ins.mode, "direction": ins.direction,
               "strategy": ins.strategy, "reason": ins.reason, "session_id": st.session_id, **res}
        audit(session, "agent_execution", row, actor=ACTOR)
        session.flush()
        results.append(row)
    return results
