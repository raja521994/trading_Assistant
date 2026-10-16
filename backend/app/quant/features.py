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


class LazyArrays(dict):
    """Column -> Python list, converted on first access (fast scalar access in bar loops, without materialising
    the ~40 feature columns a strategy never reads). NaN marks unavailable values."""

    def __init__(self, df: pd.DataFrame) -> None:
        super().__init__()
        self._df = df

    def __missing__(self, key: str) -> list:
        df = self._df
        if key == "_date":
            v = list(df.index.date)
        elif key == "_minute":
            v = (df.index.hour * 60 + df.index.minute).astype(float).tolist()
        elif key in df.columns and df[key].dtype.kind in "fiub":
            v = df[key].to_numpy(dtype=float).tolist()
        else:
            raise KeyError(key)
        self[key] = v
        return v

    def __contains__(self, key: object) -> bool:
        return dict.__contains__(self, key) or key in ("_date", "_minute") or (
            key in self._df.columns and self._df[key].dtype.kind in "fiub")

    def keys(self):  # noqa: ANN201
        return [c for c in self._df.columns if self._df[c].dtype.kind in "fiub"] + ["_date", "_minute"]

    def get(self, key, default=None):  # noqa: ANN001, ANN201
        return self[key] if key in self else default


def arrays(df: pd.DataFrame) -> dict[str, list]:
    return LazyArrays(df)
