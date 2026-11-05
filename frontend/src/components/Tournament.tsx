import clsx from "clsx";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { num } from "../lib/format";
import { DataLabel, Empty, ErrorBox, Loading, Panel, Pill } from "./ui";
import { LinesChart } from "./Charts";

type Row = {
  rank: number | null; no: number; id: string; name: string; family: string; kind: string; mode: string; direction: string;
  timeframe: string; result: string; first_failed: string | null; first_failed_detail: string | null; best_regime: string | null;
  oos_trades: number | null; profit_factor: number | null; pf_ci: [number | null, number | null] | null; expectancy_r: number | null;
  expectancy_ci: [number | null, number | null] | null; net_return_pct: number | null; benchmark_return_pct: number | null;
  cagr_pct: number | null; annualised_return_pct: number | null; max_drawdown_pct: number | null; calmar: number | null;
  win_rate_pct: number | null; avg_holding_bars: number | null; stability_score: number | null; deflated_sharpe: number | null;
  score: number | null; pass_reason: string | null;
};
type Check = { name: string; passed: boolean | null; value: unknown; threshold: unknown; detail: string };
type Bucket = { num_trades: number; profit_factor: number | null; expectancy_r: number | null; net_pnl: number };
type Detail = {
  equity_curve: [string, number][]; drawdown_curve: [string, number][]; checks: Check[];
  stability: { passed: boolean; score: number; base_params: Record<string, unknown>; note: string | null;
    heatmap: { param: string; base_value: unknown; cells: { shift: string; value: unknown; profit_factor: number | null; expectancy_r: number | null; num_trades: number }[] }[] };
  regimes: Record<string, Bucket>;
  subperiods: { method: string; working: number; total: number; periods: { label: string; start: string; end: string; num_trades: number; net_pnl: number; profit_factor: number | null; works: boolean }[] };
  bootstrap: { pf_ci: (number | null)[]; expectancy_r_ci: (number | null)[]; p_value: number | null };
  benchmark: { label?: string; return_pct?: number; max_drawdown_pct?: number; curve?: [string, number][] };
  double_costs: { profit_factor: number | null; net_pnl: number; method: string };
  folds: { fold: number; in_sample: string[]; out_of_sample: string[]; params: Record<string, unknown>; oos_trades: number; oos_return_pct: number | null; oos_profit_factor: number | null }[];
  composite: { score: number; components: Record<string, number> } | null;
  exit_reasons: Record<string, number>;
} | undefined;
type Report = {
  run_id: string; generated_at: string; seconds: number; data_label: string; headline: string; cache_key: string;
  data: { daily_symbols: number; intraday_symbols: number; daily_range: string[] | null; intraday_range: string[] | null; benchmark: string; survivorship_note: string };
  top10: Row[]; leaderboard: Row[]; summary: string[];
  not_testable: { no: number; id: string; name: string; family: string; missing_data: string[]; note: string }[];
  context_inputs: { no: number; id: string; name: string; missing_data: string[]; note: string }[];
  per_regime: Record<string, (Bucket & { id: string; name: string; result: string; qualifies: boolean })[]>;
  multiple_testing: { n_trials: number; sr0_annualised: number; tested: number; bh_discoveries: string[]; expected_false_positives: number };
  control: { tested: number; passed: number; symbols: number; label: string } | null;
  details: Record<string, Detail>;
};
type Status = { state: string; progress: number; total: number; current: string[]; message: string | null; run_id: string | null; error: string | null };
type TConfig = { criteria: Record<string, unknown>; score_weights: Record<string, number> };

const CHECK_LABEL: Record<string, string> = {
  point_in_time_universe: "Point-in-time universe", min_oos_trades: "OOS trades", profit_factor: "Profit factor + CI",
  expectancy: "Expectancy + CI", benchmark: "Beats NIFTY TR", max_drawdown: "Max drawdown",
  survivorship_sensitivity: "Survivorship sensitivity", parameter_stability: "±20% stability",
  multiple_testing: "Multiple testing (DSR)", double_costs: "2× costs", subperiods: "Sub-periods", data: "Data",
};
const ci = (c: (number | null)[] | null | undefined) => (c && c[0] != null ? `${num(c[0])}–${num(c[1])}` : "—");

