# AI Trading Dashboard — NSE/BSE research & paper trading

An explainable, risk-aware trading-intelligence dashboard for Indian equities. It scans a universe of
NSE/BSE instruments with deterministic, versioned strategies, explains every setup (facts → calculations →
assumptions → interpretation), sizes positions against configurable risk limits, backtests with walk-forward
out-of-sample evaluation, and lets you **paper trade** signals and track outcomes.

> **Not investment advice. No returns are guaranteed.** Live order execution is **disabled and not
> implemented**. In the default configuration all market data is **SIMULATED** (synthetic, deterministic sample
> data) and is labelled as such in every API response and on every page.

```
NSE/BSE + authorised providers ──► Market data & storage ──► Quant engine + AI research ──► Risk & signal validation ──► Dashboard
 (SampleProvider [SIMULATED]       (ingest, validation,        (indicators, YAML strategy     (sizing, limits, costs,       (React/TS/Vite,
  | KiteProvider [read-only,        SQLite/Postgres,            specs, scanner, scoring,       state machine, WAIT /         Lightweight Charts,
    disabled until keys exist])     coverage matrix)            regime, template explainer)    NO TRADE, paper broker)       SSE ticker)
```

## Quick start (Windows / macOS / Linux)

Prerequisites: **Python 3.11+** with [uv](https://docs.astral.sh/uv/), **Node 18+** with npm. No Docker,
database server or API keys are needed for the sample-data mode.

```powershell
# 1) Backend (FastAPI on http://127.0.0.1:8000)
cd backend
uv sync                         # creates .venv and installs dependencies
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000

# 2) Frontend (Vite dev server on http://127.0.0.1:5173, proxies /api to the backend) — in a second terminal
cd frontend
npm install
npm run dev
```

Open <http://127.0.0.1:5173>. On first start the backend creates `backend/var/trading.db` (SQLite, via Alembic
migrations), ingests ~127k simulated bars (≈5 s), runs the first scan, and then computes walk-forward evidence and
historical signal replay in a background thread (≈30 s; the header shows `Evidence: running → ready`).

Optional configuration: copy `.env.example` to `.env` (repo root or `backend/`). All values are placeholders.
To reset all local state, stop the backend and delete `backend/var/`.

### Tests, typecheck, build

```powershell
cd backend;  uv run pytest            # 63 tests: indicators, signal logic, sizing, risk limits, costs, backtest, API
cd frontend; npm run typecheck        # tsc --noEmit
cd frontend; npm run build            # production bundle in frontend/dist
```

### Useful API endpoints (http://127.0.0.1:8000/docs for the full OpenAPI UI)

| Endpoint | Purpose |
|---|---|
| `GET /api/meta` | Data label (SIMULATED/LIVE), provider, job status, live-trading flag (always false) |
| `GET /api/market/overview` | Indices, breadth, sector heatmap, movers, regime, feed health |
| `GET /api/scanner?mode=&exchange=&strategy=&state=&action=&risk=&liquidity=&min_score=` | Ranked recommendations (all spec §7 fields) |
| `GET /api/signals/{id}` | Drill-down: recommendation, deterministic explanation, state history |
| `GET /api/chart/{exchange}/{symbol}?timeframe=1d\|5m&strategy=` | Candles, indicators, S/R levels, strategy markers |
| `GET /api/quote/{exchange}/{symbol}` | Latest quote with data label |
| `POST /api/backtest` | Bar-by-bar backtest (+ optional walk-forward); `GET /api/backtest/runs/{id}/trades.csv` export |
| `POST /api/paper/orders` | User-initiated paper trade (risk-checked, idempotent) |
| `GET/PUT /api/risk/config`, `POST /api/risk/kill-switch` | Risk limits & cost model (audit-logged) |
| `GET/PUT /api/scoring/weights` | Transparent composite-score weights |
| `POST /api/assistant/ask` | AI research assistant (retrieval + calculations, no invented data) |
| `GET /api/integrations` | Provider status, capabilities, coverage matrix, not-connected list |
| `GET /api/stream` | Server-sent events: index quotes, session, job status |

## What is implemented

**Pages**: Market Overview · AI Scanner (filters + drill-down + paper-trade preview) · Signal detail · Charts
(candles, volume, EMA20/50/200, Bollinger, Supertrend, VWAP, RSI, MACD, swing S/R, signal levels, strategy
entry/exit markers, multi-timeframe table) · Strategy Lab (knowledge base, backtests, walk-forward, equity vs
benchmark, drawdown, monthly/yearly, trade log, CSV export, parameter sensitivity) · Paper Portfolio · Risk &
Performance (risk-limit editor, kill switch, scoring weights, P&L, drawdown, attribution, recommendation outcomes,
breaches) · AI Assistant · Data & Integrations (providers, capabilities, coverage matrix, not-connected list).

**Strategies** (`backend/app/quant/specs/*.yaml`, machine-readable with references, entry/stop/exit, sizing,
costs, failure modes, overfitting risks, limitations):
`ema_cross` (EMA crossover + ADX), `donchian_breakout` (Turtle-style), `rsi2_meanrev` (Connors RSI(2) in uptrend),
`supertrend_trend` (Supertrend flip + EMA200), `orb_vwap` (intraday opening-range breakout + VWAP + volume, long/short).

