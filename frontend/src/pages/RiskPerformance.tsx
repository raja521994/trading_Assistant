import clsx from "clsx";
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { inr, istTime, num, pct, titleCase, tone } from "../lib/format";
import { Empty, ErrorBox, Loading, Panel, Stat } from "../components/ui";
import { LinesChart, type Overlay } from "../components/Charts";

interface Perf {
  equity: number; peak_equity: number; daily_pnl: { date: string; realized_pnl: number }[];
  today: { date: string; realized: number; unrealized: number };
  equity_drawdown: { date: string; equity: number; drawdown_pct: number }[];
  risk: { open_positions: number; max_open_positions: number; gross_exposure: number; risk_at_stops: number;
    sector_exposure: { sector: string; value: number; pct: number; limit_pct: number }[]; drawdown_pct: number; max_drawdown_pct: number;
    daily_loss: number; daily_loss_limit: number; kill_switch: boolean };
  strategy_attribution: { strategy: string; trades: number; net_pnl: number; wins: number }[];
  recommendation_outcomes: { origin: string; strategy: string; count: number; target: number; stop: number; other: number; avg_r: number | null }[];
  risk_breaches: { ts: string; symbol?: string; violations?: string[] }[];
  notes: string[];
}
type RiskConfig = Record<string, number | boolean | string | Record<string, number | string>>;
interface WeightsResp { weights: Record<string, number>; defaults: Record<string, number>; components: Record<string, { label: string; available: boolean; description: string }>; note: string }

const RISK_FIELDS: [string, string][] = [
  ["account_equity", "Account equity (₹)"], ["risk_per_trade_pct", "Max risk per trade %"], ["max_daily_loss_pct", "Max daily loss %"],
  ["max_drawdown_pct", "Max portfolio drawdown %"], ["max_open_positions", "Max open positions"], ["max_sector_exposure_pct", "Max sector exposure %"],
  ["max_alloc_per_instrument_pct", "Max allocation / instrument %"], ["max_leverage", "Max leverage (x)"], ["min_avg_turnover_cr", "Min avg turnover (₹ cr)"],
  ["min_risk_reward", "Min risk-reward"], ["min_score", "Min composite score"], ["slippage_bps", "Slippage (bps)"],
  ["min_oos_profit_factor", "Min OOS profit factor (edge gate)"], ["min_oos_trades", "Min OOS trades (edge gate)"],
];
const BOOL_FIELDS: [string, string][] = [["allow_overnight", "Allow overnight positions"], ["allow_intraday_short", "Allow intraday shorts"],
  ["regime_filter", "Regime gate: no new longs while NIFTY < 200-DMA"], ["require_oos_edge", "Edge gate: require walk-forward OOS edge"]];

function RiskEditor() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["risk-config"], queryFn: () => api.get<{ config: RiskConfig }>("/risk/config") });
  const [cfg, setCfg] = useState<RiskConfig | null>(null);
  useEffect(() => { if (q.data) setCfg(q.data.config); }, [q.data]);
  const save = useMutation({
    mutationFn: () => api.put<{ changed: Record<string, unknown> }>("/risk/config", cfg),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["risk-config"] }); qc.invalidateQueries({ queryKey: ["performance"] }); qc.invalidateQueries({ queryKey: ["scanner"] }); },
  });
  const kill = useMutation({
    mutationFn: (on: boolean) => api.post("/risk/kill-switch", { on }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["risk-config"] }); qc.invalidateQueries({ queryKey: ["performance"] }); },
  });
  if (!cfg) return <Loading />;
  const costs = cfg.costs as Record<string, number | string>;
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <span className="text-xs">Kill switch:</span>
        <span className={clsx("text-xs font-bold", cfg.kill_switch ? "text-red-400" : "text-emerald-400")}>{cfg.kill_switch ? "ON — new paper trades blocked" : "OFF"}</span>
        <button className={clsx("btn", cfg.kill_switch ? "" : "btn-danger")} onClick={() => kill.mutate(!cfg.kill_switch)}>{cfg.kill_switch ? "Release kill switch" : "Engage kill switch"}</button>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
        {RISK_FIELDS.map(([k, label]) => (
          <label key={k} className="flex flex-col gap-0.5"><span className="label">{label}</span>
            <input className="input" type="number" step="any" value={String(cfg[k])} onChange={(e) => setCfg({ ...cfg, [k]: Number(e.target.value) })} /></label>
        ))}
        {BOOL_FIELDS.map(([k, label]) => (
          <label key={k} className="flex items-center gap-1 text-xs mt-3"><input type="checkbox" checked={!!cfg[k]} onChange={(e) => setCfg({ ...cfg, [k]: e.target.checked })} />{label}</label>
        ))}
      </div>
      <details className="text-xs">
        <summary className="cursor-pointer text-slate-400">Transaction cost model (estimates)</summary>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-2 mt-2">
          {Object.entries(costs).filter(([k]) => k !== "note").map(([k, v]) => (
            <label key={k} className="flex flex-col gap-0.5"><span className="label">{titleCase(k)}</span>
              <input className="input" type="number" step="any" value={String(v)} onChange={(e) => setCfg({ ...cfg, costs: { ...costs, [k]: Number(e.target.value) } })} /></label>
          ))}
        </div>
        <div className="mt-1 text-[10px] text-slate-500">{String(costs.note)}</div>
      </details>
      <div className="flex items-center gap-2">
        <button className="btn btn-primary" disabled={save.isPending} onClick={() => save.mutate()}>{save.isPending ? "Saving…" : "Save risk settings"}</button>
        {save.isSuccess && <span className="text-xs text-emerald-300">Saved ({Object.keys(save.data.changed).length} changes, audit-logged). Scanner re-run.</span>}
        {save.error && <ErrorBox error={save.error} />}
      </div>
    </div>
  );
}

