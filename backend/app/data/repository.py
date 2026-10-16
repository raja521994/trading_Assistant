"""Read access to stored instruments/candles with a small in-memory cache."""

from __future__ import annotations

import threading

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Candle, Instrument
from ..providers.registry import active_provider

IST = "Asia/Kolkata"


def list_instruments(session: Session, instrument_type: str | None = None) -> list[Instrument]:
    q = select(Instrument).where(Instrument.provider == active_provider().name, Instrument.is_active.is_(True))
    if instrument_type:
        q = q.where(Instrument.instrument_type == instrument_type)
    return list(session.scalars(q.order_by(Instrument.exchange, Instrument.symbol)))


def get_instrument(session: Session, symbol: str, exchange: str | None = None) -> Instrument | None:
    q = select(Instrument).where(Instrument.provider == active_provider().name, Instrument.symbol == symbol)
    if exchange:
        q = q.where(Instrument.exchange == exchange)
    return session.scalars(q).first()


class CandleStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._version = 0
        self._cache: dict[tuple[int, str], tuple[int, pd.DataFrame]] = {}
        self._stale: set[tuple[int, str]] = set()
        self.adjustments: dict[int, list[dict]] = {}  # instrument_id -> detected split/bonus back-adjustments

    def invalidate(self, keys: "list[tuple[int, str]] | None" = None) -> None:
        """Drop cached series: only `keys` (instrument_id, timeframe) when given, otherwise everything."""
        with self._lock:
            if keys is None:
                self._version += 1
                self._cache.clear()
                self._stale.clear()
            else:
                for k in keys:
                    if k[1] == "1d":
                        self._cache.pop(k, None)  # daily: full reload (split/bonus adjustment needs the whole series)
                    elif k in self._cache:
                        self._stale.add(k)  # intraday: append new bars on next load

    @property
    def version(self) -> int:
        return self._version

    def load(self, session: Session, instrument_id: int, timeframe: str) -> pd.DataFrame:
        key = (instrument_id, timeframe)
        with self._lock:
            hit = self._cache.get(key)
            version = self._version
            stale = key in self._stale
        if hit is not None and hit[0] == version and not stale:
            return hit[1]
        q = select(Candle.ts, Candle.open, Candle.high, Candle.low, Candle.close, Candle.volume).where(
            Candle.instrument_id == instrument_id, Candle.timeframe == timeframe)
        base = None
        if stale and hit is not None and hit[0] == version and len(hit[1]):
            base = hit[1]
            q = q.where(Candle.ts >= int(base.index[-1].timestamp()))  # re-read the last bar + anything newer
        rows = session.execute(q.order_by(Candle.ts)).all()
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        idx = pd.DatetimeIndex(pd.to_datetime(df["ts"].astype("int64"), unit="s", utc=True)).tz_convert(IST)
        df = df.drop(columns="ts").astype(float)
        df.index = idx.rename("ts")
        if base is not None:
            df = pd.concat([base[base.index < df.index[0]] if len(df) else base, df])
        if timeframe == "1d" and len(df):
            from .corporate_actions import adjust_for_splits

            df, events = adjust_for_splits(df)
            if events:
                self.adjustments[instrument_id] = events
        with self._lock:
            if self._version == version:
                self._cache[key] = (version, df)
                self._stale.discard(key)
        return df


candle_store = CandleStore()


def load_candles(session: Session, symbol: str, exchange: str | None, timeframe: str) -> pd.DataFrame:
    inst = get_instrument(session, symbol, exchange)
    if inst is None:
        raise KeyError(f"Unknown instrument {exchange}:{symbol}")
    return candle_store.load(session, inst.id, timeframe)
