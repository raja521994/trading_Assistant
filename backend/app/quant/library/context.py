"""Cross-instrument context for strategies that need more than their own bars (benchmark / sector relative strength).

Populated from storage by `load_context(session)` before scanning or a tournament run. Strategies receive the
series aligned to their own index in `prepare_keyed`, using only values at or before each bar (causal).
"""

from __future__ import annotations

import threading

import pandas as pd

_lock = threading.Lock()
_ctx: dict[str, object] = {"benchmark": None, "index": {}, "sector_of": {}}


def set_context(benchmark: pd.Series | None, index_closes: dict[str, pd.Series], sector_of: dict[str, str | None]
                ) -> None:
    with _lock:
        _ctx["benchmark"] = benchmark
        _ctx["index"] = dict(index_closes)
        _ctx["sector_of"] = dict(sector_of)


def get_context() -> dict[str, object]:
    with _lock:
        return dict(_ctx)


def aligned(series: pd.Series | None, index: pd.DatetimeIndex) -> pd.Series:
    """Daily `series` as-of each bar of `index` (forward-filled, never using later values).
    For intraday bars the PREVIOUS session's daily value is used (today's daily close is not known yet)."""
    if series is None or series.empty:
        return pd.Series(float("nan"), index=index)
    s = series.copy()
    if s.index.tz is None and index.tz is not None:
        s.index = s.index.tz_localize(index.tz)
    intraday = len(index) > 1 and (index.hour != 0).any()
    if intraday:
        s.index = s.index + pd.Timedelta(hours=15, minutes=30)  # known only after that session's close
    out = s.reindex(s.index.union(index)).ffill().reindex(index)
    return out


def benchmark_for(index: pd.DatetimeIndex) -> pd.Series:
    return aligned(get_context().get("benchmark"), index)  # type: ignore[arg-type]


def sector_index_for(symbol: str, index: pd.DatetimeIndex) -> tuple[str | None, pd.Series]:
    ctx = get_context()
    name = (ctx["sector_of"] or {}).get(symbol)  # type: ignore[union-attr]
    ser = (ctx["index"] or {}).get(name) if name else None  # type: ignore[union-attr]
    return (name if ser is not None else None), aligned(ser, index)


def load_context(session) -> None:  # noqa: ANN001
    from ...data.constituents import INDUSTRY_SECTOR_INDEX
    from ..universe import instrument_map, load_benchmark, load_universe

    bench = load_benchmark(session)
    idx = {sym: df["close"] for (_, sym), df in load_universe(session, "1d", "INDEX").items()}
    sector_of = {sym: INDUSTRY_SECTOR_INDEX.get(inst.sector or "") for (_, sym), inst in instrument_map(session).items()
                 if inst.instrument_type == "EQ"}
    set_context(None if bench is None else bench["close"], idx, sector_of)