function ResultBadge({ r }: { r: string }) {
  return <span className={clsx("rounded border px-1.5 py-0.5 text-[10px] font-bold",
    r === "PASS" ? "border-emerald-600 text-emerald-300" : r === "FAIL" ? "border-red-800 text-red-300" : "border-slate-600 text-slate-400")}>{r}</span>;
}

function StrategyDetail({ row, d }: { row: Row; d: Detail }) {
  if (!d) return <Empty>No detail</Empty>;
  const eq = [{ name: row.id, color: "#38bdf8", data: d.equity_curve.map(([t, v]) => ({ time: t, value: v })) }];
  if (d.benchmark?.curve) eq.push({ name: "NIFTY 50 TR", color: "#e2e8f0", data: d.benchmark.curve.map(([t, v]) => ({ time: t, value: v })) });
  return (
    <div className="space-y-3 text-xs">
      <div className="flex flex-wrap gap-1.5">
        {d.checks.map((c) => (
          <span key={c.name} title={c.detail} className={clsx("rounded border px-1.5 py-0.5",
            c.passed ? "border-emerald-800 text-emerald-300" : c.passed === false ? "border-red-800 text-red-300" : "border-slate-600 text-slate-400")}>
            {c.passed ? "✓" : c.passed === false ? "✕" : "?"} {CHECK_LABEL[c.name] ?? c.name}
          </span>))}
      </div>
      <ul className="space-y-0.5 text-slate-400">{d.checks.map((c) => <li key={c.name}><span className="text-slate-500">{CHECK_LABEL[c.name] ?? c.name}:</span> {c.detail}</li>)}</ul>
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
        <div><div className="label mb-1">OOS equity (growth of 1) vs {d.benchmark?.label ?? "NIFTY"}</div><LinesChart lines={eq} height={200} /></div>
        <div><div className="label mb-1">Drawdown %</div><LinesChart lines={[{ name: "DD", color: "#f87171", data: d.drawdown_curve.map(([t, v]) => ({ time: t, value: v })) }]} height={200} /></div>
      </div>
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-3">
        <div>
          <div className="label mb-1">Parameter stability (PF at -20% / base / +20%) · score {num(d.stability.score, 0)}</div>
          {d.stability.heatmap.length === 0 ? <div className="text-slate-500">{d.stability.note ?? "—"}</div> : (
            <table className="tbl"><thead><tr><th>Param</th>{d.stability.heatmap[0].cells.map((c) => <th key={c.shift} className="text-right">{c.shift}</th>)}</tr></thead>
              <tbody>{d.stability.heatmap.map((h) => (
                <tr key={h.param}><td>{h.param} <span className="text-slate-500">({String(h.base_value)})</span></td>
                  {h.cells.map((c) => {
                    const pf = c.profit_factor ?? 0;
                    return <td key={c.shift} className="num text-right" title={`value ${String(c.value)} · ${c.num_trades} trades · exp ${num(c.expectancy_r, 3)}R`}
                      style={{ background: pf >= 1.2 ? "rgba(34,197,94,0.35)" : pf >= 1 ? "rgba(234,179,8,0.25)" : "rgba(239,68,68,0.3)" }}>{num(c.profit_factor)}</td>;
                  })}</tr>))}</tbody></table>)}
          <div className="mt-1 text-slate-500">2× costs: PF {num(d.double_costs.profit_factor)} · net ₹{num(d.double_costs.net_pnl, 0)}</div>
        </div>
        <div>
          <div className="label mb-1">Sub-periods ({d.subperiods.working}/{d.subperiods.total} working, {d.subperiods.method})</div>
          <table className="tbl"><tbody>{d.subperiods.periods.map((p) => (
            <tr key={p.label}><td>{p.label}</td><td className="num text-right">{p.num_trades}</td><td className="num text-right">{num(p.profit_factor)}</td>
              <td className={p.works ? "text-up" : "text-down"}>{p.works ? "✓" : "✕"}</td></tr>))}</tbody></table>
        </div>
        <div>
          <div className="label mb-1">By regime (at signal time)</div>
          <table className="tbl"><thead><tr><th>Regime</th><th className="text-right">Trades</th><th className="text-right">PF</th><th className="text-right">Exp R</th></tr></thead>
            <tbody>{Object.entries(d.regimes).map(([k, v]) => (
              <tr key={k}><td>{k}</td><td className="num text-right">{v.num_trades}</td><td className={clsx("num text-right", (v.profit_factor ?? 0) >= 1 ? "text-up" : "text-down")}>{num(v.profit_factor)}</td>
                <td className="num text-right">{num(v.expectancy_r, 3)}</td></tr>))}</tbody></table>
        </div>
      </div>
      <div>
        <div className="label mb-1">Walk-forward folds (anchored, embargoed)</div>
        <table className="tbl"><thead><tr><th>#</th><th>In-sample</th><th>Out-of-sample</th><th>Params chosen</th><th className="text-right">OOS trades</th><th className="text-right">OOS PF</th><th className="text-right">OOS %</th></tr></thead>
          <tbody>{d.folds.map((f) => (
            <tr key={f.fold}><td>{f.fold}</td><td>{f.in_sample.join(" → ")}</td><td>{f.out_of_sample.join(" → ")}</td><td>{JSON.stringify(f.params)}</td>
              <td className="num text-right">{f.oos_trades}</td><td className="num text-right">{num(f.oos_profit_factor)}</td><td className="num text-right">{num(f.oos_return_pct, 1)}</td></tr>))}</tbody></table>
        <div className="mt-1 text-slate-500">Bootstrap: PF CI {ci(d.bootstrap.pf_ci)} · expectancy CI {ci(d.bootstrap.expectancy_r_ci)}R · p(mean R ≤ 0) = {num(d.bootstrap.p_value, 3)} · exits: {Object.entries(d.exit_reasons).map(([k, v]) => `${k} ${v}`).join(", ")}</div>
      </div>
    </div>
  );
}

