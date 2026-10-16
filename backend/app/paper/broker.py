"""Paper broker: user-initiated simulated trades only. No broker API is ever called.

Every order passes the risk engine; rejections are explained and audit-logged. Idempotency keys
prevent duplicate paper orders. Exits: stop, target, intraday time exit, or manual close.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuditLog, Instrument, PaperTrade, Signal
from ..providers.registry import active_provider
from ..providers.sample import now_ist
from ..quant.universe import features
from ..data.repository import candle_store, get_instrument
from ..risk.costs import order_costs, product_for_mode
from ..risk.limits import OpenPosition, PortfolioState, RiskConfig, TradeProposal, check_trade, load_risk_config
from ..risk.sizing import position_size


class PaperOrderRequest(BaseModel):
    signal_id: int | None = None
    symbol: str | None = None
    exchange: str | None = None
    direction: str | None = Field(None, pattern="^(LONG|SHORT)$")
    mode: str | None = None
    stop: float | None = None
    target: float | None = None
    qty: int | None = Field(None, ge=1)
    idempotency_key: str = Field(..., min_length=8, max_length=96)
    note: str | None = None


class PaperOrderRejected(Exception):
    def __init__(self, message: str, decision: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.decision = decision or {}


def audit(session: Session, action: str, details: dict[str, Any], actor: str = "local-user") -> None:
    session.add(AuditLog(ts=int(time.time()), actor=actor, action=action, details=details))


def _today_bounds(now: datetime) -> tuple[int, int]:
    start = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
    return int(start.timestamp()), int(start.timestamp()) + 86400


def portfolio_state(session: Session, cfg: RiskConfig, now: datetime | None = None) -> PortfolioState:
    now = now or now_ist()
    trades = session.scalars(select(PaperTrade)).all()
    realized = sum(t.net_pnl or 0.0 for t in trades if t.status == "CLOSED")
    lo, hi = _today_bounds(now)
    realized_today = sum(t.net_pnl or 0.0 for t in trades if t.status == "CLOSED" and t.exit_ts and lo <= t.exit_ts < hi)
    open_t = [t for t in trades if t.status == "OPEN"]
    unreal = 0.0
    positions = []
    for t in open_t:
        px = t.last_price or t.entry_price
        sign = 1.0 if t.direction == "LONG" else -1.0
        unreal += (px - t.entry_price) * t.qty * sign - t.entry_costs
        positions.append(OpenPosition(t.symbol, t.sector, t.entry_price * t.qty, abs(t.entry_price - t.stop) * t.qty))
    equity = cfg.account_equity + realized + unreal
    cash = cfg.account_equity + realized - sum(p.value for p, t in zip(positions, open_t, strict=True)
                                               if t.direction == "LONG")
    curve = equity_curve_points(session, cfg)
    peak = max([cfg.account_equity, equity] + [p["equity"] for p in curve])
    return PortfolioState(equity, cash, peak, realized_today, unreal, positions)


def equity_curve_points(session: Session, cfg: RiskConfig) -> list[dict[str, Any]]:
    closed = session.scalars(select(PaperTrade).where(PaperTrade.status == "CLOSED")
                             .order_by(PaperTrade.exit_ts)).all()
    eq = cfg.account_equity
    pts = []
    for t in closed:
        eq += t.net_pnl or 0.0
        pts.append({"ts": t.exit_ts, "equity": round(eq, 2), "trade_id": t.id})
    return pts


def _avg_turnover_cr(session: Session, inst: Instrument) -> float | None:
    df = candle_store.load(session, inst.id, "1d")
    if len(df) < 25:
        return None
    t20 = features((inst.exchange, inst.symbol), df).iloc[-1]["turnover20"]
    return None if t20 != t20 else float(t20) / 1e7


def place_paper_order(session: Session, req: PaperOrderRequest) -> PaperTrade:
    existing = session.scalar(select(PaperTrade).where(PaperTrade.idempotency_key == req.idempotency_key))
    if existing is not None:
        return existing
    cfg = load_risk_config(session)
    provider = active_provider()
    sig: Signal | None = None
    if req.signal_id is not None:
        sig = session.get(Signal, req.signal_id)
        if sig is None:
            raise PaperOrderRejected(f"Signal {req.signal_id} not found")
        if sig.state not in ("SETUP", "TRIGGERED", "ACTIVE"):
            raise PaperOrderRejected(f"Signal is {sig.state}; only SETUP, TRIGGERED or ACTIVE signals can be paper traded")
        if sig.action not in ("BUY", "SELL"):
            raise PaperOrderRejected(f"Signal action is {sig.action}; paper orders require a BUY or SELL action")
        p = sig.payload
        symbol, exchange, direction, mode = sig.symbol, sig.exchange, sig.direction, sig.mode
        stop = req.stop if req.stop is not None else p.get("stop_loss")
        target = req.target if req.target is not None else ((p.get("targets") or [None])[0])
        strategy = sig.strategy
    else:
        if not (req.symbol and req.direction and req.stop):
            raise PaperOrderRejected("Manual paper orders need symbol, direction and stop")
        symbol, exchange, direction = req.symbol, req.exchange or "NSE", req.direction
        mode = req.mode or "swing"
        stop, target, strategy = req.stop, req.target, "manual"
    inst = get_instrument(session, symbol, exchange)
    if inst is None or inst.instrument_type != "EQ":
        raise PaperOrderRejected(f"Unknown tradable instrument {exchange}:{symbol}")
    q = provider.quote(symbol, exchange)
    if q is None:
        raise PaperOrderRejected("No quote available - cannot simulate a fill (fail-safe)")
    from ..risk.costs import liquidity_slippage_bps

    turnover = _avg_turnover_cr(session, inst)
    slip_bps = liquidity_slippage_bps(turnover, q.last_price * (req.qty or 100))
    slip = slip_bps / 10_000.0
    fill = round(q.last_price * (1 + slip) if direction == "LONG" else q.last_price * (1 - slip), 2)
    if stop is None:
        raise PaperOrderRejected("A stop-loss is required for every paper trade")
    state = portfolio_state(session, cfg)
    qty = req.qty
    if qty is None:
        qty = position_size(state.equity, fill, stop, cfg.risk_per_trade_pct, cfg.max_alloc_per_instrument_pct,
                            cash_available=state.cash, lot_size=inst.lot_size, max_leverage=cfg.max_leverage).qty
    proposal = TradeProposal(symbol, exchange, inst.sector, direction, mode, fill, float(stop),
                             None if target is None else float(target), int(qty), _avg_turnover_cr(session, inst))
    decision = check_trade(proposal, state, cfg)
    if not decision.approved:
        audit(session, "paper_order_rejected", {"symbol": symbol, "signal_id": req.signal_id, **decision.to_dict()})
        session.commit()
        raise PaperOrderRejected("Rejected by risk engine: " + "; ".join(decision.violations), decision.to_dict())
    product = product_for_mode(mode)
    entry_costs = order_costs("BUY" if direction == "LONG" else "SELL", fill, qty, product, exchange, cfg.costs)["total"]
    now = now_ist()
    trade = PaperTrade(
        idempotency_key=req.idempotency_key, signal_id=sig.id if sig else None, symbol=symbol, exchange=exchange,
        sector=inst.sector, strategy=strategy, mode=mode, direction=direction, qty=int(qty), entry_price=fill,
        entry_ts=int(now.timestamp()), stop=float(stop), target=None if target is None else float(target),
        status="OPEN", entry_costs=round(entry_costs, 2), exit_costs=0.0, last_price=q.last_price,
        details={"quote_at_entry": q.last_price, "quote_status": q.status.value, "slippage_bps": slip_bps,
                 "product": product, "risk_checks": decision.to_dict(), "note": req.note,
                 "exit_time": (sig.payload.get("expires_at") if sig and mode == "intraday" else None)},
    )
    session.add(trade)
    session.flush()
    audit(session, "paper_order_filled", {"trade_id": trade.id, "symbol": symbol, "direction": direction, "qty": qty,
                                          "fill": fill, "signal_id": req.signal_id, "data_label": q.status.value})
    return trade


def close_paper_trade(session: Session, trade: PaperTrade, price: float, reason: str, now: datetime | None = None) -> None:
    if trade.status != "OPEN":
        return
    cfg = load_risk_config(session)
    now = now or now_ist()
    slip = cfg.slippage_bps / 10_000.0
    long = trade.direction == "LONG"
    px = price if reason.startswith("Target") else (price * (1 - slip) if long else price * (1 + slip))
    px = round(px, 2)
    product = product_for_mode(trade.mode)
    exit_costs = order_costs("SELL" if long else "BUY", px, trade.qty, product, trade.exchange, cfg.costs)["total"]
    gross = (px - trade.entry_price) * trade.qty * (1 if long else -1)
    net = gross - trade.entry_costs - exit_costs
    risk = abs(trade.entry_price - trade.stop) * trade.qty
    trade.status = "CLOSED"
    trade.exit_price = px
    trade.exit_ts = int(now.timestamp())
    trade.exit_reason = reason
    trade.exit_costs = round(exit_costs, 2)
    trade.gross_pnl = round(gross, 2)
    trade.net_pnl = round(net, 2)
    trade.r_multiple = round(net / risk, 3) if risk else None
    trade.last_price = price
    audit(session, "paper_trade_closed", {"trade_id": trade.id, "symbol": trade.symbol, "exit": px, "reason": reason,
                                          "net_pnl": trade.net_pnl})


def update_open_trades(session: Session, now: datetime | None = None) -> int:
    provider = active_provider()
    now = now or now_ist()
    closed = 0
    for t in session.scalars(select(PaperTrade).where(PaperTrade.status == "OPEN")).all():
        q = provider.quote(t.symbol, t.exchange)
        if q is None:
            continue
        px = q.last_price
        t.last_price = px
        long = t.direction == "LONG"
        if (long and px <= t.stop) or (not long and px >= t.stop):
            close_paper_trade(session, t, px, "Stop loss", now)
        elif t.target is not None and ((long and px >= t.target) or (not long and px <= t.target)):
            close_paper_trade(session, t, t.target, "Target", now)
        elif t.mode == "intraday" and (now.time() >= datetime(2000, 1, 1, 15, 15).time()
                                       or datetime.fromtimestamp(t.entry_ts, now.tzinfo).date() != now.date()):
            close_paper_trade(session, t, px, "Intraday time exit (15:15 IST)", now)
        else:
            continue
        closed += 1
    return closed
