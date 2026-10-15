"""Market, chart, metadata and streaming endpoints."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.assistant import llm_status
from ..backtest.engine import BTConfig, run_portfolio
from ..config import get_settings
from ..data.repository import candle_store, get_instrument, list_instruments
from ..data.validation import validate_candles
from ..db import SessionLocal, get_db
from ..jobs.scheduler import status as job_status
from ..models import Signal
from ..providers.registry import active_provider, provider_fallback_reason
from ..providers.sample import market_session_state, now_ist
from ..quant import indicators as ind
from ..quant.overview import market_overview
from ..quant.strategies.base import get_strategy
from ..quant.universe import features
from ..risk.limits import load_risk_config

router = APIRouter(prefix="/api")

DISCLAIMERS = [
    "Research and paper-trading tool. Not investment advice. No returns are guaranteed.",
    "Live order execution is disabled and not implemented in this build.",
    "Scores are rule-based rankings, not probabilities of profit.",
]


@router.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "time": now_ist().isoformat()}


@router.get("/meta")
def meta() -> dict[str, Any]:
    p = active_provider()
    s = get_settings()
    return {
        "app": "AI Trading Dashboard", "version": "0.1.0", "env": s.app_env,
        "provider": p.name, "provider_name": p.display_name, "data_label": p.capabilities().data_status.value,
        "provider_note": provider_fallback_reason(), "live_trading_enabled": False,
        "live_trading_note": "Order placement is not implemented; only paper trading is available.",
        "market_session": market_session_state(now_ist()), "jobs": job_status, "llm": llm_status(),
        "disclaimers": DISCLAIMERS, "server_time_ist": now_ist().isoformat(),
    }


@router.get("/market/overview")
def overview(db: Session = Depends(get_db)) -> dict[str, Any]:
    return market_overview(db)


@router.get("/instruments")
def instruments(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [{"symbol": i.symbol, "exchange": i.exchange, "name": i.name, "type": i.instrument_type,
             "sector": i.sector, "lot_size": i.lot_size, "tick_size": i.tick_size, "provider_token": i.provider_token}
            for i in list_instruments(db)]


def _t(ts: pd.Timestamp, intraday: bool) -> Any:
    return int(ts.timestamp()) + 19800 if intraday else ts.strftime("%Y-%m-%d")


def _line(s: pd.Series, intraday: bool) -> list[dict[str, Any]]:
    s = s.dropna()
    return [{"time": _t(t, intraday), "value": round(float(v), 4)} for t, v in s.items()]


def _swing_levels(df: pd.DataFrame, n: int = 5, max_levels: int = 6) -> list[dict[str, Any]]:
    """Support/resistance = recent confirmed swing highs/lows (pivot with n bars each side)."""
    h, lo, c = df["high"].to_numpy(), df["low"].to_numpy(), float(df["close"].iloc[-1])
    levels = []
    for i in range(len(df) - n - 1, max(n, len(df) - 250), -1):
        if h[i] == max(h[i - n:i + n + 1]):
            levels.append({"price": round(float(h[i]), 2), "kind": "resistance" if h[i] > c else "support",
                           "ts": df.index[i].isoformat(), "method": f"swing high ({n}-bar pivot)"})
        if lo[i] == min(lo[i - n:i + n + 1]):
            levels.append({"price": round(float(lo[i]), 2), "kind": "support" if lo[i] < c else "resistance",
                           "ts": df.index[i].isoformat(), "method": f"swing low ({n}-bar pivot)"})
        if len(levels) >= max_levels * 2:
            break
    levels.sort(key=lambda x: abs(x["price"] - c))
    return levels[:max_levels]


@router.get("/chart/{exchange}/{symbol}")
def chart(exchange: str, symbol: str, timeframe: str = Query("1d", pattern="^(1d|5m)$"),
          strategy: str | None = None, bars: int = Query(400, ge=50, le=5000),
          db: Session = Depends(get_db)) -> dict[str, Any]:
    inst = get_instrument(db, symbol, exchange)
    if inst is None:
        raise HTTPException(404, f"Unknown instrument {exchange}:{symbol}")
    df = candle_store.load(db, inst.id, timeframe)
    if df.empty:
        raise HTTPException(404, "No candles for this timeframe")
    intraday = timeframe != "1d"
    f = features((exchange, symbol), df, intraday=intraday)
    view = f.iloc[-bars:]
    out: dict[str, Any] = {
        "symbol": symbol, "exchange": exchange, "name": inst.name, "timeframe": timeframe,
        "data_label": active_provider().capabilities().data_status.value,
        "time_note": "Intraday times are shown in IST" if intraday else None,
        "candles": [{"time": _t(t, intraday), "open": r.open, "high": r.high, "low": r.low, "close": r.close}
                    for t, r in view[["open", "high", "low", "close"]].iterrows()],
        "volume": [{"time": _t(t, intraday), "value": float(v)} for t, v in view["volume"].items()],
        "indicators": {k: _line(view[k], intraday) for k in ("ema20", "ema50", "ema200", "bb_upper", "bb_lower",
                                                            "rsi14", "macd", "macd_signal", "macd_hist", "adx14")},
        "levels": _swing_levels(view),
        "quality": validate_candles(df, timeframe, active_provider().expected_last_bar(timeframe), now_ist()).to_dict(),
    }
    st = ind.supertrend(view["high"], view["low"], view["close"])
    out["indicators"]["supertrend"] = _line(st["supertrend"], intraday)
    if intraday:
        out["indicators"]["vwap"] = _line(view["vwap"], intraday)
    markers: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    if strategy:
        strat = get_strategy(strategy)
        if strat.meta.timeframe == timeframe:
            cfg = BTConfig(costs=load_risk_config(db).costs, start=view.index[0])
            res = run_portfolio(strat, {(exchange, symbol): df}, cfg)
            for t in res["trades"]:
                trades.append(t.to_dict())
                et, xt = pd.Timestamp(t.entry_ts), pd.Timestamp(t.exit_ts)
                long = t.direction == "LONG"
                markers.append({"time": _t(et, intraday), "position": "belowBar" if long else "aboveBar",
                                "color": "#22c55e" if long else "#ef4444", "shape": "arrowUp" if long else "arrowDown",
                                "text": f"{'BUY' if long else 'SELL'} {t.entry_price}"})
                markers.append({"time": _t(xt, intraday), "position": "aboveBar" if long else "belowBar",
                                "color": "#a3a3a3", "shape": "circle", "text": f"Exit {t.exit_reason}"})
            markers.sort(key=lambda m: m["time"])
    out["markers"] = markers
    out["strategy_trades"] = trades
    sigs = db.scalars(select(Signal).where(Signal.symbol == symbol, Signal.exchange == exchange,
                                           Signal.origin == "live_scan").order_by(Signal.updated_at.desc()).limit(10))
    out["signals"] = [{"id": s.id, "strategy": s.strategy, "state": s.state, "action": s.action,
                       "stop_loss": s.payload.get("stop_loss"), "targets": s.payload.get("targets"),
                       "entry": s.payload.get("entry"), "timeframe": s.payload.get("timeframe")} for s in sigs]
    return out


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    """Server-sent events: quotes for headline indices, market session and job status every few seconds."""

    async def gen():  # noqa: ANN202
        while True:
            if await request.is_disconnected():
                break
            payload = await asyncio.to_thread(_snapshot)
            yield f"event: tick\ndata: {json.dumps(payload)}\n\n"
            await asyncio.sleep(get_settings().quote_refresh_seconds)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _snapshot() -> dict[str, Any]:
    p = active_provider()
    now = now_ist()
    db = SessionLocal()
    try:
        idx = [i for i in list_instruments(db, "INDEX")]
        quotes = []
        for i in idx:
            q = p.quote(i.symbol, i.exchange)
            if q:
                quotes.append({"symbol": i.symbol, "exchange": i.exchange, "last": q.last_price,
                               "change_pct": round(q.change_pct, 3), "market_open": q.market_open,
                               "status": q.status.value})
    finally:
        db.close()
    return {"ts": now.isoformat(), "market_session": market_session_state(now), "quotes": quotes,
            "data_label": p.capabilities().data_status.value, "jobs": {k: job_status[k] for k in
                                                                        ("startup", "evidence", "last_tick")}}
