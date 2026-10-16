"""Kite Connect WebSocket (KiteTicker) binary protocol parser.

Spec: https://kite.trade/docs/connect/v3/websocket/  (prices in paise -> /100; CDS /1e7).
Message = int16 packet count, then for each packet: int16 length + packet bytes (big-endian).
Equity packets: 8 (ltp), 44 (quote), 184 (full). Index packets: 8 (ltp), 28 (quote), 32 (full).
"""

from __future__ import annotations

import struct
from typing import Any

SEGMENT_CDS = 3  # currency segment -> divisor 1e7
INDEX_SEGMENT = 9


def _divisor(token: int) -> float:
    return 1e7 if (token & 0xFF) == SEGMENT_CDS else 100.0


def parse_packet(p: bytes) -> dict[str, Any] | None:
    n = len(p)
    if n < 8:
        return None
    token, ltp = struct.unpack(">ii", p[:8])
    div = _divisor(token)
    tick: dict[str, Any] = {"token": token, "ltp": ltp / div, "mode": "ltp", "tradable": (token & 0xFF) != INDEX_SEGMENT}
    if n == 8:
        return tick
    if n in (28, 32):  # index quote/full
        high, low, open_, close, change = struct.unpack(">iiiii", p[8:28])
        tick.update({"mode": "quote" if n == 28 else "full", "high": high / div, "low": low / div,
                     "open": open_ / div, "close": close / div, "change": change / div, "tradable": False})
        if n == 32:
            tick["exchange_ts"] = struct.unpack(">i", p[28:32])[0]
        return tick
    if n >= 44:
        ltq, atp, vol, tbq, tsq, open_, high, low, close = struct.unpack(">iiiiiiiii", p[8:44])
        tick.update({"mode": "quote", "last_qty": ltq, "avg_price": atp / div, "volume": vol,
                     "buy_qty": tbq, "sell_qty": tsq, "open": open_ / div, "high": high / div, "low": low / div,
                     "close": close / div})
        if n >= 64:
            ltt, oi, oi_hi, oi_lo, ets = struct.unpack(">iiiii", p[44:64])
            tick.update({"mode": "full", "last_trade_ts": ltt, "oi": oi, "oi_day_high": oi_hi,
                         "oi_day_low": oi_lo, "exchange_ts": ets})
        if n >= 184:
            depth = {"buy": [], "sell": []}
            for i in range(10):
                q, pr, orders = struct.unpack(">iih", p[64 + i * 12: 64 + i * 12 + 10])
                depth["buy" if i < 5 else "sell"].append({"qty": q, "price": pr / div, "orders": orders})
            tick["depth"] = depth
        return tick
    return tick


def parse_message(data: bytes) -> list[dict[str, Any]]:
    """Parse one binary WebSocket frame. 1-byte frames are heartbeats -> []."""
    if len(data) < 2:
        return []
    count = struct.unpack(">h", data[:2])[0]
    out = []
    off = 2
    for _ in range(count):
        if off + 2 > len(data):
            break
        ln = struct.unpack(">h", data[off:off + 2])[0]
        off += 2
        t = parse_packet(data[off:off + ln])
        off += ln
        if t is not None:
            out.append(t)
    return out


def build_packet(token: int, ltp: float, *, index: bool = False, mode: str = "full", volume: int = 0,
                 ohlc: tuple[float, float, float, float] | None = None, exchange_ts: int = 0,
                 last_trade_ts: int = 0, last_qty: int = 0) -> bytes:
    """Encode a packet in Kite's format (used by tests and the replay feed)."""
    o, h, l, c = ohlc or (ltp, ltp, ltp, ltp)  # noqa: E741
    P = lambda x: int(round(x * 100))  # noqa: E731
    if mode == "ltp":
        return struct.pack(">ii", token, P(ltp))
    if index:
        body = struct.pack(">iiiiiii", token, P(ltp), P(h), P(l), P(o), P(c), P(ltp - c))
        return body + (struct.pack(">i", exchange_ts) if mode == "full" else b"")
    body = struct.pack(">iiiiiiiiiii", token, P(ltp), last_qty, P(ltp), volume, 0, 0, P(o), P(h), P(l), P(c))
    if mode == "quote":
        return body
    body += struct.pack(">iiiii", last_trade_ts, 0, 0, 0, exchange_ts)
    return body + b"\x00" * 120


def build_message(packets: list[bytes]) -> bytes:
    out = struct.pack(">h", len(packets))
    for p in packets:
        out += struct.pack(">h", len(p)) + p
    return out
