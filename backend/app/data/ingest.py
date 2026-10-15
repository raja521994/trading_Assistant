"""Ingest instruments and candles from a provider into the database (incremental, idempotent)."""

from __future__ import annotations

import logging
import time
from typing import Any

import pandas as pd
from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from ..models import AppSetting, Candle, Instrument
from ..providers.base import MarketDataProvider
from ..providers.sample import SampleProvider

log = logging.getLogger(__name__)


def provider_fingerprint(provider: MarketDataProvider) -> str:
    if isinstance(provider, SampleProvider):
        return f"sample:seed={provider.seed}:start={provider.start.isoformat()}"
    return provider.name


def sync_instruments(session: Session, provider: MarketDataProvider) -> dict[tuple[str, str], Instrument]:
    existing = {(i.exchange, i.symbol): i for i in
                session.scalars(select(Instrument).where(Instrument.provider == provider.name))}
    for info in provider.instruments():
        inst = existing.get((info.exchange, info.symbol))
        if inst is None:
            inst = Instrument(provider=provider.name, symbol=info.symbol, exchange=info.exchange)
            session.add(inst)
            existing[(info.exchange, info.symbol)] = inst
        inst.name = info.name
        inst.instrument_type = info.instrument_type
        inst.sector = info.sector
        inst.lot_size = info.lot_size
        inst.tick_size = info.tick_size
        inst.provider_token = info.provider_token
        inst.is_active = True
    session.flush()
    return existing


def _check_fingerprint(session: Session, provider: MarketDataProvider) -> None:
    key = f"fingerprint:{provider.name}"
    fp = provider_fingerprint(provider)
    row = session.get(AppSetting, key)
    if row is not None and row.value != fp:
        ids = select(Instrument.id).where(Instrument.provider == provider.name)
        session.execute(delete(Candle).where(Candle.instrument_id.in_(ids)))
        log.info("provider data fingerprint changed (%s -> %s); cleared stored candles", row.value, fp)
    if row is None:
        session.add(AppSetting(key=key, value=fp, updated_at=int(time.time())))
    else:
        row.value = fp
        row.updated_at = int(time.time())
    session.flush()


def ingest(session: Session, provider: MarketDataProvider, timeframes: tuple[str, ...] = ("1d", "5m")
           ) -> dict[str, Any]:
    t0 = time.perf_counter()
    _check_fingerprint(session, provider)
    instruments = sync_instruments(session, provider)
    caps = provider.capabilities()
    inserted = 0
    errors: list[str] = []
    for (exchange, symbol), inst in instruments.items():
        for tf in timeframes:
            if tf not in caps.timeframes:
                continue
            last = session.scalar(select(func.max(Candle.ts)).where(Candle.instrument_id == inst.id,
                                                                     Candle.timeframe == tf))
            start = pd.Timestamp(last + 1, unit="s", tz="UTC") if last is not None else None
            try:
                df = provider.candles(symbol, exchange, tf, start=start)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{exchange}:{symbol} {tf}: {type(exc).__name__}: {exc}")
                continue
            if df.empty:
                continue
            ts = (df.index.tz_convert("UTC").as_unit("s").asi8).tolist()
            if last is not None:
                keep = [i for i, t in enumerate(ts) if t > last]
                df = df.iloc[keep]
                ts = [ts[i] for i in keep]
            if not ts:
                continue
            rows = [
                {"instrument_id": inst.id, "timeframe": tf, "ts": t, "open": float(o), "high": float(h),
                 "low": float(lo), "close": float(c), "volume": float(v)}
                for t, o, h, lo, c, v in zip(ts, df["open"], df["high"], df["low"], df["close"], df["volume"],
                                             strict=True)
            ]
            session.execute(insert(Candle), rows)
            inserted += len(rows)
    session.flush()
    stats = {"provider": provider.name, "instruments": len(instruments), "candles_inserted": inserted,
             "errors": errors, "seconds": round(time.perf_counter() - t0, 2)}
    if inserted:
        from .repository import candle_store

        candle_store.invalidate()
    log.info("ingest complete: %s", {k: v for k, v in stats.items() if k != "errors"})
    return stats
