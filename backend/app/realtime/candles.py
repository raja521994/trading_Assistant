"""Server-side candle aggregation from ticks (exchange timestamps, IST buckets).

Bar volume = change in the exchange's cumulative day volume between ticks. A bar that started before the first
tick seen is marked partial (its open/high/low/volume may be incomplete); historical backfill replaces it later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

IST_OFFSET = 19800  # seconds
TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900}
SESSION_OPEN_S = 9 * 3600 + 15 * 60
SESSION_CLOSE_S = 15 * 3600 + 30 * 60


def bucket_start(ts: int, tf_seconds: int) -> int:
    """Start of the bar containing epoch `ts`, aligned to IST clock boundaries."""
    local = ts + IST_OFFSET
    return local - (local % tf_seconds) - IST_OFFSET


def in_session(ts: int) -> bool:
    sec = (ts + IST_OFFSET) % 86400
    return SESSION_OPEN_S <= sec < SESSION_CLOSE_S


@dataclass
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    partial: bool = False
    ticks: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "open": self.open, "high": self.high, "low": self.low, "close": self.close,
                "volume": self.volume, "partial": self.partial, "ticks": self.ticks}


@dataclass
class SeriesState:
    current: Bar | None = None
    closed: list[Bar] = field(default_factory=list)
    last_cum_volume: float | None = None


class CandleBuilder:
    def __init__(self, timeframes: tuple[str, ...] = ("1m", "5m"), keep: int = 400) -> None:
        self.timeframes = timeframes
        self.keep = keep
        self._s: dict[tuple[Any, str], SeriesState] = {}

    def state(self, key: Any, tf: str) -> SeriesState:
        return self._s.setdefault((key, tf), SeriesState())

    def on_tick(self, key: Any, price: float, ts: int, cum_volume: float | None = None) -> list[tuple[str, Bar, bool]]:
        """Apply a tick. Returns [(tf, bar, closed)] for every bar updated or closed by this tick."""
        events: list[tuple[str, Bar, bool]] = []
        if not in_session(ts):
            return events
        for tf in self.timeframes:
            st = self.state(key, tf)
            b0 = bucket_start(ts, TF_SECONDS[tf])
            first_ever = st.current is None and not st.closed
            vol_delta = 0.0
            if cum_volume is not None:
                if st.last_cum_volume is not None and cum_volume >= st.last_cum_volume:
                    vol_delta = cum_volume - st.last_cum_volume
                st.last_cum_volume = cum_volume
            cur = st.current
            if cur is not None and b0 < cur.ts:
                continue  # out-of-order tick from an older bar: ignore
            if cur is not None and b0 > cur.ts:
                st.closed.append(cur)
                if len(st.closed) > self.keep:
                    st.closed = st.closed[-self.keep:]
                events.append((tf, cur, True))
                cur = None
            if cur is None:
                # The very first bar seen mid-period is incomplete (ticks before start were not observed).
                cur = Bar(b0, price, price, price, price, vol_delta, partial=first_ever and (ts - b0) > 2)
                st.current = cur
            else:
                cur.high = max(cur.high, price)
                cur.low = min(cur.low, price)
                cur.close = price
                cur.volume += vol_delta
            cur.ticks += 1
            events.append((tf, cur, False))
        return events

    def close_due(self, key: Any, now_ts: int) -> list[tuple[str, Bar]]:
        """Close bars whose period has ended without a new tick (e.g. quiet instruments, session end)."""
        out = []
        for tf in self.timeframes:
            st = self._s.get((key, tf))
            if st and st.current and now_ts >= st.current.ts + TF_SECONDS[tf]:
                st.closed.append(st.current)
                out.append((tf, st.current))
                st.current = None
        return out

    def bars(self, key: Any, tf: str, include_current: bool = True) -> list[Bar]:
        st = self._s.get((key, tf))
        if not st:
            return []
        return st.closed + ([st.current] if include_current and st.current else [])
