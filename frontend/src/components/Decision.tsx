import clsx from "clsx";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { inr, num } from "../lib/format";
import { DataLabel, ErrorBox, Loading, Panel, Pill } from "./ui";
import { LivePrice, StaleBadge, TickAge } from "./Live";

type Gate = { name: string; passed: boolean | null; detail: string };

const TIER_STYLE: Record<string, string> = {
  VALIDATED: "border-emerald-600 text-emerald-300", PROVISIONAL: "border-sky-600 text-sky-300",
  SPECULATIVE: "border-amber-600 text-amber-300", NONE: "border-slate-700 text-slate-500",
};

export function TierBadge({ tier }: { tier?: string | null }) {
  if (!tier) return <span className="text-slate-600">—</span>;
  return <span title="Evidence tier from the strategy tournament (out-of-sample, after costs, in the current regime)"
    className={clsx("inline-block rounded border px-1 py-0.5 text-[9px] font-bold", TIER_STYLE[tier] ?? TIER_STYLE.NONE)}>{tier}</span>;
}

const POLICY_LABEL: Record<string, string> = { strict: "Strict", balanced: "Balanced", aggressive: "Aggressive" };

export function PolicySelect() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["risk-config"], queryFn: () => api.get<{ config: { evidence_policy?: string } }>("/risk/config") });
  const m = useMutation({
    mutationFn: (policy: string) => api.put("/policy", { policy }),
    onSuccess: () => { ["risk-config", "scanner", "decision", "policy"].forEach((k) => qc.invalidateQueries({ queryKey: [k] })); },
  });
  const cur = q.data?.config.evidence_policy ?? "balanced";
  return (
    <label className="flex items-center gap-1 text-xs" title="How much out-of-sample evidence a strategy needs before its setups become BUY">
      <span className="text-slate-400">Evidence policy</span>
      <select className="input py-0.5" value={cur} disabled={m.isPending} onChange={(e) => m.mutate(e.target.value)}>
        {Object.entries(POLICY_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
      </select>
      {m.isPending && <span className="text-slate-500">re-scanning…</span>}
    </label>
  );
}
type Pick = {
  symbol: string; exchange: string; name: string; sector: string; strategies: string[]; families: string[]; mode: string;
  direction: string; timeframe: string; entry_trigger: number; entry_note: string; stop: number; targets: number[];
  target_info: { method: string } | null; risk_reward: number | null; qty: number; capital_used: number; max_loss: number;
  holding_period: string; invalidation: string; score: number; score_breakdown: Record<string, number>; final_score: number;
  regime_multiplier: number; evidence: { strategy: string; profit_factor: number | null; pf_ci: (number | null)[] | null; expectancy_ci: (number | null)[] | null; oos_trades: number | null; regime_pf: number | null }[];
  confidence: { expectancy_r_range: (number | null)[] | null; win_rate_range_pct: number[] | null; note: string };
  signal_id?: number; state: string; reference_price: number; what_to_check: string; tier: string;
  ml?: { p_win: number; model_useful: boolean } | null;
};
type Decision = {
  generated_at: string; data_label: string; market_open: boolean; decision: string; message: string;
  regime: { trend: string; volatility: string; label: string; exposure_budget_pct: number; as_of: string | null; budget_note?: string;
    inputs: { name: string; value: number | null; detail?: string | null; status: string }[] };
  gates: Gate[]; picks: Pick[]; skipped: { symbol: string; final: number; reasons: string[] }[];
  watch_only: { symbol: string; exchange?: string; strategy: string; strategy_name?: string; state: string; score: number | null; timeframe?: string; signal_id?: number; reference_price?: number; what_must_change: string[] }[];
  eligible_strategies: { id: string; name: string; family: string; regime_pf: number | null; regime_trades: number | null; tier: string }[];
  rejected_strategies: { id: string; name: string; reason: string; tier?: string }[];
  policy?: { name: string; min_tier: string; info: Record<string, string> };
  decay_monitor: { id: string; name: string; paper_trades: number; paper_expectancy_r: number | null; backtest_expectancy_r: number | null; expected_band: (number | null)[]; status: string }[];
  weights: Record<string, number>; tournament_run: string | null; disclaimer: string;
};

const range = (r: (number | null)[] | null | undefined, unit = "") => (r && r[0] != null ? `${num(r[0])}${unit} to ${num(r[1])}${unit}` : "—");

function WeightsEditor({ weights }: { weights: Record<string, number> }) {
  const qc = useQueryClient();
  const [w, setW] = useState<Record<string, string>>({});
  useEffect(() => setW(Object.fromEntries(Object.entries(weights).map(([k, v]) => [k, String(v)]))), [weights]);
  const m = useMutation({
    mutationFn: () => api.put("/decision/weights", { weights: Object.fromEntries(Object.entries(w).map(([k, v]) => [k, Number(v)])) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["decision"] }),
  });
  return (
    <div className="flex flex-wrap items-end gap-2 text-xs">
      {Object.keys(w).map((k) => (
        <label key={k} className="flex flex-col gap-0.5"><span className="label">{k.replace(/_/g, " ")}</span>
          <input className="input w-20" value={w[k]} onChange={(e) => setW({ ...w, [k]: e.target.value })} /></label>))}
      <button className="btn" onClick={() => m.mutate()} disabled={m.isPending}>Save weights</button>
      {m.error && <ErrorBox error={m.error} />}
    </div>
  );
}

export function DecisionPanel() {
  const [more, setMore] = useState(false);
  const q = useQuery({ queryKey: ["decision"], queryFn: () => api.get<Decision>("/decision"), refetchInterval: 20_000 });
  if (q.isLoading) return <Panel title="Best stocks to buy (decision engine)"><Loading what="Evaluating validated strategies" /></Panel>;
  if (q.error) return <ErrorBox error={q.error} />;
  const d = q.data!;
  const noTrade = d.decision !== "BUY";
  return (
    <Panel title="Best stocks to buy — decision engine" right={<span className="flex items-center gap-2">
      <PolicySelect /><DataLabel label={d.data_label} /><span className="text-slate-500">tournament run {d.tournament_run ?? "none"} · {d.generated_at.slice(11, 19)} IST</span></span>}>
      <div className={clsx("rounded border p-3 text-base font-semibold", noTrade ? "border-amber-700 bg-amber-950/30 text-amber-200" : "border-emerald-700 bg-emerald-950/30 text-emerald-200")}>
        {d.message}
      </div>
      <div className="mt-3 grid grid-cols-1 xl:grid-cols-3 gap-3 text-xs">
        <div className="rounded border border-ink-700 p-2">
          <div className="label">Regime</div>
          <div className="text-sm font-semibold text-slate-100">{d.regime.label}</div>
          <div className="text-slate-400">Exposure budget: <span className="num text-slate-100">{d.regime.exposure_budget_pct}%</span> of normal risk per trade</div>
          {d.regime.budget_note && <div className="text-[10px] text-sky-300">{d.regime.budget_note}</div>}
          {d.policy && <div className="mt-1 text-[10px] text-slate-500">Policy <b>{d.policy.name}</b> (needs {d.policy.min_tier}+): {d.policy.info[d.policy.name]}</div>}
          <table className="tbl mt-1"><tbody>{d.regime.inputs.map((i) => (
            <tr key={i.name}><td>{i.name}</td><td className="num text-right">{i.value == null ? "—" : num(i.value)}</td>
              <td className={i.status === "ok" ? "text-slate-500" : "text-amber-300"}>{i.status === "ok" ? (i.detail ?? "") : i.status}</td></tr>))}</tbody></table>
        </div>
        <div className="rounded border border-ink-700 p-2">
          <div className="label">Gates</div>
          <ul className="space-y-1">{d.gates.map((g) => (
            <li key={g.name}><span className={g.passed ? "text-up" : g.passed === false ? "text-down" : "text-amber-300"}>{g.passed ? "✓" : g.passed === false ? "✕" : "?"}</span> <span className="text-slate-300">{g.name}:</span> <span className="text-slate-400">{g.detail}</span></li>))}</ul>
        </div>
        <div className="rounded border border-ink-700 p-2">
          <div className="label">Strategies allowed this cycle ({d.eligible_strategies.length})</div>
          {d.eligible_strategies.length === 0 ? <div className="text-slate-400">None: no strategy passed the tournament and suits the current regime.</div> :
            <ul>{d.eligible_strategies.map((e) => <li key={e.id}><TierBadge tier={e.tier} /> {e.name} <span className="text-slate-500">(PF {num(e.regime_pf)} over {e.regime_trades} trades in this regime)</span></li>)}</ul>}
          <button className="mt-1 text-sky-300 hover:underline" onClick={() => setMore((v) => !v)}>{more ? "Hide" : "Show"} rejected strategies, decay monitor and weights</button>
        </div>
      </div>

      {d.picks.length > 0 && (
        <div className="mt-3 overflow-auto">
          <table className="tbl">
            <thead><tr><th>Symbol</th><th>Evidence</th><th className="text-right">Live</th><th>Strategies (families)</th><th>Mode</th><th className="text-right">Entry trigger</th><th className="text-right">Stop</th>
              <th className="text-right">Targets</th><th className="text-right">R:R</th><th className="text-right">Qty</th><th className="text-right">Capital</th><th className="text-right">Max loss</th>
              <th>Hold</th><th className="text-right">Score → final</th><th>OOS evidence</th><th>Confidence (95%)</th><th>Invalidation</th></tr></thead>
            <tbody>{d.picks.map((p) => (
              <tr key={p.symbol}>
                <td><Link className="font-semibold text-sky-300 hover:underline" to={`/charts/${p.exchange}/${encodeURIComponent(p.symbol)}?tf=${p.timeframe}${p.signal_id ? `&signal=${p.signal_id}` : ""}`}>{p.symbol}</Link>
                  <div className="text-[10px] text-slate-500">{p.sector}</div></td>
                <td><TierBadge tier={p.tier} />{p.ml && <div className="text-[10px] text-slate-500" title={p.ml.model_useful ? "used" : "model showed no out-of-sample lift - info only"}>ML P(win) {Math.round(p.ml.p_win * 100)}%</div>}</td>
                <td className="text-right whitespace-nowrap"><LivePrice exchange={p.exchange} symbol={p.symbol} fallback={p.reference_price} />
                  <StaleBadge exchange={p.exchange} symbol={p.symbol} marketOpen={d.market_open} /><div className="text-[10px]"><TickAge exchange={p.exchange} symbol={p.symbol} /></div></td>
                <td className="whitespace-normal">{p.strategies.join(", ")}<div className="text-[10px] text-slate-500">{p.families.join(" + ")}</div></td>
                <td>{p.mode}</td><td className="num text-right">{inr(p.entry_trigger)}</td><td className="num text-right text-down">{inr(p.stop)}</td>
                <td className="num text-right text-up" title={p.target_info?.method}>{p.targets.map((t) => inr(t)).join(", ") || "trail"}</td>
                <td className="num text-right">{num(p.risk_reward, 1)}</td><td className="num text-right">{p.qty}</td><td className="num text-right">₹{inr(p.capital_used, 0)}</td>
                <td className="num text-right text-down">₹{inr(p.max_loss, 0)}</td><td className="whitespace-normal text-slate-400">{p.holding_period}</td>
                <td className="num text-right" title={Object.entries(p.score_breakdown).map(([k, v]) => `${k}: ${v}`).join("\n")}>{num(p.score, 1)} → <b>{num(p.final_score, 1)}</b></td>
                <td className="whitespace-normal">{p.evidence.map((e) => <div key={e.strategy}>{e.strategy}: PF {num(e.profit_factor)} ({range(e.pf_ci)}), {e.oos_trades} trades</div>)}</td>
                <td className="whitespace-normal">exp. {range(p.confidence.expectancy_r_range, "R")}<br />win {range(p.confidence.win_rate_range_pct, "%")}</td>
                <td className="whitespace-normal text-slate-400">{p.invalidation}<div className="text-amber-300/80">{p.what_to_check}</div></td>
              </tr>))}</tbody>
          </table>
          <div className="mt-1 text-[10px] text-slate-500">Paper trading only. Confidence ranges are 95% bootstrap/Wilson intervals from out-of-sample trades, not guarantees.</div>
        </div>
      )}

      {noTrade && d.watch_only.length > 0 && (
        <div className="mt-3">
          <div className="label mb-1">Watch only — nearest candidates and what must change</div>
          <table className="tbl"><thead><tr><th>Symbol</th><th>Strategy</th><th>State</th><th className="text-right">Score</th><th>What must change to qualify</th></tr></thead>
            <tbody>{d.watch_only.map((w) => (
              <tr key={`${w.symbol}:${w.strategy}`}>
                <td>{w.exchange ? <Link className="text-sky-300 hover:underline" to={`/charts/${w.exchange}/${encodeURIComponent(w.symbol)}?tf=${w.timeframe ?? "1d"}&strategy=${w.strategy}${w.signal_id ? `&signal=${w.signal_id}` : ""}`}>{w.symbol}</Link> : w.symbol}</td>
                <td>{w.strategy_name ?? w.strategy}</td><td>{w.state}</td><td className="num text-right">{num(w.score, 0)}</td>
                <td className="whitespace-normal text-slate-400">{w.what_must_change.join(" · ")}</td></tr>))}</tbody></table>
        </div>
      )}

      {more && (
        <div className="mt-3 grid grid-cols-1 xl:grid-cols-2 gap-3 text-xs">
          <div><div className="label mb-1">Rejected strategies</div>
            <ul className="max-h-48 overflow-auto space-y-0.5 text-slate-400">{d.rejected_strategies.map((r) => <li key={r.id}><TierBadge tier={r.tier} /> <span className="text-slate-300">{r.name}</span> — {r.reason}</li>)}</ul></div>
          <div><div className="label mb-1">Decay monitor (paper results vs backtest band)</div>
            {d.decay_monitor.length === 0 ? <div className="text-slate-500">No passed strategies to monitor.</div> : (
              <table className="tbl"><thead><tr><th>Strategy</th><th className="text-right">Paper trades</th><th className="text-right">Paper R</th><th>Expected band</th><th>Status</th></tr></thead>
                <tbody>{d.decay_monitor.map((x) => <tr key={x.id}><td>{x.name}</td><td className="num text-right">{x.paper_trades}</td><td className="num text-right">{num(x.paper_expectancy_r, 3)}</td>
                  <td>{range(x.expected_band, "R")}</td><td className={x.status.startsWith("BELOW") ? "text-down" : "text-slate-300"}>{x.status}</td></tr>)}</tbody></table>)}
          </div>
          <div className="xl:col-span-2"><div className="label mb-1">Symbol score weights</div><WeightsEditor weights={d.weights} /></div>
        </div>
      )}
      <div className="mt-2 text-[10px] text-slate-500"><Pill>Paper only</Pill> {d.disclaimer}</div>
    </Panel>
  );
}
