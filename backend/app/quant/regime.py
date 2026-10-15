"""Market regime classification from the benchmark index and universe breadth (deterministic)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from . import indicators as ind


def _classify_at(close: pd.Series, high: pd.Series, low: pd.Series, i: int, sma50: pd.Series, sma200: pd.Series,
                 adx: pd.Series, rv_rank: pd.Series, breadth: float | None) -> dict[str, Any]:
    c, s50, s200, a, rk = close.iloc[i], sma50.iloc[i], sma200.iloc[i], adx.iloc[i], rv_rank.iloc[i]
    if np.isnan(s200) or np.isnan(a):
        trend = "UNKNOWN"
    elif c > s50 > s200 and a >= 20:
        trend = "UPTREND"
    elif c < s50 < s200 and a >= 20:
        trend = "DOWNTREND"
    else:
        trend = "RANGE / TRANSITION"
    if np.isnan(rk):
        vol = "UNKNOWN"
    elif rk >= 80:
        vol = "HIGH"
    elif rk <= 20:
        vol = "LOW"
    else:
        vol = "NORMAL"
    b_txt = f"{breadth:.0f}% above 50-DMA" if breadth is not None else "breadth n/a"
    return {
        "as_of": close.index[i].isoformat(), "trend": trend, "volatility": vol,
        "breadth_pct_above_sma50": None if breadth is None else round(breadth, 1),
        "label": f"{trend.title()} · {vol.title()} volatility · {b_txt}",
        "facts": {"close": round(float(c), 2), "sma50": _r(s50), "sma200": _r(s200), "adx14": _r(a),
                  "realized_vol_20d_pct_rank": _r(rk)},
    }


def _r(x: float) -> float | None:
    return None if x is None or np.isnan(x) else round(float(x), 2)


def _breadth_at(universe: dict[Any, pd.DataFrame], ts: pd.Timestamp) -> float | None:
    above = total = 0
    for df in universe.values():
        sub = df["close"][df.index <= ts]
        if len(sub) < 50:
            continue
        total += 1
        above += int(sub.iloc[-1] > sub.iloc[-50:].mean())
    return above / total * 100.0 if total else None


def classify_regime(bench: pd.DataFrame, universe: dict[Any, pd.DataFrame], lookback: int = 20) -> dict[str, Any]:
    if bench is None or len(bench) < 60:
        return {"current": None, "previous": None, "changes": ["Insufficient benchmark history"]}
    close, high, low = bench["close"], bench["high"], bench["low"]
    sma50, sma200 = ind.sma(close, 50), ind.sma(close, 200)
    adx = ind.adx(high, low, close, 14)["adx"]
    rv = ind.realized_vol(close, 20)
    rv_rank = ind.pct_rank(rv, 252)
    n = len(bench)
    cur = _classify_at(close, high, low, n - 1, sma50, sma200, adx, rv_rank, _breadth_at(universe, close.index[-1]))
    j = max(n - 1 - lookback, 0)
    prev = _classify_at(close, high, low, j, sma50, sma200, adx, rv_rank, _breadth_at(universe, close.index[j]))
    changes = []
    for k in ("trend", "volatility"):
        if cur[k] != prev[k]:
            changes.append(f"{k.title()} changed from {prev[k]} to {cur[k]} over the last {lookback} sessions")
    if cur["breadth_pct_above_sma50"] is not None and prev["breadth_pct_above_sma50"] is not None:
        db = cur["breadth_pct_above_sma50"] - prev["breadth_pct_above_sma50"]
        changes.append(f"Breadth (% above 50-DMA) moved {db:+.1f} pts to {cur['breadth_pct_above_sma50']:.1f}%")
    ret = close.iloc[-1] / close.iloc[j] - 1.0
    changes.append(f"Benchmark return over the last {lookback} sessions: {ret * 100:+.2f}%")
    return {"current": cur, "previous": prev, "changes": changes, "lookback_sessions": lookback}
