"""Real-time layer and execution-model tests: Kite binary protocol, candle builder, live store, replay and
KiteTicker feeds (fake socket), the shared setup state machine, costs/slippage, corporate-action guard and a
generic no-look-ahead check across every strategy."""

import asyncio
import contextlib
import json
import struct
from datetime import date, datetime

import pandas as pd
import pytest

from app.data.corporate_actions import adjust_for_splits, detect_splits
from app.data.sample_generator import generate_daily
from app.quant.features import arrays
from app.quant.strategies.base import all_strategy_ids, get_strategy
from app.realtime.candles import CandleBuilder, bucket_start
from app.realtime.feeds import KiteTickerFeed, ReplayFeed
from app.realtime.protocol import build_message, build_packet, parse_message, parse_packet
from app.realtime.store import LiveStore
from app.risk.costs import fo_order_costs, liquidity_slippage_bps
from app.signals.setup_fsm import (
    ACTIVE, EXPIRED, INVALIDATED, SETUP, TRIGGERED, Plan, on_bar_close, on_next_open, on_tick, trigger_level)

IST = "Asia/Kolkata"


def ist(s: str) -> int:
    return int(pd.Timestamp(s, tz=IST).timestamp())


ITC, NIFTY = 424961, 256265  # NSE equity token / NSE index token (segment byte 9)


# ---------------------------------------------------------------- protocol

def test_protocol_equity_full_round_trip():
    ets = ist("2026-10-16 10:00:01")
    p = build_packet(ITC, 263.9, volume=12_345_678, ohlc=(260.0, 265.25, 259.1, 262.0), exchange_ts=ets,
                     last_trade_ts=ets - 1, last_qty=10)
    assert len(p) == 184
    t = parse_packet(p)
    assert t["mode"] == "full" and t["tradable"] is True
    assert t["ltp"] == pytest.approx(263.9) and t["close"] == pytest.approx(262.0)
    assert (t["open"], t["high"], t["low"]) == pytest.approx((260.0, 265.25, 259.1))
    assert t["volume"] == 12_345_678 and t["exchange_ts"] == ets and t["last_trade_ts"] == ets - 1
    assert len(t["depth"]["buy"]) == 5 and len(t["depth"]["sell"]) == 5


def test_protocol_quote_ltp_index_and_heartbeat():
    q = parse_packet(build_packet(ITC, 263.9, mode="quote", volume=5))
    assert q["mode"] == "quote" and "exchange_ts" not in q and q["volume"] == 5
    lt = parse_packet(build_packet(ITC, 263.95, mode="ltp"))
    assert lt["mode"] == "ltp" and lt["ltp"] == pytest.approx(263.95)
    idx = parse_packet(build_packet(NIFTY, 25010.5, index=True, ohlc=(24900, 25050, 24880, 24950.25), exchange_ts=7))
    assert idx["mode"] == "full" and idx["tradable"] is False and idx["exchange_ts"] == 7
    assert idx["change"] == pytest.approx(25010.5 - 24950.25)
    assert parse_message(b"\x00") == []  # 1-byte heartbeat
    msg = build_message([build_packet(ITC, 264.0, mode="ltp"), build_packet(NIFTY, 25000.0, index=True)])
    assert [t["token"] for t in parse_message(msg)] == [ITC, NIFTY]
    assert struct.unpack(">h", msg[:2])[0] == 2


# ---------------------------------------------------------------- candles + store

def test_candle_builder_buckets_volume_and_close():
    cb = CandleBuilder(("1m", "5m"))
    t0 = ist("2026-10-16 10:02:30")
    assert bucket_start(t0, 300) == ist("2026-10-16 10:00:00")
    cb.on_tick("X", 100.0, t0, 1000)
    cb.on_tick("X", 101.0, t0 + 20, 1500)
    cb.on_tick("X", 99.5, t0 + 100, 1800)
    five = cb.bars("X", "5m")
    assert len(five) == 1
    b = five[0]
    assert (b.open, b.high, b.low, b.close, b.volume) == (100.0, 101.0, 99.5, 99.5, 800)
    assert b.partial is True  # first bar observed mid-period
    ev = cb.on_tick("X", 102.0, ist("2026-10-16 10:05:01"), 2000)
    closed = [(tf, bar) for tf, bar, c in ev if c and tf == "5m"]
    assert closed and closed[0][1].ts == ist("2026-10-16 10:00:00")
    nxt = cb.bars("X", "5m")[-1]
    assert nxt.partial is False and nxt.volume == 200
    assert cb.on_tick("X", 103.0, ist("2026-10-16 09:00:00"), 2100) == []  # pre-open: ignored
    assert cb.on_tick("X", 50.0, t0, 2100) == []  # out-of-order tick from an older bar: ignored
    due = cb.close_due("X", ist("2026-10-16 10:10:00"))
    assert {tf for tf, _ in due} == {"1m", "5m"}


