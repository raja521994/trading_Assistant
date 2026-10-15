"""Supertrend flip with long-term EMA filter (long only, daily)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .. import indicators as ind
from .base import Setup, Strategy, ok


class SupertrendTrend(Strategy):
    id = "supertrend_trend"

    @property
    def warmup(self) -> int:
        return int(self.params["trend_ema"]) + 5

    def prepare(self, df: pd.DataFrame, base: pd.DataFrame | None = None) -> pd.DataFrame:
        out = super().prepare(df, base)
        st = ind.supertrend(out["high"], out["low"], out["close"], int(self.params["atr_n"]), float(self.params["mult"]))
        out["st_line"] = st["supertrend"]
        out["st_dir"] = st["direction"]
        out["st_upper"] = st["upper"]
        out["ema_trend"] = ind.ema(out["close"], int(self.params["trend_ema"]))
        return out

    def extra_snapshot_keys(self) -> list[str]:
        return ["st_line", "st_dir", "ema_trend"]

    def entry(self, a: dict[str, np.ndarray], i: int) -> Setup | None:
        p = self.params
        d0, d1, line, c, et, atr = (a["st_dir"][i - 1], a["st_dir"][i], a["st_line"][i], a["close"][i],
                                    a["ema_trend"][i], a["atr14"][i])
        if not ok(line, et, atr):
            return None
        if d0 == -1 and d1 == 1 and c > et:
            stop = min(line, c - p["min_stop_atr"] * atr)
            return Setup("LONG", stop, p.get("target_r"), [
                f"Supertrend({p['atr_n']},{p['mult']}) flipped bullish; line at {line:.2f}",
                f"Close {c:.2f} above EMA{p['trend_ema']} ({et:.2f})",
            ], f"Close below stop {stop:.2f} or Supertrend flips bearish", int(p["max_hold_bars"]))
        return None

    def exit(self, a: dict[str, np.ndarray], i: int, direction: str) -> str | None:
        if a["st_dir"][i] == -1:
            return "Supertrend flipped bearish"
        return None

    def watch(self, a: dict[str, np.ndarray], i: int) -> str | None:
        d, up, c, atr = a["st_dir"][i], a["st_upper"][i], a["close"][i], a["atr14"][i]
        if ok(up, atr) and d == -1 and 0 <= up - c < 0.5 * atr:
            return f"Close within 0.5 ATR of the Supertrend flip level {up:.2f}"
        return None