function CriteriaEditor() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["tconfig"], queryFn: () => api.get<TConfig>("/tournament/config") });
  const [crit, setCrit] = useState<Record<string, string>>({});
  const [w, setW] = useState<Record<string, string>>({});
  useEffect(() => {
    if (!q.data) return;
    setCrit(Object.fromEntries(Object.entries(q.data.criteria).filter(([, v]) => typeof v === "number").map(([k, v]) => [k, String(v)])));
    setW(Object.fromEntries(Object.entries(q.data.score_weights).map(([k, v]) => [k, String(v)])));
  }, [q.data]);
  const save = useMutation({
    mutationFn: () => api.put<TConfig>("/tournament/config", {
      criteria: Object.fromEntries(Object.entries(crit).map(([k, v]) => [k, Number(v)])),
      score_weights: Object.fromEntries(Object.entries(w).map(([k, v]) => [k, Number(v)])),
    }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tconfig"] }),
  });
  if (!q.data) return <Loading />;
  const field = (obj: Record<string, string>, set: (x: Record<string, string>) => void, k: string) => (
    <label key={k} className="flex flex-col gap-0.5"><span className="label">{k.replace(/_/g, " ")}</span>
      <input className="input w-28" value={obj[k] ?? ""} onChange={(e) => set({ ...obj, [k]: e.target.value })} /></label>);
  return (
    <div className="space-y-2 text-xs">
      <div className="label">Pass criteria (a strategy must meet ALL)</div>
      <div className="flex flex-wrap gap-2">{Object.keys(crit).map((k) => field(crit, setCrit, k))}</div>
      <div className="label pt-1">Composite score weights</div>
      <div className="flex flex-wrap gap-2">{Object.keys(w).map((k) => field(w, setW, k))}</div>
      <div className="flex items-center gap-2"><button className="btn" onClick={() => save.mutate()} disabled={save.isPending}>Save</button>
        <span className="text-amber-300/80">Changing criteria changes the config hash: re-run the tournament. Do not loosen thresholds until something passes.</span></div>
      {save.error && <ErrorBox error={save.error} />}
    </div>
  );
}


type MlStats = { n: number; win_rate_pct: number | null; expectancy_r: number | null; profit_factor_r: number | null };
type MlReport = {
  trained_at: string; setups: number; data_range: string[]; oos_auc: number | null; oos_brier: number; base_rate_brier: number;
  all: MlStats; selected: MlStats; p_no_lift: number; useful: boolean; verdict: string; rule: string;
  folds: { test_year: number; train_setups: number; test_setups: number; auc: number | null; all: MlStats; selected: MlStats }[];
  feature_importance: { feature: string; importance: number }[] | null;
};
type PolicySummary = { policy: string; min_tier: string; trend: string; info: Record<string, string>;
  strategies: { strategy: string; name: string; tier: string; accepted: boolean; oos_trades: number; profit_factor: number | null;
    regime_trades: number; regime_pf: number | null; reason: string }[] };

