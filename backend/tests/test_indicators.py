"""Indicator reference tests with hand-computed expected values."""

import math

import numpy as np
import pandas as pd
import pytest

from app.quant import indicators as ind


def s(values):
    return pd.Series(values, dtype=float)


def test_sma_basic():
    out = ind.sma(s([1, 2, 3, 4, 5]), 3)
    assert out.isna().tolist()[:2] == [True, True]
    assert out.iloc[2:].tolist() == [2.0, 3.0, 4.0]


def test_ema_sma_seeded():
    out = ind.ema(s([1, 2, 3, 4, 5, 6]), 3)
    assert np.isnan(out.iloc[0]) and np.isnan(out.iloc[1])
    assert out.iloc[2:].tolist() == pytest.approx([2.0, 3.0, 4.0, 5.0])


def test_rsi_wilder_alternating():
    out = ind.rsi(s([10, 11, 10, 11, 10]), 2)
    assert np.isnan(out.iloc[0]) and np.isnan(out.iloc[1])
    assert out.iloc[2:].tolist() == pytest.approx([50.0, 75.0, 37.5])


def test_rsi_monotonic_extremes():
    assert ind.rsi(s(range(1, 20)), 14).iloc[-1] == 100.0
    assert ind.rsi(s(range(20, 1, -1)), 14).iloc[-1] == pytest.approx(0.0)
    assert ind.rsi(s([5.0] * 20), 14).iloc[-1] == 50.0


def test_macd_linear_series_constant_spread():
    # For x_t = t with SMA-seeded EMAs, EMA lag is exactly (n-1)/2 -> MACD = 12.5 - 5.5 = 7
    out = ind.macd(s(np.arange(60)), 12, 26, 9)
    assert out["macd"].iloc[:25].isna().all()
    assert out["macd"].iloc[25:].tolist() == pytest.approx([7.0] * 35)
    assert out["signal"].iloc[:33].isna().all()
    assert out["signal"].iloc[33:].tolist() == pytest.approx([7.0] * 27)
    assert out["hist"].iloc[33:].abs().max() == pytest.approx(0.0, abs=1e-9)


def test_bollinger_population_std():
    out = ind.bollinger(s([1, 2, 3, 4, 5]), 5, 2.0)
    assert out["mid"].iloc[-1] == pytest.approx(3.0)
    assert out["upper"].iloc[-1] == pytest.approx(3.0 + 2 * math.sqrt(2))
    assert out["lower"].iloc[-1] == pytest.approx(3.0 - 2 * math.sqrt(2))


def test_true_range_gap_and_atr():
    tr = ind.true_range(s([10, 15]), s([9, 14]), s([9.5, 14.5]))
    assert tr.tolist() == pytest.approx([1.0, 5.5])
    h, lo, c = s([11, 12, 13, 14]), s([9, 10, 11, 12]), s([10, 11, 12, 13])
    a = ind.atr(h, lo, c, 2)
    assert np.isnan(a.iloc[0])
    assert a.iloc[1:].tolist() == pytest.approx([2.0, 2.0, 2.0])
    # Wilder smoothing after a TR shock: TR = [2, 2, 6] -> ATR(2) at idx2 = (2*1 + 6)/2 = 4
    a2 = ind.atr(s([11, 12, 16]), s([9, 10, 10]), s([10, 11, 15]), 2)
    assert a2.iloc[2] == pytest.approx(4.0)


def test_adx_pure_uptrend_is_100():
    n = 3
    i = np.arange(12, dtype=float)
    out = ind.adx(s(10 + i), s(8 + i), s(9 + i), n)
    assert out["adx"].iloc[: 2 * n - 1].isna().all()
    assert out["adx"].iloc[2 * n - 1:].tolist() == pytest.approx([100.0] * (12 - (2 * n - 1)))
    assert out["plus_di"].iloc[-1] == pytest.approx(50.0)
    assert out["minus_di"].iloc[-1] == pytest.approx(0.0)


def test_vwap_resets_each_session():
    idx = pd.DatetimeIndex(["2024-01-01 09:15", "2024-01-01 09:20", "2024-01-02 09:15"]).tz_localize("Asia/Kolkata")
    df = pd.DataFrame({"high": [10, 20, 30], "low": [10, 20, 30], "close": [10, 20, 30],
                       "volume": [100, 300, 50]}, index=idx, dtype=float)
    out = ind.vwap(df)
    assert out.tolist() == pytest.approx([10.0, 17.5, 30.0])


def test_supertrend_direction_follows_trend():
    i = np.arange(60, dtype=float)
    up = ind.supertrend(s(101 + i), s(99 + i), s(100 + i), 10, 3.0)
    assert up["direction"].iloc[-1] == 1
    assert (up["supertrend"].iloc[15:] < (100 + i)[15:]).all()
    dn = ind.supertrend(s(201 - i), s(199 - i), s(200 - i), 10, 3.0)
    assert dn["direction"].iloc[-1] == -1
    assert (dn["supertrend"].iloc[20:] > (200 - i)[20:]).all()


def test_supertrend_flips_on_reversal():
    i = np.arange(40, dtype=float)
    close = np.concatenate([100 + i, 139 - 3 * i])
    out = ind.supertrend(s(close + 1), s(close - 1), s(close), 10, 3.0)
    assert out["direction"].iloc[39] == 1
    assert out["direction"].iloc[-1] == -1


def test_donchian():
    out = ind.donchian(s([1, 5, 3, 4, 2]), s([0, 2, 1, 3, 1]), 3)
    assert out["upper"].iloc[2:].tolist() == [5, 5, 4]
    assert out["lower"].iloc[2:].tolist() == [0, 1, 1]
