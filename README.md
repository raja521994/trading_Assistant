# AI Trading Dashboard — NSE/BSE research & paper trading

An explainable, risk-aware trading-intelligence dashboard for Indian equities. It scans a universe of
NSE/BSE instruments with deterministic, versioned strategies, explains every setup (facts → calculations →
assumptions → interpretation), sizes positions against configurable risk limits, backtests with walk-forward
out-of-sample evaluation, and lets you **paper trade** signals and track outcomes.

> **Not investment advice. No returns are guaranteed.** Live order execution is **disabled and not
> implemented**. Live market data comes from **Zerodha Kite Connect** (official API, read-only) once you link your
> Kite Connect app and log in. Until then — or after Zerodha's daily session reset — the app shows **SIMULATED** sample
> data and labels it as such in every API response and on every page.

```
NSE/BSE + authorised providers ──► Market data & storage ──► Quant engine + AI research ──► Risk & signal validation ──► Dashboard
 (KiteProvider [LIVE, default]     (ingest, validation,        (indicators, YAML strategy     (sizing, limits, costs,       (React/TS/Vite,
  | (Upstox / Yahoo, optional)      SQLite/Postgres,            specs, scanner, scoring,       state machine, WAIT /         Lightweight Charts,
  | SampleProvider [SIMULATED])     coverage matrix)            regime, template explainer)    NO TRADE, paper broker)       SSE ticker)
```

## Link Zerodha (live data, default)

1. At <https://developers.kite.trade> create a Kite Connect app with **Redirect URL `http://127.0.0.1:5173`**. Its plan
   must include market data / historical data (otherwise Zerodha returns a permission error, which the app shows).
2. Start the app, open **Data & Integrations → Live market data — Zerodha Kite Connect**, paste the app's **API key**
   and **API secret**, click **Save keys** (stored only in `backend/.env`, never sent back to the browser).
3. Click **Login with Zerodha**, sign in on kite.zerodha.com; Zerodha redirects back to `http://127.0.0.1:5173`, and the
   backend exchanges the one-time `request_token` (SHA-256 checksum with your secret) for today's access token.
4. The backend loads ~3 years of daily bars and 30 days of 5-minute bars (≈1–2 min at Zerodha's 3 req/s historical
   limit), then polls quotes every few seconds and new 5-minute bars every minute.

Zerodha resets access tokens daily (~06:00 IST): **log in again each trading day** (the app detects the expired session
and falls back to clearly-labelled SIMULATED data until you do). The app never calls order APIs and never asks for
your Kite password, PIN or TOTP.

## Optional other data sources (hidden in the UI)

The code also contains read-only Upstox and Yahoo Finance adapters (`DATA_PROVIDER=upstox|yahoo`), not shown in the UI.