**Signal pipeline**: data validation (OHLC consistency, duplicates, gaps, staleness, sufficiency) → strategy rules on
the latest completed bar → transparent score (trend, momentum, volume, volatility, relative strength, OOS backtest
evidence, liquidity; fundamentals/derivatives/news shown as unavailable and excluded) → risk sizing + cost estimate
→ action **BUY / SELL / WAIT / NO TRADE** → persisted state machine **WATCH → SETUP → TRIGGERED → EXITED**
(+ INVALIDATED / EXPIRED) with outcome recording. Every item is marked *research candidate (not executable)*.

**Backtester**: next-bar-open fills, stop/target intrabar logic (stop first if both), gap handling, slippage,
Zerodha-style cost model, shared capital with position cap, intraday flat at session end, metrics (net return,
CAGR, max DD, Sharpe, Sortino, win rate, PF, expectancy, avg win/loss, trades, turnover, exposure, holding period,
costs, benchmark-relative), anchored walk-forward with OOS stitching. Assumptions are returned with every result.

**AI**: provider-agnostic `LLMProvider`. Default `TemplateExplainer` is deterministic and only uses computed
facts. Optional OpenAI-compatible connector (`LLM_PROVIDER=openai_compatible`, `LLM_BASE_URL`, `LLM_API_KEY`,
`LLM_MODEL`): it receives only the computed facts, and any output containing a number not present in those facts
is rejected (falls back to the deterministic interpretation). The AI cannot place orders or change risk limits.

## Data providers and what needs verification

| Provider | Status in this build | Needs verification before use |
|---|---|---|
| **SampleProvider** | Active by default. 42 equities (37 NSE, 5 BSE) + 10 indices; daily bars from 2023-01-02 and 5-minute bars for the last 20 sessions; simulated intraday quotes during 09:15–15:30 IST. **SIMULATED.** | n/a — synthetic, weekday calendar (no exchange holidays). |
| **Zerodha Kite Connect** (`backend/app/providers/kite.py`) | Implemented against the official `kiteconnect` SDK interface (instruments, historical candles with chunking, quotes, rate limiting, retries, health). **Read-only; disabled** until `uv sync --extra kite` and `KITE_API_KEY` + `KITE_ACCESS_TOKEN` are set and `DATA_PROVIDER=kite`. **Not yet tested against the live API.** | Paid Kite Connect app; historical-data add-on and per-interval lookback limits; BSE/index coverage in the instrument master; quote fields & rate limits; KiteTicker WebSocket limits and depth permissions; exchange data-redistribution terms. Access tokens must be generated daily via the official login redirect — never enter your password/PIN/TOTP in this app. |
| Fundamentals, corporate actions, news, F&O (OI/IV/Greeks/option chain), broker holdings | **Not connected** — shown as such in the UI; no placeholder buttons. | Choose licensed providers; evaluate data rights per provider. |

## Configuration

See `.env.example`. Key settings: `DATABASE_URL` (default SQLite under `backend/var/`; Postgres via
`docker-compose.yml` + `psycopg`), `DATA_PROVIDER` (`sample`/`kite`), `SAMPLE_*`, `KITE_*`, `LLM_*`,
`BACKGROUND_JOBS`, `QUOTE_REFRESH_SECONDS`. Risk limits, cost-model rates and score weights are edited in the UI
(Risk & Performance page) and stored in the database with audit-log entries.

## Security & execution controls

- No order-placement code exists; `LIVE_TRADING_ENABLED` is informational and ignored. Paper fills are simulated locally.
- Secrets are read only from server-side environment variables, never sent to the browser, and redacted from logs
  (JSON logs with a redaction filter). The frontend calls the backend via the Vite proxy; no tokens in the bundle.
- Every paper order passes the risk engine (kill switch, per-trade risk, allocation, leverage/cash, open positions,
  duplicates, sector exposure, daily loss, drawdown, liquidity, short/overnight rules, R:R); rejections are explained
  and audit-logged. Idempotency keys prevent duplicate paper orders.
- The API binds to 127.0.0.1 and has **no user authentication** yet (local single-user tool) — see CHECKLIST.md.

## Limitations (read before trusting any number)

- Sample data is synthetic; backtests, walk-forward evidence and paper P&L on it say **nothing** about real markets.
- Universe = instruments the provider currently lists (survivorship bias); no corporate-action adjustment.
- Cost-model rates are estimates (verify at zerodha.com/charges); income tax on gains is excluded.
- Weekday calendar only; exchange holidays are not modelled (validation flags such gaps as possible holidays).
- The composite score is a ranking, not a calibrated probability.
- The intraday (ORB) scanner only produces setups during the trading session (until 15:15 IST).

## Repository layout

```
backend/   FastAPI app (app/), Alembic migrations, tests/
  app/providers   MarketDataProvider interface, SampleProvider, KiteProvider, registry
  app/data        sample generator, ingestion, validation, coverage, repository/cache
  app/quant       indicators, features, strategies + YAML specs, scanner, scoring, regime, overview
  app/risk        cost model, position sizing, risk limits
  app/signals     state machine, signal engine
  app/backtest    engine, metrics, walk-forward, service (evidence, replay)
  app/paper       paper broker / tracker
  app/ai          LLM provider interface, template explainer, OpenAI-compatible connector, assistant
  app/api         REST + SSE routes;  app/jobs  background scheduler
frontend/  React + TypeScript + Vite + Tailwind + TanStack Query + Lightweight Charts
docker-compose.yml  optional Postgres (TimescaleDB image) + Redis for later phases
CHECKLIST.md        done / partial / remaining
```
