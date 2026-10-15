"""Deterministic, offline SIMULATED market data for NSE/BSE-like instruments.

Everything produced here is synthetic. Symbols and sectors resemble real listings only so the UI is
realistic; prices, volumes and returns are NOT real and must never be presented as market data.

Determinism: every random quantity is drawn from its own seeded generator, so the series for a given
date never changes when the end date advances (prefix-stable). Weekday calendar only; exchange
holidays are not modelled.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

IST = "Asia/Kolkata"
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
BAR_MINUTES = 5
BARS_PER_SESSION = 75  # 09:15 .. 15:25 bar starts

MKT_VOL = 0.009
SECTOR_VOL = 0.006


@dataclass(frozen=True)
class SampleEquity:
    symbol: str
    exchange: str
    name: str
    sector: str
    start_price: float
    beta: float
    idio_vol: float
    turnover_cr: float  # typical daily traded value in INR crore


# fmt: off
EQUITIES: list[SampleEquity] = [
    SampleEquity("RELIANCE", "NSE", "Reliance Industries", "Energy", 2500, 1.0, 0.011, 1500),
    SampleEquity("TCS", "NSE", "Tata Consultancy Services", "IT", 3300, 0.8, 0.010, 900),
    SampleEquity("HDFCBANK", "NSE", "HDFC Bank", "Banks", 1600, 1.0, 0.010, 1800),
    SampleEquity("ICICIBANK", "NSE", "ICICI Bank", "Banks", 900, 1.1, 0.011, 1300),
    SampleEquity("INFY", "NSE", "Infosys", "IT", 1500, 0.9, 0.012, 1100),
    SampleEquity("HINDUNILVR", "NSE", "Hindustan Unilever", "FMCG", 2600, 0.6, 0.009, 500),
    SampleEquity("ITC", "NSE", "ITC", "FMCG", 330, 0.7, 0.010, 700),
    SampleEquity("SBIN", "NSE", "State Bank of India", "Banks", 560, 1.2, 0.013, 1200),
    SampleEquity("BHARTIARTL", "NSE", "Bharti Airtel", "Telecom", 780, 0.8, 0.011, 800),
    SampleEquity("KOTAKBANK", "NSE", "Kotak Mahindra Bank", "Banks", 1800, 0.9, 0.011, 700),
    SampleEquity("LT", "NSE", "Larsen & Toubro", "Capital Goods", 2100, 1.1, 0.012, 800),
    SampleEquity("AXISBANK", "NSE", "Axis Bank", "Banks", 900, 1.2, 0.012, 900),
    SampleEquity("ASIANPAINT", "NSE", "Asian Paints", "Consumer", 3100, 0.7, 0.011, 450),
    SampleEquity("MARUTI", "NSE", "Maruti Suzuki", "Auto", 8600, 0.9, 0.011, 500),
    SampleEquity("SUNPHARMA", "NSE", "Sun Pharmaceutical", "Pharma", 1000, 0.6, 0.011, 500),
    SampleEquity("TITAN", "NSE", "Titan Company", "Consumer", 2500, 1.0, 0.012, 500),
    SampleEquity("BAJFINANCE", "NSE", "Bajaj Finance", "Financial Services", 6500, 1.3, 0.014, 900),
    SampleEquity("ULTRACEMCO", "NSE", "UltraTech Cement", "Cement", 7200, 0.9, 0.011, 300),
    SampleEquity("WIPRO", "NSE", "Wipro", "IT", 400, 0.8, 0.012, 400),
    SampleEquity("HCLTECH", "NSE", "HCL Technologies", "IT", 1100, 0.8, 0.011, 450),
    SampleEquity("NESTLEIND", "NSE", "Nestle India", "FMCG", 2400, 0.5, 0.009, 250),
    SampleEquity("TATAMOTORS", "NSE", "Tata Motors", "Auto", 420, 1.4, 0.016, 1400),
    SampleEquity("M&M", "NSE", "Mahindra & Mahindra", "Auto", 1250, 1.1, 0.013, 700),
    SampleEquity("POWERGRID", "NSE", "Power Grid Corporation", "Power", 220, 0.7, 0.010, 400),
    SampleEquity("NTPC", "NSE", "NTPC", "Power", 170, 0.8, 0.011, 500),
    SampleEquity("ONGC", "NSE", "Oil & Natural Gas Corporation", "Energy", 150, 1.0, 0.013, 450),
    SampleEquity("TATASTEEL", "NSE", "Tata Steel", "Metals", 110, 1.4, 0.016, 900),
    SampleEquity("JSWSTEEL", "NSE", "JSW Steel", "Metals", 680, 1.3, 0.015, 450),
    SampleEquity("HINDALCO", "NSE", "Hindalco Industries", "Metals", 420, 1.4, 0.016, 500),
    SampleEquity("COALINDIA", "NSE", "Coal India", "Metals", 230, 0.9, 0.013, 450),
    SampleEquity("DRREDDY", "NSE", "Dr. Reddy's Laboratories", "Pharma", 4500, 0.6, 0.011, 350),
    SampleEquity("CIPLA", "NSE", "Cipla", "Pharma", 900, 0.6, 0.011, 350),
    SampleEquity("BAJAJ-AUTO", "NSE", "Bajaj Auto", "Auto", 3700, 0.9, 0.011, 350),
    SampleEquity("HEROMOTOCO", "NSE", "Hero MotoCorp", "Auto", 2700, 0.9, 0.012, 300),
    SampleEquity("TECHM", "NSE", "Tech Mahindra", "IT", 1050, 1.0, 0.013, 400),
    SampleEquity("INDUSINDBK", "NSE", "IndusInd Bank", "Banks", 1100, 1.4, 0.016, 600),
    SampleEquity("ADANIPORTS", "NSE", "Adani Ports & SEZ", "Infrastructure", 800, 1.3, 0.017, 700),
    SampleEquity("BRITANNIA", "BSE", "Britannia Industries", "FMCG", 4300, 0.5, 0.010, 60),
    SampleEquity("EICHERMOT", "BSE", "Eicher Motors", "Auto", 3100, 1.0, 0.013, 45),
    SampleEquity("DIVISLAB", "BSE", "Divi's Laboratories", "Pharma", 3500, 0.7, 0.013, 30),
    SampleEquity("HDFCLIFE", "BSE", "HDFC Life Insurance", "Financial Services", 520, 0.9, 0.012, 25),
    SampleEquity("SBILIFE", "BSE", "SBI Life Insurance", "Financial Services", 1150, 0.8, 0.012, 8),
]
# fmt: on

_ALL = [e.symbol for e in EQUITIES]
INDICES: list[dict] = [
    {"symbol": "NIFTY 50", "exchange": "NSE", "name": "NIFTY 50 (simulated)", "base": 18100.0, "members": _ALL},
    {"symbol": "NIFTY BANK", "exchange": "NSE", "name": "NIFTY BANK (simulated)", "base": 43000.0,
     "members": [e.symbol for e in EQUITIES if e.sector == "Banks"]},
    {"symbol": "NIFTY FIN SERVICE", "exchange": "NSE", "name": "NIFTY FINANCIAL SERVICES (simulated)", "base": 19000.0,
     "members": [e.symbol for e in EQUITIES if e.sector in ("Banks", "Financial Services")]},
    {"symbol": "SENSEX", "exchange": "BSE", "name": "S&P BSE SENSEX (simulated)", "base": 61000.0, "members": _ALL[:30]},
    {"symbol": "NIFTY IT", "exchange": "NSE", "name": "NIFTY IT (simulated)", "base": 29000.0,
     "members": [e.symbol for e in EQUITIES if e.sector == "IT"]},
    {"symbol": "NIFTY PHARMA", "exchange": "NSE", "name": "NIFTY PHARMA (simulated)", "base": 12800.0,
     "members": [e.symbol for e in EQUITIES if e.sector == "Pharma"]},
    {"symbol": "NIFTY AUTO", "exchange": "NSE", "name": "NIFTY AUTO (simulated)", "base": 13000.0,
     "members": [e.symbol for e in EQUITIES if e.sector == "Auto"]},
    {"symbol": "NIFTY FMCG", "exchange": "NSE", "name": "NIFTY FMCG (simulated)", "base": 45000.0,
     "members": [e.symbol for e in EQUITIES if e.sector == "FMCG"]},
    {"symbol": "NIFTY METAL", "exchange": "NSE", "name": "NIFTY METAL (simulated)", "base": 6400.0,
     "members": [e.symbol for e in EQUITIES if e.sector == "Metals"]},
    {"symbol": "NIFTY ENERGY", "exchange": "NSE", "name": "NIFTY ENERGY (simulated)", "base": 25000.0,
     "members": [e.symbol for e in EQUITIES if e.sector in ("Energy", "Power")]},
]


def _key(k: str | int) -> int:
    return zlib.crc32(k.encode()) if isinstance(k, str) else int(k)


def _rng(seed: int, *keys: str | int) -> np.random.Generator:
    return np.random.default_rng(np.random.SeedSequence([seed, *[_key(k) for k in keys]]))


def _round_tick(x: np.ndarray, tick: float = 0.05) -> np.ndarray:
    return np.round(np.round(np.asarray(x, dtype=float) / tick) * tick, 2)


def _regimes(seed: int, key: str, n: int, mean_duration: float, states: list[tuple[float, float]]
             ) -> tuple[np.ndarray, np.ndarray]:
    u_switch = _rng(seed, key, "switch").random(n)
    u_pick = _rng(seed, key, "pick").random(n)
    drift = np.empty(n)
    volm = np.empty(n)
    cur = int(u_pick[0] * len(states)) if n else 0
    p = 1.0 / mean_duration
    for i in range(n):
        if i > 0 and u_switch[i] < p:
            cur = int(u_pick[i] * len(states))
        drift[i], volm[i] = states[cur]
    return drift, volm


def trading_days(start: date, end: date) -> pd.DatetimeIndex:
    return pd.bdate_range(start, end)


def last_weekday_on_or_before(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def generate_daily(seed: int, start: date, end: date) -> dict[tuple[str, str], pd.DataFrame]:
    """Generate daily OHLCV for all sample equities and indices between start and end (inclusive)."""
    days = trading_days(start, end)
    n = len(days)
    out: dict[tuple[str, str], pd.DataFrame] = {}
    if n == 0:
        return out

    m_drift, m_volm = _regimes(seed, "MARKET", n, 70,
                               [(0.0009, 0.8), (-0.0008, 1.4), (0.0, 0.9), (0.0005, 1.0)])
    mkt = m_drift + MKT_VOL * m_volm * _rng(seed, "MARKET", "z").standard_normal(n)

    sector_ret: dict[str, np.ndarray] = {}
    for sector in sorted({e.sector for e in EQUITIES}):
        s_drift, _ = _regimes(seed, "SECTOR:" + sector, n, 60, [(0.0004, 1.0), (-0.0004, 1.0), (0.0, 1.0)])
        sector_ret[sector] = s_drift + SECTOR_VOL * _rng(seed, "SECTOR:" + sector, "z").standard_normal(n)

    eq_rets: dict[str, np.ndarray] = {}
    eq_gaps: dict[str, np.ndarray] = {}
    eq_hl: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for e in EQUITIES:
        k = "EQ:" + e.symbol
        i_drift, i_volm = _regimes(seed, k, n, 45, [(0.0012, 1.0), (-0.0010, 1.1), (0.0, 0.9)])
        idio = i_drift + e.idio_vol * i_volm * _rng(seed, k, "z").standard_normal(n)
        ret = np.clip(e.beta * mkt + sector_ret[e.sector] + idio, -0.15, 0.15)
        sigma = float(np.sqrt((e.beta * MKT_VOL) ** 2 + SECTOR_VOL**2 + e.idio_vol**2))
        gap = np.clip(0.25 * sigma * _rng(seed, k, "gap").standard_normal(n), -0.05, 0.05)
        up = np.abs(0.5 * sigma * _rng(seed, k, "hi").standard_normal(n))
        dn = np.abs(0.5 * sigma * _rng(seed, k, "lo").standard_normal(n))

        close = e.start_price * np.cumprod(1.0 + ret)
        prev_close = np.concatenate([[e.start_price], close[:-1]])
        opn = prev_close * (1.0 + gap)
        high = np.maximum(opn, close) * (1.0 + up)
        low = np.minimum(opn, close) * (1.0 - dn)
        opn, high, low, close = (_round_tick(a) for a in (opn, high, low, close))
        high = np.maximum.reduce([high, opn, close])
        low = np.minimum.reduce([low, opn, close])

        vol_noise = np.exp(0.3 * _rng(seed, k, "vol").standard_normal(n))
        activity = (1.0 + 2.0 * np.abs(ret) / sigma) / 2.6
        volume = np.round(e.turnover_cr * 1e7 / close * vol_noise * activity)

        eq_rets[e.symbol] = close / prev_close - 1.0
        eq_gaps[e.symbol] = opn / prev_close - 1.0
        eq_hl[e.symbol] = (high / np.maximum(opn, close) - 1.0, 1.0 - low / np.minimum(opn, close))
        out[(e.exchange, e.symbol)] = _frame(days, opn, high, low, close, volume)

    for idx in INDICES:
        mem = idx["members"]
        ret = np.mean([eq_rets[s] for s in mem], axis=0)
        gap = np.mean([eq_gaps[s] for s in mem], axis=0)
        up = 0.6 * np.mean([eq_hl[s][0] for s in mem], axis=0)
        dn = 0.6 * np.mean([eq_hl[s][1] for s in mem], axis=0)
        close = idx["base"] * np.cumprod(1.0 + ret)
        prev_close = np.concatenate([[idx["base"]], close[:-1]])
        opn = prev_close * (1.0 + gap)
        high = np.maximum(opn, close) * (1.0 + up)
        low = np.minimum(opn, close) * (1.0 - dn)
        opn, high, low, close = (_round_tick(a) for a in (opn, high, low, close))
        high = np.maximum.reduce([high, opn, close])
        low = np.minimum.reduce([low, opn, close])
        out[(idx["exchange"], idx["symbol"])] = _frame(days, opn, high, low, close, np.zeros(n))
    return out


def _frame(days: pd.DatetimeIndex, o, h, l, c, v) -> pd.DataFrame:  # noqa: E741
    idx = pd.DatetimeIndex(days).tz_localize(IST)
    idx.name = "ts"
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v}, index=idx)


def session_bar_starts(d: date) -> pd.DatetimeIndex:
    start = pd.Timestamp(datetime.combine(d, SESSION_OPEN)).tz_localize(IST)
    idx = pd.date_range(start, periods=BARS_PER_SESSION, freq=f"{BAR_MINUTES}min")
    idx.name = "ts"
    return idx


def generate_intraday_session(seed: int, symbol: str, d: date, o: float, h: float, l: float, c: float,  # noqa: E741
                              volume: float) -> pd.DataFrame:
    """5-minute bars for one session that are exactly consistent with the daily OHLCV bar."""
    n = BARS_PER_SESSION
    rng = _rng(seed, "INTRA:" + symbol, d.toordinal())
    w = np.cumsum(rng.standard_normal(n))
    k = np.arange(1, n + 1)
    bridge = w - (k / n) * w[-1]
    rng_hl = max(h - l, 1e-6)
    path = o + (c - o) * k / n + rng_hl / (2.0 * np.sqrt(n)) * bridge
    path = np.clip(path, l, h)
    path[-1] = c
    closes = path
    opens = np.concatenate([[o], closes[:-1]])
    wick = np.abs(rng.standard_normal((2, n))) * rng_hl * 0.04
    highs = np.minimum(np.maximum(opens, closes) + wick[0], h)
    lows = np.maximum(np.minimum(opens, closes) - wick[1], l)
    highs[int(np.argmax(highs))] = h
    lows[int(np.argmin(lows))] = l
    opens, highs, lows, closes = (_round_tick(a) for a in (opens, highs, lows, closes))
    highs = np.maximum.reduce([highs, opens, closes])
    lows = np.minimum.reduce([lows, opens, closes])

    if volume > 0:
        prof = 1.0 + 1.5 * ((np.arange(n) - (n - 1) / 2) / ((n - 1) / 2)) ** 2
        prof = prof * np.exp(0.25 * rng.standard_normal(n))
        vols = np.floor(volume * prof / prof.sum())
        vols[-1] += volume - vols.sum()
    else:
        vols = np.zeros(n)
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols},
                        index=session_bar_starts(d))
