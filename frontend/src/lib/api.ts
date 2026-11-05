// Thin typed client for the FastAPI backend. All requests go through the Vite proxy (/api) —
// no broker tokens, API keys or secrets are ever present in the browser.

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, message: string, detail: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let detail: unknown = null;
    try {
      detail = (await res.json()).detail;
    } catch {
      /* non-JSON error */
    }
    const msg =
      typeof detail === "string"
        ? detail
        : detail && typeof detail === "object" && "message" in detail
          ? String((detail as { message: string }).message)
          : `${res.status} ${res.statusText}`;
    throw new ApiError(res.status, msg, detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: "PUT", body: JSON.stringify(body ?? {}) }),
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};

export function qs(params: Record<string, string | number | undefined | null | boolean>): string {
  const p = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  });
  const s = p.toString();
  return s ? `?${s}` : "";
}

// ---------- Types (subset of backend payloads used by the UI) ----------
export type Action = "BUY" | "SELL" | "WAIT" | "NO TRADE";
export type SignalState = "WATCH" | "SETUP" | "TRIGGERED" | "ACTIVE" | "INVALIDATED" | "EXITED" | "EXPIRED";

export interface Meta {
  app: string;
  version: string;
  provider: string;
  provider_name: string;
  data_label: string;
  provider_note: string | null;
  live_trading_enabled: boolean;
  live_trading_note: string;
  market_session: string;
  disclaimers: string[];
  server_time_ist: string;
  jobs: { startup: string; evidence: string; last_tick: number | null; errors: string[] };
  llm: { active_provider: string; model: string | null; note: string };
}

export interface ScoreComponent {
  value: number | null;
  detail: string;
}

export interface Recommendation {
  signal_id?: number;
  symbol: string;
  exchange: string;
  name: string;
  sector: string | null;
  instrument_type: string;
  mode: string;
  modes: string[];
  timeframe: string;
  strategy: string;
  strategy_name: string;
  strategy_version: string;
  generated_at: string;
  data_timestamp: string;
  signal_bar_ts: string;
  freshness: { status: string; data_label: string; quality: string; market_session: string };
  reference_price: number;
  direction: "bullish" | "bearish" | "neutral";
  direction_code: string;
  state: SignalState;
  action: Action;
  action_reasons: string[];
  entry: { reference: number; zone: [number, number] | null; note: string; trigger?: number | null; filled?: number | null };
  stop_loss: number | null;
  invalidation: string;
  targets: number[];
  target_note: string | null;
  risk_reward: number | null;
  holding_period: string;
  liquidity: { rating: string; avg_turnover_cr: number | null; min_required_cr?: number; spread?: string };
  position_size: {
    qty: number;
    risk_per_unit: number;
    risk_amount: number;
    capital_required: number;
    allocation_pct: number;
    limiting_factor: string;
    assumptions: string[];
  } | null;
  indicators: Record<string, number | null>;
  supporting_evidence: string[];
  conflicting_evidence: string[];
  event_risks: string[];
  oos_performance: Evidence;
  estimated_costs: { total: number; pct_of_turnover: number; basis: string; buy_side: number; sell_side: number } | null;
  score: number;
  score_breakdown: {
    components: Record<string, ScoreComponent>;
    weights: Record<string, number>;
    used: string[];
    excluded: string[];
    note: string;
  };
  risk_level: string;
  regime: string | null;
  research_only: boolean;
  research_only_reason: string;
  executable: boolean;
  expires_at: string;
  data_quality: DataQuality;
  gates?: Record<string, { name: string; applies: boolean; passed: boolean; detail: string }>;
  final_score?: number;
  regime_multiplier?: number;
  gate_multiplier?: number;
  blocked?: boolean;
  blocked_by?: string[];
  confluence?: { count: number; strategies: string[]; all_strategies: string[]; conflict: boolean };
  stop_info?: { stop_pct: number; cap_pct: number; atr14: number | null; atr_multiple: number | null; within_cap: boolean; rule: string } | null;
  target_info?: { method: string; p50_r?: number; p75_r?: number | null; n_trades?: number } | null;
  evidence_tier?: string | null;
  ml?: { p_win: number; threshold: number; above_threshold: boolean; model_useful: boolean; oos_auc: number | null } | null;
  ml_multiplier?: number;
}

