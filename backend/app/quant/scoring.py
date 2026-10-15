"""Transparent, configurable composite scoring.

Each component is a deterministic value in [-1, +1] (positive = supports the trade direction) with a
human-readable detail. The composite is 50 + 50 x weighted mean over AVAILABLE components -> 0..100.
It is a rule-based ranking score, NOT a probability of profit (no calibration has been performed).
"""

from __future__ import annotations

import math
import time
from typing import Any

from sqlalchemy.orm import Session

from ..models import AppSetting

DEFAULT_WEIGHTS: dict[str, float] = {
    "trend": 0.25, "momentum": 0.15, "volume": 0.10, "volatility": 0.10, "relative_strength": 0.15,
    "backtest_evidence": 0.15, "liquidity": 0.10, "fundamentals": 0.0, "derivatives": 0.0, "news_events": 0.0,
}
COMPONENT_INFO: dict[str, dict[str, Any]] = {
    "trend": {"label": "Trend", "available": True,
              "description": "Close vs EMA50 and EMA50 vs EMA200 (intraday: close vs VWAP, EMA20 vs EMA50 on 5m)."},
    "momentum": {"label": "Momentum", "available": True, "description": "RSI(14) distance from 50 and MACD histogram sign."},
    "volume": {"label": "Volume confirmation", "available": True,
               "description": "Latest volume vs prior 20-bar average (direction-neutral)."},
    "volatility": {"label": "Volatility regime", "available": True,
                   "description": "ATR% percentile over 1 year: moderate preferred, extreme penalised."},
    "relative_strength": {"label": "Relative strength", "available": True,
                          "description": "63-session return minus NIFTY 50 63-session return."},
    "backtest_evidence": {"label": "Backtest evidence (OOS)", "available": True,
                          "description": "Walk-forward out-of-sample profit factor of the strategy."},
    "liquidity": {"label": "Liquidity", "available": True,
                  "description": "20-session average traded value vs the configured minimum (log scale)."},
    "fundamentals": {"label": "Fundamental quality", "available": False,
                     "description": "Not connected - no fundamentals provider configured."},
    "derivatives": {"label": "Derivatives positioning", "available": False,
                    "description": "Not connected - no OI / option-chain data."},
    "news_events": {"label": "News & event risk", "available": False,
                    "description": "Not connected - no news or corporate-event feed."},
}
WEIGHTS_KEY = "scoring_weights"


def load_weights(session: Session) -> dict[str, float]:
    row = session.get(AppSetting, WEIGHTS_KEY)
    w = dict(DEFAULT_WEIGHTS)
    if row:
        w.update({k: float(v) for k, v in row.value.items() if k in DEFAULT_WEIGHTS})
    return w


def save_weights(session: Session, weights: dict[str, float]) -> dict[str, float]:
    clean = {k: max(0.0, min(1.0, float(weights.get(k, DEFAULT_WEIGHTS[k])))) for k in DEFAULT_WEIGHTS}
    row = session.get(AppSetting, WEIGHTS_KEY)
    if row is None:
        session.add(AppSetting(key=WEIGHTS_KEY, value=clean, updated_at=int(time.time())))
    else:
        row.value = clean
        row.updated_at = int(time.time())
    session.flush()
    return clean


def _clip(x: float) -> float:
    return max(-1.0, min(1.0, x))


def _valid(*v: float | None) -> bool:
    return all(x is not None and x == x for x in v)


def _sign(x: float) -> float:
    return 1.0 if x > 0 else (-1.0 if x < 0 else 0.0)