**Yahoo Finance.** No Yahoo account, login or API key is used — the backend reads public
Yahoo Finance data through the open-source [`yfinance`](https://github.com/ranaroussi/yfinance) library: daily bars since
2023, 5-minute bars for the last 30 days (Yahoo keeps ~60), and quotes refreshed every ~30 s, all batched across the
universe. Caveats: Yahoo has **no official API** (undocumented endpoints; personal, non-commercial use per Yahoo's terms),
NSE/BSE data may be **delayed up to ~15 minutes** (labelled DELAYED), requests can be rate-limited, and some NSE sector
indices (Fin Service, Auto, FMCG, Metal, Energy) have no daily history on Yahoo — their daily bars are derived from
5-minute data (~60 days only), which is disclosed in Data & Integrations. Choose the active source in
**Data & Integrations → Active market-data source**.

### Zero-delay alternative: Upstox (free)

1. Have an Upstox account (free to open at upstox.com).
2. Open <https://account.upstox.com/developer/apps#analytics> → **Analytics** tab → **Generate Token** → **Confirm**, and copy the token.
3. In the dashboard go to **Data & Integrations → Live market data — Upstox**, paste the token and click **Connect**
   (or put `UPSTOX_ACCESS_TOKEN=...` in `backend/.env` and restart the backend).

The token is validated with one read-only quote call and stored **only on the server** (`backend/.env`, git-ignored);
it is never returned to the browser or logged. The Analytics Token is valid for 1 year, needs no daily login, and is
strictly **read-only** — it cannot place or modify orders. After connecting, the backend downloads ~3 years of daily
bars and the last 30 days of 5-minute bars for the universe (about a minute), then polls real-time LTP quotes every
few seconds and new 5-minute bars every minute. Exchange holidays come from the Upstox holiday API.

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
migrations) and, in a background thread, ingests history from the active provider, runs the first scan, then
computes walk-forward evidence and historical signal replay (the header shows `Evidence: running → ready`).
With sample data this takes ≈30 s; with Upstox ≈1–2 minutes on first connect.

Optional configuration: copy `.env.example` to `.env` (repo root or `backend/`). All values are placeholders.
To reset all local state, stop the backend and delete `backend/var/`.

### Tests, typecheck, build

```powershell
cd backend;  uv run pytest            # 85 tests: indicators, signal logic, sizing, risk limits, costs, backtest, API
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
| `GET /api/integrations/kite`, `POST .../kite/keys`, `POST/DELETE .../kite/session` | Zerodha status, save API key+secret (server-side), exchange the login `request_token`, logout |
| `POST /api/integrations/provider` | Choose the active data source (yahoo / upstox / kite / sample) |
| `POST/DELETE /api/integrations/upstox/token` | Validate + store (server-side) / remove the Upstox Analytics Token and switch provider |
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
| **Yahoo Finance** (`backend/app/providers/yahoo.py`) — optional, hidden | Real data via `yfinance` (no login): batched daily + 5-minute candles, quotes, holiday inference (no bars on a weekday), derived daily bars for indices Yahoo lacks. Labelled DELAYED. Verified end-to-end against real Yahoo data (ingest → scan → walk-forward). | Unofficial/undocumented endpoints, personal non-commercial use only; may be delayed or rate-limited; raw (unadjusted) prices; static universe. |
| **Upstox** (`backend/app/providers/upstox.py`) — optional, hidden | Real-time, read-only via the free Analytics Token: instrument master (public BOD JSON), daily candles (V3, from 2023), 5-minute candles (last 30 days + today's completed bars), batched LTP quotes (V3, ≤500 keys/call), market holidays. Rate-limited to 8 req/s (Upstox limit 50/s, 500/min). Universe: NIFTY 50-style list (configurable `UPSTOX_UNIVERSE`) + 10 indices. Unit-tested with a mocked HTTP transport; token rejection verified against the real API. | Universe is static (not auto-synced with index rebalances); candles not adjusted here for corporate actions; WebSocket streaming, option chain, fundamentals and news APIs exist at Upstox but are not wired yet; redistribution terms if shared beyond personal use. |
| **SampleProvider** | Used only when no live credentials are configured (or `DATA_PROVIDER=sample`). 42 equities + 10 indices, deterministic. **SIMULATED.** | n/a — synthetic, weekday calendar. |
| **Zerodha Kite Connect** (`backend/app/providers/kite.py`) — **default** | Official Kite Connect v3 REST API, read-only: instrument dumps (NSE/BSE, cached daily), daily + 5-minute historical candles (chunked, 2.5 req/s), batched quotes (≤450/call, 1 req/s), in-app login via the official redirect flow with checksum token exchange, daily-expiry detection, holiday inference, clear errors for expired sessions and plans without market data. Unit-tested with a mocked HTTP transport; the real token endpoint was verified to respond. | Your plan includes quotes + historical data; full live ingest needs your login; static universe; KiteTicker WebSocket not wired (REST polling). |
| Fundamentals, corporate actions, news, F&O (OI/IV/Greeks/option chain), broker holdings | **Not connected** — shown as such in the UI; no placeholder buttons. | Choose licensed providers; evaluate data rights per provider. |

## Configuration

See `.env.example`. Key settings: `DATABASE_URL` (default SQLite under `backend/var/`; Postgres via
`docker-compose.yml` + `psycopg`), `DATA_PROVIDER` (`kite` default / `sample` / `upstox` / `yahoo`), `UNIVERSE`, `KITE_*`, `YAHOO_*`, `UPSTOX_*`, `SAMPLE_*`, `KITE_*`, `LLM_*`,
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

- Sample data (used only when live data is not connected) is synthetic; results on it say **nothing** about real markets.
- Even on real data, a backtest/walk-forward result is not a promise of future performance.
- Universe = instruments the provider currently lists (survivorship bias); no corporate-action adjustment.
- Cost-model rates are estimates (verify at zerodha.com/charges); income tax on gains is excluded.
- Exchange holidays: from Upstox (current year) when connected; with Yahoo a weekday with no NIFTY 50 bar after 09:45 is treated as a holiday. Historical gap checks use the NIFTY 50 bar dates as the session calendar.
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
