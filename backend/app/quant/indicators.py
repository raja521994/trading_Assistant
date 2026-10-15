"""Deterministic, causal technical indicators (each value uses only data up to and including that bar).

Conventions (documented so results are reproducible and comparable with TA-Lib-style references):
- EMA: alpha = 2/(n+1), seeded with the SMA of the first n valid values.
- Wilder smoothing (RSI, ATR, ADX): alpha = 1/n, seeded with the simple mean of the first n values.
- RSI: first value at index n (needs n price changes). avg_loss == 0 -> 100 (or 50 if avg_gain == 0 too).
- ATR: TR[0] = high-low; first ATR at index n-1 = mean(TR[0..n-1]).
- Bollinger: population standard deviation (ddof=0).
- VWAP: typical price (H+L+C)/3, cumulative per IST session date.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _seeded_smooth(values: np.ndarray, n: int, alpha: float) -> np.ndarray:
    out = np.full(len(values), np.nan)
    valid = np.flatnonzero(~np.isnan(values))
    if len(valid) < n:
        return out
    f = valid[0]
    if len(values) - f < n:
        return out
    seed_idx = f + n - 1
    out[seed_idx] = np.mean(values[f:f + n])
    prev = out[seed_idx]
    for i in range(seed_idx + 1, len(values)):
        v = values[i]
        if np.isnan(v):
            out[i] = prev
            continue
        prev = alpha * v + (1.0 - alpha) * prev
        out[i] = prev
    return out


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return pd.Series(_seeded_smooth(s.to_numpy(dtype=float), n, 2.0 / (n + 1)), index=s.index)


def rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder's moving average."""
    return pd.Series(_seeded_smooth(s.to_numpy(dtype=float), n, 1.0 / n), index=s.index)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    ag = rma(gain, n).to_numpy()
    al = rma(loss, n).to_numpy()
    out = np.full(len(close), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = ag / al
        out = 100.0 - 100.0 / (1.0 + rs)
    out = np.where((al == 0) & (ag > 0), 100.0, out)
    out = np.where((al == 0) & (ag == 0), 50.0, out)
    out = np.where(np.isnan(ag) | np.isnan(al), np.nan, out)
    return pd.Series(out, index=close.index)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig}, index=close.index)


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    return pd.DataFrame({"mid": mid, "upper": mid + k * sd, "lower": mid - k * sd,
                         "width": (2 * k * sd) / mid}, index=close.index)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    tr.iloc[0] = high.iloc[0] - low.iloc[0]
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    return rma(true_range(high, low, close), n)


def adx(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.DataFrame:
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    plus_dm.iloc[0] = np.nan
    minus_dm.iloc[0] = np.nan
    tr = true_range(high, low, close)
    tr.iloc[0] = np.nan
    s_tr = rma(tr, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * rma(plus_dm, n) / s_tr
        minus_di = 100.0 * rma(minus_dm, n) / s_tr
        denom = plus_di + minus_di
        dx = (100.0 * (plus_di - minus_di).abs() / denom).where(denom > 0, 0.0)
    dx = dx.where(~(plus_di.isna() | minus_di.isna()))
    return pd.DataFrame({"adx": rma(dx, n), "plus_di": plus_di, "minus_di": minus_di}, index=high.index)


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP (resets each IST calendar date). Requires a tz-aware DatetimeIndex."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = tp * df["volume"]
    day = pd.Series(df.index.date, index=df.index)
    cum_pv = pv.groupby(day).cumsum()
    cum_v = df["volume"].groupby(day).cumsum()
    return (cum_pv / cum_v.replace(0, np.nan)).rename("vwap")


def supertrend(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 10, mult: float = 3.0) -> pd.DataFrame:
    a = atr(high, low, close, n).to_numpy()
    h, lo, c = high.to_numpy(float), low.to_numpy(float), close.to_numpy(float)
    hl2 = (h + lo) / 2.0
    bu = hl2 + mult * a
    bl = hl2 - mult * a
    m = len(c)
    fu = np.full(m, np.nan)
    fl = np.full(m, np.nan)
    st = np.full(m, np.nan)
    direction = np.zeros(m)
    start = np.flatnonzero(~np.isnan(a))
    if len(start):
        i0 = start[0]
        fu[i0], fl[i0] = bu[i0], bl[i0]
        direction[i0] = 1 if c[i0] >= hl2[i0] else -1
        st[i0] = fl[i0] if direction[i0] == 1 else fu[i0]
        for i in range(i0 + 1, m):
            fu[i] = bu[i] if (bu[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
            fl[i] = bl[i] if (bl[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
            if direction[i - 1] == -1:
                direction[i] = 1 if c[i] > fu[i] else -1
            else:
                direction[i] = -1 if c[i] < fl[i] else 1
            st[i] = fl[i] if direction[i] == 1 else fu[i]
    return pd.DataFrame({"supertrend": st, "direction": direction, "upper": fu, "lower": fl}, index=close.index)


def donchian(high: pd.Series, low: pd.Series, n: int = 20) -> pd.DataFrame:
    upper = high.rolling(n, min_periods=n).max()
    lower = low.rolling(n, min_periods=n).min()
    return pd.DataFrame({"upper": upper, "lower": lower, "mid": (upper + lower) / 2.0}, index=high.index)


def realized_vol(close: pd.Series, n: int = 20, periods_per_year: int = 252) -> pd.Series:
    return np.log(close).diff().rolling(n, min_periods=n).std(ddof=1) * np.sqrt(periods_per_year)


def pct_rank(s: pd.Series, n: int = 252) -> pd.Series:
    """Percentile rank (0-100) of each value within its trailing window (causal)."""
    return s.rolling(n, min_periods=max(20, n // 4)).apply(lambda w: (w[:-1] < w[-1]).mean() * 100.0 if len(w) > 1
                                                            else np.nan, raw=True)


INDICATOR_CATALOG = {
    "sma": "Simple moving average", "ema": "Exponential moving average (SMA-seeded)",
    "rsi": "Relative Strength Index (Wilder)", "macd": "MACD 12/26/9", "bollinger": "Bollinger Bands 20/2",
    "atr": "Average True Range (Wilder)", "adx": "Average Directional Index (Wilder)",
    "vwap": "Session VWAP (intraday)", "supertrend": "Supertrend 10/3", "donchian": "Donchian channel",
}
