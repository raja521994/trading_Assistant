"""Live feed sources: KiteTicker (asyncio, native binary protocol), replay of stored bars, simulated (sample).

Exactly one feed runs, inside the backend process. The browser never connects to Zerodha.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime
from typing import Any

import pandas as pd

from .protocol import parse_message
from .store import LiveStore

log = logging.getLogger(__name__)
KITE_WS = "wss://ws.kite.trade"
MAX_TOKENS_PER_CONNECTION = 3000


class FeedStatus:
    def __init__(self, source: str) -> None:
        self.source = source
        self.state = "stopped"  # stopped | connecting | connected | reconnecting | auth_error | gave_up | replaying
        self.connected_since: float | None = None
        self.last_reconnect: float | None = None
        self.reconnects = 0
        self.last_error: str | None = None
        self.subscribed: dict[str, int] = {}

    def as_dict(self) -> dict[str, Any]:
        iso = lambda t: None if t is None else datetime.fromtimestamp(t).isoformat(timespec="seconds")  # noqa: E731
        return {"source": self.source, "state": self.state, "connected_since": iso(self.connected_since),
                "last_reconnect": iso(self.last_reconnect), "reconnects": self.reconnects,
                "last_error": self.last_error, "subscribed": self.subscribed,
                "subscribed_total": sum(self.subscribed.values())}


class KiteTickerFeed:
    """Kite WebSocket client: on_connect -> subscribe + set modes; reconnect with exponential backoff and automatic
    re-subscribe; stops (on_noreconnect) on authentication failure or after `max_retries`."""

    def __init__(self, api_key: str, access_token: str, store: LiveStore, full_tokens: list[int],
                 quote_tokens: list[int], max_retries: int = 50, url: str = KITE_WS, connect=None,
                 record_path: str | None = None) -> None:
        self._api_key, self._access_token = api_key, access_token
        self.record = open(record_path, "a", encoding="utf-8", buffering=1) if record_path else None  # noqa: SIM115
        self.store = store
        self.full_tokens = list(dict.fromkeys(full_tokens))[:MAX_TOKENS_PER_CONNECTION]
        room = MAX_TOKENS_PER_CONNECTION - len(self.full_tokens)
        self.quote_tokens = [t for t in dict.fromkeys(quote_tokens) if t not in set(self.full_tokens)][:room]
        self.max_retries = max_retries
        self.url = url
        self._connect = connect
        self.status = FeedStatus("kite")
        self._stop = asyncio.Event()
        self._ws = None

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()

    async def _subscribe(self, ws) -> None:  # noqa: ANN001
        tokens = self.full_tokens + self.quote_tokens
        if not tokens:
            return
        await ws.send(json.dumps({"a": "subscribe", "v": tokens}))
        if self.full_tokens:
            await ws.send(json.dumps({"a": "mode", "v": ["full", self.full_tokens]}))
        if self.quote_tokens:
            await ws.send(json.dumps({"a": "mode", "v": ["quote", self.quote_tokens]}))
        self.status.subscribed = {"full": len(self.full_tokens), "quote": len(self.quote_tokens)}

    async def run(self) -> None:
        if self._connect is None:
            from websockets.asyncio.client import connect as ws_connect

            self._connect = ws_connect
        url = f"{self.url}?api_key={self._api_key}&access_token={self._access_token}"
        delay, attempts = 1.0, 0
        while not self._stop.is_set():
            self.status.state = "connecting" if attempts == 0 else "reconnecting"
            try:
                async with self._connect(url, max_size=None, ping_interval=20, open_timeout=15) as ws:
                    self._ws = ws
                    self.status.state, self.status.connected_since, self.status.last_error = "connected", time.time(), None
                    if attempts:
                        self.status.reconnects += 1
                        self.status.last_reconnect = time.time()
                    log.info("kite ticker connected (%d full, %d quote)", len(self.full_tokens), len(self.quote_tokens))
                    await self._subscribe(ws)
                    delay, attempts = 1.0, 0
                    while not self._stop.is_set():
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=15)  # heartbeats arrive every ~1-2 s
                        except asyncio.TimeoutError:
                            raise ConnectionError("no data or heartbeat for 15 s") from None
                        if isinstance(msg, (bytes, bytearray)):
                            now = time.time()
                            if self.record is not None and len(msg) > 1:
                                import base64

                                self.record.write(json.dumps({"t": round(now, 3),
                                                              "b": base64.b64encode(bytes(msg)).decode()}) + "\n")
                            for t in parse_message(bytes(msg)):
                                self.store.apply(t, "kite", now)
                        else:
                            self._on_text(msg)
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    break
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                self.status.last_error = f"{type(exc).__name__}: {exc}"[:300]
                if status_code in (401, 403):
                    self.status.state = "auth_error"
                    log.warning("kite ticker: authentication rejected (%s) - login with Zerodha again", status_code)
                    return  # on_noreconnect
                attempts += 1
                if attempts > self.max_retries:
                    self.status.state = "gave_up"
                    log.warning("kite ticker: giving up after %d reconnect attempts", attempts - 1)
                    return
                self.status.state = "reconnecting"
                wait = min(delay, 60.0) * (0.8 + 0.4 * random.random())
                log.warning("kite ticker disconnected (%s); reconnecting in %.1fs", self.status.last_error, wait)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
                delay *= 2
            finally:
                self._ws = None
        self.status.state = "stopped"

    def _on_text(self, msg: str) -> None:
        try:
            j = json.loads(msg)
        except ValueError:
            return
        if j.get("type") == "error":
            self.status.last_error = str(j.get("data"))[:300]
            log.warning("kite ticker error message: %s", self.status.last_error)


class ReplayFeed:
    """Replays stored 5-minute bars of one session as ticks (O -> H/L -> C path), for testing outside market hours.
    Ticks keep their original exchange timestamps; data is labelled REPLAY."""

    def __init__(self, store: LiveStore, bars: dict[tuple[str, str], pd.DataFrame], speed: float = 60.0,
                 ticks_per_bar: int = 6, loop: bool = True) -> None:
        self.store, self.bars, self.speed, self.ticks_per_bar, self.loop = store, bars, speed, ticks_per_bar, loop
        self.status = FeedStatus("replay")
        self._stop = asyncio.Event()

    async def stop(self) -> None:
        self._stop.set()

    def events(self) -> list[tuple[int, dict[str, Any]]]:
        token_of = {v: k for k, v in self.store.tokens.items()}
        evs: list[tuple[int, dict[str, Any]]] = []
        for key, df in self.bars.items():
            tok = token_of.get(key)
            if tok is None or df.empty:
                continue
            cum = 0.0
            day_open = float(df["open"].iloc[0])
            hi, lo = -1e18, 1e18
            for ts, r in df.iterrows():
                t0 = int(pd.Timestamp(ts).timestamp())
                path = [r.open, r.low, r.high, r.close] if r.close >= r.open else [r.open, r.high, r.low, r.close]
                n = self.ticks_per_bar
                pts = [path[min(int(i * 3 / max(n - 1, 1) + 0.5), 3)] for i in range(n)]
                for i, px in enumerate(pts):
                    cum += float(r.volume) / n
                    hi, lo = max(hi, px), min(lo, px)
                    ets = t0 + int(i * 299 / max(n - 1, 1))
                    evs.append((ets, {"token": tok, "ltp": float(px), "volume": cum, "open": day_open, "high": hi,
                                      "low": lo, "close": None, "exchange_ts": ets}))
        evs.sort(key=lambda e: e[0])
        return evs

    async def run(self) -> None:
        evs = self.events()
        self.status.subscribed = {"replay": len(self.bars)}
        if not evs:
            self.status.state, self.status.last_error = "stopped", "no stored bars to replay"
            return
        self.status.state, self.status.connected_since = "replaying", time.time()
        while not self._stop.is_set():
            start_wall, start_ex = time.time(), evs[0][0]
            for ets, raw in evs:
                if self._stop.is_set():
                    break
                due = start_wall + (ets - start_ex) / self.speed
                wait = due - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                self.store.apply(raw, "replay")
            if not self.loop:
                break
            self.store.reset()  # replay the session again from the start
        self.status.state = "stopped"


class TickFileFeed:
    """Replays a recorded tick file (JSONL of raw Kite binary frames: {"t": recv_epoch, "b": base64}) through the
    exact production path: binary parser -> store -> candle builder. Record one with RECORD_TICKS=<path>."""

    def __init__(self, store: LiveStore, path: str, speed: float = 1.0, loop: bool = False) -> None:
        self.store, self.path, self.speed, self.loop = store, path, speed, loop
        self.status = FeedStatus("replay-file")
        self._stop = asyncio.Event()

    async def stop(self) -> None:
        self._stop.set()

    def frames(self) -> list[tuple[float, bytes]]:
        import base64

        out = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    j = json.loads(line)
                    out.append((float(j["t"]), base64.b64decode(j["b"])))
        return out

    async def run(self) -> None:
        frames = self.frames()
        self.status.subscribed = {"frames": len(frames)}
        if not frames:
            self.status.state, self.status.last_error = "stopped", "empty tick file"
            return
        self.status.state, self.status.connected_since = "replaying", time.time()
        while not self._stop.is_set():
            wall0, t0 = time.time(), frames[0][0]
            for t, data in frames:
                if self._stop.is_set():
                    break
                wait = wall0 + (t - t0) / self.speed - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                for tick in parse_message(data):
                    self.store.apply(tick, "replay")
            if not self.loop:
                break
            self.store.reset()
        self.status.state = "stopped"


class SimulatedFeed:
    """For the SIMULATED sample provider: pushes its (synthetic) quotes once per second."""

    def __init__(self, store: LiveStore, quote_fn, keys: list[tuple[str, str]]) -> None:  # noqa: ANN001
        self.store, self.quote_fn, self.keys = store, quote_fn, keys
        self.status = FeedStatus("simulated")
        self._stop = asyncio.Event()

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        token_of = {v: k for k, v in self.store.tokens.items()}
        self.status.state, self.status.connected_since = "connected", time.time()
        self.status.subscribed = {"simulated": len(self.keys)}
        while not self._stop.is_set():
            for key in self.keys:
                q = await asyncio.to_thread(self.quote_fn, key[1], key[0])
                if q is not None and key in token_of:
                    self.store.apply({"token": token_of[key], "ltp": q.last_price, "close": q.prev_close,
                                      "exchange_ts": int(time.time())}, "simulated")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
        self.status.state = "stopped"