def test_store_apply_drain_and_latency():
    s = LiveStore()
    s.register(ITC, "NSE", "ITC")
    ets = ist("2026-10-16 11:00:00")
    raw = parse_packet(build_packet(ITC, 263.9, volume=100, ohlc=(260, 265, 259, 262), exchange_ts=ets))
    t = s.apply(raw, "kite", recv_ts=ets + 0.4)
    assert t.prev_close == 262 and t.change == pytest.approx(1.9) and t.exchange_ts == ets
    assert s.apply({"token": 999, "ltp": 1.0}) is None  # unknown token
    changed, bars = s.drain()
    assert [c.symbol for c in changed] == ["ITC"] and bars
    assert s.drain() == ([], [])
    assert s.metrics.snapshot()["exchange_to_server_p95_s"] == pytest.approx(0.4)
    assert t.as_wire()["lp"] == 263.9


def test_replay_feed_rebuilds_bars_from_stored_candles():
    s = LiveStore()
    s.register(ITC, "NSE", "ITC")
    idx = pd.DatetimeIndex([pd.Timestamp("2026-10-16 09:15", tz=IST), pd.Timestamp("2026-10-16 09:20", tz=IST)])
    df = pd.DataFrame({"open": [262.0, 263.0], "high": [264.0, 263.5], "low": [261.5, 262.2],
                       "close": [263.0, 262.4], "volume": [6000.0, 1200.0]}, index=idx)
    feed = ReplayFeed(s, {("NSE", "ITC"): df}, speed=1e9, loop=False)
    asyncio.run(feed.run())
    assert s.get("NSE", "ITC").ltp == pytest.approx(262.4) and s.get("NSE", "ITC").source == "replay"
    b1, b2 = s.candles.bars(("NSE", "ITC"), "5m")
    assert (b1.open, b1.high, b1.low, b1.close) == (262.0, 264.0, 261.5, 263.0)
    assert (b2.open, b2.high, b2.low, b2.close) == (263.0, 263.5, 262.2, 262.4)
    assert b2.volume == pytest.approx(1200.0)


# ---------------------------------------------------------------- KiteTicker client with a fake socket

class FakeWS:
    def __init__(self, frames, feed):
        self.frames, self.feed, self.sent = list(frames), feed, []

    async def send(self, m):
        self.sent.append(json.loads(m))

    async def recv(self):
        if self.frames:
            return self.frames.pop(0)
        await self.feed.stop()
        raise ConnectionError("closed")

    async def close(self):
        pass


def test_kite_ticker_subscribes_parses_and_reconnects():
    s = LiveStore()
    s.register(ITC, "NSE", "ITC")
    calls = {"n": 0}
    holder = {}

    @contextlib.asynccontextmanager
    async def connect(url, **kw):
        calls["n"] += 1
        assert "api_key=k" in url and "access_token=t" in url
        if calls["n"] == 1:
            raise ConnectionError("network down")  # first attempt fails -> backoff -> reconnect
        ws = FakeWS([b"\x00", build_message([build_packet(ITC, 264.1, exchange_ts=ist("2026-10-16 10:00:00"))]),
                     json.dumps({"type": "order", "data": {}})], holder["feed"])
        holder["ws"] = ws
        yield ws

    feed = KiteTickerFeed("k", "t", s, full_tokens=[ITC], quote_tokens=[ITC, 1], connect=connect)
    holder["feed"] = feed
    asyncio.run(feed.run())
    assert calls["n"] == 2 and feed.status.reconnects == 1 and feed.status.state == "stopped"
    sent = holder["ws"].sent
    assert sent[0] == {"a": "subscribe", "v": [ITC, 1]}
    assert {"a": "mode", "v": ["full", [ITC]]} in sent and {"a": "mode", "v": ["quote", [1]]} in sent
    assert s.get("NSE", "ITC").ltp == pytest.approx(264.1)


def test_kite_ticker_stops_on_auth_error():
    class Resp:
        status_code = 403

    class Rejected(Exception):
        response = Resp()

    @contextlib.asynccontextmanager
    async def connect(url, **kw):
        raise Rejected("forbidden")
        yield  # pragma: no cover

    feed = KiteTickerFeed("k", "t", LiveStore(), [ITC], [], connect=connect)
    asyncio.run(feed.run())
    assert feed.status.state == "auth_error" and "access_token" not in (feed.status.last_error or "")


# ---------------------------------------------------------------- setup state machine

def test_fsm_long_trigger_fill_and_expiry():
    p = Plan("LONG", trigger=100.5, stop=98.0, expiry_bars=2)
    assert on_bar_close(p, 100.4, 99.0, 100.2) == SETUP
    assert on_bar_close(p, 101.0, 99.5, 100.6) == TRIGGERED
    assert on_next_open(p, 100.8, slippage=0.001) == ACTIVE and p.fill == pytest.approx(100.9008)
    e = Plan("LONG", 100.5, 98.0, expiry_bars=2)
    on_bar_close(e, 100.4, 99.0, 100.0)
    assert on_bar_close(e, 100.4, 99.0, 100.0) == EXPIRED
    g = Plan("LONG", 100.5, 98.0)
    on_bar_close(g, 101, 100, 100.6)
    assert on_next_open(g, 97.9) == INVALIDATED and g.fill is None


