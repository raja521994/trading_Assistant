import clsx from "clsx";
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type CurvePoint, type Evidence, type Trade } from "../lib/api";
import { inr, istTime, num, pct, tone } from "../lib/format";
import { DataLabel, Empty, ErrorBox, KV, Loading, Panel, Pill, Stat } from "../components/ui";
import { LinesChart, type Overlay } from "../components/Charts";

interface StrategySpec {
  id: string; name: string; version: string; category: string; modes: string[]; timeframe: string; direction: string;
  reference: string; market_conditions: string; entry: string; stop_loss: string; exit: string; position_sizing: string;
  costs: string; failure_modes: string[]; overfitting_risks: string; limitations: string; params: Record<string, unknown>;
  param_grid: Record<string, unknown[]>; evidence: Evidence; indicators: string[]; required_data: string[];
}
interface Metrics { [k: string]: number | string | boolean | null }
interface BacktestResult {
  id?: number; strategy: string; strategy_name: string; version: string; timeframe: string; params: Record<string, unknown>;
  universe: string[]; metrics: Metrics; equity: CurvePoint[]; drawdown: CurvePoint[]; benchmark: CurvePoint[];
  periods: { monthly: { period: string; return_pct: number }[]; yearly: { period: string; return_pct: number }[] };
  trades: Trade[]; skipped_entries: number; assumptions: string[]; warnings: string[]; data_label: string; seconds: number;
  walk_forward?: { folds: { fold: number; in_sample: { start: string; end: string }; out_of_sample: { start: string; end: string }; selected_params: Record<string, unknown>; is_metrics: Metrics; oos_metrics: Metrics }[]; oos_metrics: Metrics | null; oos_equity: CurvePoint[]; method: string; error?: string | null };
}

const METRIC_ROWS: [string, string, (v: number | null) => string][] = [
  ["net_return_pct", "Net return", (v) => pct(v)], ["cagr_pct", "CAGR", (v) => pct(v)],
  ["max_drawdown_pct", "Max drawdown", (v) => pct(v)], ["sharpe", "Sharpe", (v) => num(v)], ["sortino", "Sortino", (v) => num(v)],
  ["win_rate_pct", "Win rate", (v) => pct(v, 1, false)], ["profit_factor", "Profit factor", (v) => num(v)],
  ["expectancy", "Expectancy / trade", (v) => `₹${inr(v)}`], ["expectancy_r", "Expectancy (R)", (v) => num(v, 3)],
  ["avg_win", "Avg winner", (v) => `₹${inr(v)}`], ["avg_loss", "Avg loser", (v) => `₹${inr(v)}`],
  ["num_trades", "Trades", (v) => String(v ?? "—")], ["turnover_x", "Turnover (x)", (v) => num(v)],
  ["exposure_pct", "Exposure time", (v) => pct(v, 1, false)], ["avg_holding_bars", "Avg holding (bars)", (v) => num(v, 1)],
  ["total_costs", "Total costs", (v) => `₹${inr(v, 0)}`], ["benchmark_return_pct", "Benchmark (NIFTY 50)", (v) => pct(v)],
  ["excess_return_pct", "Excess vs benchmark", (v) => pct(v)],
];

function MetricsGrid({ m }: { m: Metrics }) {
  return (
    <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
      {METRIC_ROWS.map(([k, label, f]) => {
        const v = m[k] as number | null | undefined;
        return <Stat key={k} label={label} value={v === undefined ? "—" : f(v ?? null)}
          valueClass={["net_return_pct", "excess_return_pct", "cagr_pct"].includes(k) ? tone(v ?? null) : undefined} />;
      })}
    </div>
  );
}

