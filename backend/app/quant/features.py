"""Common causal features used by strategies, scoring and the UI."""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import indicators as ind


def add_common_features(df: pd.DataFrame, intraday: bool = False) -> pd.DataFrame:
    c, h, lo, v = df["close"], df["high"], df["low"], df["volume"]
    cols: dict[str, pd.Series] = {}
    cols["ema20"] = ind.ema(c, 20)
    cols["ema50"] = ind.ema(c, 50)
    cols["ema200"] = ind.ema(c, 200)
    cols["sma200"] = ind.sma(c, 200)
    cols["rsi14"] = ind.rsi(c, 14)
    m = ind.macd(c)
    cols["macd"], cols["macd_signal"], cols["macd_hist"] = m["macd"], m["signal"], m["hist"]
    a = ind.adx(h, lo, c, 14)
    cols["adx14"], cols["plus_di"], cols["minus_di"] = a["adx"], a["plus_di"], a["minus_di"]
    cols["atr14"] = ind.atr(h, lo, c, 14)
    cols["atr_pct"] = cols["atr14"] / c * 100.0
    prior_vol = v.shift(1).rolling(20, min_periods=20).mean()
    cols["vol_avg20"] = prior_vol
    cols["vol_ratio"] = v / prior_vol.replace(0, np.nan)
    cols["turnover20"] = (c * v).rolling(20, min_periods=20).mean()
    bb = ind.bollinger(c)
    cols["bb_upper"], cols["bb_lower"], cols["bb_width"] = bb["upper"], bb["lower"], bb["width"]
    if intraday:
        cols["vwap"] = ind.vwap(df)
    else:
        cols["ret_21"] = c.pct_change(21)
        cols["ret_63"] = c.pct_change(63)
        cols["atr_pct_rank"] = ind.pct_rank(cols["atr_pct"], 252)
    return pd.concat([df, pd.DataFrame(cols, index=df.index)], axis=1)


def arrays(df: pd.DataFrame) -> dict[str, list]:
    """Column -> Python list (fast scalar access in bar loops). NaN marks unavailable values."""
    a: dict[str, list] = {col: df[col].to_numpy(dtype=float).tolist() for col in df.columns
                          if df[col].dtype.kind in "fiub"}
    a["_date"] = list(df.index.date)
    a["_minute"] = (df.index.hour * 60 + df.index.minute).astype(float).tolist()
    return a
