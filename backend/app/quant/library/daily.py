"""Daily event-driven strategies (long only: no overnight shorting in Indian cash equity).

Every rule reads arrays only at indices <= i (the signal bar). Indicator columns are causal; pivots are confirmed
k bars late. Entries follow the shared SETUP -> TRIGGERED -> ACTIVE state machine (trigger = signal-bar high unless
`trigger_rule` says otherwise), so entries always fill at a later bar's open.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .. import indicators as ind
from ..strategies.base import Setup, Strategy, ok
from .context import benchmark_for, sector_index_for

A = dict[str, list]


def spec(id_: str, name: str, family: str, params: dict[str, Any], modes: list[str] | None = None,
         timeframe: str = "1d", direction: str = "long_only", **extra: Any) -> dict[str, Any]:
    return {"id": id_, "name": name, "version": "1.0.0", "category": family, "modes": modes or ["swing"],
            "timeframe": timeframe, "direction": direction, "params": params, **extra}


class LibStrategy(Strategy):
    SPEC: dict[str, Any] = {}

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(self.SPEC, params)

    def long_setup(self, a: A, i: int, stop: float, reasons: list[str], invalidation: str,
                   target_r: float | None = None) -> Setup | None:
        c = a["close"][i]
        if not ok(stop) or stop >= c:
            return None
        return Setup("LONG", stop, target_r if target_r is not None else self.params.get("target_r"), reasons,
                     invalidation, int(self.params.get("max_hold_bars", 30)))

    def atr_stop(self, a: A, i: int, mult: float | None = None) -> float:
        return a["close"][i] - (mult or float(self.params.get("atr_stop_mult", 2.0))) * a["atr14"][i]


def _cross_up(x0: float, x1: float, l0: float, l1: float) -> bool:
    return ok(x0, x1, l0, l1) and x0 <= l0 and x1 > l1


# ------------------------------------------------------------------ A. trend following

class HighBreakout52w(LibStrategy):
    SPEC = spec("hi52_breakout", "52-week-high breakout with volume", "Trend following",
                {"lookback": 252, "vol_mult": 1.5, "atr_stop_mult": 2.0, "target_r": None, "max_hold_bars": 60,
                 "exit_ema": 50}, ["swing", "positional"])

    @property
    def warmup(self) -> int:
        return int(self.params["lookback"]) + 5

    def prepare(self, df, base=None):
        out = super().prepare(df, base)
        out["hh_prev"] = out["high"].rolling(int(self.params["lookback"]), min_periods=int(self.params["lookback"])).max().shift(1)
        out["ema_exit"] = ind.ema(out["close"], int(self.params["exit_ema"]))
        return out

    def entry(self, a, i):
        p = self.params
        c, hh, hh0, vr = a["close"][i], a["hh_prev"][i], a["hh_prev"][i - 1], a["vol_ratio"][i]
        if ok(hh, hh0, vr, a["atr14"][i]) and c > hh and a["close"][i - 1] <= hh0 and vr >= p["vol_mult"]:
            return self.long_setup(a, i, self.atr_stop(a, i), [
                f"Close {c:.2f} above the prior {p['lookback']}-session high {hh:.2f}",
                f"Volume {vr:.1f}x the 20-day average (>= {p['vol_mult']}x)"],
                f"Close back below the breakout level {hh:.2f} or below EMA{p['exit_ema']}")
        return None

    def exit(self, a, i, direction):
        if ok(a["ema_exit"][i]) and a["close"][i] < a["ema_exit"][i]:
            return f"Close below EMA{self.params['exit_ema']}"
        return None


class MinerviniVcp(LibStrategy):
    SPEC = spec("minervini_vcp", "Minervini trend template + VCP breakout", "Trend following",
                {"base_bars": 15, "max_base_pct": 12.0, "vol_dry": 0.85, "vol_mult": 1.4, "target_r": None,
                 "max_hold_bars": 60}, ["swing", "positional"])

    @property
    def warmup(self) -> int:
        return 270

    def prepare(self, df, base=None):
        out = super().prepare(df, base)
        c, n = out["close"], int(self.params["base_bars"])
        out["sma50"], out["sma150"] = ind.sma(c, 50), ind.sma(c, 150)
        out["sma200_21"] = out["sma200"].shift(21)
        out["low52"], out["high52"] = out["low"].rolling(252).min(), out["high"].rolling(252).max()
        out["base_high"] = out["high"].rolling(n).max().shift(1)
        out["base_low"] = out["low"].rolling(n).min().shift(1)
        out["base_vol"] = out["volume"].rolling(n).mean().shift(1) / out["volume"].rolling(50).mean().shift(1)
        return out

    def entry(self, a, i):
        p = self.params
        c = a["close"][i]
        s50, s150, s200, s200o = a["sma50"][i], a["sma150"][i], a["sma200"][i], a["sma200_21"][i]
        bh, bl, bv, vr = a["base_high"][i], a["base_low"][i], a["base_vol"][i], a["vol_ratio"][i]
        if not ok(s50, s150, s200, s200o, bh, bl, bv, vr, a["low52"][i], a["high52"][i]):
            return None
        template = c > s50 > s150 > s200 and s200 > s200o and c >= 1.3 * a["low52"][i] and c >= 0.75 * a["high52"][i]
        depth = (bh - bl) / bh * 100
        if template and depth <= p["max_base_pct"] and bv <= p["vol_dry"] and c > bh and a["close"][i - 1] <= bh \
                and vr >= p["vol_mult"]:
            stop = max(bl, c - 2.5 * a["atr14"][i])
            return self.long_setup(a, i, stop, [
                "Trend template: close > SMA50 > SMA150 > SMA200, SMA200 rising, >=30% above 52w low, "
                "within 25% of 52w high",
                f"Volatility contraction: {p['base_bars']}-day base {depth:.1f}% deep (<= {p['max_base_pct']}%), "
                f"volume dried up to {bv:.2f}x the 50-day average",
                f"Breakout close {c:.2f} above the base high {bh:.2f} on {vr:.1f}x volume"],
                f"Close below the base low {bl:.2f} / stop {stop:.2f}, or close below SMA50")
        return None

    def exit(self, a, i, direction):
        if ok(a["sma50"][i]) and a["close"][i] < a["sma50"][i]:
            return "Close below SMA50"
        return None


class DarvasBox(LibStrategy):
    SPEC = spec("darvas_box", "Darvas box breakout", "Trend following",
                {"box_bars": 20, "min_age": 3, "max_box_pct": 15.0, "vol_mult": 1.3, "trail_bars": 10,
                 "target_r": None, "max_hold_bars": 60})

    @property
    def warmup(self) -> int:
        return 60

    def prepare(self, df, base=None):
        out = super().prepare(df, base)
        n = int(self.params["box_bars"])
        h = out["high"]
        out["box_top"] = h.rolling(n).max().shift(1)
        out["box_bottom"] = out["low"].rolling(n).min().shift(1)
        # bars since the box top was made (must have held for min_age bars to form a box)
        out["top_age"] = h.shift(1).rolling(n).apply(lambda w: len(w) - 1 - int(np.argmax(w)), raw=True)
        out["trail_low"] = out["low"].rolling(int(self.params["trail_bars"])).min().shift(1)
        return out

    def entry(self, a, i):
        p = self.params
        c, top, bot, age, vr = a["close"][i], a["box_top"][i], a["box_bottom"][i], a["top_age"][i], a["vol_ratio"][i]
        if not ok(top, bot, age, vr) or a["close"][i - 1] > a["box_top"][i - 1]:
            return None
        if c > top and age >= p["min_age"] and (top - bot) / top * 100 <= p["max_box_pct"] and vr >= p["vol_mult"]:
            stop = max(bot, c - 2.5 * a["atr14"][i])
            return self.long_setup(a, i, stop, [
                f"Close {c:.2f} broke out of a Darvas box {bot:.2f}-{top:.2f} (top held {int(age)} bars)",
                f"Volume {vr:.1f}x average"], f"Close back inside the box / below {stop:.2f}")
        return None

    def exit(self, a, i, direction):
        if ok(a["trail_low"][i]) and a["close"][i] < a["trail_low"][i]:
            return f"Close below the {self.params['trail_bars']}-bar low (trailing)"
        return None


class IchimokuBreakout(LibStrategy):
    SPEC = spec("ichimoku_breakout", "Ichimoku cloud breakout", "Trend following",
                {"tenkan": 9, "kijun": 26, "senkou": 52, "target_r": None, "max_hold_bars": 60})

    @property
    def warmup(self) -> int:
        return int(self.params["senkou"]) + int(self.params["kijun"]) + 5

    def prepare(self, df, base=None):
        out = super().prepare(df, base)
        p = self.params
        ich = ind.ichimoku(out["high"], out["low"], int(p["tenkan"]), int(p["kijun"]), int(p["senkou"]))
        out = pd.concat([out, ich], axis=1)
        out["cloud_top"] = out[["span_a", "span_b"]].max(axis=1, skipna=False)
        return out

    def entry(self, a, i):
        c, top, top0 = a["close"][i], a["cloud_top"][i], a["cloud_top"][i - 1]
        tk, kj = a["tenkan"][i], a["kijun"][i]
        if _cross_up(a["close"][i - 1], c, top0, top) and ok(tk, kj) and tk > kj and a["span_a"][i] > a["span_b"][i]:
            stop = max(kj, c - 2.5 * a["atr14"][i]) if kj < c else c - 2.0 * a["atr14"][i]
            return self.long_setup(a, i, stop, [
                f"Close {c:.2f} crossed above the cloud top {top:.2f}", f"Tenkan {tk:.2f} > Kijun {kj:.2f}",
                "Bullish cloud (span A above span B)"], f"Close below Kijun {kj:.2f}")
        return None

    def exit(self, a, i, direction):
        if ok(a["kijun"][i]) and a["close"][i] < a["kijun"][i]:
            return "Close below Kijun-sen"
        return None


class HigherHighBreakout(LibStrategy):
    SPEC = spec("weekly_hh_breakout", "Weekly/monthly higher-high breakout", "Trend following",
                {"period": "W", "target_r": None, "max_hold_bars": 80}, ["swing", "positional"])

    @property
    def warmup(self) -> int:
        return 70

    def prepare(self, df, base=None):
        out = super().prepare(df, base)
        per = pd.Series(out.index.tz_localize(None).to_period(self.params["period"]).astype(str), index=out.index)
        g = out.groupby(per)
        agg = pd.DataFrame({"h": g["high"].max(), "l": g["low"].min()})
        prev = agg.shift(1)
        prev2 = agg.shift(2)
        # values of the last COMPLETED period, known on every bar of the current period
        out["p_high"] = per.map(prev["h"]).astype(float).to_numpy()
        out["p_low"] = per.map(prev["l"]).astype(float).to_numpy()
        out["p2_high"] = per.map(prev2["h"]).astype(float).to_numpy()
        out["p2_low"] = per.map(prev2["l"]).astype(float).to_numpy()
        return out

    def entry(self, a, i):
        c, ph, pl, p2h, p2l = a["close"][i], a["p_high"][i], a["p_low"][i], a["p2_high"][i], a["p2_low"][i]
        if not ok(ph, pl, p2h, p2l):
            return None
        if ph > p2h and pl > p2l and c > ph and a["close"][i - 1] <= a["p_high"][i - 1]:
            per = "week" if self.params["period"] == "W" else "month"
            stop = max(pl, c - 2.5 * a["atr14"][i])
            return self.long_setup(a, i, stop, [
                f"Prior {per} made a higher high ({ph:.2f} > {p2h:.2f}) and higher low ({pl:.2f} > {p2l:.2f})",
                f"Close {c:.2f} broke above the prior {per}'s high"], f"Close below the prior {per}'s low {pl:.2f}")
        return None

    def exit(self, a, i, direction):
        if ok(a["p_low"][i]) and a["close"][i] < a["p_low"][i]:
            return "Close below the prior period's low"
        return None


class RibbonMacd(LibStrategy):
    SPEC = spec("ribbon_macd", "MA ribbon + MACD trend continuation", "Trend following",
                {"atr_stop_mult": 2.0, "target_r": None, "max_hold_bars": 40})

    @property
    def warmup(self) -> int:
        return 80

    def prepare(self, df, base=None):
        out = super().prepare(df, base)
        for n in (8, 13, 21, 34, 55):
            out[f"rb{n}"] = ind.ema(out["close"], n)
        return out

    def entry(self, a, i):
        r = [a[f"rb{n}"][i] for n in (8, 13, 21, 34, 55)]
        h0, h1 = a["macd_hist"][i - 1], a["macd_hist"][i]
        if ok(*r, h0, h1) and all(r[k] > r[k + 1] for k in range(4)) and h0 <= 0 < h1 and a["close"][i] > r[0]:
            return self.long_setup(a, i, self.atr_stop(a, i), [
                "EMA ribbon 8>13>21>34>55 fully aligned (uptrend)", "MACD histogram turned positive (momentum resumes)"],
                "EMA8 closes below EMA21 (ribbon breaks)")
        return None

    def exit(self, a, i, direction):
        if ok(a["rb8"][i], a["rb21"][i]) and a["rb8"][i] < a["rb21"][i]:
            return "EMA8 below EMA21 (ribbon broken)"
        return None


class EmaPullback(LibStrategy):
    SPEC = spec("ema_pullback", "Pullback to 20/50 EMA in a strong uptrend", "Trend following",
                {"ema": 20, "adx_min": 20, "target_r": 2.0, "max_hold_bars": 20})

    @property
    def warmup(self) -> int:
        return 210

    def entry(self, a, i):
        p = self.params
        e = a[f"ema{int(p['ema'])}"][i]
        c, lo, e20, e50, e200, adx = a["close"][i], a["low"][i], a["ema20"][i], a["ema50"][i], a["ema200"][i], a["adx14"][i]
        if not ok(e, e20, e50, e200, adx, a["atr14"][i]):
            return None
        if e20 > e50 > e200 and adx >= p["adx_min"] and lo <= e * 1.003 and c > e and c > a["open"][i]:
            stop = min(lo, e50) - 0.5 * a["atr14"][i]
            return self.long_setup(a, i, stop, [
                f"Uptrend: EMA20 {e20:.2f} > EMA50 {e50:.2f} > EMA200 {e200:.2f}, ADX {adx:.1f}",
                f"Pulled back to EMA{int(p['ema'])} ({e:.2f}) and closed back above it on a green bar"],
                f"Close below EMA50 / stop {stop:.2f}")
        return None

    def exit(self, a, i, direction):
        if ok(a["ema50"][i]) and a["close"][i] < a["ema50"][i]:
            return "Close below EMA50"
        return None


# ------------------------------------------------------------------ B. relative strength (event-driven)

class RsNewHigh(LibStrategy):
    SPEC = spec("rs_new_high", "Relative strength line at new highs (vs NIFTY and sector)", "Momentum / relative strength",
                {"rs_lookback": 126, "atr_stop_mult": 2.0, "target_r": None, "max_hold_bars": 60, "use_sector": True},
                ["swing", "positional"], required_data=["Daily OHLCV", "NIFTY 50 daily", "Sector index daily"])

    @property
    def warmup(self) -> int:
        return int(self.params["rs_lookback"]) + 60

    def prepare_keyed(self, key, df, base=None):
        out = self.prepare(df, base)
        n = int(self.params["rs_lookback"])
        rs = out["close"] / benchmark_for(out.index)
        out["rs"] = rs
        out["rs_hh"] = rs.rolling(n, min_periods=n).max().shift(1)
        out["rs_sma50"] = ind.sma(rs, 50)
        name, sec = sector_index_for(key[1], out.index)
        srs = out["close"] / sec
        out["srs"], out["srs_sma50"] = srs, ind.sma(srs, 50)
        self._sector = name
        return out

    def entry(self, a, i):
        if "rs" not in a:
            return None
        rs, hh, hh0 = a["rs"][i], a["rs_hh"][i], a["rs_hh"][i - 1]
        c = a["close"][i]
        if not ok(rs, hh, hh0, a["ema50"][i], a["atr14"][i]):
            return None
        if rs > hh and a["rs"][i - 1] <= hh0 and c > a["ema50"][i]:
            reasons = [f"RS line (close / NIFTY 50) at a new {self.params['rs_lookback']}-day high",
                       f"Close {c:.2f} above EMA50"]
            if self.params.get("use_sector"):
                s, s50 = a["srs"][i], a["srs_sma50"][i]
                if ok(s, s50):
                    if s <= s50:
                        return None
                    reasons.append("Outperforming its sector index (stock/sector ratio above its 50-day average)")
            return self.long_setup(a, i, self.atr_stop(a, i), reasons, "RS line falls below its 50-day average")
        return None

    def exit(self, a, i, direction):
        if "rs" in a and ok(a["rs"][i], a["rs_sma50"][i]) and a["rs"][i] < a["rs_sma50"][i]:
            return "RS line below its 50-day average"
        return None


# ------------------------------------------------------------------ C. mean reversion

class BollingerReversion(LibStrategy):
    SPEC = spec("bollinger_reversion", "Bollinger reversion with trend filter", "Mean reversion",
                {"atr_stop_mult": 1.5, "target_r": None, "max_hold_bars": 10})
    trigger_rule = "signal_close"

    @property
    def warmup(self) -> int:
        return 210

    def entry(self, a, i):
        c, c0, lb, lb0, s200 = a["close"][i], a["close"][i - 1], a["bb_lower"][i], a["bb_lower"][i - 1], a["sma200"][i]
        if ok(lb, lb0, s200, a["atr14"][i]) and c > s200 and c0 < lb0 and c > lb:
            stop = min(a["low"][i], a["low"][i - 1]) - 0.5 * a["atr14"][i]
            return self.long_setup(a, i, stop, [
                f"Uptrend filter: close above SMA200 {s200:.2f}",
                f"Closed back inside the lower Bollinger band ({lb:.2f}) after a close below it"],
                f"Close below {stop:.2f}")
        return None

    def exit(self, a, i, direction):
        mid = (a["bb_upper"][i] + a["bb_lower"][i]) / 2
        if ok(mid) and a["close"][i] >= mid:
            return "Reverted to the Bollinger middle band"
        return None


class OversoldBounce(LibStrategy):
    SPEC = spec("oversold_bounce", "52-week-low reversal with capitulation volume", "Mean reversion",
                {"near_low_pct": 3.0, "vol_mult": 2.5, "target_r": 2.0, "max_hold_bars": 15})

    @property
    def warmup(self) -> int:
        return 260

    def prepare(self, df, base=None):
        out = super().prepare(df, base)
        out["low52_prev"] = out["low"].rolling(252).min().shift(1)
        out["rsi_min3"] = out["rsi14"].rolling(3).min()
        return out

    def entry(self, a, i):
        p = self.params
        lo, c, h, l52, vr = a["low"][i], a["close"][i], a["high"][i], a["low52_prev"][i], a["vol_ratio"][i]
        if not ok(l52, vr, a["rsi_min3"][i], a["atr14"][i]):
            return None
        if lo <= l52 * (1 + p["near_low_pct"] / 100) and vr >= p["vol_mult"] and c > (h + lo) / 2 \
                and a["rsi_min3"][i] < 30:
            stop = lo - 0.5 * a["atr14"][i]
            return self.long_setup(a, i, stop, [
                f"Low {lo:.2f} within {p['near_low_pct']}% of the 52-week low {l52:.2f}",
                f"Capitulation volume {vr:.1f}x average with a close in the upper half of the range",
                "RSI(14) below 30 within the last 3 sessions"], f"Close below the reversal-bar low {lo:.2f}")
        return None


# ------------------------------------------------------------------ E. volume / participation

class RelVolBreakout(LibStrategy):
    SPEC = spec("relvol_breakout", "Relative-volume breakout (>2x, close near high)", "Volume / participation",
                {"vol_mult": 2.0, "range_pos": 0.75, "lookback": 20, "atr_stop_mult": 2.0, "target_r": None,
                 "max_hold_bars": 20})

    @property
    def warmup(self) -> int:
        return 60

    def prepare(self, df, base=None):
        out = super().prepare(df, base)
        out["hh_n"] = out["high"].rolling(int(self.params["lookback"])).max().shift(1)
        return out

    def entry(self, a, i):
        p = self.params
        c, h, lo, vr, hh = a["close"][i], a["high"][i], a["low"][i], a["vol_ratio"][i], a["hh_n"][i]
        if not ok(vr, hh, a["ema50"][i], a["atr14"][i]) or h <= lo:
            return None
        pos = (c - lo) / (h - lo)
        if vr >= p["vol_mult"] and pos >= p["range_pos"] and c > hh and c > a["ema50"][i]:
            return self.long_setup(a, i, self.atr_stop(a, i), [
                f"Volume {vr:.1f}x the 20-day average", f"Close in the top {100 - p['range_pos'] * 100:.0f}% of the range",
                f"Close above the prior {p['lookback']}-day high {hh:.2f}"], "Close below EMA20")
        return None

    def exit(self, a, i, direction):
        if ok(a["ema20"][i]) and a["close"][i] < a["ema20"][i]:
            return "Close below EMA20"
        return None


class ObvDivergence(LibStrategy):
    SPEC = spec("obv_divergence", "OBV / accumulation-distribution bullish divergence", "Volume / participation",
                {"lookback": 20, "atr_stop_mult": 2.0, "target_r": 2.0, "max_hold_bars": 20})

    @property
    def warmup(self) -> int:
        return 220

    def prepare(self, df, base=None):
        out = super().prepare(df, base)
        n = int(self.params["lookback"])
        obv = ind.obv(out["close"], out["volume"])
        ad = ind.ad_line(out["high"], out["low"], out["close"], out["volume"])
        out["div"] = ((out["close"] < out["close"].shift(n)) & (obv > obv.shift(n)) & (ad > ad.shift(n))).astype(float)
        out["div_recent"] = out["div"].rolling(5).max()
        return out

    def entry(self, a, i):
        if ok(a["div_recent"][i - 1], a["ema20"][i], a["ema20"][i - 1], a["sma200"][i]) and a["div_recent"][i - 1] > 0 \
                and _cross_up(a["close"][i - 1], a["close"][i], a["ema20"][i - 1], a["ema20"][i]) \
                and a["close"][i] > a["sma200"][i]:
            return self.long_setup(a, i, self.atr_stop(a, i), [
                f"Bullish divergence: price lower than {self.params['lookback']} days ago while OBV and A/D rose",
                "Close crossed back above EMA20 (confirmation)", "Long-term uptrend (above SMA200)"],
                "Close below the stop / divergence fails")
        return None


# ------------------------------------------------------------------ F. price action

class _PivotBase(LibStrategy):
    @property
    def warmup(self) -> int:
        return 210

    def prepare(self, df, base=None):
        out = super().prepare(df, base)
        piv = ind.confirmed_pivots(out["high"], out["low"], int(self.params.get("pivot_k", 3)))
        return pd.concat([out, piv], axis=1)


class PivotSR(_PivotBase):
    SPEC = spec("pivot_sr", "Swing-pivot support/resistance bounce or break", "Price action",
                {"mode": "break", "pivot_k": 3, "vol_mult": 1.2, "target_r": 2.0, "max_hold_bars": 20})

    def entry(self, a, i):
        p = self.params
        c, ph, pl, ppl = a["close"][i], a["last_ph"][i], a["last_pl"][i], a["prev_pl"][i]
        if not ok(ph, pl, ppl, a["atr14"][i], a["vol_ratio"][i], a["ema200"][i]):
            return None
        if p["mode"] == "break":
            if c > ph and a["close"][i - 1] <= a["last_ph"][i - 1] and pl > ppl and a["vol_ratio"][i] >= p["vol_mult"]:
                return self.long_setup(a, i, max(pl, c - 2.5 * a["atr14"][i]), [
                    f"Close {c:.2f} broke the last confirmed swing high (resistance) {ph:.2f}",
                    f"Higher swing low {pl:.2f} > {ppl:.2f}", f"Volume {a['vol_ratio'][i]:.1f}x average"],
                    f"Close below the swing low {pl:.2f}")
        elif a["low"][i] <= pl * 1.01 and c > pl and c > a["open"][i] and c > a["ema200"][i]:
            return self.long_setup(a, i, pl - 0.5 * a["atr14"][i], [
                f"Tested the confirmed swing-low support {pl:.2f} and closed green above it",
                "Long-term uptrend (above EMA200)"], f"Close below support {pl:.2f}")
        return None


class CandleAtSupport(_PivotBase):
    SPEC = spec("candle_sr", "Candlestick reversal at support with volume", "Price action",
                {"pivot_k": 3, "near_pct": 1.5, "vol_mult": 1.2, "target_r": 2.0, "max_hold_bars": 10})

    def _pattern(self, a, i) -> str | None:
        o, h, lo, c = a["open"][i], a["high"][i], a["low"][i], a["close"][i]
        o1, c1 = a["open"][i - 1], a["close"][i - 1]
        body, rng = abs(c - o), h - lo
        if rng <= 0:
            return None
        if c1 < o1 and c > o and c >= o1 and o <= c1:
            return "bullish engulfing"
        if c > o and (min(o, c) - lo) >= 2 * body and (h - max(o, c)) <= 0.3 * rng and body > 0:
            return "hammer"
        o2, c2 = a["open"][i - 2], a["close"][i - 2]
        if c2 < o2 and abs(c1 - o1) < 0.3 * abs(c2 - o2) and c > o and c > (o2 + c2) / 2:
            return "morning star"
        return None

    def entry(self, a, i):
        p = self.params
        pl = a["last_pl"][i]
        if not ok(pl, a["vol_ratio"][i], a["atr14"][i]):
            return None
        lo = min(a["low"][i], a["low"][i - 1])
        if abs(lo - pl) / pl * 100 > p["near_pct"] or a["vol_ratio"][i] < p["vol_mult"]:
            return None
        pat = self._pattern(a, i)
        if pat:
            return self.long_setup(a, i, lo - 0.25 * a["atr14"][i], [
                f"{pat.capitalize()} at confirmed swing-low support {pl:.2f}",
                f"Volume {a['vol_ratio'][i]:.1f}x average"], f"Close below the pattern low {lo:.2f}")
        return None


class StructureBos(_PivotBase):
    SPEC = spec("structure_bos", "HH/HL structure and break of structure", "Price action",
                {"pivot_k": 3, "target_r": None, "max_hold_bars": 40})

    def entry(self, a, i):
        c, ph, pph, pl, ppl = a["close"][i], a["last_ph"][i], a["prev_ph"][i], a["last_pl"][i], a["prev_pl"][i]
        if not ok(ph, pph, pl, ppl, a["atr14"][i]):
            return None
        if ph > pph and pl > ppl and c > ph and a["close"][i - 1] <= a["last_ph"][i - 1]:
            return self.long_setup(a, i, pl, [
                f"Higher highs ({ph:.2f} > {pph:.2f}) and higher lows ({pl:.2f} > {ppl:.2f})",
                f"Break of structure: close {c:.2f} above the last swing high"],
                f"Close below the last higher low {pl:.2f} (structure broken)")
        return None

    def exit(self, a, i, direction):
        if ok(a["last_pl"][i]) and a["close"][i] < a["last_pl"][i]:
            return "Close below the last swing low (structure broken)"
        return None


class TtmSqueeze(LibStrategy):
    """John Carter's TTM Squeeze ("Mastering the Trade", ch. 11): Bollinger Bands inside Keltner Channels = volatility
    compression; the trade is taken when the squeeze releases with positive, rising momentum."""
    SPEC = spec("ttm_squeeze", "TTM Squeeze breakout (John Carter)", "Trend following",
                {"n": 20, "bb_k": 2.0, "kc_k": 1.5, "min_squeeze": 5, "atr_stop_mult": 2.0, "target_r": None,
                 "max_hold_bars": 30})

    @property
    def warmup(self) -> int:
        return 220

    def prepare(self, df, base=None):
        out = super().prepare(df, base)
        n = int(self.params["n"])
        c, h, lo = out["close"], out["high"], out["low"]
        mid = c.rolling(n).mean()
        sd = c.rolling(n).std(ddof=0)
        atr = ind.atr(h, lo, c, n)
        sq_on = ((mid + self.params["bb_k"] * sd) < (mid + self.params["kc_k"] * atr)) & \
                ((mid - self.params["bb_k"] * sd) > (mid - self.params["kc_k"] * atr))
        out["sq_on"] = sq_on.astype(float)
        out["sq_run"] = sq_on.astype(int).groupby((~sq_on).cumsum()).cumsum().astype(float)
        dev = c - ((h.rolling(n).max() + lo.rolling(n).min()) / 2 + mid) / 2
        out["sq_mom"] = _linreg_end(dev, n)
        return out

    def entry(self, a, i):
        p = self.params
        run_prev, on, m0, m1 = a["sq_run"][i - 1], a["sq_on"][i], a["sq_mom"][i - 1], a["sq_mom"][i]
        if not ok(run_prev, m0, m1, a["ema50"][i], a["atr14"][i]):
            return None
        if run_prev >= p["min_squeeze"] and on == 0 and m1 > 0 and m1 > m0 and a["close"][i] > a["ema50"][i]:
            return self.long_setup(a, i, self.atr_stop(a, i), [
                f"Squeeze released after {int(run_prev)} bars of Bollinger Bands inside Keltner Channels",
                f"Momentum {m1:.2f} positive and rising", "Close above EMA50"], "Momentum turns down / stop")
        return None

    def exit(self, a, i, direction):
        m0, m1, m2 = a["sq_mom"][i - 2], a["sq_mom"][i - 1], a["sq_mom"][i]
        if ok(m0, m1, m2) and m2 < m1 < m0:
            return "Squeeze momentum fell for two consecutive bars"
        return None


def _linreg_end(s: pd.Series, n: int) -> pd.Series:
    """Rolling least-squares line evaluated at the window's last point (causal, vectorised)."""
    y = s.to_numpy(float)
    x = np.arange(len(y), dtype=float)
    valid = ~np.isnan(y)
    y0 = np.where(valid, y, 0.0)
    cs = lambda v: np.concatenate([[0.0], np.cumsum(v)])  # noqa: E731
    sy, sxy, cnt = cs(y0), cs(x * y0), cs(valid.astype(float))
    out = np.full(len(y), np.nan)
    xm = (n - 1) / 2.0
    sxx = n * (n * n - 1) / 12.0
    for_idx = np.arange(n - 1, len(y))
    a, b = for_idx - n + 1, for_idx + 1
    full = (cnt[b] - cnt[a]) == n
    s_y = sy[b] - sy[a]
    s_xy = sxy[b] - sxy[a] - a * s_y  # x relative to window start
    slope = (s_xy - xm * s_y) / sxx
    val = s_y / n + slope * xm
    out[for_idx[full]] = val[full]
    return pd.Series(out, index=s.index)