def score_components(direction: str, snap: dict[str, float | None], daily: dict[str, float | None],
                     bench_ret_63: float | None, evidence: dict[str, Any] | None, min_turnover_cr: float,
                     intraday: bool, contrarian: list[str]) -> dict[str, dict[str, Any]]:
    d = 1.0 if direction == "LONG" else -1.0
    comp: dict[str, dict[str, Any]] = {}

    c = snap.get("close")
    if intraday and _valid(c, snap.get("vwap"), snap.get("ema20"), snap.get("ema50")):
        v = 0.5 * _sign(c - snap["vwap"]) + 0.5 * _sign(snap["ema20"] - snap["ema50"])
        comp["trend"] = {"value": d * v, "detail": f"Close {c:.2f} vs VWAP {snap['vwap']:.2f}; EMA20 "
                                                   f"{snap['ema20']:.2f} vs EMA50 {snap['ema50']:.2f} (5m)"}
    elif _valid(c, snap.get("ema50"), snap.get("ema200")):
        v = 0.5 * _sign(c - snap["ema50"]) + 0.5 * _sign(snap["ema50"] - snap["ema200"])
        comp["trend"] = {"value": d * v, "detail": f"Close {c:.2f} vs EMA50 {snap['ema50']:.2f}; EMA50 vs "
                                                   f"EMA200 {snap['ema200']:.2f}"}
    else:
        comp["trend"] = {"value": None, "detail": "Insufficient history for trend"}

    if "momentum" in contrarian:
        comp["momentum"] = {"value": None, "detail": "Excluded: contrarian (mean-reversion) strategy expects weak "
                                                     "short-term momentum at entry"}
    elif _valid(snap.get("rsi14"), snap.get("macd_hist")):
        v = 0.6 * _clip((snap["rsi14"] - 50.0) / 25.0) + 0.4 * _sign(snap["macd_hist"])
        comp["momentum"] = {"value": d * v, "detail": f"RSI(14) {snap['rsi14']:.1f}; MACD hist {snap['macd_hist']:.3f}"}
    else:
        comp["momentum"] = {"value": None, "detail": "Insufficient history for momentum"}

    vr = snap.get("vol_ratio")
    comp["volume"] = ({"value": _clip(vr - 1.0), "detail": f"Volume {vr:.2f}x prior 20-bar average"}
                      if _valid(vr) else {"value": None, "detail": "Volume unavailable"})

    rank = daily.get("atr_pct_rank")
    if _valid(rank):
        if rank > 90:
            v, txt = -1.0, "extreme (top decile) - wider stops, gap risk"
        elif rank > 80:
            v, txt = 0.0, "elevated"
        elif rank < 10:
            v, txt = 0.0, "compressed - breakouts may fail or expand sharply"
        else:
            v, txt = 0.5, "moderate"
        comp["volatility"] = {"value": v, "detail": f"Daily ATR% {daily.get('atr_pct', 0):.2f}% at the "
                                                    f"{rank:.0f}th percentile (1y): {txt}"}
    else:
        comp["volatility"] = {"value": None, "detail": "Insufficient history for volatility percentile"}

    r63 = daily.get("ret_63")
    if _valid(r63, bench_ret_63):
        diff = r63 - bench_ret_63
        comp["relative_strength"] = {"value": d * _clip(diff / 0.10),
                                     "detail": f"63-session return {r63 * 100:.1f}% vs NIFTY 50 {bench_ret_63 * 100:.1f}%"}
    else:
        comp["relative_strength"] = {"value": None, "detail": "Benchmark or history unavailable"}

    if not evidence or evidence.get("status") != "ready":
        comp["backtest_evidence"] = {"value": None, "detail": "Walk-forward evidence pending"}
    else:
        n, pf = evidence.get("num_trades") or 0, evidence.get("profit_factor")
        if n < 10 or pf is None:
            comp["backtest_evidence"] = {"value": 0.0, "detail": f"Only {n} OOS trades - insufficient evidence"}
        else:
            comp["backtest_evidence"] = {"value": _clip(pf - 1.0),
                                         "detail": f"OOS profit factor {pf:.2f} over {n} trades (walk-forward)"}

    t20 = daily.get("turnover20")
    if _valid(t20) and t20 > 0:
        cr = t20 / 1e7
        v = _clip(math.log10(cr / max(min_turnover_cr, 0.01)))
        comp["liquidity"] = {"value": v, "detail": f"20-session avg traded value Rs {cr:,.1f} cr "
                                                   f"(minimum Rs {min_turnover_cr} cr)"}
    else:
        comp["liquidity"] = {"value": None, "detail": "Traded value unavailable"}

    for k in ("fundamentals", "derivatives", "news_events"):
        comp[k] = {"value": None, "detail": COMPONENT_INFO[k]["description"]}
    for k, v in comp.items():
        if v["value"] is not None:
            v["value"] = round(float(v["value"]), 3)
    return comp


def composite(components: dict[str, dict[str, Any]], weights: dict[str, float]) -> dict[str, Any]:
    num = den = 0.0
    used, excluded = [], []
    for k, c in components.items():
        w = weights.get(k, 0.0)
        if c["value"] is None or w <= 0:
            excluded.append(k)
            continue
        num += w * c["value"]
        den += w
        used.append(k)
    score = 50.0 + 50.0 * (num / den) if den > 0 else 50.0
    return {"score": round(score, 1), "used": used, "excluded": excluded,
            "note": "Composite rule score 0-100 (50 = neutral). Not a probability of profit."}
