"""Periodic-rebalance portfolio backtester (cross-sectional momentum, factor baskets, sector rotation, dual momentum).

Execution model:
- Selection is computed on the CLOSE of the last trading day of each period (month by default) using data up to
  that close only; orders execute at the NEXT session's OPEN with adverse slippage (same as the event engine).
- Names leaving the target set are sold; new names are bought at equal weight (1/N of current equity, cash
  permitting). Continuing holdings are not resized (limits turnover; documented).
- Costs: delivery (CNC) round-trip costs deducted when each holding is closed. Idle cash can earn a configurable
  liquid-fund yield (default 0 - only dual momentum uses it, and says so).
- Trade R multiple: net P&L / (qty x 2 x ATR14 at entry) - a volatility risk unit, because rebalance strategies
  have no stop. This keeps expectancy comparable with stop-based strategies (documented approximation).
"""

from __future__ import annotations

from typing import Any, Callable

import numpy as np
import pandas as pd

from ..quant import indicators as ind
from ..risk.costs import round_trip_costs
from .engine import BTConfig, Trade

Key = tuple[str, str]
Selector = Callable[[int, "Panel"], list[Key]]


class Panel:
    """Aligned daily matrices (dates x symbols) for vectorised, causal selection."""

    def __init__(self, data: dict[Key, pd.DataFrame]) -> None:
        self.keys = list(data.keys())
        idx = sorted(set().union(*[set(df.index) for df in data.values()])) if data else []
        self.index = pd.DatetimeIndex(idx)
        cols = {}
        for f in ("open", "close", "high", "low", "volume"):
            cols[f] = pd.DataFrame({k: data[k][f] for k in self.keys}).reindex(self.index)
        self.open = cols["open"].to_numpy(float)
        self.close = cols["close"].to_numpy(float)
        self.close_ff = cols["close"].ffill().to_numpy(float)
        self.value = (cols["close"] * cols["volume"]).to_numpy(float)
        self.atr = np.column_stack([ind.atr(cols["high"][k], cols["low"][k], cols["close"][k], 14).to_numpy(float)
                                    for k in self.keys]) if self.keys else np.empty((len(self.index), 0))
        c = cols["close"].ffill()
        logp = np.log(c.where(c > 0))
        self.logret = logp.diff().to_numpy(float)
        self.col = {k: j for j, k in enumerate(self.keys)}

    def ret(self, t: int, back: int, skip: int = 0) -> np.ndarray:
        """Return from t-back to t-skip (NaN where unavailable)."""
        if t - back < 0:
            return np.full(len(self.keys), np.nan)
        with np.errstate(divide="ignore", invalid="ignore"):
            return self.close_ff[t - skip] / self.close_ff[t - back] - 1.0

    def vol(self, t: int, n: int = 252) -> np.ndarray:
        if t - n < 0:
            return np.full(len(self.keys), np.nan)
        w = self.logret[t - n + 1:t + 1]
        with np.errstate(invalid="ignore"):
            out = np.nanstd(w, axis=0, ddof=1) * np.sqrt(252)
        out[np.sum(~np.isnan(w), axis=0) < n * 0.8] = np.nan
        return out

    def liquid(self, t: int, min_cr: float = 10.0) -> np.ndarray:
        w = self.value[max(0, t - 19):t + 1]
        cnt = np.sum(~np.isnan(w), axis=0)
        tot = np.nansum(w, axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(cnt > 0, tot / np.maximum(cnt, 1), 0.0) / 1e7 >= min_cr


def period_ends(index: pd.DatetimeIndex, freq: str = "M") -> set[int]:
    per = index.tz_localize(None).to_period(freq)
    last = pd.Series(range(len(index)), index=per).groupby(level=0).max()
    return set(last.tolist())


def run_rebalance(select: Selector, data: dict[Key, pd.DataFrame], cfg: BTConfig, freq: str = "M",
                  cash_yield: float = 0.0, max_names: int = 30, panel: Panel | None = None) -> dict[str, Any]:
    pnl = panel or Panel(data)
    idx = pnl.index
    if len(idx) == 0:
        return {"equity": pd.Series(dtype=float), "trades": [], "exposure": 0.0, "skipped_entries": 0}
    win = np.ones(len(idx), dtype=bool)
    if cfg.start is not None:
        win &= np.asarray(idx >= cfg.start)
    if cfg.end is not None:
        win &= np.asarray(idx <= cfg.end)
    win[:260] = False  # one year of history for lookbacks
    days = np.flatnonzero(win)
    if len(days) < 2:
        return {"equity": pd.Series(dtype=float), "trades": [], "exposure": 0.0, "skipped_entries": 0}
    ends = period_ends(idx, freq)
    slip = {k: (cfg.slippage_fn(k[1], k[0]) if callable(cfg.slippage_fn) else cfg.slippage_bps) / 1e4 for k in pnl.keys}
    cash = cfg.capital
    hold: dict[Key, dict[str, Any]] = {}
    trades: list[Trade] = []
    eq_vals, invested_days, skipped = [], 0, 0
    pending: list[Key] | None = None
    daily_y = (1 + cash_yield) ** (1 / 252) - 1

    def close_pos(k: Key, t: int, px: float, reason: str) -> None:
        nonlocal cash
        h = hold.pop(k)
        cash += px * h["qty"]
        rt = round_trip_costs("LONG", h["entry"], px, h["qty"], "CNC", k[0], cfg.costs)
        cash -= rt["total"] - h["entry_cost_paid"]
        gross = (px - h["entry"]) * h["qty"]
        net = gross - rt["total"]
        risk = max(2 * h["atr"] * h["qty"], 1e-9)
        trades.append(Trade(k[1], k[0], "LONG", idx[h["i"]].isoformat(), round(h["entry"], 2), idx[t].isoformat(),
                            round(px, 2), h["qty"], round(h["entry"] - 2 * h["atr"], 2), None, reason, round(gross, 2),
                            round(rt["total"], 2), round(net, 2), round(net / risk, 3), t - h["i"] + 1,
                            round(net / (h["entry"] * h["qty"]) * 100, 3), round(h["entry"] * h["qty"], 2),
                            round(px * h["qty"], 2), h["reasons"], 0.0, 0.0, idx[h["sig"]].isoformat()))

    for t in days:
        cash *= 1 + daily_y
        if pending is not None:
            target = set(pending[:max_names])
            for k in [k for k in hold if k not in target]:
                o = pnl.open[t, pnl.col[k]]
                px = (o if o == o else pnl.close_ff[t, pnl.col[k]]) * (1 - slip[k])
                close_pos(k, t, px, "Rebalance: dropped from selection")
            eq_open = cash + sum(h["qty"] * (pnl.open[t, pnl.col[k]] if pnl.open[t, pnl.col[k]] == pnl.open[t, pnl.col[k]]
                                             else pnl.close_ff[t, pnl.col[k]]) for k, h in hold.items())
            alloc = eq_open / max(len(target), 1)
            for k in pending[:max_names]:
                if k in hold:
                    continue
                j = pnl.col[k]
                o = pnl.open[t, j]
                if o != o or o <= 0:
                    skipped += 1
                    continue
                px = o * (1 + slip[k])
                qty = int(min(alloc, cash * 0.995) // px)
                if qty < 1:
                    skipped += 1
                    continue
                buy_cost = round_trip_costs("LONG", px, px, qty, "CNC", k[0], cfg.costs)["buy_side"]
                cash -= px * qty + buy_cost
                atr = pnl.atr[t - 1, j]
                hold[k] = {"qty": qty, "entry": px, "i": t, "sig": t - 1, "entry_cost_paid": buy_cost,
                           "atr": atr if atr == atr else px * 0.02, "reasons": [f"Selected at rebalance on "
                                                                                f"{idx[t - 1].date()}"]}
            pending = None
        mtm = cash + sum(h["qty"] * pnl.close_ff[t, pnl.col[k]] for k, h in hold.items())
        eq_vals.append(mtm)
        if hold:
            invested_days += 1
        if t in ends and t != days[-1]:
            pending = [k for k in select(t, pnl) if k in pnl.col]
    t_last = int(days[-1])
    for k in list(hold):
        close_pos(k, t_last, pnl.close_ff[t_last, pnl.col[k]], "End of test period")
    eq_vals[-1] = cash
    equity = pd.Series(eq_vals, index=idx[days])
    trades.sort(key=lambda x: x.entry_ts)
    return {"equity": equity, "trades": trades, "exposure": invested_days / len(days), "skipped_entries": skipped,
            "skipped_stop_cap": 0}