def test_fsm_short_and_stop_before_trigger_and_ticks():
    s = Plan("SHORT", trigger=99.5, stop=102.0)
    assert on_bar_close(s, 100.0, 99.0, 99.4) == TRIGGERED
    assert on_next_open(s, 99.0, slippage=0.001) == ACTIVE and s.fill == pytest.approx(98.901)
    inv = Plan("SHORT", 99.5, 102.0)
    assert on_bar_close(inv, 102.5, 99.0, 99.0) == INVALIDATED  # stop takes priority over a same-bar trigger
    t = Plan("LONG", 100.5, 98.0)
    assert on_tick(t, 100.9) == SETUP  # tick-triggering disabled by default (closed bars confirm)
    assert on_tick(t, 100.9, allow_tick_trigger=True) == TRIGGERED
    t2 = Plan("LONG", 100.5, 98.0)
    assert on_tick(t2, 97.9) == INVALIDATED
    assert trigger_level("signal_high", "LONG", 101, 99, 100) == 101
    assert trigger_level("signal_high", "SHORT", 101, 99, 100) == 99
    assert trigger_level("signal_close", "LONG", 101, 99, 100) == 100


# ---------------------------------------------------------------- costs, slippage, corporate actions

def test_liquidity_slippage_tiers_and_impact():
    assert liquidity_slippage_bps(600) == 3 and liquidity_slippage_bps(150) == 5
    assert liquidity_slippage_bps(2) == 40 and liquidity_slippage_bps(None) == 40
    # 1 lakh order on 100 cr ADV: 2 * sqrt(1e5 / 1e9) * 100 = 2 bps impact
    assert liquidity_slippage_bps(100, 1e5) == pytest.approx(7.0)


def test_fo_costs():
    fut = fo_order_costs("SELL", 1_000_000, "FUT")
    assert fut["brokerage"] == 20.0 and fut["stt"] == pytest.approx(200.0) and fut["stamp_duty"] == 0
    opt = fo_order_costs("BUY", 10_000, "OPT")
    assert opt["brokerage"] == 20.0 and opt["stt"] == 0 and opt["stamp_duty"] == pytest.approx(0.3)
    assert opt["total"] > opt["brokerage"]


def test_split_detection_and_back_adjustment():
    idx = pd.bdate_range("2024-01-01", periods=6).tz_localize(IST)
    df = pd.DataFrame({"open": [1000, 1010, 1005, 503, 505, 508.0], "high": [1015, 1020, 1012, 510, 512, 515.0],
                       "low": [995, 1000, 998, 500, 501, 503.0], "close": [1010, 1004, 1006, 506, 507, 510.0],
                       "volume": [100, 100, 100, 200, 200, 200.0]}, index=idx)
    ev = detect_splits(df)
    assert len(ev) == 1 and ev[0]["ratio"] == 2.0 and ev[0]["date"] == idx[3].isoformat()
    adj, _ = adjust_for_splits(df)
    assert adj["close"].iloc[2] == pytest.approx(503.0) and adj["volume"].iloc[0] == 200
    assert adj["close"].iloc[3:].tolist() == df["close"].iloc[3:].tolist()
    normal = generate_daily(3, date(2023, 1, 2), date(2023, 12, 29))
    assert all(detect_splits(d) == [] for d in normal.values())


# ---------------------------------------------------------------- no look-ahead, every strategy

def test_no_strategy_uses_future_bars():
    from app.providers.sample import SampleProvider

    daily = next(v for v in generate_daily(11, date(2022, 1, 3), date(2024, 12, 31)).values() if v["volume"].sum())
    sp = SampleProvider(intraday_sessions=6, clock=lambda: datetime(2025, 6, 27, 18, 0, tzinfo=pd.Timestamp.now(IST).tzinfo))
    intra = sp.candles("RELIANCE", "NSE", "5m")
    for sid in all_strategy_ids():
        strat = get_strategy(sid)
        df = intra if strat.intraday else daily
        cut = int(len(df) * 0.7)
        full, trunc = arrays(strat.prepare(df)), arrays(strat.prepare(df.iloc[:cut]))
        for i in range(strat.warmup + 1, cut):
            a1, a2 = strat.entry(full, i), strat.entry(trunc, i)
            assert (a1 is None) == (a2 is None), f"{sid}: look-ahead at bar {i}"
            if a1 is not None:
                assert a1.stop == pytest.approx(a2.stop), f"{sid}: stop uses future data at bar {i}"