function MlPanel() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["ml"], queryFn: () => api.get<{ report: MlReport | null; status: { state: string; message: string | null; error: string | null } }>("/ml/report"),
    refetchInterval: (x) => (x.state.data?.status.state === "running" ? 3000 : 60_000) });
  const train = useMutation({ mutationFn: () => api.post("/ml/train", {}), onSuccess: () => qc.invalidateQueries({ queryKey: ["ml"] }) });
  const r = q.data?.report;
  const st = q.data?.status;
  const sv = (x: MlStats) => `${x.n} setups · win ${num(x.win_rate_pct, 1)}% · exp ${num(x.expectancy_r, 3)}R · PF ${num(x.profit_factor_r)}`;
  return (
    <Panel title="ML meta-labelling model (Lopez de Prado) — walk-forward validation" right={
      <button className="btn" disabled={st?.state === "running" || train.isPending} onClick={() => train.mutate()}>{st?.state === "running" ? `Training… ${st.message ?? ""}` : "Retrain"}</button>}>
      {!r ? <div className="text-xs text-slate-500">No model trained yet. Training collects every historical setup of the daily strategies (~2–4 min).</div> : (
        <div className="space-y-2 text-xs">
          <div className={clsx("rounded border p-2 font-semibold", r.useful ? "border-emerald-700 text-emerald-200" : "border-amber-700 text-amber-200")}>{r.verdict}</div>
          <div className="text-slate-400">Gradient-boosted classifier predicts whether each strategy setup ends in profit from causal features
            (trend, momentum, volatility, volume, relative strength, stop distance, NIFTY regime, India VIX). Trained only on setups closed before each test year.
            {" "}{r.setups} setups {r.data_range.join(" → ")} · trained {r.trained_at}.</div>
          <div className="grid grid-cols-2 xl:grid-cols-4 gap-2">
            <div><div className="label">OOS AUC (0.5 = random)</div><div className="num text-slate-100">{num(r.oos_auc, 3)}</div></div>
            <div><div className="label">Brier vs base rate</div><div className="num text-slate-100">{num(r.oos_brier, 3)} vs {num(r.base_rate_brier, 3)}</div></div>
            <div><div className="label">All OOS setups</div><div className="num text-slate-100">{sv(r.all)}</div></div>
            <div><div className="label">Model-selected setups</div><div className={clsx("num", (r.selected.expectancy_r ?? 0) > (r.all.expectancy_r ?? 0) ? "text-up" : "text-down")}>{sv(r.selected)}</div></div>
          </div>
          <table className="tbl"><thead><tr><th>Test year</th><th className="text-right">Train</th><th className="text-right">Test</th><th className="text-right">AUC</th><th>All setups</th><th>Selected by model</th></tr></thead>
            <tbody>{r.folds.map((f) => <tr key={f.test_year}><td>{f.test_year}</td><td className="num text-right">{f.train_setups}</td><td className="num text-right">{f.test_setups}</td>
              <td className={clsx("num text-right", (f.auc ?? 0.5) > 0.55 ? "text-up" : (f.auc ?? 0.5) < 0.5 ? "text-down" : "")}>{num(f.auc, 3)}</td><td className="num">{sv(f.all)}</td><td className="num">{sv(f.selected)}</td></tr>)}</tbody></table>
          <div className="text-[10px] text-slate-500">Decision rule: {r.rule}. Bootstrap p(no lift) = {num(r.p_no_lift, 3)}. Retrained weekly; switches on automatically only if a future validation shows lift.</div>
        </div>)}
      {st?.state === "error" && <div className="text-xs text-red-300">{st.error}</div>}
    </Panel>
  );
}

const TIER_CLS: Record<string, string> = { VALIDATED: "text-emerald-300", PROVISIONAL: "text-sky-300", SPECULATIVE: "text-amber-300", NONE: "text-slate-500" };

