"""Coverage matrix: what each provider actually supplies, as stored in the database."""

from __future__ import annotations

from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Candle, Instrument
from ..providers.registry import active_provider, all_providers

IST = "Asia/Kolkata"


def _ts(v: int | None) -> str | None:
    return pd.Timestamp(v, unit="s", tz="UTC").tz_convert(IST).isoformat() if v is not None else None


def coverage_matrix(session: Session) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    active = active_provider().name
    for name, p in all_providers().items():
        caps = p.capabilities()
        health = p.health()
        stored = session.execute(
            select(Instrument.exchange, Instrument.instrument_type, Candle.timeframe,
                   func.count(func.distinct(Candle.instrument_id)), func.min(Candle.ts), func.max(Candle.ts),
                   func.count())
            .join(Candle, Candle.instrument_id == Instrument.id)
            .where(Instrument.provider == name)
            .group_by(Instrument.exchange, Instrument.instrument_type, Candle.timeframe)
        ).all()
        if not stored:
            rows.append({
                "provider": name, "provider_name": p.display_name, "active": name == active,
                "exchange": ", ".join(caps.exchanges), "instrument_type": ", ".join(caps.instrument_types),
                "timeframe": "-", "instruments": 0, "history_start": None, "history_end": None, "bars": 0,
                "status": health.status.value, "connected": health.connected,
                "last_update": health.last_update.isoformat() if health.last_update else None,
                "latency_ms": health.latency_ms, "missing_fields": caps.missing_fields,
                "note": health.message,
            })
            continue
        for exch, itype, tf, n_inst, t_min, t_max, n_bars in stored:
            rows.append({
                "provider": name, "provider_name": p.display_name, "active": name == active,
                "exchange": exch, "instrument_type": itype, "timeframe": tf, "instruments": int(n_inst),
                "history_start": _ts(t_min), "history_end": _ts(t_max), "bars": int(n_bars),
                "status": health.status.value, "connected": health.connected,
                "last_update": health.last_update.isoformat() if health.last_update else None,
                "latency_ms": health.latency_ms,
                "missing_fields": caps.missing_fields + (["volume (indices)"] if itype == "INDEX" else []),
                "note": caps.history_note,
            })
    return rows