class ConnorsRsi(LibStrategy):
    """Larry Connors & Alvarez ConnorsRSI(3,2,100) pullback in a long-term uptrend."""
    SPEC = spec("connors_rsi", "ConnorsRSI pullback (Connors)", "Mean reversion",
                {"entry_crsi": 10, "exit_sma": 5, "atr_stop_mult": 3.0, "target_r": None, "max_hold_bars": 10})
    trigger_rule = "signal_close"

    @property
    def warmup(self) -> int:
        return 210

    def prepare(self, df, base=None):
        out = super().prepare(df, base)
        c = out["close"]
        d = np.sign(c.diff().fillna(0.0)).to_numpy()
        streak = np.zeros(len(d))
        for k in range(1, len(d)):
            if d[k] == 0:
                streak[k] = 0
            elif d[k] == np.sign(streak[k - 1]) or streak[k - 1] == 0:
                streak[k] = streak[k - 1] + d[k]
            else:
                streak[k] = d[k]
        roc = c.pct_change()
        out["crsi"] = (ind.rsi(c, 3) + ind.rsi(pd.Series(streak, index=c.index), 2)
                       + ind.pct_rank(roc, 101)) / 3.0
        out["sma_exit"] = ind.sma(c, int(self.params["exit_sma"]))
        return out

    def entry(self, a, i):
        cr, s200 = a["crsi"][i], a["sma200"][i]
        if ok(cr, s200, a["atr14"][i]) and a["close"][i] > s200 and cr < self.params["entry_crsi"]:
            return self.long_setup(a, i, self.atr_stop(a, i), [
                f"ConnorsRSI {cr:.1f} < {self.params['entry_crsi']} (short-term oversold)",
                f"Long-term uptrend: close above SMA200 {s200:.2f}"], "Close below the volatility stop")
        return None

    def exit(self, a, i, direction):
        if ok(a["sma_exit"][i]) and a["close"][i] > a["sma_exit"][i]:
            return f"Close above SMA{self.params['exit_sma']}"
        return None


