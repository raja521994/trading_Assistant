"""EMA fast/slow crossover with ADX trend-strength filter (long only, daily)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .. import indicators as ind
from .base import Setup, Strategy, ok


class EmaCross(Strategy):
    id = "ema_cross"

    @property
    def warmup(self) -> int:
        return int(self.params["slow"]) + 30

    def prepare(self, df: pd.DataFrame, base: pd.DataFrame | None = None) -> pd.DataFrame:
        out = super().prepare(df, base)
        out["ema_fast"] = ind.ema(out["close"], int(self.params["fast"]))
        out["ema_slow"] = ind.ema(out["close"], int(self.params["slow"]))
        return out

    def extra_snapshot_keys(self) -> list[str]:
        return ["ema_fast", "ema_slow"]

    def entry(self, a: dict[str, np.ndarray], i: int) -> Setup | None:
        p = self.params
        f0, s0, f1, s1 = a["ema_fast"][i - 1], a["ema_slow"][i - 1], a["ema_fast"][i], a["ema_slow"][i]
        c, adx, atr = a["close"][i], a["adx14"][i], a["atr14"][i]
        if not ok(f0, s0, f1, s1, adx, atr):
            return None
        if f0 <= s0 and f1 > s1 and adx >= p["adx_min"] and c > s1:
            stop = c - p["atr_stop_mult"] * atr
            return Setup("LONG", stop, p["target_r"], [
                f"EMA{p['fast']} ({f1:.2f}) crossed above EMA{p['slow']} ({s1:.2f}) on the close",
                f"ADX(14) = {adx:.1f} >= {p['adx_min']} (trend strength filter)",
                f"Close {c:.2f} above EMA{p['slow']}",
            ], f"Close below stop {stop:.2f} ({p['atr_stop_mult']}x ATR) or EMA{p['fast']} crosses back below "
               f"EMA{p['slow']}", int(p["max_hold_bars"]))
        return None

    def exit(self, a: dict[str, np.ndarray], i: int, direction: str) -> str | None:
        if ok(a["ema_fast"][i], a["ema_slow"][i]) and a["ema_fast"][i] < a["ema_slow"][i]:
            return f"EMA{self.params['fast']} crossed below EMA{self.params['slow']}"
        return None

    def watch(self, a: dict[str, np.ndarray], i: int) -> str | None:
        f, s, f0 = a["ema_fast"][i], a["ema_slow"][i], a["ema_fast"][i - 1]
        if ok(f, s, f0) and f <= s and (s - f) / a["close"][i] < 0.005 and f > f0:
            return f"EMA{self.params['fast']} rising and within 0.5% of EMA{self.params['slow']} (crossover pending)"
        return None
