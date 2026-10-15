"""Backtesting, paper trading, performance, AI assistant and integration-status endpoints."""

from __future__ import annotations

import csv
import io
import os
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.assistant import answer, llm_status, suggested_questions
from ..backtest.service import BacktestRequest, latest_evidence, run_backtest
from ..config import get_settings, update_env_file
from ..data.coverage import coverage_matrix
from ..db import get_db
from ..jobs.scheduler import _lock, switch_provider, status as job_status
from ..logging_setup import add_secret
from ..models import AuditLog, BacktestRun, PaperTrade, Signal
from ..paper.broker import (
    PaperOrderRejected,
    PaperOrderRequest,
    audit,
    close_paper_trade,
    equity_curve_points,
    place_paper_order,
    portfolio_state,
)
from ..providers.kite import KiteError, exchange_request_token
from ..providers.kite import login_url as kite_login_url
from ..providers.registry import active_provider, all_providers, listed_providers, provider_fallback_reason
from ..providers.sample import now_ist
from ..providers.upstox import validate_token
from ..risk.limits import load_risk_config

router = APIRouter(prefix="/api")
IST = "Asia/Kolkata"


def _ist(epoch: int | None) -> str | None:
    return pd.Timestamp(epoch, unit="s", tz="UTC").tz_convert(IST).isoformat() if epoch else None