function Results({ r }: { r: BacktestResult }) {
  const eq: Overlay[] = useMemo(() => [
    { name: "Strategy", color: "#38bdf8", data: r.equity },
    ...(r.benchmark.length ? [{ name: "NIFTY 50 (buy & hold)", color: "#64748b", data: r.benchmark, dashed: true }] : []),
    ...(r.walk_forward?.oos_equity?.length ? [{ name: "Walk-forward OOS", color: "#22c55e", data: r.walk_forward.oos_equity }] : []),
  ], [r]);
  const dd: Overlay[] = useMemo(() => [{ name: "Drawdown %", color: "#ef4444", data: r.drawdown }], [r]);
  const [showAll, setShowAll] = useState(false);
  const trades = showAll ? r.trades : r.trades.slice(-100);
  return (
    <div className="space-y-3">
      <Panel title={`${r.strategy_name} v${r.version} · ${r.universe.length} instruments · ${r.metrics.period_start} → ${r.metrics.period_end}`}
        right={<span className="flex items-center gap-2"><DataLabel label={r.data_label} />{r.id && <a className="text-sky-300 hover:underline" href={`/api/backtest/runs/${r.id}/trades.csv`}>Export trades CSV</a>}<span className="text-slate-500">{r.seconds}s</span></span>}>
        {r.warnings.map((w, i) => <div key={i} className="mb-1 text-xs text-amber-300">⚠ {w}</div>)}
        <MetricsGrid m={r.metrics} />
        <div className="mt-2 text-[10px] text-slate-500">Skipped entries (position cap / cash): {r.skipped_entries}. Params: {JSON.stringify(r.params)}</div>
      </Panel>
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
        <Panel title="Equity curve vs benchmark" bodyClass="p-1"><LinesChart lines={eq} height={260} /></Panel>
        <Panel title="Drawdown" bodyClass="p-1"><LinesChart lines={dd} height={260} /></Panel>
      </div>
      {r.walk_forward && (
        <Panel title="Walk-forward / out-of-sample">
          {r.walk_forward.error ? <div className="text-xs text-amber-300">{r.walk_forward.error}</div> : (
            <>
              <div className="text-[11px] text-slate-400 mb-2">{r.walk_forward.method}</div>
              <table className="tbl">
                <thead><tr><th>Fold</th><th>In-sample</th><th>Out-of-sample</th><th>Selected params</th><th className="text-right">IS Sharpe</th><th className="text-right">OOS Sharpe</th><th className="text-right">OOS trades</th><th className="text-right">OOS net</th><th className="text-right">OOS PF</th></tr></thead>
                <tbody>{r.walk_forward.folds.map((f) => (
                  <tr key={f.fold}><td>{f.fold}</td><td>{f.in_sample.start} → {f.in_sample.end}</td><td>{f.out_of_sample.start} → {f.out_of_sample.end}</td>
                    <td className="text-slate-400">{JSON.stringify(f.selected_params)}</td><td className="num text-right">{num(f.is_metrics.sharpe as number)}</td>
                    <td className="num text-right">{num(f.oos_metrics.sharpe as number)}</td><td className="num text-right">{String(f.oos_metrics.num_trades)}</td>
                    <td className={clsx("num text-right", tone(f.oos_metrics.net_return_pct as number))}>{pct(f.oos_metrics.net_return_pct as number)}</td>
                    <td className="num text-right">{num(f.oos_metrics.profit_factor as number)}</td></tr>))}</tbody>
              </table>
              {r.walk_forward.oos_metrics && <div className="mt-3"><div className="label mb-1">Stitched OOS metrics</div><MetricsGrid m={r.walk_forward.oos_metrics} /></div>}
            </>
          )}
        </Panel>
      )}
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-3">
        <Panel title="Yearly returns">
          <table className="tbl"><tbody>{r.periods.yearly.map((p) => <tr key={p.period}><td>{p.period}</td><td className={clsx("num text-right", tone(p.return_pct))}>{pct(p.return_pct)}</td></tr>)}</tbody></table>
        </Panel>
        <Panel title="Monthly returns" className="xl:col-span-2" bodyClass="max-h-56 overflow-auto">
          <div className="grid grid-cols-3 md:grid-cols-6 gap-1">
            {r.periods.monthly.map((p) => (
              <div key={p.period} className="rounded px-1.5 py-1 text-[11px]" style={{ background: p.return_pct >= 0 ? `rgba(34,197,94,${Math.min(Math.abs(p.return_pct) / 8, 0.6) + 0.08})` : `rgba(239,68,68,${Math.min(Math.abs(p.return_pct) / 8, 0.6) + 0.08})` }}>
                <div className="text-slate-300">{p.period}</div><div className="num text-white">{pct(p.return_pct, 1)}</div>
              </div>))}
          </div>
        </Panel>
      </div>
      <Panel title={`Trade log (${r.trades.length}${r.trades.length > 100 && !showAll ? ", latest 100 shown" : ""})`}
        right={r.trades.length > 100 ? <button className="btn" onClick={() => setShowAll(!showAll)}>{showAll ? "Show latest 100" : "Show all"}</button> : null}
        bodyClass="p-0 max-h-96 overflow-auto">
        {r.trades.length === 0 ? <Empty>No trades.</Empty> : (
          <table className="tbl">
            <thead><tr><th>Symbol</th><th>Dir</th><th>Entry</th><th className="text-right">Price</th><th>Exit</th><th className="text-right">Price</th><th className="text-right">Qty</th><th>Reason</th><th className="text-right">Costs</th><th className="text-right">Net P&L</th><th className="text-right">R</th><th className="text-right">Bars</th></tr></thead>
            <tbody>{[...trades].reverse().map((t, i) => (
              <tr key={i}><td>{t.symbol}</td><td>{t.direction}</td><td>{istTime(t.entry_ts)}</td><td className="num text-right">{inr(t.entry_price)}</td>
                <td>{istTime(t.exit_ts)}</td><td className="num text-right">{inr(t.exit_price)}</td><td className="num text-right">{t.qty}</td>
                <td className="text-slate-400">{t.exit_reason}</td><td className="num text-right">{inr(t.costs)}</td>
                <td className={clsx("num text-right", tone(t.net_pnl))}>{inr(t.net_pnl)}</td><td className={clsx("num text-right", tone(t.r_multiple))}>{num(t.r_multiple)}</td>
                <td className="num text-right">{t.bars_held}</td></tr>))}</tbody>
          </table>
        )}
      </Panel>
      <Panel title="Assumptions"><ul className="text-xs text-slate-400 space-y-0.5">{r.assumptions.map((a, i) => <li key={i}>• {a}</li>)}</ul></Panel>
    </div>
  );
}

