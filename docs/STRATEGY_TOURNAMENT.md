# Strategy tournament: method and results

This document explains how every strategy in the library is validated, and what the latest run on real Zerodha Kite
data found. The same numbers are in **Strategy Lab → Strategy tournament** and in the exported CSV / HTML report
(`backend/var/tournament/<run_id>/`).

> Research and paper trading only. Not investment advice. Out-of-sample results do not guarantee future returns.

## 1. Library (52 strategies from the brief)

| Group | Testable here | Not testable (missing data) | Context only |
|---|---|---|---|
| A Trend following (1-10) | all 10 | – | – |
| B Momentum & RS (11-16) | 6 (5 portfolio rebalance + RS line) | – | – |
| C Mean reversion (17-22) | 6 (incl. pairs via stock futures) | – | – |
| D Intraday (23-30) | 7 | #28 gap-and-go on news (news feed) | – |
| E Volume (31-34) | #31, #33 | #32 delivery % (NSE bhavcopy), #34 bulk/block/insider deals | – |
| F Price action (35-37) | all 3 (confirmed pivots, no repainting) | – | – |
| G Fundamental (38-44) | – | all 7 (fundamentals, EPS/result dates, valuation, dividends) | – |
| H Derivatives (45-49) | – | all 5 (OI / futures / option-chain history not ingested) | – |
| I Market context (50-52) | – | – | #50 used by the regime classifier; #51, #52 not connected |

Code: `backend/app/quant/library/` (catalog, daily, intraday, portfolio/pairs), plus the five original YAML
strategies in `backend/app/quant/specs/`. Each catalog entry declares timeframe, modes, direction, regime hypothesis,
data requirements, a **fixed parameter grid** (classic published values) and the parameters perturbed in the stability
test. Grids are set before testing and are never tuned to make a strategy pass.

## 2. Data

- Daily OHLCV from 2018-01-01 for the current **NIFTY 200** constituents (niftyindices.com list, cached in
  `backend/config/universe/`) and 18 indices, including INDIA VIX, sector indices, NIFTY200 Momentum 30 and the
  NIFTY50 Dividend Points index.
- 5-minute bars for one year for the NIFTY 50 names (intraday strategies).
- Benchmark: **NIFTY 50 total return**, built from the price index plus reinvested daily increments of the NIFTY50
  Dividend Points index.
- Splits and bonuses: unadjusted jumps are detected and back-adjusted (`data/corporate_actions.py`).
- **Survivorship bias:** constituents are today's, not point-in-time. See checks 5 and 0 below.

## 3. Execution model (same engine as the live scanner)

A signal on a bar's close creates a SETUP with a trigger level. A later **closed** bar must cross the trigger
(TRIGGERED), and the entry fills at the **next bar's open** with liquidity-based slippage. This uses the shared state
machine in `signals/setup_fsm.py`, so there are no same-bar fills and no look-ahead. Untriggered setups expire after 3
bars. Stops are filled at the stop price (or at the open on a gap), and the stop is assumed to fill first if a bar
touches both stop and target. Stops wider than the per-mode cap (intraday 1.5%, swing 6%, positional 10%) are
skipped. Charges come from `config/charges.yaml` (CNC / MIS / futures, with an as-of date). Slippage is 3–40 bps per
side by 20-day traded value, plus a square-root size impact.

## 4. Validation (per strategy, `backend/app/validation/evaluate.py`)

- **Walk-forward:** 4 anchored folds. Each in-sample window ends 5 days before its out-of-sample (OOS) window
  (embargo). In-sample positions are closed at the in-sample end (purge). Parameters are chosen from the fixed grid
  by in-sample Sharpe. Only the stitched OOS trades and equity are used as evidence.
- **Metrics:** trades, win rate, average win/loss (₹ and R), expectancy (R), profit factor, net return, CAGR,
  annualised return, max drawdown, Calmar, Sharpe, Sortino, exposure, holding time, MFE/MAE, turnover, costs and
  capacity.
- **Bootstrap:** 2,000 resamples of the OOS trade sequence give 95% CIs for profit factor and expectancy, and
  p(mean R ≤ 0).
- **Multiple testing:** a deflated Sharpe probability against the best Sharpe expected by luck among all tested
  configurations (null variance 1/T). Benjamini–Hochberg FDR is reported, along with the expected number of false
  positives.
- **Stability:** each declared parameter is shifted ±20% around the most frequently selected parameters, and the
  results are shown as a profit-factor heatmap.
- **Robustness:** 2× costs and slippage (trade-level recomputation), calendar sub-periods (2018-19, 2020, 2021, 2022,
  2023-26; five equal chunks for short histories), and a regime split (bull/bear/sideways, NIFTY above/below its
  200-DMA, VIX high/low).