function WeightsEditor() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["weights"], queryFn: () => api.get<WeightsResp>("/scoring/weights") });
  const [w, setW] = useState<Record<string, number> | null>(null);
  useEffect(() => { if (q.data) setW(q.data.weights); }, [q.data]);
  const save = useMutation({
    mutationFn: (weights: Record<string, number>) => api.put("/scoring/weights", weights),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["weights"] }); qc.invalidateQueries({ queryKey: ["scanner"] }); },
  });
  if (!w || !q.data) return <Loading />;
  return (
    <div className="space-y-2">
      {Object.entries(q.data.components).map(([k, c]) => (
        <div key={k} className={clsx("flex items-center gap-2 text-xs", !c.available && "opacity-50")}>
          <span className="w-44 shrink-0" title={c.description}>{c.label}</span>
          <input type="range" min={0} max={0.5} step={0.05} disabled={!c.available} value={w[k] ?? 0} onChange={(e) => setW({ ...w, [k]: Number(e.target.value) })} className="flex-1" />
          <span className="num w-10 text-right">{num(w[k] ?? 0)}</span>
          {!c.available && <span className="text-[10px] text-slate-500 w-32">not connected</span>}
        </div>
      ))}
      <div className="text-[10px] text-slate-500">{q.data.note} Unavailable components are excluded and the remaining weights renormalised.</div>
      <div className="flex gap-2">
        <button className="btn btn-primary" disabled={save.isPending} onClick={() => save.mutate(w)}>{save.isPending ? "Saving…" : "Save weights & re-scan"}</button>
        <button className="btn" onClick={() => setW(q.data!.defaults)}>Defaults</button>
        {save.isSuccess && <span className="text-xs text-emerald-300">Saved.</span>}
      </div>
    </div>
  );
}

