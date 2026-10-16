# Phase 0 — Audit and plan (2026-10-16)

Scope note: autopilot run. Where the brief says "ask before changes that materially alter results", the change is
made, explained here and in the phase summaries, and kept configurable so it can be reverted.

## 1. Why data is 30–60 s stale (root causes)

| # | Root cause | Where | Effect |
|---|---|---|---|
| R1 | Prices come from **REST polling** (`/quote` every ≥3 s, cached), not the Kite WebSocket | `providers/kite.py::quote` | 3 s+ floor, 1 req/s quote rate limit shared by everything |
| R2 | Backend → browser push (`/api/stream` SSE) runs every **5 s** and only for indices | `api/routes_market.py::stream` | Header ticker ≥5 s old |
| R3 | Pages poll REST with `refetchInterval` **10–60 s** (overview 15 s, scanner 20 s, charts 30–60 s) | `frontend/src/pages/*` | Tables/charts up to a minute old |
| R4 | Charts rebuild all series with `setData()` on every refresh | `components/Charts.tsx` | Flicker, CPU, no per-tick updates |
| R5 | 5-minute candles only arrive via the **60 s ingest** job (historical REST) | `jobs/scheduler.py` | Forming candle never shown; signals lag a full bar + 60 s |
| R6 | Regime/playbook use the **stored daily close** while the header uses the live quote | `quant/regime.py`, `quant/scanner.py` | NIFTY 22,997 vs 23,120 inconsistency |

## 2. Bugs and design flaws (confirmed in code)

| Ref | Finding | Root cause |
|---|---|---|
| B1 | HCLTECH "TRIGGERED" with entry 1,320 < reference 1,360 | TRIGGERED meant "next-open fill happened"; reference = latest close. No explicit trigger level; no ACTIVE state |
| B2 | TECHM score 88 while blocked | Gates changed the *action* but not the rank; blocked rows sorted with actionable ones |
| B3 | ITC chart showed Supertrend for a 5m ORB candidate | "Chart →" passes tf/strategy but overlays are global toggles; no signal-specific overlays (ORB high/low, entry/stop/target) |
| B4 | 7–12 % stops, 20–36 % targets | Stops = strategy rule (e.g. Supertrend line, 2×ATR on volatile names) without a per-mode cap; targets = fixed R multiple |
| B5 | Direction labels | Shorts already intraday-only; label not explicit enough in UI |
| B6 | Duplicate rows per symbol | One row per (strategy, symbol); no grouping / confluence |
| B7 | Trading vs investing mixed | Single list; no fundamentals data for investing |
| B8 | Look-ahead test covers 4 strategies only; no split/bonus guard; survivorship bias | `tests/test_signals.py`; Kite candles not checked for corporate-action jumps |
| B9 | Cost rates hard-coded; flat slippage; no F&O model | `risk/costs.py` |
| B10 | Explainability: OK for score; missing formula inputs for stops/targets | — |

## 3. Data availability (what Kite gives vs what needs other sources)

| Dataset | Source in this build | Strategies that depend on it |
|---|---|---|
| Live ticks, OHLC, volume, depth | Kite WebSocket (KiteTicker) | all live features |
| Daily / intraday history | Kite historical API (day: ~2000 days/request; 5m: 100 days/request) | all price strategies |
| Index constituents (Nifty 200) | NSE public CSV `ind_nifty200list.csv` (current list only — **not point-in-time**) | universe; survivorship bias disclosed |
| NSE holiday calendar | `config/nse_holidays.yaml` (user-maintained) + inference from the live feed | market hours |
| Delivery %, bhavcopy | NSE archives — **not wired** | #32 → NOT TESTABLE |
| Bulk/block deals, promoter buying | NSE/BSE — **not wired** | #34 → NOT TESTABLE |
| Fundamentals (ROCE, ROE, EPS, P/E…) | none | #38–44 → NOT TESTABLE; Investing mode shows "data missing" |
| Results dates / corporate actions / events | none | #41, event gate → NOT TESTABLE / UNKNOWN |
| News | none | #28 → NOT TESTABLE (needs news) |
| F&O: OI history, option chains, PCR, max pain, basis, VIX history | Kite has instruments + quotes, history not wired | #45–49 → NOT TESTABLE |
| FII/DII, USD-INR, crude, US10Y, GIFT Nifty | none | #51 context → shown as missing |

## 4. Plan

- **Phase 1 – real-time + bug fixes.** Native asyncio KiteTicker client (binary protocol per Kite docs) running once
  in the backend → in-process tick store (Redis not installed; single-process app — trade-off documented) →
  server-side 1m/5m candle builder seeded from historical backfill → WebSocket fan-out to the browser at ~4 Hz with
  only changed symbols → `series.update()` charts, flash cells, STALE badges (>5 s in market hours), freshness gate,
  health metrics. Tick replay mode for off-hours testing. Fixes B1–B6, B8, B9, R6.
- **Phase 2 – strategy library + validation engine.** Pluggable registry covering every testable strategy in the brief
  (event-driven trade strategies + portfolio/rebalance strategies); untestable ones registered as NOT TESTABLE with the
  missing dataset. Validation: walk-forward with purge/embargo, bootstrap CIs, Holm multiple-testing control +
  deflated Sharpe, ±20 % parameter perturbation, 2× costs, sub-periods, regime split, benchmark test.
- **Phase 3 – tournament.** Background process with progress, cached by data hash + config hash; Top 10 PASSED
  (never padded), leaderboard with first failed criterion, NOT TESTABLE list, per-regime results, equity curves,
  stability heatmap, CSV + HTML export. Random-walk control: no strategy may pass.
- **Phase 4 – decision engine + UI.** Regime (bull/bear/sideways × vol) with exposure budget; only passed +
  regime-suitable strategies; per-symbol score with editable weights × regime × gate multipliers; portfolio
  constraints; TOP N ≤ 5 or exactly "NO TRADE: no setup has a validated edge in the current regime" + Watch Only.
- **Phase 5 – tests, replay, docs.**
