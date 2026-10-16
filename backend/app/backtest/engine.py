"""Bar-by-bar, shared-capital portfolio backtester.

Execution model (documented assumptions):
- Signals are evaluated on a bar's CLOSE and create a SETUP with a trigger level (signals/setup_fsm.py). A later
  closed bar crossing the trigger makes it TRIGGERED; the entry fills at the NEXT bar's OPEN plus adverse slippage.
  Setups expire after `strategy.expiry_bars` bars or are invalidated if the stop is hit first.
- Stops: if the open gaps through the stop, exit at the open; otherwise at the stop price; adverse slippage applied.
- Targets: limit-style fill at the target (or at the open if it gaps beyond); no slippage.
- If stop and target both fall inside the same bar, the STOP is assumed to fill first (conservative).
- Strategy close-based exits and max-holding exits fill at the next bar's open.
- Intraday strategies are flattened at the close of the last bar of each session.
- All symbols share one capital pool. Position size = fixed-fractional risk on current equity, capped by
  max allocation per position, available cash (no leverage) and a maximum number of open positions.
  When more entries trigger at the same open than slots/cash allow, symbols are filled in universe order
  (documented selection rule; no look-ahead ranking).
- Costs: round-trip costs from the configurable cost model are deducted when a trade closes.
- Idle cash earns nothing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ..quant.features import arrays
from ..quant.strategies.base import Setup, Strategy
from ..signals.setup_fsm import ACTIVE, SETUP, TRIGGERED, Plan, on_bar_close, on_next_open, trigger_level
from ..risk.costs import CostModel, product_for_mode, round_trip_costs
from ..risk.sizing import position_size


@dataclass
class BTConfig:
    capital: float = 1_000_000.0
    risk_pct: float = 1.0
    max_alloc_pct: float = 15.0
    max_positions: int = 10
    slippage_bps: float = 5.0
    costs: CostModel = field(default_factory=CostModel)
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None
    max_stop_pct: float | None = None  # skip setups whose stop is wider than this % of entry (per-mode cap)
    slippage_fn: object | None = None  # optional (symbol, exchange) -> bps (liquidity-based slippage)

    def with_window(self, capital: float, start: pd.Timestamp | None, end: pd.Timestamp | None) -> "BTConfig":
        return BTConfig(capital, self.risk_pct, self.max_alloc_pct, self.max_positions, self.slippage_bps,
                        self.costs, start, end, self.max_stop_pct, self.slippage_fn)


@dataclass
class Trade:
    symbol: str
    exchange: str
    direction: str
    entry_ts: str
    entry_price: float
    exit_ts: str
    exit_price: float
    qty: int
    stop: float
    target: float | None
    exit_reason: str
    gross_pnl: float
    costs: float
    net_pnl: float
    r_multiple: float
    bars_held: int
    return_pct: float
    entry_value: float
    exit_value: float
    signal_reasons: list[str] = field(default_factory=list)
    mfe_r: float = 0.0  # maximum favourable excursion while held, in R
    mae_r: float = 0.0  # maximum adverse excursion while held, in R
    signal_ts: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _Sym:
    __slots__ = ("symbol", "exchange", "a", "idx", "map", "last_i", "pos", "pending", "pending_exit",
                 "last_entry_date", "last_close", "n", "slip")

    def __init__(self, symbol: str, exchange: str, a: dict[str, list], idx: pd.DatetimeIndex,
                 win_idx: np.ndarray) -> None:
        self.symbol, self.exchange, self.a, self.idx = symbol, exchange, a, idx
        self.map = dict(zip(idx.as_unit("ns").asi8[win_idx].tolist(), win_idx.tolist(), strict=True))
        self.last_i = int(win_idx[-1]) if len(win_idx) else -1
        self.pos: dict[str, Any] | None = None
        self.pending: tuple[Setup, int] | None = None
        self.pending_exit: str | None = None
        self.last_entry_date = None
        self.last_close: float | None = None
        self.n = len(idx)
        self.slip = 0.0


def run_portfolio(strategy: Strategy, data: dict[tuple[str, str], pd.DataFrame], cfg: BTConfig,
                  prepared: dict[tuple[str, str], pd.DataFrame] | None = None) -> dict[str, Any]:
    syms: list[_Sym] = []
    for (exch, sym), df in data.items():
        p = prepared[(exch, sym)] if prepared and (exch, sym) in prepared else strategy.prepare(df)
        idx = p.index
        win = np.ones(len(p), dtype=bool)
        if cfg.start is not None:
            win &= np.asarray(idx >= cfg.start)
        if cfg.end is not None:
            win &= np.asarray(idx <= cfg.end)
        win[: max(strategy.warmup, 1)] = False
        win_idx = np.flatnonzero(win)
        if len(win_idx):
            syms.append(_Sym(sym, exch, arrays(p), idx, win_idx))
    timeline = sorted({t for s in syms for t in s.map})
    trades: list[Trade] = []
    if not timeline:
        return {"equity": pd.Series(dtype=float), "trades": trades, "exposure": 0.0, "skipped_entries": 0}

    for s in syms:
        bps = cfg.slippage_fn(s.symbol, s.exchange) if callable(cfg.slippage_fn) else cfg.slippage_bps
        s.slip = float(bps) / 10_000.0
    product = product_for_mode(strategy.meta.modes[0])
    skipped_stop_cap = 0
    realized = cfg.capital
    equity_vals: list[float] = []
    bars_with_pos = 0
    skipped = 0
    tz = syms[0].idx.tz

    def open_value() -> float:
        return sum(s.pos["entry"] * s.pos["qty"] for s in syms if s.pos is not None)

    def mtm() -> float:
        u = 0.0
        for s in syms:
            if s.pos is not None and s.last_close is not None:
                sign = 1.0 if s.pos["dir"] == "LONG" else -1.0
                u += (s.last_close - s.pos["entry"]) * s.pos["qty"] * sign
        return realized + u

    def close_pos(s: _Sym, i: int, px: float, reason: str) -> None:
        nonlocal realized
        pos = s.pos
        assert pos is not None
        sign = 1.0 if pos["dir"] == "LONG" else -1.0
        gross = (px - pos["entry"]) * pos["qty"] * sign
        rt = round_trip_costs(pos["dir"], pos["entry"], px, pos["qty"], product, s.exchange, cfg.costs)
        net = gross - rt["total"]
        realized += net
        risk = abs(pos["entry"] - pos["stop"]) * pos["qty"]
        risk_u = abs(pos["entry"] - pos["stop"]) or 1e-9
        trades.append(Trade(
            s.symbol, s.exchange, pos["dir"], s.idx[pos["i"]].isoformat(), round(pos["entry"], 2),
            s.idx[i].isoformat(), round(px, 2), pos["qty"], round(pos["stop"], 2),
            None if pos["target"] is None else round(pos["target"], 2), reason, round(gross, 2),
            round(rt["total"], 2), round(net, 2), round(net / risk, 3) if risk else 0.0, i - pos["i"] + 1,
            round(net / (pos["entry"] * pos["qty"]) * 100.0, 3), round(pos["entry"] * pos["qty"], 2),
            round(px * pos["qty"], 2), pos["reasons"], round(pos["mfe"] / risk_u, 3), round(pos["mae"] / risk_u, 3),
            s.idx[pos["sig_i"]].isoformat()))
        s.pos = None

    for t in timeline:
        active = [(s, s.map[t]) for s in syms if t in s.map]
        # 1) exits scheduled for this open
        for s, i in active:
            slip = s.slip
            if s.pos is not None and s.pending_exit is not None:
                o = s.a["open"][i]
                close_pos(s, i, o * (1 - slip) if s.pos["dir"] == "LONG" else o * (1 + slip), s.pending_exit)
                s.pending_exit = None
        # 2) entries: a TRIGGERED setup fills at this bar's open (state machine shared with the live scanner)
        for s, i in active:
            if s.pos is not None or s.pending is None:
                continue
            setup, sig_i, plan = s.pending
            if plan.state != TRIGGERED:
                continue
            s.pending = None
            if strategy.intraday and s.a["_date"][i] != s.a["_date"][sig_i]:
                continue
            long = setup.direction == "LONG"
            if on_next_open(plan, s.a["open"][i], s.slip) != ACTIVE:
                continue
            fill = plan.fill
            if cfg.max_stop_pct is not None and abs(fill - setup.stop) / fill * 100.0 > cfg.max_stop_pct:
                skipped_stop_cap += 1
                continue
            n_open = sum(1 for x in syms if x.pos is not None)
            if n_open >= cfg.max_positions:
                skipped += 1
                continue
            eq_now = mtm()
            cash = realized - open_value()
            size = position_size(eq_now, fill, setup.stop, cfg.risk_pct, cfg.max_alloc_pct, cash_available=cash)
            if size.qty < 1:
                skipped += 1
                continue
            risk_u = abs(fill - setup.stop)
            tgt = None
            if setup.target_r:
                tgt = fill + setup.target_r * risk_u if long else fill - setup.target_r * risk_u
            s.pos = {"dir": setup.direction, "entry": fill, "stop": setup.stop, "target": tgt, "qty": size.qty,
                     "i": i, "max_hold": setup.max_hold_bars, "reasons": setup.reasons, "mfe": 0.0, "mae": 0.0,
                     "sig_i": sig_i}
            s.last_entry_date = s.a["_date"][i]
        # 3) intrabar stop/target, close-based exits, new signals
        for s, i in active:
            a = s.a
            o, h, lo, c = a["open"][i], a["high"][i], a["low"][i], a["close"][i]
            slip = s.slip
            s.last_close = c
            if s.pos is not None:
                long = s.pos["dir"] == "LONG"
                st, tg = s.pos["stop"], s.pos["target"]
                fav = (h - s.pos["entry"]) if long else (s.pos["entry"] - lo)
                adv = (s.pos["entry"] - lo) if long else (h - s.pos["entry"])
                s.pos["mfe"] = max(s.pos["mfe"], fav)
                s.pos["mae"] = max(s.pos["mae"], adv)
                exit_px, reason = None, None
                if long:
                    if o <= st:
                        exit_px, reason = o * (1 - slip), "Stop (gap through)"
                    elif lo <= st:
                        exit_px, reason = st * (1 - slip), "Stop loss"
                    elif tg is not None and o >= tg:
                        exit_px, reason = o, "Target (gap)"
                    elif tg is not None and h >= tg:
                        exit_px, reason = tg, "Target"
                else:
                    if o >= st:
                        exit_px, reason = o * (1 + slip), "Stop (gap through)"
                    elif h >= st:
                        exit_px, reason = st * (1 + slip), "Stop loss"
                    elif tg is not None and o <= tg:
                        exit_px, reason = o, "Target (gap)"
                    elif tg is not None and lo <= tg:
                        exit_px, reason = tg, "Target"
                if exit_px is not None:
                    close_pos(s, i, float(exit_px), str(reason))
            if s.pos is not None:
                long = s.pos["dir"] == "LONG"
                session_end = strategy.flat_at_session_end and (i == s.n - 1 or a["_date"][i + 1] != a["_date"][i])
                if session_end:
                    close_pos(s, i, c * (1 - slip) if long else c * (1 + slip), "Session end (flat)")
                elif i == s.last_i:
                    close_pos(s, i, c, "End of test period")
                else:
                    r = strategy.exit(a, i, s.pos["dir"])
                    if r is None and i - s.pos["i"] + 1 >= s.pos["max_hold"]:
                        r = "Max holding period"
                    s.pending_exit = r
            # advance a waiting SETUP with this closed bar (trigger / invalidate / expire)
            if s.pos is None and s.pending is not None and s.pending[2].state == SETUP and i > s.pending[1]:
                plan = s.pending[2]
                if strategy.intraday and a["_date"][i] != a["_date"][s.pending[1]]:
                    s.pending = None
                elif on_bar_close(plan, h, lo, c) not in (SETUP, TRIGGERED):
                    s.pending = None
            if s.pos is None and s.pending is None and i < s.last_i:
                if not (strategy.one_trade_per_session and s.last_entry_date == a["_date"][i]):
                    setup = strategy.entry(a, i)
                    if setup is not None:
                        trig = trigger_level(strategy.trigger_rule, setup.direction, h, lo, c)
                        s.pending = (setup, i, Plan(setup.direction, trig, setup.stop, strategy.expiry_bars))
        if any(s.pos is not None for s in syms):
            bars_with_pos += 1
        equity_vals.append(mtm())

    eq_index = pd.DatetimeIndex(pd.to_datetime(timeline, unit="ns", utc=True)).tz_convert(tz)
    equity = pd.Series(equity_vals, index=eq_index)
    trades.sort(key=lambda x: x.entry_ts)
    return {"equity": equity, "trades": trades, "exposure": bars_with_pos / len(timeline), "skipped_entries": skipped,
            "skipped_stop_cap": skipped_stop_cap}
