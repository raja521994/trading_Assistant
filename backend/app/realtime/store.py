"""Single in-process live data store: latest tick per instrument, candle builder, health metrics.

Every live number in the app (header, overview, scanner, charts, paper trading, regime) reads from here so the UI
never mixes sources. Redis is not used: the app is a single process; an in-process store avoids a network hop.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .candles import Bar, CandleBuilder

STALE_SECONDS = 5.0


@dataclass
class LiveTick:
    exchange: str
    symbol: str
    token: int
    ltp: float
    prev_close: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    exchange_ts: int | None = None  # epoch seconds (exchange clock)
    recv_ts: float = 0.0  # epoch seconds (server clock)
    depth: dict[str, Any] | None = None
    source: str = "kite"

    @property
    def change(self) -> float | None:
        return None if not self.prev_close else self.ltp - self.prev_close

    @property
    def change_pct(self) -> float | None:
        return None if not self.prev_close else (self.ltp / self.prev_close - 1.0) * 100.0

    def age(self, now: float | None = None) -> float:
        return (now or time.time()) - self.recv_ts

    def as_wire(self, now: float | None = None) -> dict[str, Any]:
        return {"e": self.exchange, "s": self.symbol, "lp": self.ltp, "pc": self.prev_close, "o": self.open,
                "h": self.high, "l": self.low, "v": self.volume, "et": self.exchange_ts, "rt": round(self.recv_ts, 3),
                "ch": None if self.change is None else round(self.change, 2),
                "chp": None if self.change_pct is None else round(self.change_pct, 3), "src": self.source}


@dataclass
class Metrics:
    tick_times: deque = field(default_factory=lambda: deque(maxlen=20000))
    latencies: deque = field(default_factory=lambda: deque(maxlen=5000))
    client_latencies: deque = field(default_factory=lambda: deque(maxlen=2000))

    @staticmethod
    def _pct(values: list[float], q: float) -> float | None:
        if not values:
            return None
        v = sorted(values)
        return round(v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))], 3)

    @staticmethod
    def _copy(d: deque) -> list[float]:
        for _ in range(5):  # the feed appends from another thread; retry if a copy races with an append
            try:
                return list(d)
            except RuntimeError:
                continue
        return []

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        recent = [t for t in self._copy(self.tick_times) if now - t <= 10]
        lat = self._copy(self.latencies)
        cl = self._copy(self.client_latencies)
        return {"ticks_per_sec": round(len(recent) / 10.0, 1),
                "exchange_to_server_p50_s": self._pct(lat, 0.5), "exchange_to_server_p95_s": self._pct(lat, 0.95),
                "tick_to_screen_p50_s": self._pct(cl, 0.5), "tick_to_screen_p95_s": self._pct(cl, 0.95),
                "latency_note": "Kite exchange timestamps have 1-second resolution, so latencies are +/-1 s."}


class LiveStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.ticks: dict[tuple[str, str], LiveTick] = {}
        self.tokens: dict[int, tuple[str, str]] = {}
        self.candles = CandleBuilder(("1m", "5m"))
        self.metrics = Metrics()
        self._changed: set[tuple[str, str]] = set()
        self._bar_events: list[tuple[tuple[str, str], str, Bar, bool]] = []
        self.last_tick_ts: float | None = None

    def register(self, token: int, exchange: str, symbol: str) -> None:
        self.tokens[int(token)] = (exchange, symbol)

    def reset(self) -> None:
        with self._lock:
            self.ticks.clear()
            self._changed.clear()
            self._bar_events.clear()
            self.candles = CandleBuilder(("1m", "5m"))
            self.last_tick_ts = None

    def apply(self, raw: dict[str, Any], source: str = "kite", recv_ts: float | None = None) -> LiveTick | None:
        key = self.tokens.get(int(raw["token"]))
        if key is None:
            return None
        now = recv_ts or time.time()
        with self._lock:
            t = self.ticks.get(key)
            if t is None:
                t = LiveTick(key[0], key[1], int(raw["token"]), raw["ltp"])
                self.ticks[key] = t
            t.ltp = raw["ltp"]
            for f in ("open", "high", "low", "volume"):
                if raw.get(f) is not None:
                    setattr(t, f, raw[f])
            if raw.get("close"):
                t.prev_close = raw["close"]
            if raw.get("depth"):
                t.depth = raw["depth"]
            ets = raw.get("exchange_ts") or raw.get("last_trade_ts")
            t.exchange_ts = int(ets) if ets else int(now)
            t.recv_ts = now
            t.source = source
            self._changed.add(key)
            self.last_tick_ts = now
            self.metrics.tick_times.append(now)
            if ets:
                self.metrics.latencies.append(max(now - int(ets), 0.0))
            for tf, bar, closed in self.candles.on_tick(key, t.ltp, t.exchange_ts, raw.get("volume")):
                self._bar_events.append((key, tf, bar, closed))
        return t

    def get(self, exchange: str, symbol: str) -> LiveTick | None:
        return self.ticks.get((exchange, symbol))

    def drain(self) -> tuple[list[LiveTick], list[tuple[tuple[str, str], str, Bar, bool]]]:
        with self._lock:
            changed = [self.ticks[k] for k in self._changed if k in self.ticks]
            self._changed.clear()
            # keep only the latest event per (key, tf, ts, closed)
            seen: dict[tuple, tuple] = {}
            for ev in self._bar_events:
                seen[(ev[0], ev[1], ev[2].ts, ev[3])] = ev
            self._bar_events.clear()
        return changed, list(seen.values())


store = LiveStore()
