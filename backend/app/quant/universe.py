"""Loads the analysable universe from storage and caches strategy-prepared frames per data version."""

from __future__ import annotations

import threading
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from ..data.repository import candle_store, list_instruments
from ..models import Instrument
from .features import add_common_features, arrays
from .strategies.base import Strategy

BENCHMARK = ("NSE", "NIFTY 50")
# Bars needed by live views (scanner, overview, assistant): enough for 200-day averages to converge and for the
# setup window. Backtests and the tournament use the full history.
LIVE_TAIL = {"1d": 750, "5m": 750}

_lock = threading.Lock()
_prep_cache: dict[tuple, tuple[pd.DataFrame, dict[str, list]]] = {}
_feat_cache: dict[tuple, pd.DataFrame] = {}


def load_universe(session: Session, timeframe: str, instrument_type: str = "EQ", tail: int | None = None
                  ) -> dict[tuple[str, str], pd.DataFrame]:
    out: dict[tuple[str, str], pd.DataFrame] = {}
    for inst in list_instruments(session, instrument_type):
        df = candle_store.load(session, inst.id, timeframe)
        if len(df):
            out[(inst.exchange, inst.symbol)] = df.iloc[-tail:] if tail else df
    return out


def instrument_map(session: Session) -> dict[tuple[str, str], Instrument]:
    return {(i.exchange, i.symbol): i for i in list_instruments(session)}


def load_benchmark(session: Session) -> pd.DataFrame | None:
    for inst in list_instruments(session, "INDEX"):
        if (inst.exchange, inst.symbol) == BENCHMARK:
            return candle_store.load(session, inst.id, "1d")
    return None


def _ckey(df: pd.DataFrame) -> tuple:
    """Content key of a series (so only instruments whose bars changed are recomputed after an ingest)."""
    if not len(df):
        return (0, candle_store.version)
    last = df.iloc[-1]
    return (len(df), df.index[0].value, df.index[-1].value, float(df["close"].iloc[0]), float(last["open"]),
            float(last["high"]), float(last["low"]), float(last["close"]), float(last["volume"]), candle_store.version)


def prepared(strategy: Strategy, key: tuple[str, str], df: pd.DataFrame) -> pd.DataFrame:
    return prepared_with_arrays(strategy, key, df)[0]


def prepared_with_arrays(strategy: Strategy, key: tuple[str, str], df: pd.DataFrame
                         ) -> tuple[pd.DataFrame, dict[str, list]]:
    ck = (strategy.meta.id, tuple(sorted((k, str(v)) for k, v in strategy.params.items())), key, _ckey(df))
    with _lock:
        hit = _prep_cache.get(ck)
    if hit is not None:
        return hit
    p = strategy.prepare_keyed(key, df, features(key, df, strategy.intraday))
    entry = (p, arrays(p))
    with _lock:
        if len(_prep_cache) > 1500:
            _prep_cache.clear()
        _prep_cache[ck] = entry
    return entry


def features(key: tuple[str, str], df: pd.DataFrame, intraday: bool = False) -> pd.DataFrame:
    ck = (key, intraday, _ckey(df))
    with _lock:
        hit = _feat_cache.get(ck)
    if hit is not None:
        return hit
    f = add_common_features(df, intraday=intraday)
    with _lock:
        if len(_feat_cache) > 600:
            _feat_cache.clear()
        _feat_cache[ck] = f
    return f


def last_row(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {}
    r = df.iloc[-1]
    return {k: (None if pd.isna(v) else float(v)) for k, v in r.items()}
