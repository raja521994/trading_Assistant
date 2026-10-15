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
    """Incremental, idempotent ingestion. The most recent stored bar is always re-fetched and replaced so that
    provisional values (e.g. today's close before the exchange's official closing price) get corrected."""
    t0 = time.perf_counter()
    _check_fingerprint(session, provider)
    instruments = sync_instruments(session, provider)
    caps = provider.capabilities()
    inserted = written = 0
    errors: list[str] = []
    ids = [i.id for i in instruments.values()]
    # Self-repair: intraday bars must sit exactly on bar boundaries (earlier builds could store misaligned bars).
    if ids:
        session.execute(delete(Candle).where(Candle.instrument_id.in_(ids), Candle.timeframe == "5m",
                                             (Candle.ts % 300) != 0))
    expected = {tf: provider.expected_last_bar(tf) for tf in timeframes if tf in caps.timeframes}
    refresh = {tf: provider.needs_refresh(tf) for tf in timeframes if tf in caps.timeframes}
    for (exchange, symbol), inst in instruments.items():
        for tf in timeframes:
            if tf not in caps.timeframes:
                continue
            last = session.scalar(select(func.max(Candle.ts)).where(Candle.instrument_id == inst.id,
                                                                     Candle.timeframe == tf))
            exp = expected.get(tf)
            if last is not None and exp is not None and last >= int(pd.Timestamp(exp).timestamp()) \
                    and not refresh.get(tf):
                continue  # already up to date; no provider call needed
            start = pd.Timestamp(last, unit="s", tz="UTC") if last is not None else None
            try:
                df = provider.candles(symbol, exchange, tf, start=start)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{exchange}:{symbol} {tf}: {type(exc).__name__}: {exc}")
                continue
            if df.empty:
                continue
            ts = (df.index.tz_convert("UTC").as_unit("s").asi8).tolist()
            keep = [i for i, t in enumerate(ts) if (tf == "1d" or t % 300 == 0) and (last is None or t >= last)]
            df = df.iloc[keep]
            ts = [ts[i] for i in keep]
            if not ts:
                continue
            session.execute(delete(Candle).where(Candle.instrument_id == inst.id, Candle.timeframe == tf,
                                                 Candle.ts >= min(ts)))
            rows = [
                {"instrument_id": inst.id, "timeframe": tf, "ts": t, "open": float(o), "high": float(h),
                 "low": float(lo), "close": float(c), "volume": float(v)}
                for t, o, h, lo, c, v in zip(ts, df["open"], df["high"], df["low"], df["close"], df["volume"],
                                             strict=True)
            ]
            session.execute(insert(Candle), rows)
            inserted += len(rows) - (1 if last is not None and min(ts) == last else 0)
            written += len(rows)
    session.flush()
    stats = {"provider": provider.name, "instruments": len(instruments), "candles_inserted": inserted,
             "candles_written": written, "errors": errors, "seconds": round(time.perf_counter() - t0, 2)}
    if written:
        from .repository import candle_store

        candle_store.invalidate()
    log.info("ingest complete: %s", {k: v for k, v in stats.items() if k != "errors"})
    return stats
