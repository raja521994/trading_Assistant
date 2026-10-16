"""Intraday (5-minute) strategies. Long and short (intraday MIS shorting is allowed in Indian cash equity).

Session context (previous day's high/low/close, CPR, NR7, first-hour range) is computed from completed bars of
earlier sessions or of the current session up to bar i only. One trade per symbol per session; flat by exit_time.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .. import indicators as ind
from ..strategies.base import Setup, ok
from .daily import LibStrategy, spec

OPEN_MIN = 9 * 60 + 15


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def ispec(id_: str, name: str, family: str, params: dict[str, Any], **extra: Any) -> dict[str, Any]:
    base = {"last_entry": "14:30", "exit_time": "15:15", "target_r": 1.5, "max_hold_bars": 75, "allow_short": True}
    return spec(id_, name, family, {**base, **params}, ["intraday"], "5m", "long_short", **extra)


def session_columns(out: pd.DataFrame) -> pd.DataFrame:
    """Previous-session OHLC/range stats aligned to each intraday bar (all from completed sessions)."""
    day = pd.Series(out.index.date, index=out.index)
    g = out.groupby(day)
    daily = pd.DataFrame({"o": g["open"].first(), "h": g["high"].max(), "l": g["low"].min(), "c": g["close"].last()})
    daily["rng"] = daily["h"] - daily["l"]
    daily["nr7"] = (daily["rng"] <= daily["rng"].rolling(7).min()).astype(float)
    daily["nr4"] = (daily["rng"] <= daily["rng"].rolling(4).min()).astype(float)
    daily["inside"] = ((daily["h"] <= daily["h"].shift(1)) & (daily["l"] >= daily["l"].shift(1))).astype(float)
    prev = daily.shift(1)
    m = {c: day.map(prev[c]).astype(float).to_numpy() for c in ("h", "l", "c", "rng", "nr7", "nr4", "inside")}
    out["pdh"], out["pdl"], out["pdc"], out["pdr"] = m["h"], m["l"], m["c"], m["rng"]
    out["p_nr7"], out["p_nr4"], out["p_inside"] = m["nr7"], m["nr4"], m["inside"]
    out["day_open"] = day.map(daily["o"]).astype(float).to_numpy()  # known from the first bar's open onwards
    k = day.groupby(day).cumcount()
    out["bar_k"] = k.astype(float)
    out["hod"] = out["high"].groupby(day).cummax()
    out["lod"] = out["low"].groupby(day).cummin()
    return out


class IntradayLib(LibStrategy):
    @property
    def warmup(self) -> int:
        return 80

    @property
    def one_trade_per_session(self) -> bool:
        return True

    def prepare(self, df, base=None):
        return session_columns(super().prepare(df, base))

    def in_window(self, a, i, first: str = "09:30") -> bool:
        close_min = a["_minute"][i] + 5
        return a["_date"][i] == a["_date"][i - 1] and _hm(first) <= close_min <= _hm(self.params["last_entry"])

    def mk(self, a, i, direction: str, stop: float, reasons: list[str], inval: str,
           target_r: float | None = None) -> Setup | None:
        c = a["close"][i]
        if direction == "SHORT" and not self.params.get("allow_short", True):
            return None
        if not ok(stop) or (direction == "LONG" and stop >= c) or (direction == "SHORT" and stop <= c):
            return None
        return Setup(direction, stop, target_r if target_r is not None else self.params.get("target_r"), reasons,
                     inval + f", or time exit {self.params['exit_time']} IST", int(self.params["max_hold_bars"]))

    def exit(self, a, i, direction):
        if a["_minute"][i] + 5 >= _hm(self.params["exit_time"]):
            return f"Time exit ({self.params['exit_time']} IST)"
        return None


class VwapReversion(IntradayLib):
    SPEC = ispec("vwap_reversion", "VWAP reversion (intraday)", "Mean reversion",
                 {"z": 2.0, "adx_max": 20, "target_r": None})
    trigger_rule = "signal_close"

    def prepare(self, df, base=None):
        out = super().prepare(df, base)
        dev = out["close"] - out["vwap"]
        out["dev_z"] = dev / dev.rolling(20).std()
        return out

    def entry(self, a, i):
        p = self.params
        z, vw, adx, atr = a["dev_z"][i], a["vwap"][i], a["adx14"][i], a["atr14"][i]
        if not self.in_window(a, i, "09:45") or not ok(z, vw, adx, atr) or adx > p["adx_max"]:
            return None
        c = a["close"][i]
        if z <= -p["z"] and a["rsi14"][i] < 30:
            stop = c - 1.5 * atr
            return self.mk(a, i, "LONG", stop, [f"Price {abs(z):.1f} std below VWAP {vw:.2f} in a non-trending session "
                                                f"(ADX {adx:.0f})", "RSI(14) below 30"], f"Price below {stop:.2f}",
                           max((vw - c) / (c - stop), 0.5))
        if z >= p["z"] and a["rsi14"][i] > 70:
            stop = c + 1.5 * atr
            return self.mk(a, i, "SHORT", stop, [f"Price {z:.1f} std above VWAP {vw:.2f} in a non-trending session",
                                                 "RSI(14) above 70"], f"Price above {stop:.2f}",
                           max((c - vw) / (stop - c), 0.5))
        return None

    def exit(self, a, i, direction):
        vw, c = a["vwap"][i], a["close"][i]
        if ok(vw) and ((direction == "LONG" and c >= vw) or (direction == "SHORT" and c <= vw)):
            return "Reverted to VWAP"
        return super().exit(a, i, direction)


class GapFill(IntradayLib):
    SPEC = ispec("gap_fill", "Opening gap fill", "Mean reversion", {"min_gap_pct": 0.5, "max_gap_pct": 2.0})

    def entry(self, a, i):
        p = self.params
        if a["bar_k"][i] != 2 or not ok(a["pdc"][i], a["day_open"][i]):  # decide at the close of the first 15 minutes
            return None
        o, pdc, c = a["day_open"][i], a["pdc"][i], a["close"][i]
        gap = (o / pdc - 1) * 100
        hi15 = max(a["high"][i - 2:i + 1])
        lo15 = min(a["low"][i - 2:i + 1])
        if p["min_gap_pct"] <= gap <= p["max_gap_pct"] and c < o:
            stop = hi15 * 1.001
            return self.mk(a, i, "SHORT", stop, [f"Gap up {gap:.2f}% vs previous close {pdc:.2f}",
                                                 "First 15 minutes failed to extend (closed below the open)"],
                           f"Price above the first-15-minute high {hi15:.2f}", max((c - pdc) / (stop - c), 0.5))
        if -p["max_gap_pct"] <= gap <= -p["min_gap_pct"] and c > o:
            stop = lo15 * 0.999
            return self.mk(a, i, "LONG", stop, [f"Gap down {gap:.2f}% vs previous close {pdc:.2f}",
                                                "First 15 minutes failed to extend (closed above the open)"],
                           f"Price below the first-15-minute low {lo15:.2f}", max((pdc - c) / (c - stop), 0.5))
        return None


class VwapPullback(IntradayLib):
    SPEC = ispec("vwap_pullback", "VWAP trend pullback", "Intraday", {"trend_bars": 6})

    def prepare(self, df, base=None):
        out = super().prepare(df, base)
        n = int(self.params["trend_bars"])
        day = pd.Series(out.index.date, index=out.index)
        above = (out["close"] > out["vwap"]).astype(float)
        out["above_n"] = above.shift(1).groupby(day).rolling(n).sum().reset_index(level=0, drop=True)
        out["swing_lo3"] = out["low"].rolling(3).min()
        out["swing_hi3"] = out["high"].rolling(3).max()
        return out

    def entry(self, a, i):
        n = self.params["trend_bars"]
        vw, ab, c, o = a["vwap"][i], a["above_n"][i], a["close"][i], a["open"][i]
        if not self.in_window(a, i, "09:45") or not ok(vw, ab, a["ema20"][i]):
            return None
        if ab >= n and a["ema20"][i] > vw and a["low"][i] <= vw * 1.001 and c > vw and c > o:
            return self.mk(a, i, "LONG", min(a["swing_lo3"][i], vw) - 0.25 * a["atr14"][i], [
                f"Trend day: closed above VWAP for the prior {n} bars, EMA20 above VWAP",
                f"Pulled back to VWAP {vw:.2f} and closed back above it on a green bar"], "Price back below VWAP")
        if ab == 0 and a["ema20"][i] < vw and a["high"][i] >= vw * 0.999 and c < vw and c < o:
            return self.mk(a, i, "SHORT", max(a["swing_hi3"][i], vw) + 0.25 * a["atr14"][i], [
                f"Down-trend day: closed below VWAP for the prior {n} bars", f"Rallied to VWAP {vw:.2f} and rejected"],
                "Price back above VWAP")
        return None


class CprTrend(IntradayLib):
    SPEC = ispec("cpr_trend", "CPR + pivots (narrow CPR trend day)", "Intraday",
                 {"narrow_pct": 0.25, "or_bars": 3, "target_r": 2.0})

    def prepare(self, df, base=None):
        out = super().prepare(df, base)
        p = (out["pdh"] + out["pdl"] + out["pdc"]) / 3
        bc = (out["pdh"] + out["pdl"]) / 2
        tc = 2 * p - bc
        out["cpr_top"], out["cpr_bot"] = np.maximum(tc, bc), np.minimum(tc, bc)
        out["cpr_w"] = (out["cpr_top"] - out["cpr_bot"]) / p * 100
        out["r1"], out["s1"] = 2 * p - out["pdl"], 2 * p - out["pdh"]
        n = int(self.params["or_bars"])
        day = pd.Series(out.index.date, index=out.index)
        k = out["bar_k"]
        out["or_h"] = out["high"].where(k < n).groupby(day).transform("max").where(k >= n)
        out["or_l"] = out["low"].where(k < n).groupby(day).transform("min").where(k >= n)
        return out

    def entry(self, a, i):
        p = self.params
        w, top, bot, oh, ol = a["cpr_w"][i], a["cpr_top"][i], a["cpr_bot"][i], a["or_h"][i], a["or_l"][i]
        if not self.in_window(a, i) or not ok(w, top, bot, oh, ol) or w > p["narrow_pct"]:
            return None
        c, c0 = a["close"][i], a["close"][i - 1]
        if c > oh and c0 <= oh and c > top:
            return self.mk(a, i, "LONG", min(bot, (oh + ol) / 2), [
                f"Narrow CPR ({w:.2f}% wide <= {p['narrow_pct']}%) -> trend-day bias",
                f"Broke the opening-range high {oh:.2f} above the CPR top {top:.2f}"], "Price back inside the CPR")
        if c < ol and c0 >= ol and c < bot:
            return self.mk(a, i, "SHORT", max(top, (oh + ol) / 2), [
                f"Narrow CPR ({w:.2f}% wide) -> trend-day bias",
                f"Broke the opening-range low {ol:.2f} below the CPR bottom {bot:.2f}"], "Price back inside the CPR")
        return None


class PrevDayBreakout(IntradayLib):
    SPEC = ispec("pdh_pdl_breakout", "Previous-day high/low breakout", "Intraday",
                 {"vol_mult": 1.5, "stop_frac": 0.3})

    def entry(self, a, i):
        p = self.params
        pdh, pdl, pdr, vr = a["pdh"][i], a["pdl"][i], a["pdr"][i], a["vol_ratio"][i]
        if not self.in_window(a, i) or not ok(pdh, pdl, pdr, vr) or vr < p["vol_mult"]:
            return None
        c, c0 = a["close"][i], a["close"][i - 1]
        if c > pdh and c0 <= pdh and a["hod"][i - 1] <= pdh:
            return self.mk(a, i, "LONG", pdh - p["stop_frac"] * pdr, [
                f"First close above the previous-day high {pdh:.2f}", f"Volume {vr:.1f}x the prior-20-bar average"],
                "Price back below the previous-day high")
        if c < pdl and c0 >= pdl and a["lod"][i - 1] >= pdl:
            return self.mk(a, i, "SHORT", pdl + p["stop_frac"] * pdr, [
                f"First close below the previous-day low {pdl:.2f}", f"Volume {vr:.1f}x average"],
                "Price back above the previous-day low")
        return None


class NarrowRangeBreakout(IntradayLib):
    SPEC = ispec("nr7_breakout", "Inside-bar / NR4 / NR7 breakout", "Intraday", {"pattern": "nr7", "target_r": 2.0})

    def entry(self, a, i):
        flag = a[f"p_{self.params['pattern']}"][i]
        pdh, pdl = a["pdh"][i], a["pdl"][i]
        if not self.in_window(a, i) or not ok(flag, pdh, pdl) or flag < 1:
            return None
        c, c0, mid = a["close"][i], a["close"][i - 1], (pdh + pdl) / 2
        name = self.params["pattern"].upper()
        if c > pdh and c0 <= pdh and a["hod"][i - 1] <= pdh:
            return self.mk(a, i, "LONG", mid, [f"Previous session was {name} (range contraction)",
                                               f"Broke above its high {pdh:.2f}"], f"Price below its midpoint {mid:.2f}")
        if c < pdl and c0 >= pdl and a["lod"][i - 1] >= pdl:
            return self.mk(a, i, "SHORT", mid, [f"Previous session was {name}", f"Broke below its low {pdl:.2f}"],
                           f"Price above its midpoint {mid:.2f}")
        return None


class SupertrendIntraday(IntradayLib):
    SPEC = ispec("supertrend_intraday", "Supertrend(10,3) intraday with VWAP filter", "Intraday",
                 {"st_n": 10, "st_mult": 3.0, "target_r": None})

    def prepare(self, df, base=None):
        out = super().prepare(df, base)
        st = ind.supertrend(out["high"], out["low"], out["close"], int(self.params["st_n"]), float(self.params["st_mult"]))
        out["st"], out["st_dir"] = st["supertrend"], st["direction"]
        return out

    def entry(self, a, i):
        d0, d1, st, vw, c = a["st_dir"][i - 1], a["st_dir"][i], a["st"][i], a["vwap"][i], a["close"][i]
        if not self.in_window(a, i, "09:45") or not ok(st, vw):
            return None
        if d0 == -1 and d1 == 1 and c > vw:
            return self.mk(a, i, "LONG", st, ["Supertrend flipped up on a closed 5-minute bar",
                                              f"Close above VWAP {vw:.2f}"], "Supertrend flips down")
        if d0 == 1 and d1 == -1 and c < vw:
            return self.mk(a, i, "SHORT", st, ["Supertrend flipped down", f"Close below VWAP {vw:.2f}"],
                           "Supertrend flips up")
        return None

    def exit(self, a, i, direction):
        d = a["st_dir"][i]
        if (direction == "LONG" and d == -1) or (direction == "SHORT" and d == 1):
            return "Supertrend flipped against the position"
        return super().exit(a, i, direction)


class HourMomentum(IntradayLib):
    SPEC = ispec("hour_momentum", "First-hour momentum / last-hour continuation", "Intraday",
                 {"window": "first", "min_move_pct": 0.6, "target_r": None})

    def entry(self, a, i):
        p = self.params
        close_min = a["_minute"][i] + 5
        at = _hm("10:15") if p["window"] == "first" else _hm("14:30")
        if close_min != at or a["_date"][i] != a["_date"][i - 1] or not ok(a["day_open"][i], a["vwap"][i]):
            return None
        o, c, vw, hod, lod = a["day_open"][i], a["close"][i], a["vwap"][i], a["hod"][i], a["lod"][i]
        move = (c / o - 1) * 100
        rng = hod - lod
        if rng <= 0:
            return None
        label = "First hour" if p["window"] == "first" else "Session so far"
        if move >= p["min_move_pct"] and c > vw and (c - lod) / rng >= 0.7:
            return self.mk(a, i, "LONG", max(lod, c - 2 * a["atr14"][i]), [
                f"{label} up {move:.2f}% from the open, closing near the high", f"Above VWAP {vw:.2f}"],
                "Price below the session low")
        if move <= -p["min_move_pct"] and c < vw and (hod - c) / rng >= 0.7:
            return self.mk(a, i, "SHORT", min(hod, c + 2 * a["atr14"][i]), [
                f"{label} down {move:.2f}% from the open, closing near the low", f"Below VWAP {vw:.2f}"],
                "Price above the session high")
        return None


INTRADAY = [VwapReversion, GapFill, VwapPullback, CprTrend, PrevDayBreakout, NarrowRangeBreakout, SupertrendIntraday,
            HourMomentum]
