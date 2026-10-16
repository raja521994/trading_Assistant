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
        self.adjustments: dict[int, list[dict]] = {}  # instrument_id -> detected split/bonus back-adjustments

    def invalidate(self) -> None:
        with self._lock:
            self._version += 1
            self._cache.clear()

    @property
    def version(self) -> int:
        return self._version

    def load(self, session: Session, instrument_id: int, timeframe: str) -> pd.DataFrame:
        key = (instrument_id, timeframe)
        with self._lock:
            hit = self._cache.get(key)
            version = self._version
        if hit is not None and hit[0] == version:
            return hit[1]
        rows = session.execute(
            select(Candle.ts, Candle.open, Candle.high, Candle.low, Candle.close, Candle.volume)
            .where(Candle.instrument_id == instrument_id, Candle.timeframe == timeframe)
            .order_by(Candle.ts)
        ).all()
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        idx = pd.DatetimeIndex(pd.to_datetime(df["ts"].astype("int64"), unit="s", utc=True)).tz_convert(IST)
        df = df.drop(columns="ts").astype(float)
        df.index = idx.rename("ts")
        if timeframe == "1d" and len(df):
            from .corporate_actions import adjust_for_splits

            df, events = adjust_for_splits(df)
            if events:
                self.adjustments[instrument_id] = events
        with self._lock:
            if self._version == version:
                self._cache[key] = (version, df)
        return df


candle_store = CandleStore()


def load_candles(session: Session, symbol: str, exchange: str | None, timeframe: str) -> pd.DataFrame:
    inst = get_instrument(session, symbol, exchange)
    if inst is None:
        raise KeyError(f"Unknown instrument {exchange}:{symbol}")
    return candle_store.load(session, inst.id, timeframe)