export interface DataQuality {
  ok: boolean;
  status: string;
  freshness: string;
  bars: number;
  last_ts: string | null;
  expected_last_ts: string | null;
  missing_bars: number;
  issues: string[];
}

export interface Evidence {
  status: string;
  strategy?: string;
  version?: string;
  num_trades?: number;
  profit_factor?: number | null;
  win_rate_pct?: number | null;
  expectancy_r?: number | null;
  net_return_pct?: number | null;
  max_drawdown_pct?: number | null;
  sharpe?: number | null;
  benchmark_return_pct?: number | null;
  period_start?: string;
  period_end?: string;
  low_sample?: boolean;
  data_label?: string;
  method?: string;
  folds?: { fold: number; params: Record<string, unknown>; oos: Record<string, number | null>; is: Record<string, number | null> }[];
}

export interface Regime {
  current: { as_of: string; trend: string; volatility: string; breadth_pct_above_sma50: number | null; label: string; facts: Record<string, number | null> } | null;
  previous: Regime["current"];
  changes: string[];
}

export interface Playbook {
  benchmark_above_200dma: boolean | null;
  headline: string;
  data_label: string;
  note: string;
  settings: { regime_filter: boolean; require_oos_edge: boolean; min_oos_profit_factor: number; min_oos_trades: number };
  strategies: {
    strategy: string; name: string; timeframe: string; oos_trades: number | null; oos_profit_factor: number | null;
    oos_net_return_pct: number | null; benchmark_return_pct: number | null;
    regime_split: Record<string, { num_trades: number; profit_factor: number | null; expectancy_r: number | null }> | null;
    regime_gate: boolean; edge_gate: boolean; edge_detail: string;
  }[];
}

export interface ScanResponse {
  playbook?: Playbook;
  generated_at: string;
  data_label: string;
  recommendations: Recommendation[];
  blocked: { symbol: string; exchange: string; timeframe: string; action: string; reason: string; issues: string[]; strategies: string[] }[];
  notes: string[];
  regime: Regime | null;
  total: number;
  counts: Record<string, number>;
  filters: {
    modes: string[];
    unavailable_modes: Record<string, string>;
    exchanges: string[];
    strategies: string[];
    states: string[];
    actions: string[];
    risk_levels: string[];
    liquidity: string[];
  };
}

export interface Trade {
  symbol: string;
  exchange: string;
  direction: string;
  entry_ts: string;
  entry_price: number;
  exit_ts: string;
  exit_price: number;
  qty: number;
  stop: number;
  target: number | null;
  exit_reason: string;
  gross_pnl: number;
  costs: number;
  net_pnl: number;
  r_multiple: number;
  bars_held: number;
  return_pct: number;
}

export interface CurvePoint {
  time: string;
  value: number;
}

export interface PaperTrade {
  id: number;
  signal_id: number | null;
  symbol: string;
  exchange: string;
  sector: string | null;
  strategy: string;
  mode: string;
  direction: string;
  qty: number;
  entry_price: number;
  entry_time: string;
  stop: number;
  target: number | null;
  status: string;
  last_price: number | null;
  unrealized_pnl: number | null;
  exit_price: number | null;
  exit_time: string | null;
  exit_reason: string | null;
  costs: number;
  net_pnl: number | null;
  r_multiple: number | null;
  value: number;
  data_label: string | null;
}

export interface Explanation {
  facts: string[];
  calculations: string[];
  assumptions: string[];
  interpretation: string[];
  provider: string;
  warnings: string[];
  sources: string[];
}