export default function RiskPerformance() {
  const q = useQuery({ queryKey: ["performance"], queryFn: () => api.get<Perf>("/performance"), refetchInterval: 10_000 });
  const ddLines: Overlay[] = useMemo(() => q.data ? [{ name: "Drawdown %", color: "#ef4444", data: q.data.equity_drawdown.map((r) => ({ time: r.date, value: r.drawdown_pct })) }] : [], [q.data]);
  const pnlLines: Overlay[] = useMemo(() => q.data ? [{ name: "Daily P&L", color: "#38bdf8", data: q.data.daily_pnl.map((r) => ({ time: r.date, value: r.realized_pnl })) }] : [], [q.data]);
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const d = q.data!;
  const r = d.risk;
  return (
    <div className="space-y-3">
      <h1 className="text-lg font-semibold">Risk & Performance</h1>
      <Panel title="Live risk (paper account)">
        <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-8 gap-3">
          <Stat label="Equity" value={`₹${inr(d.equity, 0)}`} sub={`peak ₹${inr(d.peak_equity, 0)}`} />
          <Stat label="Today realized" value={`₹${inr(d.today.realized)}`} valueClass={tone(d.today.realized)} />
          <Stat label="Today unrealized" value={`₹${inr(d.today.unrealized)}`} valueClass={tone(d.today.unrealized)} />
          <Stat label="Daily loss / limit" value={`₹${inr(r.daily_loss, 0)}`} sub={`limit ₹${inr(r.daily_loss_limit, 0)}`} valueClass={r.daily_loss >= r.daily_loss_limit ? "text-down" : ""} />
          <Stat label="Drawdown / limit" value={pct(-r.drawdown_pct)} sub={`limit ${r.max_drawdown_pct}%`} />
          <Stat label="Open / max" value={`${r.open_positions} / ${r.max_open_positions}`} />
          <Stat label="Gross exposure" value={`₹${inr(r.gross_exposure, 0)}`} />
          <Stat label="Risk at stops" value={`₹${inr(r.risk_at_stops, 0)}`} sub={r.kill_switch ? "KILL SWITCH ON" : undefined} />
        </div>
        {r.sector_exposure.length > 0 && (
          <table className="tbl mt-3"><thead><tr><th>Sector</th><th className="text-right">Exposure</th><th className="text-right">% equity</th><th className="text-right">Limit</th></tr></thead>
            <tbody>{r.sector_exposure.map((s) => <tr key={s.sector}><td>{s.sector}</td><td className="num text-right">₹{inr(s.value, 0)}</td>
              <td className={clsx("num text-right", s.pct > s.limit_pct && "text-down")}>{pct(s.pct, 1, false)}</td><td className="num text-right">{s.limit_pct}%</td></tr>)}</tbody></table>
        )}
      </Panel>
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
        <Panel title="Daily realized P&L" bodyClass="p-1">{pnlLines[0].data.length ? <LinesChart lines={pnlLines} height={200} histogram /> : <Empty>No closed paper trades yet.</Empty>}</Panel>
        <Panel title="Drawdown (realized)" bodyClass="p-1">{ddLines[0].data.length ? <LinesChart lines={ddLines} height={200} /> : <Empty>No closed paper trades yet.</Empty>}</Panel>
      </div>
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
        <Panel title="Strategy attribution (paper)">
          {d.strategy_attribution.length === 0 ? <Empty>No closed paper trades.</Empty> : (
            <table className="tbl"><thead><tr><th>Strategy</th><th className="text-right">Trades</th><th className="text-right">Wins</th><th className="text-right">Net P&L</th></tr></thead>
              <tbody>{d.strategy_attribution.map((a) => <tr key={a.strategy}><td>{a.strategy}</td><td className="num text-right">{a.trades}</td><td className="num text-right">{a.wins}</td>
                <td className={clsx("num text-right", tone(a.net_pnl))}>₹{inr(a.net_pnl)}</td></tr>)}</tbody></table>)}
        </Panel>
        <Panel title="Recommendation outcomes">
          {d.recommendation_outcomes.length === 0 ? <Empty>No resolved signals yet (historical replay runs after startup evidence).</Empty> : (
            <table className="tbl"><thead><tr><th>Origin</th><th>Strategy</th><th className="text-right">Signals</th><th className="text-right">Target</th><th className="text-right">Stop</th><th className="text-right">Other exit</th><th className="text-right">Avg R</th></tr></thead>
              <tbody>{d.recommendation_outcomes.map((o) => <tr key={`${o.origin}:${o.strategy}`}><td>{o.origin}</td><td>{o.strategy}</td><td className="num text-right">{o.count}</td>
                <td className="num text-right text-up">{o.target}</td><td className="num text-right text-down">{o.stop}</td><td className="num text-right">{o.other}</td>
                <td className={clsx("num text-right", tone(o.avg_r))}>{num(o.avg_r, 3)}</td></tr>)}</tbody></table>)}
          <div className="mt-1 text-[10px] text-slate-500">{d.notes.join(" ")}</div>
        </Panel>
      </div>
      <Panel title={`Risk-limit breaches / rejected paper orders (${d.risk_breaches.length})`} bodyClass="p-0 max-h-64 overflow-auto">
        {d.risk_breaches.length === 0 ? <Empty>No rejected orders.</Empty> : (
          <table className="tbl"><thead><tr><th>Time</th><th>Symbol</th><th>Violations</th></tr></thead>
            <tbody>{d.risk_breaches.map((b, i) => <tr key={i}><td>{istTime(b.ts)}</td><td>{b.symbol}</td><td className="whitespace-normal text-red-300">{(b.violations ?? []).join("; ")}</td></tr>)}</tbody></table>)}
      </Panel>
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
        <Panel title="Risk limits (enforced on every paper order; never bypassed by AI)"><RiskEditor /></Panel>
        <Panel title="Scoring weights (transparent composite)"><WeightsEditor /></Panel>
      </div>
    </div>
  );
}