export default function StrategyLab() {
  const qc = useQueryClient();
  const strats = useQuery({ queryKey: ["strategies"], queryFn: () => api.get<StrategySpec[]>("/strategies"), refetchInterval: 30_000 });
  const runs = useQuery({ queryKey: ["runs"], queryFn: () => api.get<{ id: number; created_at: string; strategy: string; summary: Metrics }[]>("/backtest/runs") });
  const [sid, setSid] = useState("ema_cross");
  const [form, setForm] = useState({ symbols: "", start: "", end: "", capital: 1000000, risk_pct: 1, max_alloc_pct: 15, max_positions: 10, slippage_bps: 5, walk_forward: true, folds: 3 });
  const [params, setParams] = useState<Record<string, unknown>>({});
  const [result, setResult] = useState<BacktestResult | null>(null);
  const spec = strats.data?.find((s) => s.id === sid);
  useEffect(() => { if (spec) setParams(spec.params); }, [spec]);

  const run = useMutation({
    mutationFn: () => api.post<BacktestResult>("/backtest", {
      strategy: sid, params,
      symbols: form.symbols.trim() ? form.symbols.split(/[,\s]+/).filter(Boolean) : null,
      start: form.start || null, end: form.end || null, capital: Number(form.capital), risk_pct: Number(form.risk_pct),
      max_alloc_pct: Number(form.max_alloc_pct), max_positions: Number(form.max_positions), slippage_bps: Number(form.slippage_bps),
      walk_forward: form.walk_forward, folds: Number(form.folds),
    }),
    onSuccess: (r) => { setResult(r); qc.invalidateQueries({ queryKey: ["runs"] }); },
  });
  const loadRun = async (id: number) => {
    const r = await api.get<{ result: BacktestResult }>(`/backtest/runs/${id}`);
    setResult({ ...r.result, id });
  };
  const field = (k: keyof typeof form, label: string, type = "number", w = "w-24") => (
    <label className="flex flex-col gap-0.5"><span className="label">{label}</span>
      <input className={clsx("input", w)} type={type} value={String(form[k])} onChange={(e) => setForm({ ...form, [k]: e.target.value })} /></label>
  );

  return (
    <div className="space-y-3">
      <h1 className="text-lg font-semibold">Strategy Lab</h1>
      {strats.isLoading && <Loading />}
      {strats.error && <ErrorBox error={strats.error} />}
      {strats.data && (
        <Panel title="Strategy knowledge base & walk-forward evidence" bodyClass="p-0 overflow-auto">
          <table className="tbl">
            <thead><tr><th>Strategy</th><th>Category</th><th>Modes</th><th>TF</th><th className="text-right">OOS trades</th><th className="text-right">Win %</th><th className="text-right">PF</th><th className="text-right">Exp (R)</th><th className="text-right">OOS net</th><th className="text-right">Max DD</th><th className="text-right">Sharpe</th><th>Notes</th></tr></thead>
            <tbody>{strats.data.map((s) => {
              const e = s.evidence;
              return (
                <tr key={s.id} onClick={() => setSid(s.id)} className={clsx("cursor-pointer", sid === s.id && "bg-ink-800")}>
                  <td className="font-semibold text-slate-100">{s.name} <span className="text-[10px] text-slate-500">v{s.version}</span></td>
                  <td>{s.category}</td><td>{s.modes.join(", ")}</td><td>{s.timeframe}</td>
                  {e.status === "ready" ? (<>
                    <td className="num text-right">{e.num_trades}</td><td className="num text-right">{num(e.win_rate_pct, 1)}</td>
                    <td className="num text-right">{num(e.profit_factor)}</td><td className="num text-right">{num(e.expectancy_r, 3)}</td>
                    <td className={clsx("num text-right", tone(e.net_return_pct))}>{pct(e.net_return_pct)}</td>
                    <td className="num text-right">{pct(e.max_drawdown_pct)}</td><td className="num text-right">{num(e.sharpe)}</td>
                    <td>{e.low_sample && <Pill className="text-amber-300">low sample</Pill>} <Pill>{e.data_label}</Pill></td></>
                  ) : <td colSpan={8} className="text-slate-500">evidence {e.status}</td>}
                </tr>);
            })}</tbody>
          </table>
        </Panel>
      )}

      {spec && (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
          <Panel title={`Specification · ${spec.name}`}>
            <div className="space-y-1 text-xs">
              <KV k="Reference" v={spec.reference} mono={false} />
              <KV k="Market conditions" v={spec.market_conditions} mono={false} />
              <KV k="Entry" v={spec.entry} mono={false} />
              <KV k="Stop-loss" v={spec.stop_loss} mono={false} />
              <KV k="Exit" v={spec.exit} mono={false} />
              <KV k="Position sizing" v={spec.position_sizing} mono={false} />
              <KV k="Costs" v={spec.costs} mono={false} />
              <KV k="Failure modes" v={spec.failure_modes.join(" ")} mono={false} />
              <KV k="Overfitting risks" v={spec.overfitting_risks} mono={false} />
              <KV k="Limitations" v={spec.limitations} mono={false} />
              <KV k="Walk-forward grid" v={JSON.stringify(spec.param_grid)} />
            </div>
          </Panel>
          <Panel title="Backtest configuration">
            <div className="flex flex-wrap gap-3">
              {field("symbols", "Symbols (blank = all)", "text", "w-56")}
              {field("start", "Start", "date", "w-36")}
              {field("end", "End", "date", "w-36")}
              {field("capital", "Capital ₹", "number", "w-28")}
              {field("risk_pct", "Risk/trade %")}
              {field("max_alloc_pct", "Max alloc %")}
              {field("max_positions", "Max positions")}
              {field("slippage_bps", "Slippage bps")}
              {field("folds", "WF folds")}
              <label className="flex items-center gap-1 text-xs mt-4"><input type="checkbox" checked={form.walk_forward} onChange={(e) => setForm({ ...form, walk_forward: e.target.checked })} />Walk-forward OOS</label>
            </div>
            <div className="mt-3 label">Parameters (sensitivity: change and re-run)</div>
            <div className="flex flex-wrap gap-3 mt-1">
              {Object.entries(params).map(([k, v]) => (
                <label key={k} className="flex flex-col gap-0.5"><span className="label">{k}</span>
                  <input className="input w-24" value={v === null ? "" : String(v)} onChange={(e) => {
                    const raw = e.target.value;
                    const parsed = raw === "" ? null : raw === "true" ? true : raw === "false" ? false : Number.isNaN(Number(raw)) ? raw : Number(raw);
                    setParams({ ...params, [k]: parsed });
                  }} /></label>
              ))}
            </div>
            <div className="mt-3 flex items-center gap-2">
              <button className="btn btn-primary" disabled={run.isPending} onClick={() => run.mutate()}>{run.isPending ? "Running backtest…" : "Run backtest"}</button>
              <button className="btn" onClick={() => setParams(spec.params)}>Reset params</button>
              <span className="text-[10px] text-slate-500">Transaction costs come from Risk settings (Zerodha-style estimates).</span>
            </div>
            {run.error && <div className="mt-2"><ErrorBox error={run.error} /></div>}
            <div className="mt-4 label">Previous runs</div>
            <div className="max-h-40 overflow-auto">
              <table className="tbl"><tbody>{(runs.data ?? []).map((r) => (
                <tr key={r.id} className="cursor-pointer" onClick={() => loadRun(r.id)}><td>#{r.id}</td><td>{r.strategy}</td><td>{r.created_at ? istTime(r.created_at) : ""}</td>
                  <td className={clsx("num text-right", tone(r.summary.net_return_pct as number))}>{pct(r.summary.net_return_pct as number)}</td>
                  <td className="num text-right">{String(r.summary.num_trades)} trades</td></tr>))}</tbody></table>
            </div>
          </Panel>
        </div>
      )}
      {run.isPending && <Loading what="Running bar-by-bar simulation" />}
      {result && <Results r={result} />}
    </div>
  );
}
