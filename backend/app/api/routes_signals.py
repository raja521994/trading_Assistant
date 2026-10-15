"""Scanner, signals, strategies, scoring weights and risk configuration endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.template_explainer import explain_recommendation
from ..backtest.service import latest_evidence
from ..db import get_db
from ..jobs.scheduler import _lock
from ..models import PaperTrade, Signal, SignalEvent
from ..paper.broker import audit, portfolio_state
from ..quant.scoring import COMPONENT_INFO, DEFAULT_WEIGHTS, load_weights, save_weights
from ..quant.strategies.base import all_strategy_ids, get_strategy, load_specs
from ..risk.limits import RiskConfig, TradeProposal, check_trade, load_risk_config, save_risk_config
from ..risk.sizing import position_size
from ..signals.engine import last_scan, run_scan_and_persist
from ..signals.state_machine import ALLOWED, DESCRIPTIONS, STATES

router = APIRouter(prefix="/api")


def _ensure_scan(db: Session) -> dict[str, Any]:
    r = last_scan()
    if not r:
        with _lock:
            r = run_scan_and_persist(db)
            db.commit()
    return r


@router.get("/scanner")
def scanner(mode: str | None = None, exchange: str | None = None, strategy: str | None = None,
            state: str | None = None, action: str | None = None, risk: str | None = None,
            liquidity: str | None = None, min_score: float | None = None, db: Session = Depends(get_db)
            ) -> dict[str, Any]:
    r = _ensure_scan(db)
    recs = r["recommendations"]

    def keep(x: dict[str, Any]) -> bool:
        return ((not mode or mode in x["modes"]) and (not exchange or x["exchange"] == exchange)
                and (not strategy or x["strategy"] == strategy) and (not state or x["state"] == state)
                and (not action or x["action"] == action) and (not risk or x["risk_level"] == risk)
                and (not liquidity or x["liquidity"]["rating"] == liquidity)
                and (min_score is None or x["score"] >= min_score))

    filtered = [x for x in recs if keep(x)]
    counts: dict[str, int] = {}
    for x in recs:
        counts[x["action"]] = counts.get(x["action"], 0) + 1
    return {**{k: v for k, v in r.items() if k != "recommendations"}, "recommendations": filtered,
            "total": len(recs), "counts": counts,
            "filters": {"modes": ["intraday", "swing", "positional", "long-term", "futures", "options"],
                        "unavailable_modes": {"long-term": "Fundamentals provider not connected",
                                              "futures": "Derivatives data not connected",
                                              "options": "Option-chain data not connected"},
                        "exchanges": ["NSE", "BSE"], "strategies": all_strategy_ids(),
                        "states": STATES, "actions": ["BUY", "SELL", "WAIT", "NO TRADE"],
                        "risk_levels": ["LOW", "MEDIUM", "HIGH"], "liquidity": ["HIGH", "MEDIUM", "LOW"]}}


@router.post("/scanner/refresh")
def scanner_refresh(db: Session = Depends(get_db)) -> dict[str, Any]:
    with _lock:
        r = run_scan_and_persist(db)
        db.commit()
    return {"generated_at": r["generated_at"], "count": len(r["recommendations"]), "persisted": r["persisted"],
            "seconds": r["seconds"]}


def _sig_brief(s: Signal) -> dict[str, Any]:
    p = s.payload or {}
    return {"id": s.id, "origin": s.origin, "symbol": s.symbol, "exchange": s.exchange, "mode": s.mode,
            "strategy": s.strategy, "strategy_name": p.get("strategy_name"), "direction": s.direction,
            "action": s.action, "state": s.state, "score": s.score, "generated_at": s.generated_at,
            "updated_at": s.updated_at, "reference_price": p.get("reference_price"), "stop_loss": p.get("stop_loss"),
            "targets": p.get("targets"), "outcome": s.outcome}


@router.get("/signals")
def signals(state: str | None = None, origin: str | None = None, symbol: str | None = None,
            limit: int = Query(200, ge=1, le=2000), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    q = select(Signal)
    if state:
        q = q.where(Signal.state == state)
    if origin:
        q = q.where(Signal.origin == origin)
    if symbol:
        q = q.where(Signal.symbol == symbol)
    return [_sig_brief(s) for s in db.scalars(q.order_by(Signal.updated_at.desc()).limit(limit))]


@router.get("/signals/{signal_id}")
def signal_detail(signal_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    s = db.get(Signal, signal_id)
    if s is None:
        raise HTTPException(404, "Signal not found")
    events = db.scalars(select(SignalEvent).where(SignalEvent.signal_id == s.id).order_by(SignalEvent.ts)).all()
    rec = {**s.payload, "signal_id": s.id, "state": s.state}
    explanation = explain_recommendation(rec).to_dict() if s.origin == "live_scan" else None
    trades = db.scalars(select(PaperTrade).where(PaperTrade.signal_id == s.id)).all()
    return {"signal": _sig_brief(s), "recommendation": rec, "explanation": explanation,
            "events": [{"ts": e.ts, "from": e.from_state, "to": e.to_state, "price": e.price, "reason": e.reason}
                       for e in events],
            "paper_trades": [{"id": t.id, "status": t.status, "qty": t.qty, "entry_price": t.entry_price,
                              "net_pnl": t.net_pnl} for t in trades],
            "state_machine": {"states": STATES, "allowed": {k: sorted(v) for k, v in ALLOWED.items()},
                              "descriptions": DESCRIPTIONS}}


@router.get("/strategies")
def strategies(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    ev = latest_evidence(db)
    out = []
    for sid, spec in load_specs().items():
        strat = get_strategy(sid)
        out.append({**spec, "params": strat.params, "evidence": ev.get(sid, {"status": "pending"})})
    return out


@router.get("/scoring/weights")
def get_weights(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"weights": load_weights(db), "defaults": DEFAULT_WEIGHTS, "components": COMPONENT_INFO,
            "note": "Composite rule score (0-100, 50 = neutral). Not a probability of profit."}


@router.put("/scoring/weights")
def put_weights(weights: dict[str, float], db: Session = Depends(get_db)) -> dict[str, Any]:
    w = save_weights(db, weights)
    audit(db, "scoring_weights_updated", {"weights": w})
    db.commit()
    with _lock:
        run_scan_and_persist(db)
        db.commit()
    return {"weights": w}


@router.get("/risk/config")
def get_risk(db: Session = Depends(get_db)) -> dict[str, Any]:
    cfg = load_risk_config(db)
    st = portfolio_state(db, cfg)
    return {"config": cfg.model_dump(), "portfolio": {"equity": round(st.equity, 2), "cash": round(st.cash, 2),
                                                      "peak_equity": round(st.peak_equity, 2),
                                                      "open_positions": len(st.open_positions)}}


@router.put("/risk/config")
def put_risk(cfg: RiskConfig, db: Session = Depends(get_db)) -> dict[str, Any]:
    old = load_risk_config(db).model_dump()
    save_risk_config(db, cfg)
    changed = {k: {"from": old[k], "to": v} for k, v in cfg.model_dump().items() if old.get(k) != v}
    audit(db, "risk_config_updated", {"changed": changed})
    db.commit()
    with _lock:
        run_scan_and_persist(db)
        db.commit()
    return {"config": cfg.model_dump(), "changed": changed}


class KillSwitch(BaseModel):
    on: bool


@router.post("/risk/kill-switch")
def kill_switch(body: KillSwitch, db: Session = Depends(get_db)) -> dict[str, Any]:
    cfg = load_risk_config(db)
    cfg.kill_switch = body.on
    save_risk_config(db, cfg)
    audit(db, "kill_switch", {"on": body.on})
    db.commit()
    return {"kill_switch": body.on}


class RiskCheckBody(BaseModel):
    symbol: str
    exchange: str = "NSE"
    sector: str | None = None
    direction: str = "LONG"
    mode: str = "swing"
    entry: float
    stop: float
    target: float | None = None
    qty: int | None = None
    avg_turnover_cr: float | None = None


@router.post("/risk/check")
def risk_check(body: RiskCheckBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    cfg = load_risk_config(db)
    st = portfolio_state(db, cfg)
    size = position_size(st.equity, body.entry, body.stop, cfg.risk_per_trade_pct, cfg.max_alloc_per_instrument_pct,
                         cash_available=st.cash, max_leverage=cfg.max_leverage)
    qty = body.qty if body.qty is not None else size.qty
    d = check_trade(TradeProposal(body.symbol, body.exchange, body.sector, body.direction, body.mode, body.entry,
                                  body.stop, body.target, qty, body.avg_turnover_cr), st, cfg)
    return {"sizing": size.to_dict(), "qty": qty, "decision": d.to_dict()}