function PolicyTiers() {
  const q = useQuery({ queryKey: ["policy"], queryFn: () => api.get<PolicySummary>("/policy"), refetchInterval: 60_000 });
  const d = q.data;
  if (!d) return null;
  return (
    <Panel title={`Evidence tiers in the current ${d.trend.toUpperCase()} regime — policy '${d.policy}' accepts ${d.min_tier}+`} bodyClass="p-0 overflow-auto max-h-[50vh]">
      <table className="tbl"><thead><tr><th>Strategy</th><th>Tier</th><th>Used now</th><th className="text-right">OOS trades</th><th className="text-right">OOS PF</th><th className="text-right">Regime trades</th><th className="text-right">Regime PF</th><th>Why</th></tr></thead>
        <tbody>{d.strategies.map((x) => <tr key={x.strategy}><td>{x.name}</td><td className={clsx("font-semibold", TIER_CLS[x.tier])}>{x.tier}</td>
          <td className={x.accepted ? "text-up" : "text-slate-500"}>{x.accepted ? "yes" : "no"}</td><td className="num text-right">{x.oos_trades}</td>
          <td className="num text-right">{num(x.profit_factor)}</td><td className="num text-right">{x.regime_trades}</td><td className="num text-right">{num(x.regime_pf)}</td>
          <td className="whitespace-normal text-slate-400">{x.reason}</td></tr>)}</tbody></table>
      <div className="p-2 text-[10px] text-slate-500">{Object.entries(d.info).map(([k, v]) => <div key={k}><b>{k}</b>: {v}</div>)}</div>
    </Panel>
  );
}

