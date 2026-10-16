# Implementation checklist

Scope: the original MVP (Phases 1, 3, 4 + paper trading) plus the real-time / strategy-tournament brief
(docs/AUDIT_AND_PLAN.md, docs/STRATEGY_TOURNAMENT.md). Status legend: ✅ done · 🟡 partial · ⬜ remaining.

## Foundation (Phase 1)
- ✅ Repository structure, FastAPI backend, React/TS/Vite/Tailwind frontend
- ✅ Configuration via env / `.env` (placeholders in `.env.example`), JSON logging with secret redaction
- ✅ SQLAlchemy 2 models + Alembic migration; SQLite default, `DATABASE_URL` switch to Postgres
- 🟡 Postgres/TimescaleDB + Redis: `docker-compose.yml` provided, not exercised (Docker not installed); Redis unused
- ✅ Deterministic SIMULATED sample provider (42 equities NSE/BSE, 10 indices, daily + 5m, simulated intraday quotes)
- ✅ Local startup with documented commands; tests

## Market data (Phase 2 — partially in scope)
- ✅ Provider-independent `MarketDataProvider` interface with explicit capabilities / health
- ✅ **Yahoo Finance provider (default)** via yfinance: real NSE/BSE daily + 5m candles and quotes, no login;
  labelled DELAYED; derived daily bars for indices Yahoo lacks; verified end-to-end against real data
- ⬜ Yahoo is an unofficial source (personal use, may be delayed/rate-limited) — use Upstox for zero-delay data
- ✅ **Upstox real-time provider (zero-delay option)** via free read-only Analytics Token: instrument master, daily + 5m candles,
  batched LTP quotes, holiday calendar, rate limiting, retries, health; in-app token connect (stored server-side only)
- 🟡 Upstox: verified against the real API for token validation/rejection; full live ingest needs the user's token
- ⬜ Upstox WebSocket streaming (currently REST polling every few seconds), option chain, fundamentals, news APIs
- ✅ **Zerodha Kite Connect (default)** — official REST API, read-only: instruments, daily + 5m candles, quotes,
  in-app API-key/secret save and **Login with Zerodha** redirect flow (checksum token exchange), daily expiry handling
- ✅ Kite verified live: closes match the Kite app (official closing-auction price via quote), aligned 5m candles,
  self-repair of misaligned bars, upsert of recent bars
- ✅ Evidence-based gates (regime filter + walk-forward OOS edge per regime) and Market playbook
- ✅ KiteTicker WebSocket streaming (native binary protocol, FULL mode incl. depth, backoff/re-subscribe, tick store,
  1m/5m candle builder, browser WebSocket fan-out, STALE badges, feed-health panel, tick recording + replay)
- ⬜ Holdings/positions/orders read endpoints (not needed for paper trading)
- ✅ Incremental ingestion (skips calls when data is current), validation (OHLC, duplicates, gaps vs exchange
  session calendar, staleness, sufficiency), coverage matrix
- ✅ Live and sample signals kept separate (signals tagged by provider; switching providers retires old active signals)
- ✅ Split/bonus jump detection + back-adjustment; NIFTY 200 constituents auto-synced weekly (current list)
- ⬜ Point-in-time constituents / delisted instruments (survivorship bias disclosed and tested, not removed)

## Quant engine (Phase 3)
- ✅ Indicators: SMA, EMA, RSI, MACD, Bollinger, ATR, ADX/DI, VWAP, Supertrend, Donchian (+ reference tests)
- ✅ Versioned YAML strategy specs (reference, conditions, entry/stop/exit, sizing, costs, failure modes, overfitting risks)
- ✅ Strategies: EMA cross + ADX, Donchian breakout, RSI(2) mean reversion, Supertrend, ORB + VWAP (intraday)
- ✅ Market-wide scanner with filters (mode, exchange, strategy, state, action, risk, liquidity, score)
- ✅ Transparent configurable scoring; unavailable components (fundamentals, derivatives, news) excluded & disclosed
- ✅ Market regime classifier (trend, volatility, breadth) with "what changed"
- ✅ Every recommendation includes all spec §7 fields; WAIT / NO TRADE on conflicts, stale/invalid data, risk failures
- ✅ Signal state machine WATCH/SETUP/TRIGGERED/INVALIDATED/EXITED/EXPIRED with event history and outcomes
- 🟡 Support/resistance (swing pivots) on charts; not used as a strategy input
- ✅ 52-strategy library: 34 testable modules (trend, momentum/RS, mean reversion incl. pairs, intraday, volume,
  price action), 15 NOT TESTABLE with the missing dataset, 3 context inputs (see docs/STRATEGY_TOURNAMENT.md)
