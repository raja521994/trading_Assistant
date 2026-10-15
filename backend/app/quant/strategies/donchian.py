"""Donchian channel breakout (Turtle-style, long only, daily)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .. import indicators as ind
from .base import Setup, Strategy, ok


class DonchianBreakout(Strategy):
    id = "donchian_breakout"

    @property
    def warmup(self) -> int:
        return int(self.params["entry_n"]) + 20

    def prepare(self, df: pd.DataFrame, base: pd.DataFrame | None = None) -> pd.DataFrame:
        out = super().prepare(df, base)
        en, xn = int(self.params["entry_n"]), int(self.params["exit_n"])
        out["dc_upper_prev"] = ind.donchian(out["high"], out["low"], en)["upper"].shift(1)
        out["dc_exit_prev"] = ind.donchian(out["high"], out["low"], xn)["lower"].shift(1)
        return out

    def extra_snapshot_keys(self) -> list[str]:
        return ["dc_upper_prev", "dc_exit_prev"]

    def entry(self, a: dict[str, np.ndarray], i: int) -> Setup | None:
        p = self.params
        c, c0 = a["close"][i], a["close"][i - 1]
        up, up0, atr = a["dc_upper_prev"][i], a["dc_upper_prev"][i - 1], a["atr14"][i]
        if not ok(up, up0, atr):
            return None
        if c > up and c0 <= up0:
            stop = c - p["atr_stop_mult"] * atr
            return Setup("LONG", stop, p.get("target_r"), [
                f"Close {c:.2f} broke above the prior {p['entry_n']}-session high {up:.2f}",
                f"Initial stop {p['atr_stop_mult']}x ATR(14) = {p['atr_stop_mult'] * atr:.2f} below close",
            ], f"Close below stop {stop:.2f}, or close below the prior {p['exit_n']}-session low (trailing exit)",
                int(p["max_hold_bars"]))
        return None

    def exit(self, a: dict[str, np.ndarray], i: int, direction: str) -> str | None:
        lo = a["dc_exit_prev"][i]
        if ok(lo) and a["close"][i] < lo:
            return f"Close below prior {self.params['exit_n']}-session low ({lo:.2f})"
        return None

    def watch(self, a: dict[str, np.ndarray], i: int) -> str | None:
        up, c = a["dc_upper_prev"][i], a["close"][i]
        if ok(up) and c <= up and (up - c) / c < 0.01:
            return f"Within 1% of the {self.params['entry_n']}-session high {up:.2f}"
        return None
