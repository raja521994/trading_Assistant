"""Corporate-action guard: detect unadjusted splits/bonuses in price history and back-adjust them.

A split/bonus shows up as an overnight price ratio close to a standard ratio (2:1, 5:1, 1:1 bonus = 2x, 3:2 = 1.5x ...)
with a large move that a normal session doesn't produce. Already-adjusted data has no such jumps -> no-op.
Detected adjustments are reported so they are visible (data quality), never silently applied.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

STANDARD_RATIOS = [1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 10.0, 20.0]
TOLERANCE = 0.03  # relative tolerance around a standard ratio
MIN_JUMP = 0.2  # ignore moves smaller than 20%


def detect_splits(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df is None or len(df) < 2:
        return []
    prev_close = df["close"].shift(1).to_numpy()
    opn = df["open"].to_numpy()
    out = []
    for i in range(1, len(df)):
        if not prev_close[i] or not opn[i]:
            continue
        r = prev_close[i] / opn[i]
        if abs(r - 1) < MIN_JUMP:
            continue
        for std in STANDARD_RATIOS:
            for cand in (std, 1 / std):  # split (price falls) or reverse split / consolidation (price rises)
                if abs(r / cand - 1) <= TOLERANCE:
                    out.append({"date": df.index[i].isoformat(), "ratio": cand, "observed_ratio": round(float(r), 4)})
                    break
            else:
                continue
            break
    return out


def adjust_for_splits(df: pd.DataFrame) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Back-adjust prices (divide) and volumes (multiply) before each detected split."""
    events = detect_splits(df)
    if not events:
        return df, []
    out = df.copy()
    factor = np.ones(len(out))
    for ev in events:
        pos = out.index.get_loc(pd.Timestamp(ev["date"]))
        factor[:pos] *= ev["ratio"]
    for col in ("open", "high", "low", "close"):
        out[col] = out[col].to_numpy() / factor
    out["volume"] = out["volume"].to_numpy() * factor
    return out, events