### Pass criteria (`backend/config/tournament.yaml`; all must hold, checked in this order)

0. *(cross-sectional portfolio strategies only)* point-in-time universe. **This always fails here**, because ranking
   today's constituents selects stocks already known to have survived and grown into the index. The real NIFTY200
   Momentum 30 index is shown as the unbiased reference.
1. OOS trades ≥ 30 (100+ preferred).
2. OOS profit factor ≥ 1.2, with the CI lower bound > 1.0.
3. Expectancy > 0R, with the CI lower bound > 0.
4. Beats NIFTY 50 total return, **or** a positive return with a max drawdown at least 30% lower.
5. Max drawdown ≤ 25%.
6. *(daily event strategies)* survivorship sensitivity: PF ≥ 1.1 and net > 0 on names already established in the
   first year of data (listed by month 1 and in the top half by first-year traded value, an ex-ante selection).
7. Parameter stability: every ±20% variant keeps PF ≥ 1.0 and at least half of the base expectancy.
8. Multiple testing: deflated Sharpe probability ≥ 0.95.
9. Still profitable with 2× costs and slippage.
10. Profitable in at least 3 of 5 sub-periods.

The composite score (0–100) used to rank PASSED strategies is: edge 30, risk-adjusted 20, robustness 20,
significance 15, benchmark 10, capacity 5. Weights are editable in Strategy Lab.

**Control:** the daily strategies are also run on zero-drift random walks with each stock's own volatility and real
volumes. If anything passes there, the validation is broken.

## 5. Latest results (real Kite data, run 2026-10-16)

**0 of 34 testable strategies passed every criterion. The Top 10 is empty, and nothing is padded into it.**
The random-walk control passed 0 of 24.

| Family | Passed | Median OOS PF | Reading |
|---|---|---|---|
| Trend following | 0 / 10 | 1.41 | Profitable, mostly beats NIFTY on drawdown, but not distinguishable from luck after 46 trials |
| Momentum & RS | 0 / 6 | 2.16 | Large simulated returns are survivorship-driven (see below); the RS-line event version has PF 1.07 |
| Mean reversion | 0 / 6 | 0.72 | No edge after costs (RSI(2) 0.84, Bollinger 0.81, pairs 0.78) |
| Intraday | 0 / 7 | 0.60 | Consistent losses after MIS costs and slippage (ORB 0.66, VWAP pullback 0.51) |
| Volume | 0 / 2 | 1.08 | Weak |
| Price action | 0 / 3 | 1.18 | Weak |

Closest candidates:

- **Darvas box breakout:** 142 OOS trades, PF 1.91 (CI 1.13–3.01), expectancy +0.40R (CI 0.10–0.73), net +46.6%
  with max DD −12.4% (NIFTY TR +174%, max DD −36.5%). It survives the established-names test (PF 1.85), ±20%
  parameters, 2× costs and 4/5 sub-periods. It **fails only multiple testing**: OOS Sharpe 0.87 against a luck bar of
  0.85, so the deflated-Sharpe probability is 0.51.
- **52-week-high breakout:** similar profile (PF 1.75, +0.44R). It also fails the sub-period test (2 of 5).

These two strategies return far less than buy-and-hold over the period. They pass the benchmark check only through
the lower-drawdown rule.

**Survivorship evidence:** simulated cross-sectional momentum on today's NIFTY 200 returned +868% OOS, while the real
NIFTY200 Momentum 30 index returned +189% over the same window. That gap is why check 0 exists.

## 6. How to run

- UI: Strategy Lab → **Run tournament**. It takes about 10 minutes with 3 worker processes and is cached by data hash
  + config hash. It also re-runs weekly, outside market hours.
- CLI: `cd backend; $env:TOURNAMENT_WORKERS=3; uv run python -m app.validation.run_cli [--force] [--only id1,id2]`
- API: `POST /api/tournament/run`, `GET /api/tournament/status`, `GET /api/tournament/report`,
  `GET /api/tournament/report.csv`, `GET /api/tournament/report.html`, `GET/PUT /api/tournament/config`.

## 7. Known limitations

- Constituents are not point-in-time. Delisted and demoted stocks (e.g. Yes Bank, Vodafone Idea) are missing.
- Intraday history is one year (sub-periods become five equal chunks).
- Pairs ignore futures margin, lot sizes and roll costs.
- Portfolio strategies use a volatility-based risk unit (2 × ATR) for R.
- Charges are estimates. Verify them at zerodha.com/charges and update `config/charges.yaml`.