- ⬜ Redundant/correlated-signal detection across indicators

## Backtesting (Phase 4)
- ✅ Bar-by-bar engine: next-bar-open fills, gaps, stop-before-target, slippage, Zerodha-style costs, shared capital,
  position cap, intraday session flattening
- ✅ Metrics: net return, CAGR, max DD, Sharpe, Sortino, win rate, PF, expectancy, avg win/loss, trades, turnover,
  exposure, holding period, costs, benchmark-relative; monthly/yearly returns; drawdown series
- ✅ Anchored walk-forward with in-sample grid selection and stitched OOS evidence (used by the scanner score)
- ✅ Trade log, CSV export, persisted runs, parameter re-runs (sensitivity by hand)
- ✅ Validation engine: embargoed walk-forward, bootstrap CIs, deflated Sharpe + BH-FDR, ±20% stability heatmaps,
  2× costs, sub-periods, regime split, NIFTY total-return benchmark, survivorship sensitivity, random-walk control
- ✅ Strategy tournament (background workers, cache, Top 10 PASSED only, leaderboard, CSV/HTML export, weekly schedule)
- ✅ Decision engine: Top N <= 5 or exact NO TRADE message, Watch Only with what must change, decay monitor

## Paper trading & monitoring (Phase 7 — partial)
- ✅ User-initiated paper orders from signals or manual entry; full risk check; idempotency; audit log
- ✅ Automatic stop / target / intraday time exits from (simulated) quotes; manual close; outcome log
- ✅ Signal outcomes tracked forward (live scan) and via historical replay
- ✅ Risk & performance page: P&L, drawdown, exposure, attribution, outcomes, breaches, kill switch
- 🟡 Strategy drift: decay monitor disables passed strategies whose paper results fall below their OOS band;
  feed failures shown as STALE / feed state (no push notifications)

## AI (Phase 6 — partial)
- ✅ Provider-agnostic LLM interface, deterministic template explainer (default)
- ✅ Optional OpenAI-compatible connector with numeric-claim guardrail (untested against a real endpoint)
- ✅ Research assistant: why-in-scanner, strategy, conflicting evidence, downside risk, OOS performance,
  relative strength, regime change
- ⬜ Retrieval over external research documents; news summarisation (no news provider)

## Out of scope this round (shown as "not connected" in the UI)
- ⬜ F&O terminal (option chain, OI, IV, Greeks, payoff), futures basis
- ⬜ Fundamentals / long-term investing screens
- ⬜ ML models (baselines, gradient boosting, calibration)
- ⬜ Live order execution (not implemented by design; would require explicit enablement, compliance review,
  order preview, reconciliation)

## Security / operations
- ✅ No secrets in source or frontend; server-side env only; log redaction; CORS restricted to the dev origin
- ✅ Audit log for paper orders, rejections, risk-config and weight changes, kill switch
- ⬜ Dashboard user authentication/authorisation (API binds to 127.0.0.1; single local user)
- ⬜ HTTPS termination, backups/restore procedure, error monitoring, job queue (currently in-process asyncio + thread)

## Real-time + strategy tournament brief (Parts 1-8)
- ✅ Part 1 real-time data: one KiteTicker connection, tick store, candle builder, browser WebSocket, STALE gate,
  market hours + `config/nse_holidays.yaml`, playbook/header from one store, feed-health panel; tick->screen p95 < 1 s
- ✅ Part 2 bugs B1-B10 (state machine with trigger + ACTIVE, gated final score, chart overlays, ATR/structure
  stops with per-mode caps, MFE targets, confluence, split guard, look-ahead test for every strategy, charges.yaml
  with liquidity slippage and F&O costs, explainability fields)
- 🟡 Part 2 B7 Trading vs Investing: investing (fundamentals-led) mode is NOT TESTABLE without fundamentals data
- ✅ Part 3 strategy library (52 catalogued; untestable ones report the missing data)
- ✅ Part 4 validation engine;  ✅ Part 5 tournament (result: 0 of 34 passed on current data)
- ✅ Part 6 decision engine;  ✅ Part 7 UI (Strategy Lab tournament, Scanner tabs + decision panel, chart overlays,
  live candles, portfolio heat, regime inputs, assistant grounded in tournament/decision data, disclaimers)
- ✅ Part 8 tests (125), replay mode (stored bars or recorded tick file), config files, README
- ⬜ Redis (single-process in-memory store chosen; documented trade-off), point-in-time constituents, news,
  fundamentals, delivery %, F&O history, FII/DII and macro feeds
