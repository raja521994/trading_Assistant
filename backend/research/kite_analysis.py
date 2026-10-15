"""Research on the stored market data (any provider): regime split of OOS trades, cost drag, regime-filter test,
cross-sectional momentum, current market state. Read-only. Run:  cd backend; uv run python -m research.kite_analysis"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.backtest.engine import BTConfig, run_portfolio
from app.backtest.metrics import compute_metrics
from app.backtest.walkforward import walk_forward
from app.db import SessionLocal
from app.quant import indicators as ind
from app.quant.strategies.base import get_strategy
from app.quant.universe import load_benchmark, load_universe
from app.risk.limits import load_risk_config

s = SessionLocal()
data = load_universe(s, "1d")
bench = load_benchmark(s)
costs = load_risk_config(s).costs
b = bench["close"]
print(f"Universe: {len(data)} stocks, {min(len(d) for d in data.values())}-{max(len(d) for d in data.values())} daily bars, "
      f"{b.index[0].date()} .. {b.index[-1].date()}")
print(f"NIFTY 50 buy&hold whole period: {(b.iloc[-1] / b.iloc[0] - 1) * 100:.1f}%  "
      f"(from 2023-12-13: {(b.iloc[-1] / b[b.index >= '2023-12-13'].iloc[0] - 1) * 100:.1f}%)")

# 1) Regime at entry for each strategy's OOS trades
sma200 = ind.sma(b, 200)
up = (b > sma200)
print("\n== OOS trades split by market regime at entry (NIFTY above/below 200-DMA) ==")
for sid in ("donchian_breakout", "supertrend_trend", "ema_cross", "rsi2_meanrev"):
    wf = walk_forward(sid, data, BTConfig(costs=costs), folds=3, benchmark=b)
    rows = []
    for t in wf["oos_trades"]:
        d = pd.Timestamp(t.entry_ts).normalize()
        reg = up.asof(d) if d >= up.index[0] else np.nan
        rows.append((bool(reg), t.net_pnl, t.r_multiple, t.costs, t.gross_pnl))
    df = pd.DataFrame(rows, columns=["up", "net", "r", "costs", "gross"])
    for reg, g in df.groupby("up"):
        pf = g.net[g.net > 0].sum() / -g.net[g.net <= 0].sum() if (g.net <= 0).any() else np.nan
        print(f"{sid:18s} {'UPTREND  ' if reg else 'DOWNTREND'} trades={len(g):4d} net=Rs{g.net.sum():>10,.0f} "
              f"PF={pf:5.2f} avgR={g.r.mean():6.3f}")
    print(f"{sid:18s} costs Rs{df.costs.sum():,.0f} = {df.costs.sum() / max(abs(df.gross).sum(), 1) * 100:.1f}% of gross |P&L|")

# 2) Regime filter test: only allow long entries when NIFTY > its 200-DMA (walk-forward OOS)
print("\n== Walk-forward OOS with a market-regime filter (long only when NIFTY > 200-DMA) ==")


class RegimeFiltered:
    def __init__(self, inner, regime_up):
        self.inner, self.regime_up = inner, regime_up

    def __getattr__(self, k):
        return getattr(self.inner, k)

    def entry(self, a, i):
        d = a["_date"][i]
        ok = self.regime_up.get(d, False)
        return self.inner.entry(a, i) if ok else None


regime_by_date = {t.date(): bool(v) for t, v in up.items()}
for sid in ("donchian_breakout", "supertrend_trend", "ema_cross"):
    base = get_strategy(sid)
    for label, strat in (("no filter", base), ("regime filter", RegimeFiltered(base, regime_by_date))):
        res = []
        dates = sorted({d for df in data.values() for d in df.index.date})
        chunk = len(dates) // 4
        for k in range(1, 4):
            st = pd.Timestamp(dates[k * chunk]).tz_localize("Asia/Kolkata")
            en = pd.Timestamp(dates[min((k + 1) * chunk, len(dates) - 1)]).tz_localize("Asia/Kolkata")
            r = run_portfolio(strat, data, BTConfig(costs=costs, start=st, end=en))
            m = compute_metrics(r["equity"], r["trades"], 1_000_000, r["exposure"], b)
            res.append((m["num_trades"], m["net_return_pct"], m.get("benchmark_return_pct")))
        print(f"{sid:18s} {label:13s} " + " | ".join(f"fold{i + 1}: {n:3d}tr {ret:6.1f}% (bench {bm:5.1f}%)"
                                                  for i, (n, ret, bm) in enumerate(res)))

# 3) Cross-sectional momentum (classic, published): monthly, hold top 10 by 6-1 month return, equal weight
print("\n== Cross-sectional momentum (6-1 month, top 10 of universe, monthly rebalance, 0.25% round-trip cost) ==")
closes = pd.DataFrame({k[1]: v["close"] for k, v in data.items()}).sort_index()
closes.index = closes.index.normalize()
month_ends = closes.groupby([closes.index.year, closes.index.month]).tail(1).index
rets, rows = [], []
for i in range(7, len(month_ends) - 1):
    t0, t1 = month_ends[i], month_ends[i + 1]
    look = closes.loc[:month_ends[i - 1]]
    if len(look) < 130:
        continue
    mom = look.iloc[-1] / look.iloc[-127] - 1  # ~6 months ending one month before rebalance
    mom = mom.dropna()
    top = mom.nlargest(10).index
    period = closes.loc[t0:t1, top]
    r = (period.iloc[-1] / period.iloc[0] - 1).mean() - 0.0025
    br = b[(b.index.normalize() >= t0) & (b.index.normalize() <= t1)]
    rb = br.iloc[-1] / br.iloc[0] - 1
    rets.append((t1, r, rb))
mdf = pd.DataFrame(rets, columns=["month", "strat", "bench"]).set_index("month")
eq = (1 + mdf.strat).cumprod()
beq = (1 + mdf.bench).cumprod()
dd = (eq / eq.cummax() - 1).min()
print(f"months={len(mdf)} total={(eq.iloc[-1] - 1) * 100:.1f}% vs NIFTY {(beq.iloc[-1] - 1) * 100:.1f}% | "
      f"monthly hit-rate vs NIFTY {(mdf.strat > mdf.bench).mean() * 100:.0f}% | maxDD {dd * 100:.1f}% | "
      f"Sharpe~{mdf.strat.mean() / mdf.strat.std() * np.sqrt(12):.2f}")
for y, g in mdf.groupby(mdf.index.year):
    print(f"  {y}: momentum {((1 + g.strat).prod() - 1) * 100:6.1f}%  NIFTY {((1 + g.bench).prod() - 1) * 100:6.1f}%")

# 4) Current state
print("\n== Current market state ==")
last = b.iloc[-1]
print(f"NIFTY 50 {last:.2f}; 50-DMA {ind.sma(b, 50).iloc[-1]:.2f}; 200-DMA {sma200.iloc[-1]:.2f}; "
      f"20d return {(b.iloc[-1] / b.iloc[-21] - 1) * 100:.1f}%; 63d return {(b.iloc[-1] / b.iloc[-64] - 1) * 100:.1f}%")
above = sum(1 for d in data.values() if d["close"].iloc[-1] > d["close"].iloc[-200:].mean())
print(f"Breadth: {above}/{len(data)} stocks above their 200-day average")
look = closes.iloc[:-21]
mom_now = (look.iloc[-1] / look.iloc[-127] - 1).dropna().sort_values(ascending=False)
print("Momentum leaders now (6-1m):", ", ".join(f"{k} {v * 100:+.0f}%" for k, v in mom_now.head(10).items()))
