"""RSI(2) pullback mean reversion inside a long-term uptrend (Connors-style, long only, daily)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .. import indicators as ind
from .base import Setup, Strategy, ok


class Rsi2MeanReversion(Strategy):
    id = "rsi2_meanrev"
    trigger_rule = "signal_close"

    @property
    def warmup(self) -> int:
        return int(self.params["trend_sma"]) + 5

    def prepare(self, df: pd.DataFrame, base: pd.DataFrame | None = None) -> pd.DataFrame:
        out = super().prepare(df, base)
        out["rsi_fast"] = ind.rsi(out["close"], int(self.params["rsi_n"]))
        out["sma_trend"] = ind.sma(out["close"], int(self.params["trend_sma"]))
        out["sma_exit"] = ind.sma(out["close"], int(self.params["exit_sma"]))
        return out

    def extra_snapshot_keys(self) -> list[str]:
        return ["rsi_fast", "sma_trend", "sma_exit"]

    def entry(self, a: dict[str, np.ndarray], i: int) -> Setup | None:
        p = self.params
        c, r, tr, atr = a["close"][i], a["rsi_fast"][i], a["sma_trend"][i], a["atr14"][i]
        if not ok(r, tr, atr):
            return None
        if c > tr and r < p["entry_rsi"]:
            stop = c - p["atr_stop_mult"] * atr
            return Setup("LONG", stop, p.get("target_r"), [
                f"Close {c:.2f} above SMA{p['trend_sma']} ({tr:.2f}): long-term uptrend",
                f"RSI({p['rsi_n']}) = {r:.1f} < {p['entry_rsi']}: short-term oversold pullback",
            ], f"Close below protective stop {stop:.2f} ({p['atr_stop_mult']}x ATR), or close below SMA{p['trend_sma']}",
                int(p["max_hold_bars"]))
        return None

    def exit(self, a: dict[str, np.ndarray], i: int, direction: str) -> str | None:
        p = self.params
        if ok(a["sma_exit"][i]) and a["close"][i] > a["sma_exit"][i]:
            return f"Close above SMA{p['exit_sma']} (mean-reversion exit)"
        if ok(a["sma_trend"][i]) and a["close"][i] < a["sma_trend"][i]:
            return f"Close below SMA{p['trend_sma']} (trend filter lost)"
        return None

    def watch(self, a: dict[str, np.ndarray], i: int) -> str | None:
        p = self.params
        r, tr, c = a["rsi_fast"][i], a["sma_trend"][i], a["close"][i]
        if ok(r, tr) and c > tr and p["entry_rsi"] <= r < p["entry_rsi"] * 2:
            return f"Uptrend intact and RSI({p['rsi_n']}) = {r:.1f} approaching oversold (< {p['entry_rsi']})"
        return None
