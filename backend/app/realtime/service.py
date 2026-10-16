"""Realtime service: owns the single live feed and pushes changes to browsers over one WebSocket each.

Push cadence: every 250 ms (≈4 Hz) only symbols that changed are sent; candle updates only for the chart a client
has open. Browsers never poll for prices.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from ..config import get_settings
from .feeds import KiteTickerFeed, ReplayFeed, SimulatedFeed
from .store import STALE_SECONDS, store

log = logging.getLogger(__name__)
PUSH_INTERVAL = 0.25


@dataclass(eq=False)  # identity hash: clients live in a set
class Client:
    ws: WebSocket
    charts: set[tuple[str, str, str]] = field(default_factory=set)  # (exchange, symbol, tf)


class RealtimeService:
    def __init__(self) -> None:
        self.feed: KiteTickerFeed | ReplayFeed | SimulatedFeed | None = None
        self.feed_task: asyncio.Task | None = None
        self.push_task: asyncio.Task | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[Client] = set()
        self.mode_note = "not started"

    # ---------------- lifecycle ----------------
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        await self._start_feed()
        if self.push_task is None:
            self.push_task = asyncio.create_task(self._push_loop())

    async def stop(self) -> None:
        if self.feed is not None:
            await self.feed.stop()
        for t in (self.feed_task, self.push_task):
            if t is not None:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        self.feed = self.feed_task = self.push_task = None

    def restart_threadsafe(self) -> None:
        """Called from worker threads after a provider switch / Zerodha login."""
        if self.loop is not None and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._restart_feed(), self.loop)

    async def _restart_feed(self) -> None:
        if self.feed is not None:
            await self.feed.stop()
        if self.feed_task is not None:
            self.feed_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.feed_task
        self.feed = self.feed_task = None
        store.reset()
        await self._start_feed()

    async def _start_feed(self) -> None:
        from ..data.repository import candle_store, list_instruments
        from ..db import SessionLocal
        from ..providers.registry import active_provider

        s = get_settings()
        provider = active_provider()
        db = SessionLocal()
        try:
            insts = list_instruments(db)
            keys = []
            for i in insts:
                tok = int(i.provider_token) if (i.provider_token or "").isdigit() else (abs(hash((i.exchange, i.symbol))) % 2_000_000_000)
                store.register(tok, i.exchange, i.symbol)
                keys.append(((i.exchange, i.symbol), tok, i.instrument_type))
            mode = s.realtime_mode
            if mode == "off":
                self.mode_note = "Realtime feed disabled (REALTIME_MODE=off)"
                return
            if mode == "replay" and s.replay_file:
                from .feeds import TickFileFeed

                self.feed = TickFileFeed(store, s.replay_file, speed=s.replay_speed, loop=True)
                self.mode_note = f"REPLAY of recorded Kite ticks from {s.replay_file} at {s.replay_speed}x (not live)"
            elif mode == "replay":
                bars = {}
                for i in insts:
                    df = candle_store.load(db, i.id, "5m")
                    if len(df):
                        last_day = df.index[-1].date() if s.replay_date is None else pd_date(s.replay_date)
                        bars[(i.exchange, i.symbol)] = df[df.index.date == last_day]
                self.feed = ReplayFeed(store, bars, speed=s.replay_speed)
                self.mode_note = f"REPLAY of stored 5-minute bars at {s.replay_speed}x (not live)"
            elif provider.name == "kite":
                api_key = s.kite_api_key.get_secret_value() if s.kite_api_key else ""
                token = s.kite_access_token.get_secret_value() if s.kite_access_token else ""
                full = [t for (_, t, typ) in keys if typ == "INDEX"]
                quote = [t for (_, t, typ) in keys if typ != "INDEX"]
                if len(keys) <= 400:
                    # Small universe: FULL for everything (exchange timestamps for candles, depth for liquidity).
                    full, quote = full + quote, []
                self.feed = KiteTickerFeed(api_key, token, store, full, quote, record_path=s.record_ticks or None)
                self.mode_note = "Kite WebSocket (KiteTicker), live"
            elif provider.name == "sample":
                self.feed = SimulatedFeed(store, provider.quote, [k for (k, _, _) in keys])
                self.mode_note = "SIMULATED ticks from the sample provider (not real data)"
            else:
                self.mode_note = f"No streaming feed for provider '{provider.name}' (REST quotes only)"
                return
        finally:
            db.close()
        self.feed_task = asyncio.create_task(self.feed.run())

    async def set_full_mode(self, symbols: list[tuple[str, str]]) -> None:
        """Upgrade watchlist/candidate symbols to FULL mode (depth + exchange timestamps)."""
        feed = self.feed
        if not isinstance(feed, KiteTickerFeed) or feed._ws is None:
            return
        token_of = {v: k for k, v in store.tokens.items()}
        toks = [token_of[k] for k in symbols if k in token_of and token_of[k] not in feed.full_tokens]
        if toks:
            import json

            feed.full_tokens += toks
            feed.quote_tokens = [t for t in feed.quote_tokens if t not in set(toks)]
            await feed._ws.send(json.dumps({"a": "mode", "v": ["full", toks]}))
            feed.status.subscribed = {"full": len(feed.full_tokens), "quote": len(feed.quote_tokens)}

    def set_full_mode_threadsafe(self, symbols: list[tuple[str, str]]) -> None:
        if self.loop is not None and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.set_full_mode(symbols), self.loop)

    # ---------------- status ----------------
    def health(self) -> dict[str, Any]:
        from ..providers.registry import active_provider

        now = time.time()
        p = active_provider()
        in_session = p.session_state(_now_ist()) == "OPEN"
        ages = {f"{k[0]}:{k[1]}": round(now - t.recv_ts, 1) for k, t in store.ticks.items()}
        stale = sorted(k for k, a in ages.items() if a > STALE_SECONDS) if in_session else []
        hist = getattr(p, "api_usage", lambda: None)()
        return {"feed": self.feed.status.as_dict() if self.feed else {"source": None, "state": "off"},
                "mode": self.mode_note, "market_open": in_session, "clients": len(self.clients),
                "instruments_with_ticks": len(store.ticks), "registered": len(store.tokens),
                "last_tick_age_s": None if store.last_tick_ts is None else round(now - store.last_tick_ts, 2),
                "stale_threshold_s": STALE_SECONDS, "stale_symbols": stale, **store.metrics.snapshot(),
                "historical_api": hist}

    def freshness(self, exchange: str, symbol: str) -> dict[str, Any]:
        """Per-symbol freshness used by gates: stale if no tick for > 5 s during market hours."""
        from ..providers.registry import active_provider

        t = store.get(exchange, symbol)
        p = active_provider()
        in_session = p.session_state(_now_ist()) == "OPEN"
        streaming = self.feed is not None and self.feed.status.state in ("connected", "replaying")
        if t is None:
            return {"tick_age_s": None, "stale": bool(in_session and streaming), "in_session": in_session,
                    "streaming": streaming}
        age = round(time.time() - t.recv_ts, 1)
        return {"tick_age_s": age, "stale": bool(in_session and age > STALE_SECONDS), "in_session": in_session,
                "streaming": streaming}

    # ---------------- browser fan-out ----------------
    async def handle_client(self, ws: WebSocket) -> None:
        await ws.accept()
        client = Client(ws)
        self.clients.add(client)
        try:
            now = time.time()
            await ws.send_json({"type": "snapshot", "server_ts": now, "status": self._status_msg(),
                                "data": [t.as_wire(now) for t in list(store.ticks.values())]})
            while True:
                msg = await ws.receive_json()
                op = msg.get("op")
                if op == "chart":
                    key = (msg["exchange"], msg["symbol"], msg.get("tf", "5m"))
                    client.charts = {key}
                    bars = store.candles.bars((key[0], key[1]), key[2]) if key[2] in ("1m", "5m") else []
                    await ws.send_json({"type": "bars", "e": key[0], "s": key[1], "tf": key[2],
                                        "bars": [b.as_dict() for b in bars[-300:]]})
                elif op == "unchart":
                    client.charts = set()
                elif op == "latency":
                    store.metrics.client_latencies.extend(float(x) for x in msg.get("samples", [])[:200])
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception:  # noqa: BLE001
            log.debug("ws client error", exc_info=True)
        finally:
            self.clients.discard(client)

    def _status_msg(self) -> dict[str, Any]:
        st = self.feed.status.as_dict() if self.feed else {"source": None, "state": "off"}
        return {"feed": st.get("source"), "state": st.get("state"), "mode": self.mode_note,
                "last_tick_ts": store.last_tick_ts, "stale_after_s": STALE_SECONDS}

    async def _push_loop(self) -> None:
        last_status = 0.0
        while True:
            await asyncio.sleep(PUSH_INTERVAL)
            try:
                changed, bars = store.drain()
                if not self.clients:
                    continue
                now = time.time()
                msgs: list[dict[str, Any]] = []
                if changed:
                    msgs.append({"type": "ticks", "server_ts": now, "data": [t.as_wire(now) for t in changed]})
                if now - last_status > 2:
                    msgs.append({"type": "status", "server_ts": now, **self._status_msg()})
                    last_status = now
                for c in list(self.clients):
                    out = list(msgs)
                    for key, tf, bar, closed in bars:
                        if (key[0], key[1], tf) in c.charts:
                            out.append({"type": "bar", "e": key[0], "s": key[1], "tf": tf, "closed": closed,
                                        "bar": bar.as_dict()})
                    for m in out:
                        try:
                            await c.ws.send_json(m)
                        except Exception:  # noqa: BLE001
                            self.clients.discard(c)
                            break
            except Exception:  # noqa: BLE001
                log.exception("realtime push loop error")


def _now_ist():  # noqa: ANN202
    from ..providers.sample import now_ist

    return now_ist()


def pd_date(s: str):  # noqa: ANN201
    from datetime import date

    return date.fromisoformat(s)


service = RealtimeService()
