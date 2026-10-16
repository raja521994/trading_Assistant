"""Portfolio / rebalance strategies (B11, B13-B16) and pairs spread reversion (C22).

All share the Strategy interface (`run_custom`) so walk-forward, validation and the tournament treat them like
event-driven strategies. Selection uses data up to the rebalance-day close only (see backtest/rebalance.py).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from ...backtest.engine import BTConfig, Trade
from ...backtest.rebalance import Panel, run_rebalance
from ...risk.costs import fo_order_costs
from .context import aligned, get_context
from .daily import LibStrategy, spec


_PANELS: dict[int, tuple[dict, Panel]] = {}


def _panel_for(data: dict) -> Panel:
    """One Panel per data dict (walk-forward and stability runs reuse the same data object)."""
    hit = _PANELS.get(id(data))
    if hit is not None and hit[0] is data:
        return hit[1]
    if len(_PANELS) > 4:
        _PANELS.clear()
    p = Panel(data)
    _PANELS[id(data)] = (data, p)
    return p


class PortfolioStrategy(LibStrategy):
    """Base for cross-sectional strategies: no per-bar entry rule; `select(t, panel)` returns ranked keys."""

    kind = "portfolio"

    @property
    def warmup(self) -> int:
        return 260

    def prepare(self, df, base=None):
        return df

    def entry(self, a, i):
        return None

    def select(self, t: int, p: Panel) -> list[tuple[str, str]]:
        raise NotImplementedError

    def run_custom(self, data: dict, cfg: BTConfig) -> dict[str, Any]:
        return run_rebalance(self.select, data, cfg, self.params.get("freq", "M"),
                             float(self.params.get("cash_yield", 0.0)), int(self.params.get("top_n", 20)), _panel_for(data))

    def latest_selection(self, data: dict) -> list[tuple[str, str]]:
        p = Panel(data)
        return self.select(len(p.index) - 1, p) if len(p.index) > 260 else []

    @staticmethod
    def rank(p: Panel, score: np.ndarray, n: int, mask: np.ndarray | None = None) -> list[tuple[str, str]]:
        s = np.where(np.isnan(score) | (~mask if mask is not None else False), -np.inf, score)
        order = np.argsort(-s)
        return [p.keys[j] for j in order[:n] if np.isfinite(s[j])]


class CrossSectionalMomentum(PortfolioStrategy):
    SPEC = spec("xs_momentum", "Cross-sectional momentum (12-1 month, top decile)", "Momentum / relative strength",
                {"lookback": 252, "skip": 21, "top_n": 20, "min_turnover_cr": 10.0}, ["positional"],
                rebalance="monthly")

    def select(self, t, p):
        r = p.ret(t, int(self.params["lookback"]), int(self.params["skip"]))
        return self.rank(p, r, int(self.params["top_n"]), p.liquid(t, self.params["min_turnover_cr"]))


class MomentumBasket(PortfolioStrategy):
    SPEC = spec("momentum_basket", "Momentum factor basket (Nifty200 Momentum 30 style)", "Momentum / relative strength",
                {"top_n": 30, "min_turnover_cr": 10.0}, ["positional"], rebalance="monthly",
                benchmark_index="NIFTY200 MOMENTUM 30")

    def select(self, t, p):
        vol = p.vol(t)
        with np.errstate(invalid="ignore", divide="ignore"):
            m6, m12 = p.ret(t, 126) / vol, p.ret(t, 252) / vol

        def z(x):
            ok = ~np.isnan(x)
            if ok.sum() < 5:
                return np.full_like(x, np.nan)
            return (x - np.nanmean(x)) / (np.nanstd(x) or 1.0)

        score = (z(m6) + z(m12)) / 2
        return self.rank(p, score, int(self.params["top_n"]), p.liquid(t, self.params["min_turnover_cr"]))


class LowVolMomentum(PortfolioStrategy):
    SPEC = spec("lowvol_momentum", "Low-volatility + momentum combination", "Momentum / relative strength",
                {"top_n": 20, "min_turnover_cr": 10.0}, ["positional"], rebalance="monthly")

    def select(self, t, p):
        mom = p.ret(t, 252, 21)
        vol = p.vol(t)
        ok = ~np.isnan(mom) & ~np.isnan(vol) & p.liquid(t, self.params["min_turnover_cr"])
        if ok.sum() < 10:
            return []
        med = np.nanmedian(mom[ok])
        return self.rank(p, -vol, int(self.params["top_n"]), ok & (mom >= med))


class SectorRotation(PortfolioStrategy):
    SPEC = spec("sector_rotation", "Sector rotation (top sectors, trade the leaders)", "Momentum / relative strength",
                {"lookback": 63, "top_sectors": 2, "per_sector": 3, "top_n": 6, "min_turnover_cr": 10.0},
                ["positional"], rebalance="monthly", required_data=["Daily OHLCV", "Sector index daily closes"])

    def select(self, t, p):
        ctx = get_context()
        sector_of: dict = ctx["sector_of"] or {}  # type: ignore[assignment]
        idx_closes: dict = ctx["index"] or {}  # type: ignore[assignment]
        n = int(self.params["lookback"])
        sec_ret = {}
        for name in set(v for v in sector_of.values() if v):
            s = idx_closes.get(name)
            if s is None:
                continue
            sa = aligned(s, p.index).to_numpy(float)
            if t - n >= 0 and sa[t] == sa[t] and sa[t - n] == sa[t - n]:
                sec_ret[name] = sa[t] / sa[t - n] - 1
        top = sorted(sec_ret, key=sec_ret.get, reverse=True)[: int(self.params["top_sectors"])]
        r = p.ret(t, n)
        liq = p.liquid(t, self.params["min_turnover_cr"])
        out = []
        for name in top:
            mask = np.array([sector_of.get(k[1]) == name for k in p.keys]) & liq
            out += self.rank(p, r, int(self.params["per_sector"]), mask)
        return out


class DualMomentum(PortfolioStrategy):
    SPEC = spec("dual_momentum", "Dual momentum (absolute + relative) with liquid-fund fallback",
                "Momentum / relative strength",
                {"top_n": 10, "cash_yield": 0.065, "min_turnover_cr": 10.0}, ["positional"], rebalance="monthly",
                assumptions=["Cash earns an assumed 6.5%/yr liquid-fund yield when out of the market"])

    def select(self, t, p):
        bench = get_context().get("benchmark")
        b = aligned(bench, p.index).to_numpy(float)  # type: ignore[arg-type]
        hurdle = float(self.params["cash_yield"])
        if t < 252 or not (b[t] == b[t] and b[t - 252] == b[t - 252]) or b[t] / b[t - 252] - 1 <= hurdle:
            return []  # absolute momentum off -> hold cash (liquid fund)
        r = p.ret(t, 252, 21)
        return self.rank(p, r, int(self.params["top_n"]), p.liquid(t, self.params["min_turnover_cr"]) & (p.ret(t, 252) > hurdle))


DEFAULT_PAIRS = [("HDFCBANK", "ICICIBANK"), ("KOTAKBANK", "AXISBANK"), ("TCS", "INFY"), ("HCLTECH", "WIPRO"),
                 ("TATASTEEL", "JSWSTEEL"), ("MARUTI", "M&M"), ("BAJFINANCE", "BAJAJFINSV"), ("SBIN", "BANKBARODA"),
                 ("NTPC", "POWERGRID"), ("SUNPHARMA", "DRREDDY"), ("ULTRACEMCO", "AMBUJACEM"),
                 ("HINDUNILVR", "NESTLEIND"), ("ONGC", "IOC"), ("BPCL", "HINDPETRO"), ("TECHM", "LTIM")]


class PairsReversion(LibStrategy):
    SPEC = spec("pairs_reversion", "Pairs / sector-spread reversion (stock futures, paper only)", "Mean reversion",
                {"window": 60, "entry_z": 2.0, "stop_z": 3.5, "max_hold": 20, "leg_pct": 10.0}, ["swing"],
                direction="long_short",
                assumptions=["Both legs modelled as stock futures (cash shorts cannot be carried overnight); futures "
                             "charges applied; margin, lot sizes and roll costs are not modelled",
                             "Pairs are fixed, same-industry pairs chosen on economic grounds (not data-mined)"])
    kind = "pairs"

    @property
    def warmup(self) -> int:
        return int(self.params["window"]) + 5

    def prepare(self, df, base=None):
        return df

    def entry(self, a, i):
        return None

    def pairs(self, data: dict) -> list[tuple[tuple[str, str], tuple[str, str]]]:
        have = {k[1]: k for k in data}
        return [(have[x], have[y]) for x, y in DEFAULT_PAIRS if x in have and y in have]

    def zscores(self, pa: pd.Series, pb: pd.Series) -> pd.DataFrame:
        n = int(self.params["window"])
        la, lb = np.log(pa), np.log(pb)
        beta = la.rolling(n).cov(lb) / lb.rolling(n).var()
        spread = la - beta * lb
        mu, sd = spread.rolling(n).mean(), spread.rolling(n).std()
        return pd.DataFrame({"z": (spread - mu) / sd, "sd": sd})

    def latest_signals(self, data: dict) -> list[dict[str, Any]]:
        out = []
        for ka, kb in self.pairs(data):
            df = pd.concat([data[ka]["close"], data[kb]["close"]], axis=1, keys=["a", "b"]).dropna()
            if len(df) < self.warmup:
                continue
            z = self.zscores(df["a"], df["b"])["z"]
            out.append({"pair": f"{ka[1]}/{kb[1]}", "z": round(float(z.iloc[-1]), 2), "as_of": str(df.index[-1].date())})
        return out

    def run_custom(self, data: dict, cfg: BTConfig) -> dict[str, Any]:
        p = self.params
        pairs = self.pairs(data)
        frames = {}
        for ka, kb in pairs:
            df = pd.concat([data[ka]["open"], data[ka]["close"], data[kb]["open"], data[kb]["close"]], axis=1,
                           keys=["oa", "ca", "ob", "cb"]).dropna()
            frames[(ka, kb)] = pd.concat([df, self.zscores(df["ca"], df["cb"])], axis=1)
        idx = pd.DatetimeIndex(sorted(set().union(*[set(f.index) for f in frames.values()]))) if frames else []
        if not len(idx):
            return {"equity": pd.Series(dtype=float), "trades": [], "exposure": 0.0, "skipped_entries": 0}
        win = np.ones(len(idx), dtype=bool)
        if cfg.start is not None:
            win &= np.asarray(idx >= cfg.start)
        if cfg.end is not None:
            win &= np.asarray(idx <= cfg.end)
        days = idx[win]
        slip = {k: (cfg.slippage_fn(k[1], k[0]) if callable(cfg.slippage_fn) else cfg.slippage_bps) / 1e4
                for pr in pairs for k in pr}
        cash = cfg.capital
        pos: dict[tuple, dict[str, Any]] = {}
        pend_entry: dict[tuple, int] = {}
        pend_exit: dict[tuple, str] = {}
        trades: list[Trade] = []
        eq, inv = [], 0
        cost_mult = max(cfg.costs.stt_delivery_pct / 0.1, 1.0) if cfg.costs.stt_delivery_pct else 1.0

        def leg_costs(value: float) -> float:
            return (fo_order_costs("BUY", value, "FUT")["total"] + fo_order_costs("SELL", value, "FUT")["total"]) * cost_mult

        for d in days:
            for pr, f in frames.items():
                if d not in f.index:
                    continue
                r = f.loc[d]
                ka, kb = pr
                if pr in pend_exit and pr in pos:
                    h = pos.pop(pr)
                    sa, sb = h["sa"], h["sb"]
                    pxa = r["oa"] * (1 - sa * slip[ka]) if sa > 0 else r["oa"] * (1 + slip[ka])
                    pxb = r["ob"] * (1 - sb * slip[kb]) if sb > 0 else r["ob"] * (1 + slip[kb])
                    gross = sa * h["qa"] * (pxa - h["pa"]) + sb * h["qb"] * (pxb - h["pb"])
                    costs = leg_costs(h["qa"] * h["pa"]) + leg_costs(h["qb"] * h["pb"])
                    net = gross - costs
                    cash += net
                    risk = max(h["risk"], 1e-9)
                    trades.append(Trade(f"{ka[1]}/{kb[1]}", ka[0], "LONG" if sa > 0 else "SHORT", h["ts"],
                                        round(h["z"], 3), d.isoformat(), round(float(r["z"]), 3), h["qa"], round(h["stopz"], 2),
                                        0.0, pend_exit.pop(pr), round(gross, 2), round(costs, 2), round(net, 2),
                                        round(net / risk, 3), h["bars"], round(net / h["notional"] * 100, 3),
                                        round(h["notional"], 2), round(h["notional"] + gross, 2),
                                        [f"Spread z-score {h['z']:+.2f} (entry), long {'A' if sa > 0 else 'B'} / short "
                                         f"{'B' if sa > 0 else 'A'}; prices shown are z-scores"], 0.0, 0.0, h["sig"]))
                if pr in pend_entry and pr not in pos:
                    z0 = pend_entry.pop(pr)
                    equity_now = cash + sum(x["mtm"] for x in pos.values())
                    notional = equity_now * p["leg_pct"] / 100
                    qa, qb = int(notional // r["oa"]), int(notional // r["ob"])
                    if qa >= 1 and qb >= 1:
                        sa = -1 if z0 > 0 else 1  # z>0: A rich vs B -> short A, long B
                        sb = -sa
                        pa = r["oa"] * (1 + slip[ka]) if sa > 0 else r["oa"] * (1 - slip[ka])
                        pb = r["ob"] * (1 + slip[kb]) if sb > 0 else r["ob"] * (1 - slip[kb])
                        risk = notional * (p["stop_z"] - abs(z0)) * float(r["sd"]) if r["sd"] == r["sd"] else notional * 0.05
                        pos[pr] = {"sa": sa, "sb": sb, "qa": qa, "qb": qb, "pa": pa, "pb": pb, "z": z0, "bars": 0,
                                   "ts": d.isoformat(), "notional": qa * pa + qb * pb, "risk": max(risk, notional * 0.01),
                                   "stopz": math.copysign(p["stop_z"], z0), "mtm": 0.0, "sig": d.isoformat()}
                z = float(r["z"])
                if pr in pos:
                    h = pos[pr]
                    h["bars"] += 1
                    h["mtm"] = h["sa"] * h["qa"] * (r["ca"] - h["pa"]) + h["sb"] * h["qb"] * (r["cb"] - h["pb"])
                    if z == z:
                        if (h["z"] > 0 and z <= 0) or (h["z"] < 0 and z >= 0):
                            pend_exit[pr] = "Spread reverted (z crossed 0)"
                        elif abs(z) >= p["stop_z"]:
                            pend_exit[pr] = f"Stop: spread diverged (|z| >= {p['stop_z']})"
                        elif h["bars"] >= p["max_hold"]:
                            pend_exit[pr] = "Time exit"
                elif z == z and abs(z) >= p["entry_z"] and abs(z) < p["stop_z"] and d != days[-1]:
                    pend_entry[pr] = z
            mtm = cash + sum(x["mtm"] for x in pos.values())
            eq.append(mtm)
            if pos:
                inv += 1
        for pr, h in pos.items():  # close at the last close (mark-to-market already in equity)
            gross = h["mtm"]
            costs = leg_costs(h["qa"] * h["pa"]) + leg_costs(h["qb"] * h["pb"])
            cash += gross - costs
            trades.append(Trade(f"{pr[0][1]}/{pr[1][1]}", pr[0][0], "LONG" if h["sa"] > 0 else "SHORT", h["ts"],
                                round(h["z"], 3), days[-1].isoformat(), 0.0, h["qa"], round(h["stopz"], 2), 0.0,
                                "End of test period", round(gross, 2), round(costs, 2), round(gross - costs, 2),
                                round((gross - costs) / h["risk"], 3), h["bars"],
                                round((gross - costs) / h["notional"] * 100, 3), round(h["notional"], 2),
                                round(h["notional"] + gross, 2), [], 0.0, 0.0, h["sig"]))
        if eq:
            eq[-1] = cash
        trades.sort(key=lambda x: x.entry_ts)
        return {"equity": pd.Series(eq, index=days), "trades": trades, "exposure": inv / max(len(days), 1),
                "skipped_entries": 0, "skipped_stop_cap": 0}


PORTFOLIO = [CrossSectionalMomentum, MomentumBasket, LowVolMomentum, SectorRotation, DualMomentum, PairsReversion]