class ElderTripleScreen(LibStrategy):
    """Alexander Elder's Triple Screen ("Trading for a Living"): weekly trend up (EMA of weekly-equivalent length
    rising), daily oscillator pullback (2-day Force Index below zero), buy-stop above the previous high."""
    SPEC = spec("elder_triple_screen", "Elder Triple Screen", "Trend following",
                {"trend_ema": 65, "atr_stop_mult": 2.0, "target_r": 2.0, "max_hold_bars": 20})

    @property
    def warmup(self) -> int:
        return 210

    def prepare(self, df, base=None):
        out = super().prepare(df, base)
        n = int(self.params["trend_ema"])
        out["tr_ema"] = ind.ema(out["close"], n)
        out["tr_ema_5"] = out["tr_ema"].shift(5)
        out["force2"] = ind.ema(out["close"].diff() * out["volume"], 2)
        return out

    def entry(self, a, i):
        e, e5, f, c = a["tr_ema"][i], a["tr_ema_5"][i], a["force2"][i], a["close"][i]
        if not ok(e, e5, f, a["atr14"][i], a["ema200"][i]):
            return None
        if e > e5 and c > e and c > a["ema200"][i] and f < 0 and a["force2"][i - 1] >= 0:
            stop = min(a["low"][i], a["low"][i - 1]) - 0.5 * a["atr14"][i]
            return self.long_setup(a, i, stop, [
                f"Screen 1: weekly trend up (EMA{self.params['trend_ema']} rising, close above it)",
                "Screen 2: 2-day Force Index turned negative (pullback)",
                "Screen 3: buy only above this bar's high (trigger)"], "Close below the 2-day low stop")
        return None

    def exit(self, a, i, direction):
        if ok(a["tr_ema"][i]) and a["close"][i] < a["tr_ema"][i]:
            return f"Close below EMA{self.params['trend_ema']} (trend screen failed)"
        return None


DAILY = [HighBreakout52w, MinerviniVcp, DarvasBox, IchimokuBreakout, HigherHighBreakout, RibbonMacd, EmaPullback,
         RsNewHigh, BollingerReversion, OversoldBounce, RelVolBreakout, ObvDivergence, PivotSR, CandleAtSupport,
         StructureBos, TtmSqueeze, ConnorsRsi, ElderTripleScreen]