# ---------------- Backtesting ----------------
@router.post("/backtest")
def backtest(req: BacktestRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        out = run_backtest(db, req)
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    return out


@router.get("/backtest/runs")
def backtest_runs(kind: str = "user", limit: int = Query(50, le=500), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(select(BacktestRun).where(BacktestRun.kind == kind).order_by(BacktestRun.id.desc()).limit(limit))
    return [{"id": r.id, "created_at": _ist(r.created_at), "strategy": r.strategy, "params": r.params,
             "summary": {k: r.summary.get(k) for k in ("num_trades", "net_return_pct", "max_drawdown_pct", "sharpe",
                                                       "win_rate_pct", "profit_factor", "status")}} for r in rows]


@router.get("/backtest/runs/{run_id}")
def backtest_run(run_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    r = db.get(BacktestRun, run_id)
    if r is None:
        raise HTTPException(404, "Run not found")
    return {"id": r.id, "kind": r.kind, "created_at": _ist(r.created_at), "summary": r.summary, "result": r.result}


@router.get("/backtest/runs/{run_id}/trades.csv")
def backtest_trades_csv(run_id: int, db: Session = Depends(get_db)) -> Response:
    r = db.get(BacktestRun, run_id)
    if r is None:
        raise HTTPException(404, "Run not found")
    trades = r.result.get("trades") or r.result.get("oos_trades") or []
    buf = io.StringIO()
    if trades:
        cols = [k for k in trades[0] if k != "signal_reasons"]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(trades)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="backtest_{run_id}_trades.csv"'})


@router.get("/backtest/evidence")
def evidence(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"status": job_status["evidence"], "evidence": latest_evidence(db)}


# ---------------- Paper trading ----------------
def _trade(t: PaperTrade) -> dict[str, Any]:
    sign = 1 if t.direction == "LONG" else -1
    unreal = None
    if t.status == "OPEN" and t.last_price is not None:
        unreal = round((t.last_price - t.entry_price) * t.qty * sign - t.entry_costs, 2)
    return {"id": t.id, "signal_id": t.signal_id, "symbol": t.symbol, "exchange": t.exchange, "sector": t.sector,
            "strategy": t.strategy, "mode": t.mode, "direction": t.direction, "qty": t.qty,
            "entry_price": t.entry_price, "entry_time": _ist(t.entry_ts), "stop": t.stop, "target": t.target,
            "status": t.status, "last_price": t.last_price, "unrealized_pnl": unreal, "exit_price": t.exit_price,
            "exit_time": _ist(t.exit_ts), "exit_reason": t.exit_reason, "costs": round(t.entry_costs + t.exit_costs, 2),
            "gross_pnl": t.gross_pnl, "net_pnl": t.net_pnl, "r_multiple": t.r_multiple,
            "value": round(t.entry_price * t.qty, 2), "data_label": (t.details or {}).get("quote_status")}


@router.post("/paper/orders")
def paper_order(req: PaperOrderRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        with _lock:
            t = place_paper_order(db, req)
            db.commit()
    except PaperOrderRejected as exc:
        raise HTTPException(422, {"message": str(exc), "decision": exc.decision}) from exc
    return {"trade": _trade(t), "note": "Simulated paper fill - no order was sent to any broker."}


@router.get("/paper/trades")
def paper_trades(status: str | None = None, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    q = select(PaperTrade)
    if status:
        q = q.where(PaperTrade.status == status)
    return [_trade(t) for t in db.scalars(q.order_by(PaperTrade.id.desc()))]


@router.post("/paper/trades/{trade_id}/close")
def paper_close(trade_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    t = db.get(PaperTrade, trade_id)
    if t is None:
        raise HTTPException(404, "Trade not found")
    if t.status != "OPEN":
        raise HTTPException(409, "Trade already closed")
    q = active_provider().quote(t.symbol, t.exchange)
    if q is None:
        raise HTTPException(503, "No quote available")
    with _lock:
        close_paper_trade(db, t, q.last_price, "Manual close")
        db.commit()
    return {"trade": _trade(t)}


@router.get("/paper/summary")
def paper_summary(db: Session = Depends(get_db)) -> dict[str, Any]:
    cfg = load_risk_config(db)
    st = portfolio_state(db, cfg)
    trades = db.scalars(select(PaperTrade)).all()
    closed = [t for t in trades if t.status == "CLOSED"]
    open_t = [t for t in trades if t.status == "OPEN"]
    wins = [t for t in closed if (t.net_pnl or 0) > 0]
    sectors: dict[str, float] = defaultdict(float)
    for t in open_t:
        sectors[t.sector or "Other"] += t.entry_price * t.qty
    return {
        "starting_equity": cfg.account_equity, "equity": round(st.equity, 2), "cash": round(st.cash, 2),
        "realized_pnl": round(sum(t.net_pnl or 0 for t in closed), 2), "unrealized_pnl": round(st.unrealized_pnl, 2),
        "open_positions": len(open_t), "closed_trades": len(closed),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 2) if closed else None,
        "allocation": [{"sector": k, "value": round(v, 2), "pct_of_equity": round(v / st.equity * 100, 2)}
                       for k, v in sorted(sectors.items(), key=lambda kv: -kv[1])],
        "invested_pct": round(sum(sectors.values()) / st.equity * 100, 2) if st.equity else 0,
        "risk_at_stops": round(sum(p.risk for p in st.open_positions), 2),
        "equity_curve": [{"time": _ist(p["ts"]), "equity": p["equity"]} for p in equity_curve_points(db, cfg)],
        "note": "Paper account only. Holdings/positions from a broker account are not connected.",
    }


# ---------------- Risk & performance ----------------
@router.get("/performance")
def performance(db: Session = Depends(get_db)) -> dict[str, Any]:
    cfg = load_risk_config(db)
    st = portfolio_state(db, cfg)
    trades = db.scalars(select(PaperTrade)).all()
    closed = sorted([t for t in trades if t.status == "CLOSED"], key=lambda t: t.exit_ts or 0)
    daily: dict[str, float] = defaultdict(float)
    for t in closed:
        d = pd.Timestamp(t.exit_ts, unit="s", tz="UTC").tz_convert(IST).date().isoformat()
        daily[d] += t.net_pnl or 0.0
    today = now_ist().date().isoformat()
    daily_rows = [{"date": d, "realized_pnl": round(v, 2)} for d, v in sorted(daily.items())]
    eq, peak, dd = cfg.account_equity, cfg.account_equity, []
    for r in daily_rows:
        eq += r["realized_pnl"]
        peak = max(peak, eq)
        dd.append({"date": r["date"], "equity": round(eq, 2), "drawdown_pct": round((eq / peak - 1) * 100, 3)})
    attribution: dict[str, dict[str, float]] = defaultdict(lambda: {"trades": 0, "net_pnl": 0.0, "wins": 0})
    for t in closed:
        a = attribution[t.strategy]
        a["trades"] += 1
        a["net_pnl"] += t.net_pnl or 0.0
        a["wins"] += 1 if (t.net_pnl or 0) > 0 else 0
    sigs = [s for s in db.scalars(select(Signal).where(Signal.outcome.is_not(None),
                                                     Signal.provider == active_provider().name)).all() if s.outcome]
    outcomes: dict[tuple[str, str], dict[str, Any]] = {}
    for s in sigs:
        o = outcomes.setdefault((s.origin, s.strategy), {"origin": s.origin, "strategy": s.strategy, "count": 0,
                                                         "target": 0, "stop": 0, "other": 0, "r_sum": 0.0, "r_n": 0})
        o["count"] += 1
        res = (s.outcome or {}).get("result", "other")
        o["target" if res == "target" else "stop" if res == "stop" else "other"] += 1
        r = (s.outcome or {}).get("r_multiple")
        if r is not None:
            o["r_sum"] += r
            o["r_n"] += 1
    outcome_rows = [{**{k: v for k, v in o.items() if k not in ("r_sum", "r_n")},
                     "avg_r": round(o["r_sum"] / o["r_n"], 3) if o["r_n"] else None} for o in outcomes.values()]
    breaches = db.scalars(select(AuditLog).where(AuditLog.action == "paper_order_rejected")
                          .order_by(AuditLog.id.desc()).limit(50)).all()
    sector_exp: dict[str, float] = defaultdict(float)
    for p in st.open_positions:
        sector_exp[p.sector or "Other"] += p.value
    dd_now = (st.peak_equity - st.equity) / st.peak_equity * 100 if st.peak_equity else 0.0
    loss_today = -(st.realized_pnl_today + min(st.unrealized_pnl, 0.0))
    return {
        "equity": round(st.equity, 2), "peak_equity": round(st.peak_equity, 2),
        "daily_pnl": daily_rows, "today": {"date": today, "realized": round(st.realized_pnl_today, 2),
                                           "unrealized": round(st.unrealized_pnl, 2)},
        "equity_drawdown": dd,
        "risk": {
            "open_positions": len(st.open_positions), "max_open_positions": cfg.max_open_positions,
            "gross_exposure": round(sum(p.value for p in st.open_positions), 2),
            "risk_at_stops": round(sum(p.risk for p in st.open_positions), 2),
            "sector_exposure": [{"sector": k, "value": round(v, 2), "pct": round(v / st.equity * 100, 2),
                                 "limit_pct": cfg.max_sector_exposure_pct} for k, v in sector_exp.items()],
            "drawdown_pct": round(dd_now, 3), "max_drawdown_pct": cfg.max_drawdown_pct,
            "daily_loss": round(max(loss_today, 0.0) + 0.0, 2),
            "daily_loss_limit": round(st.equity * cfg.max_daily_loss_pct / 100, 2),
            "kill_switch": cfg.kill_switch,
        },
        "strategy_attribution": [{"strategy": k, **{kk: round(vv, 2) for kk, vv in v.items()}}
                                 for k, v in attribution.items()],
        "recommendation_outcomes": sorted(outcome_rows, key=lambda r: (r["origin"], r["strategy"])),
        "risk_breaches": [{"ts": _ist(b.ts), **b.details} for b in breaches],
        "notes": ["Recommendation outcomes include 'historical_replay' signals (strategy rules replayed over recent "
                  "stored bars) and 'live_scan' signals tracked forward. Both use SIMULATED data in sample mode."],
    }


# ---------------- AI assistant ----------------
class AskBody(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)
    signal_id: int | None = None


@router.post("/assistant/ask")
def ask(body: AskBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    return answer(db, body.question, body.signal_id)


@router.get("/assistant/meta")
def assistant_meta() -> dict[str, Any]:
    return {"suggested": suggested_questions(), "llm": llm_status(),
            "guardrails": ["Answers are built from stored data, scanner output, risk calculations and backtests.",
                           "LLM output (if configured) may only interpret supplied facts; any new number causes rejection.",
                           "The assistant cannot place, modify or cancel orders and cannot change risk limits."]}


# ---------------- Integrations ----------------
@router.get("/integrations")
def integrations(db: Session = Depends(get_db)) -> dict[str, Any]:
    s = get_settings()
    providers = []
    for name, p in listed_providers().items():
        caps = p.capabilities()
        h = p.health()
        providers.append({
            "name": name, "display_name": p.display_name, "enabled": p.enabled, "active": p is active_provider(),
            "health": {"connected": h.connected, "status": h.status.value, "message": h.message,
                       "latency_ms": h.latency_ms, "last_update": h.last_update.isoformat() if h.last_update else None,
                       "errors": h.errors},
            "capabilities": {k: v for k, v in caps.__dict__.items() if k != "data_status"} | {
                "data_status": caps.data_status.value},
            "missing_requirements": getattr(p, "missing_requirements", lambda: [])(),
        })
    not_connected = [
        {"name": "F&O terminal (option chain, OI, IV, Greeks)", "reason": "No derivatives data provider connected"},
        {"name": "Fundamentals & financial statements", "reason": "No fundamentals provider connected"},
        {"name": "Corporate actions / earnings calendar", "reason": "No corporate-events provider connected"},
        {"name": "News & exchange announcements", "reason": "No authorised news provider connected"},
        {"name": "Broker holdings / positions / orders", "reason": "Kite read-only account endpoints not wired; "
                                                                   "order execution not implemented"},
        {"name": "WebSocket streaming (KiteTicker)", "reason": "Not wired in this build"},
        {"name": "Machine-learning models", "reason": "Out of scope for this build"},
    ]
    return {"configured_provider": s.data_provider, "active_provider": active_provider().name,
            "fallback_note": provider_fallback_reason(), "providers": providers, "coverage": coverage_matrix(db),
            "not_connected": not_connected, "llm": llm_status(), "jobs": job_status,
            "execution": {"live_trading_enabled": False, "paper_trading": True,
                          "note": "No order-placement code exists. Paper fills are simulated locally."},
            "kite_setup": [
                "In https://developers.kite.trade create a Kite Connect app with Redirect URL http://127.0.0.1:5173 "
                "(its plan must include market data / historical data).",
                "Paste the app's API key and API secret below and click Save (stored server-side only).",
                "Click 'Login with Zerodha' and sign in on kite.zerodha.com; you are redirected back and the app "
                "exchanges the one-time request token for today's access token.",
                "Zerodha resets access tokens daily (~06:00 IST) - log in again each trading day.",
                "Never enter your Kite password, PIN or TOTP into this application.",
            ],
            "server_time": datetime.now().isoformat(), "checked_at": int(time.time())}


class TokenBody(BaseModel):
    token: str = Field(..., min_length=20, max_length=4096)


@router.post("/integrations/upstox/token")
def set_upstox_token(body: TokenBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Validate a read-only Upstox Analytics Token, store it server-side (backend/.env) and switch to live data.
    The token is never returned, logged or stored in the browser."""
    token = body.token.strip()
    ok, msg = validate_token(token)
    if not ok:
        audit(db, "upstox_token_rejected", {"reason": msg})
        db.commit()
        raise HTTPException(400, msg)
    add_secret(token)
    update_env_file("UPSTOX_ACCESS_TOKEN", token)
    update_env_file("DATA_PROVIDER", "upstox")
    os.environ["UPSTOX_ACCESS_TOKEN"] = token
    os.environ["DATA_PROVIDER"] = "upstox"
    audit(db, "upstox_token_saved", {"token_hint": f"...{token[-4:]}"})
    db.commit()
    switch_provider()
    return {"connected": True, "message": msg + ". Loading live history in the background (about a minute).",
            "token_hint": f"...{token[-4:]}"}


@router.delete("/integrations/upstox/token")
def delete_upstox_token(db: Session = Depends(get_db)) -> dict[str, Any]:
    update_env_file("UPSTOX_ACCESS_TOKEN", None)
    os.environ.pop("UPSTOX_ACCESS_TOKEN", None)
    if get_settings().data_provider == "upstox":
        update_env_file("DATA_PROVIDER", "yahoo")
        os.environ["DATA_PROVIDER"] = "yahoo"
    audit(db, "upstox_token_removed", {})
    db.commit()
    switch_provider()
    return {"connected": False}


class ProviderBody(BaseModel):
    name: str


# ---------------- Zerodha Kite Connect (official login flow) ----------------
class KiteKeysBody(BaseModel):
    api_key: str = Field(..., min_length=6, max_length=64, pattern=r"^[A-Za-z0-9]+$")
    api_secret: str = Field(..., min_length=6, max_length=128, pattern=r"^[A-Za-z0-9]+$")


class KiteSessionBody(BaseModel):
    request_token: str = Field(..., min_length=6, max_length=128, pattern=r"^[A-Za-z0-9]+$")


def _kite_status() -> dict[str, Any]:
    s = get_settings()
    p = all_providers()["kite"]
    return {"keys_saved": bool(s.kite_api_key and s.kite_api_secret),
            "api_key_hint": f"...{s.kite_api_key.get_secret_value()[-4:]}" if s.kite_api_key else None,
            "logged_in": p.enabled, "token_created": s.kite_token_created,
            "missing": getattr(p, "missing_requirements", lambda: [])(),
            "login_url": kite_login_url(s.kite_api_key.get_secret_value()) if s.kite_api_key else None,
            "redirect_url_hint": "Register http://127.0.0.1:5173 as the Redirect URL of your Kite Connect app"}


@router.get("/integrations/kite")
def kite_status() -> dict[str, Any]:
    return _kite_status()


@router.post("/integrations/kite/keys")
def kite_save_keys(body: KiteKeysBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Store the Kite Connect API key + secret server-side (backend/.env). The secret is never returned."""
    add_secret(body.api_secret)
    update_env_file("KITE_API_KEY", body.api_key)
    update_env_file("KITE_API_SECRET", body.api_secret)
    update_env_file("DATA_PROVIDER", "kite")
    os.environ.update({"KITE_API_KEY": body.api_key, "KITE_API_SECRET": body.api_secret, "DATA_PROVIDER": "kite"})
    audit(db, "kite_keys_saved", {"api_key_hint": f"...{body.api_key[-4:]}"})
    db.commit()
    switch_provider()
    return _kite_status()


@router.post("/integrations/kite/session")
def kite_create_session(body: KiteSessionBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Exchange the one-time request_token (from Zerodha's redirect) for today's access token."""
    s = get_settings()
    if not (s.kite_api_key and s.kite_api_secret):
        raise HTTPException(400, "Save your Kite API key and secret first")
    try:
        data = exchange_request_token(s.kite_api_key.get_secret_value(), s.kite_api_secret.get_secret_value(),
                                      body.request_token)
    except KiteError as exc:
        audit(db, "kite_login_failed", {"error": str(exc)})
        db.commit()
        raise HTTPException(400, f"Zerodha login failed: {exc.message or exc}") from exc
    token = data["access_token"]
    add_secret(token)
    created = now_ist().isoformat()
    update_env_file("KITE_ACCESS_TOKEN", token)
    update_env_file("KITE_TOKEN_CREATED", created)
    update_env_file("DATA_PROVIDER", "kite")
    os.environ.update({"KITE_ACCESS_TOKEN": token, "KITE_TOKEN_CREATED": created, "DATA_PROVIDER": "kite"})
    audit(db, "kite_login", {"user_id": data.get("user_id")})
    db.commit()
    switch_provider()
    return {**_kite_status(), "logged_in": True, "user_id": data.get("user_id"), "user_name": data.get("user_name"),
            "message": "Logged in to Zerodha. Loading live market history in the background (about a minute)."}


@router.delete("/integrations/kite/session")
def kite_logout(db: Session = Depends(get_db)) -> dict[str, Any]:
    update_env_file("KITE_ACCESS_TOKEN", None)
    update_env_file("KITE_TOKEN_CREATED", None)
    os.environ.pop("KITE_ACCESS_TOKEN", None)
    os.environ.pop("KITE_TOKEN_CREATED", None)
    audit(db, "kite_logout", {})
    db.commit()
    switch_provider()
    return _kite_status()


@router.post("/integrations/provider")
def set_provider(body: ProviderBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Choose the active market-data provider (persisted to backend/.env)."""
    p = all_providers().get(body.name)
    if p is None:
        raise HTTPException(400, f"Unknown provider '{body.name}'")
    if not p.enabled:
        raise HTTPException(400, f"{p.display_name} needs credentials first")
    update_env_file("DATA_PROVIDER", body.name)
    os.environ["DATA_PROVIDER"] = body.name
    audit(db, "data_provider_changed", {"provider": body.name})
    db.commit()
    switch_provider()
    return {"active": body.name}
