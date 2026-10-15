"""Opening-range breakout with VWAP and volume confirmation (intraday 5m, long and short)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Setup, Strategy, ok

SESSION_OPEN_MIN = 9 * 60 + 15


def _hhmm_to_min(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


class OrbVwap(Strategy):
    id = "orb_vwap"

    @property
    def warmup(self) -> int:
        return 25

    @property
    def one_trade_per_session(self) -> bool:
        return True

    def prepare(self, df: pd.DataFrame, base: pd.DataFrame | None = None) -> pd.DataFrame:
        out = super().prepare(df, base)
        n_or = int(self.params["or_minutes"]) // 5
        day = pd.Series(df.index.date, index=df.index)
        k = day.groupby(day).cumcount()
        out["bar_k"] = k.astype(float)
        hi_or = out["high"].where(k < n_or).groupby(day).transform("max")
        lo_or = out["low"].where(k < n_or).groupby(day).transform("min")
        # Only known once the opening range has completed (causal)
        out["or_high"] = hi_or.where(k >= n_or)
        out["or_low"] = lo_or.where(k >= n_or)
        return out

    def extra_snapshot_keys(self) -> list[str]:
        return ["vwap", "or_high", "or_low"]

    def entry(self, a: dict[str, np.ndarray], i: int) -> Setup | None:
        p = self.params
        if a["_date"][i] != a["_date"][i - 1]:
            return None
        minute = a["_minute"][i] + 5  # bar close time
        if minute > _hhmm_to_min(p["last_entry"]):
            return None
        c, c0 = a["close"][i], a["close"][i - 1]
        oh, ol, vw, vr = a["or_high"][i], a["or_low"][i], a["vwap"][i], a["vol_ratio"][i]
        if not ok(oh, ol, vw, vr):
            return None
        mid = (oh + ol) / 2.0
        oh0 = a["or_high"][i - 1]
        if c > oh and (not ok(oh0) or c0 <= oh0) and c > vw and vr >= p["vol_mult"]:
            return Setup("LONG", mid, p["target_r"], [
                f"Close {c:.2f} broke above the {p['or_minutes']}-min opening-range high {oh:.2f}",
                f"Close above session VWAP {vw:.2f}",
                f"Bar volume {vr:.1f}x the prior-20-bar average (>= {p['vol_mult']}x)",
            ], f"Price back below the opening-range midpoint {mid:.2f}, or time exit at {p['exit_time']} IST",
                int(p["max_hold_bars"]))
        ol0 = a["or_low"][i - 1]
        if p.get("allow_short", True) and c < ol and (not ok(ol0) or c0 >= ol0) and c < vw and vr >= p["vol_mult"]:
            return Setup("SHORT", mid, p["target_r"], [
                f"Close {c:.2f} broke below the {p['or_minutes']}-min opening-range low {ol:.2f}",
                f"Close below session VWAP {vw:.2f}",
                f"Bar volume {vr:.1f}x the prior-20-bar average (>= {p['vol_mult']}x)",
            ], f"Price back above the opening-range midpoint {mid:.2f}, or time exit at {p['exit_time']} IST",
                int(p["max_hold_bars"]))
        return None

    def exit(self, a: dict[str, np.ndarray], i: int, direction: str) -> str | None:
        if a["_minute"][i] + 5 >= _hhmm_to_min(self.params["exit_time"]):
            return f"Time exit ({self.params['exit_time']} IST)"
        return None

    def watch(self, a: dict[str, np.ndarray], i: int) -> str | None:
        oh, ol, c = a["or_high"][i], a["or_low"][i], a["close"][i]
        if ok(oh, ol) and a["_minute"][i] + 5 <= _hhmm_to_min(self.params["last_entry"]):
            if ol <= c <= oh and min(oh - c, c - ol) / c < 0.002:
                return f"Price within 0.2% of the opening-range boundary ({ol:.2f} - {oh:.2f})"
        return None