export function TournamentPanel() {
  const qc = useQueryClient();
  const [force, setForce] = useState(false);
  const [sel, setSel] = useState<string | null>(null);
  const [showCfg, setShowCfg] = useState(false);
  const st = useQuery({ queryKey: ["tstatus"], queryFn: () => api.get<Status>("/tournament/status"),
    refetchInterval: (q) => (q.state.data?.state === "running" ? 2000 : 15000) });
  const rep = useQuery({ queryKey: ["treport"], queryFn: () => api.get<Report>("/tournament/report"), retry: false, refetchInterval: 60_000 });
  const run = useMutation({ mutationFn: () => api.post<Status>("/tournament/run", { force }), onSuccess: () => qc.invalidateQueries({ queryKey: ["tstatus"] }) });
  const running = st.data?.state === "running";
  useEffect(() => { if (st.data?.state === "done") qc.invalidateQueries({ queryKey: ["treport"] }); }, [st.data?.state, st.data?.run_id, qc]);
  const r = rep.data;
  const selRow = r?.leaderboard.find((x) => x.id === sel);

  return (
    <div className="space-y-3">
      <Panel title="Strategy tournament — honest out-of-sample validation" right={<span className="flex items-center gap-2">
        {r && <DataLabel label={r.data_label} />}
        {r && <a className="text-sky-300 hover:underline" href={`/api/tournament/report.csv?run_id=${r.run_id}`}>CSV</a>}
        {r && <a className="text-sky-300 hover:underline" href={`/api/tournament/report.html?run_id=${r.run_id}`} target="_blank" rel="noreferrer">HTML report</a>}
        <button className="text-slate-400 hover:text-slate-200" onClick={() => setShowCfg((v) => !v)}>{showCfg ? "Hide criteria" : "Edit criteria & weights"}</button>
      </span>}>
        <div className="flex flex-wrap items-center gap-3 text-xs">
          <button className="btn btn-primary" disabled={running || run.isPending} onClick={() => run.mutate()}>{running ? "Running…" : "Run tournament"}</button>
          <label className="flex items-center gap-1 text-slate-400"><input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} /> ignore cache</label>
          {running && st.data && (
            <div className="flex min-w-[260px] flex-1 items-center gap-2">
              <div className="h-2 flex-1 overflow-hidden rounded bg-ink-700"><div className="h-full bg-sky-500" style={{ width: `${(st.data.progress / Math.max(st.data.total, 1)) * 100}%` }} /></div>
              <span className="num text-slate-300">{st.data.progress}/{st.data.total}</span>
              <span className="text-slate-500 truncate">{st.data.message} {st.data.current.slice(0, 4).join(", ")}</span>
            </div>)}
          {st.data?.state === "error" && <span className="text-red-300">{st.data.error}</span>}
          {r && !running && <span className="text-slate-500">Last run {r.generated_at} · {r.seconds}s · daily {r.data.daily_symbols} symbols {r.data.daily_range?.join(" → ")} · 5-min {r.data.intraday_symbols} symbols {r.data.intraday_range?.join(" → ")} · benchmark {r.data.benchmark}</span>}
        </div>
        {showCfg && <div className="mt-3 border-t border-ink-700 pt-3"><CriteriaEditor /></div>}
        {rep.isLoading && <Loading />}
        {rep.error && !r && <div className="mt-2 text-xs text-slate-500">No tournament report yet. Run the tournament (it runs in background worker processes; about 20–40 minutes for the full library).</div>}
      </Panel>

      {r && (
        <>
          <Panel title={`Top 10 passed strategies — ${r.headline}`}>
            {r.top10.length === 0 ? (
              <div className="rounded border border-amber-700 bg-amber-950/30 p-3 text-sm text-amber-200">
                No strategy passed every criterion on this data. Nothing is padded into the Top 10 — the decision engine will show NO TRADE until a strategy earns a validated edge.
              </div>
            ) : (
              <table className="tbl"><thead><tr><th>#</th><th>Strategy</th><th>Family</th><th>Mode</th><th>Dir</th><th>TF</th><th>Best regime</th><th className="text-right">Trades</th>
                <th className="text-right">PF (CI)</th><th className="text-right">Exp R</th><th className="text-right">Net vs NIFTY</th><th className="text-right">CAGR</th><th className="text-right">Max DD</th>
                <th className="text-right">Calmar</th><th className="text-right">Win %</th><th className="text-right">Hold</th><th className="text-right">Stability</th><th className="text-right">Score</th><th>Why it passed</th></tr></thead>
                <tbody>{r.top10.map((x) => (
                  <tr key={x.id} className="cursor-pointer" onClick={() => setSel(x.id)}>
                    <td>{x.rank}</td><td className="font-semibold">{x.name}</td><td>{x.family}</td><td>{x.mode}</td><td>{x.direction}</td><td>{x.timeframe}</td><td>{x.best_regime ?? "—"}</td>
                    <td className="num text-right">{x.oos_trades}</td><td className="num text-right">{num(x.profit_factor)} <span className="text-slate-500">({ci(x.pf_ci)})</span></td>
                    <td className="num text-right">{num(x.expectancy_r, 3)}</td><td className="num text-right">{num(x.net_return_pct, 1)}% / {num(x.benchmark_return_pct, 1)}%</td>
                    <td className="num text-right">{num(x.cagr_pct, 1)}</td><td className="num text-right">{num(x.max_drawdown_pct, 1)}</td><td className="num text-right">{num(x.calmar)}</td>
                    <td className="num text-right">{num(x.win_rate_pct, 1)}</td><td className="num text-right">{num(x.avg_holding_bars, 1)}</td><td className="num text-right">{num(x.stability_score, 0)}</td>
                    <td className="num text-right font-semibold">{num(x.score, 1)}</td><td className="whitespace-normal text-slate-400">{x.pass_reason}</td></tr>))}</tbody></table>
            )}
          </Panel>

          <Panel title="What the evidence says">
            <ul className="space-y-1 text-xs text-slate-300">{r.summary.map((s, i) => <li key={i}>• {s}</li>)}</ul>
            <div className="mt-2 text-[11px] text-amber-300/80">{r.data.survivorship_note} Research and paper trading only — not investment advice.</div>
          </Panel>

          <div className={clsx("grid gap-3", selRow ? "grid-cols-1 2xl:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]" : "grid-cols-1")}>
            <Panel title={`Full leaderboard (${r.leaderboard.length} tested)`} right={<span className="text-slate-500">click a row for the evidence</span>} bodyClass="p-0 overflow-auto max-h-[70vh]">
              <table className="tbl"><thead><tr><th>#</th><th>Strategy</th><th>Family</th><th>TF</th><th>Result</th><th>First failed</th><th className="text-right">Trades</th><th className="text-right">PF</th>
                <th className="text-right">Exp R</th><th className="text-right">Net %</th><th className="text-right">NIFTY %</th><th className="text-right">Max DD</th><th className="text-right">DSR</th><th className="text-right">Score</th></tr></thead>
                <tbody>{r.leaderboard.map((x) => (
                  <tr key={x.id} onClick={() => setSel(x.id)} className={clsx("cursor-pointer", sel === x.id && "bg-ink-800")}>
                    <td>{x.no}</td><td>{x.name}</td><td className="text-slate-400">{x.family}</td><td>{x.timeframe}</td><td><ResultBadge r={x.result} /></td>
                    <td className="text-slate-400" title={x.first_failed_detail ?? ""}>{x.first_failed ? CHECK_LABEL[x.first_failed] ?? x.first_failed : "—"}</td>
                    <td className="num text-right">{x.oos_trades ?? "—"}</td><td className={clsx("num text-right", (x.profit_factor ?? 0) >= 1 ? "text-up" : "text-down")}>{num(x.profit_factor)}</td>
                    <td className="num text-right">{num(x.expectancy_r, 3)}</td><td className="num text-right">{num(x.net_return_pct, 1)}</td><td className="num text-right">{num(x.benchmark_return_pct, 1)}</td>
                    <td className="num text-right">{num(x.max_drawdown_pct, 1)}</td><td className="num text-right">{num(x.deflated_sharpe, 2)}</td><td className="num text-right">{num(x.score, 1)}</td></tr>))}</tbody></table>
            </Panel>
            {selRow && (
              <Panel title={`${selRow.name} — ${selRow.result}`} right={<button className="text-slate-400" onClick={() => setSel(null)}>✕</button>} bodyClass="overflow-auto max-h-[70vh]">
                <StrategyDetail row={selRow} d={r.details[selRow.id]} />
              </Panel>)}
          </div>

          <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
            <Panel title="Results per regime (passed strategies with PF ≥ threshold and ≥ 20 trades)">
              <table className="tbl"><thead><tr><th>Regime</th><th>Qualifying passed strategies</th><th className="text-right">Best PF (any, ≥20 trades)</th></tr></thead>
                <tbody>{Object.entries(r.per_regime).sort().map(([k, items]) => {
                  const best = Math.max(0, ...items.filter((x) => x.num_trades >= 20).map((x) => x.profit_factor ?? 0));
                  const q = items.filter((x) => x.qualifies).map((x) => x.name);
                  return <tr key={k}><td>{k}</td><td className={q.length ? "text-up" : "text-slate-500"}>{q.join(", ") || "none"}</td><td className="num text-right">{num(best)}</td></tr>;
                })}</tbody></table>
            </Panel>
            <Panel title="Multiple testing & control">
              <div className="space-y-1 text-xs text-slate-300">
                <div>{r.multiple_testing.tested} strategies / {r.multiple_testing.n_trials} configurations tested. Expected false positives at 5%: <span className="num">{r.multiple_testing.expected_false_positives}</span>.</div>
                <div>Deflated-Sharpe luck threshold: annualised Sharpe <span className="num">{num(r.multiple_testing.sr0_annualised)}</span>. BH-FDR discoveries: {r.multiple_testing.bh_discoveries.join(", ") || "none"}.</div>
                {r.control && <div className={r.control.passed === 0 ? "text-up" : "text-red-300"}>Random-walk control: {r.control.passed} of {r.control.tested} strategies passed on {r.control.symbols} zero-drift random walks {r.control.passed === 0 ? "(validation discriminates)" : "(WARNING: validation too lenient)"}.</div>}
              </div>
            </Panel>
          </div>

          <PolicyTiers />
          <MlPanel />

          <Panel title={`NOT TESTABLE (${r.not_testable.length}) and context inputs (${r.context_inputs.length})`}>
            <table className="tbl"><thead><tr><th>#</th><th>Strategy / input</th><th>Missing data</th><th>Note</th></tr></thead>
              <tbody>
                {r.not_testable.map((x) => <tr key={x.id}><td>{x.no}</td><td>{x.name} <Pill>NOT TESTABLE</Pill></td><td className="whitespace-normal text-amber-300/90">{x.missing_data.join("; ")}</td><td className="whitespace-normal text-slate-500">{x.note}</td></tr>)}
                {r.context_inputs.map((x) => <tr key={x.id}><td>{x.no}</td><td>{x.name} <Pill>CONTEXT</Pill></td><td className="whitespace-normal text-amber-300/90">{x.missing_data.join("; ") || "—"}</td><td className="whitespace-normal text-slate-500">{x.note}</td></tr>)}
              </tbody></table>
          </Panel>
        </>
      )}
    </div>
  );
}
