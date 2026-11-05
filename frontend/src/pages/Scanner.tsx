import clsx from "clsx";
import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, qs, type Recommendation, type ScanResponse } from "../lib/api";
import { inr, istTime, num, titleCase } from "../lib/format";
import { ActionBadge, DataLabel, Empty, ErrorBox, Loading, Panel, ScoreBar, StateBadge } from "../components/ui";
import { PaperTradeBox, RecommendationDetail } from "../components/Recommendation";
import { LivePrice, StaleBadge } from "../components/Live";
import { DecisionPanel, PolicySelect, TierBadge } from "../components/Decision";

type Tab = "buy" | "watch" | "all";

interface Filters { mode: string; exchange: string; strategy: string; state: string; action: string; risk: string; liquidity: string; min_score: string }
const EMPTY: Filters = { mode: "", exchange: "", strategy: "", state: "", action: "", risk: "", liquidity: "", min_score: "" };

function Select({ label, value, options, onChange }: { label: string; value: string; options: string[]; onChange: (v: string) => void }) {
  return (
    <label className="flex flex-col gap-0.5">
      <span className="label">{label}</span>
      <select className="input" value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">All</option>
        {options.map((o) => <option key={o} value={o}>{titleCase(o)}</option>)}
      </select>
    </label>
  );
}

export default function Scanner() {
  const [f, setF] = useState<Filters>(EMPTY);
  const [selected, setSelected] = useState<Recommendation | null>(null);
  const [tab, setTab] = useState<Tab>("buy");
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["scanner", f],
    queryFn: () => api.get<ScanResponse>(`/scanner${qs({ ...f })}`),
    refetchInterval: 20_000,
  });
  const refresh = useMutation({
    mutationFn: () => api.post("/scanner/refresh"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scanner"] }),
  });
  const set = (k: keyof Filters) => (v: string) => setF((p) => ({ ...p, [k]: v }));
  const d = q.data;
  const availableModes = d ? d.filters.modes.filter((m) => !(m in d.filters.unavailable_modes)) : [];
  const all = d?.recommendations ?? [];
  const buyNow = all.filter((r) => !r.blocked);
  const watchOnly = all.filter((r) => r.blocked);
  const rows = tab === "buy" ? buyNow : tab === "watch" ? watchOnly : all;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold">AI Trading Scanner</h1>
        {d && <DataLabel label={d.data_label} />}
        {d && <span className="text-xs text-slate-500">{d.total} candidates · generated {istTime(d.generated_at)} IST · {Object.entries(d.counts).map(([k, v]) => `${k} ${v}`).join(" · ")}</span>}
        <span className="ml-auto"><PolicySelect /></span>
        <button className="btn" onClick={() => refresh.mutate()} disabled={refresh.isPending}>{refresh.isPending ? "Scanning…" : "Re-scan now"}</button>
      </div>

      <DecisionPanel />

      {d && (
        <Panel bodyClass="p-2">
          <div className="flex flex-wrap items-end gap-3">
            <Select label="Mode" value={f.mode} options={availableModes} onChange={set("mode")} />
            <Select label="Exchange" value={f.exchange} options={d.filters.exchanges} onChange={set("exchange")} />
            <Select label="Strategy" value={f.strategy} options={d.filters.strategies} onChange={set("strategy")} />
            <Select label="Signal state" value={f.state} options={d.filters.states} onChange={set("state")} />
            <Select label="Action" value={f.action} options={d.filters.actions} onChange={set("action")} />
            <Select label="Risk level" value={f.risk} options={d.filters.risk_levels} onChange={set("risk")} />
            <Select label="Liquidity" value={f.liquidity} options={d.filters.liquidity} onChange={set("liquidity")} />
            <label className="flex flex-col gap-0.5"><span className="label">Min score</span>
              <input className="input w-20" type="number" min={0} max={100} value={f.min_score} onChange={(e) => set("min_score")(e.target.value)} /></label>
            <button className="btn" onClick={() => setF(EMPTY)}>Clear</button>
            <div className="text-[10px] text-slate-500 max-w-md">
              Not available: {Object.entries(d.filters.unavailable_modes).map(([m, why]) => `${m} (${why})`).join("; ")}.
            </div>
          </div>
        </Panel>
      )}

      {d?.playbook && (
        <Panel title="Market playbook (evidence-based)" right={<DataLabel label={d.playbook.data_label} />}>
          <div className={clsx("text-sm font-semibold", d.playbook.benchmark_above_200dma === false ? "text-amber-300" : "text-slate-100")}>{d.playbook.headline}</div>
          <table className="tbl mt-2">
            <thead><tr><th>Strategy</th><th className="text-right">OOS trades</th><th className="text-right">OOS PF</th><th className="text-right">OOS net</th><th className="text-right">NIFTY</th>
              <th className="text-right">PF NIFTY&gt;200DMA</th><th className="text-right">PF NIFTY&lt;200DMA</th><th>Regime gate</th><th>Edge gate</th></tr></thead>
            <tbody>{d.playbook.strategies.map((s) => (
              <tr key={s.strategy} title={s.edge_detail}>
                <td>{s.name} <span className="text-[10px] text-slate-500">{s.timeframe}</span></td>
                <td className="num text-right">{s.oos_trades ?? "—"}</td>
                <td className={clsx("num text-right", (s.oos_profit_factor ?? 0) >= 1 ? "text-up" : "text-down")}>{num(s.oos_profit_factor)}</td>
                <td className="num text-right">{s.oos_net_return_pct === null ? "—" : `${num(s.oos_net_return_pct, 1)}%`}</td>
                <td className="num text-right">{s.benchmark_return_pct === null ? "—" : `${num(s.benchmark_return_pct, 1)}%`}</td>
                <td className="num text-right">{num(s.regime_split?.up?.profit_factor)} <span className="text-[10px] text-slate-500">({s.regime_split?.up?.num_trades ?? 0})</span></td>
                <td className="num text-right">{num(s.regime_split?.down?.profit_factor)} <span className="text-[10px] text-slate-500">({s.regime_split?.down?.num_trades ?? 0})</span></td>
                <td>{s.regime_gate ? <span className="text-up">pass</span> : <span className="text-amber-300">blocked</span>}</td>
                <td>{s.edge_gate ? <span className="text-up">pass</span> : <span className="text-amber-300">blocked</span>}</td>
              </tr>))}</tbody>
          </table>
          <div className="mt-1 text-[10px] text-slate-500">{d.playbook.note} Gates: regime filter {d.playbook.settings.regime_filter ? "on" : "off"}, OOS edge ≥ PF {d.playbook.settings.min_oos_profit_factor} over ≥ {d.playbook.settings.min_oos_trades} trades (change in Risk & Performance).</div>
        </Panel>
      )}

      {q.isLoading && <Loading what="Scanning universe" />}
      {q.error && <ErrorBox error={q.error} />}
      {d && d.notes.map((n, i) => <div key={i} className="rounded border border-ink-600 bg-ink-850 p-2 text-xs text-slate-400">{n}</div>)}

      {d && (
        <div className={clsx("grid gap-3", selected ? "grid-cols-1 2xl:grid-cols-[minmax(0,1fr)_560px]" : "grid-cols-1")}>
          <Panel title={<span className="flex items-center gap-1">
              {([["buy", `Buy now (${buyNow.length})`], ["watch", `Watch only — blocked (${watchOnly.length})`], ["all", `All signals (${all.length})`]] as [Tab, string][]).map(([k, label]) => (
                <button key={k} onClick={() => setTab(k)} className={clsx("rounded px-2 py-0.5 text-xs", tab === k ? "bg-sky-900/60 text-sky-200" : "text-slate-400 hover:text-slate-200")}>{label}</button>))}
            </span>} right={<span className="text-slate-500">ranked by final score · click a row for the drill-down</span>} bodyClass="p-0 overflow-auto max-h-[70vh]">
            {rows.length === 0 ? <Empty>{tab === "buy" ? "NO TRADE — no setup passes every gate right now. See Watch only for blocked setups and why." : "No candidates match these filters."}</Empty> : (
              <table className="tbl">
                <thead><tr>
                  <th>Symbol</th><th>Strategy</th><th>Evidence</th><th>Mode</th><th>State</th><th>Action</th><th>Dir</th>
                  <th className="text-right">Live</th><th className="text-right">Trigger / fill</th><th className="text-right">Stop</th>
                  <th className="text-right">Target</th><th className="text-right">R:R</th><th className="text-right">Qty</th>
                  <th className="text-right">Final</th><th>Score</th><th>Confl.</th><th>Gates</th><th>Risk</th><th>Liq</th>
                </tr></thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={`${r.strategy}:${r.exchange}:${r.symbol}`} onClick={() => setSelected(r)}
                      className={clsx("cursor-pointer", selected?.signal_id === r.signal_id && "bg-ink-800")}>
                      <td><span className="font-semibold text-slate-100">{r.symbol}</span> <span className="text-[10px] text-slate-500">{r.exchange}</span></td>
                      <td className="text-slate-300">{r.strategy_name}</td>
                      <td><TierBadge tier={r.evidence_tier} />{r.ml && <div className="text-[9px] text-slate-500" title={r.ml.model_useful ? "ML filter active" : "ML model showed no out-of-sample lift - shown for information only"}>ML {Math.round(r.ml.p_win * 100)}%</div>}</td>
                      <td>{r.mode}</td>
                      <td><StateBadge state={r.state} /></td>
                      <td title={r.action_reasons.join("\n")}><ActionBadge action={r.action} /></td>
                      <td className={r.direction === "bullish" ? "text-up" : r.direction === "bearish" ? "text-down" : "text-slate-400"}>{r.direction}</td>
                      <td className="text-right whitespace-nowrap"><LivePrice exchange={r.exchange} symbol={r.symbol} fallback={r.reference_price} />
                        <StaleBadge exchange={r.exchange} symbol={r.symbol} marketOpen={r.freshness.market_session === "OPEN"} /></td>
                      <td className="num text-right">{r.state === "WATCH" ? "—" : r.entry.filled != null ? <span title="filled at next open">{inr(r.entry.filled)}</span> : inr(r.entry.trigger ?? r.entry.reference)}</td>
                      <td className="num text-right text-down">{inr(r.stop_loss)}</td>
                      <td className="num text-right text-up">{r.targets.length ? inr(r.targets[0]) : r.state === "WATCH" ? "—" : "trail"}</td>
                      <td className="num text-right">{r.risk_reward ? num(r.risk_reward, 1) : "—"}</td>
                      <td className="num text-right">{r.position_size?.qty ?? "—"}</td>
                      <td className={clsx("num text-right", r.blocked ? "text-slate-500" : "text-slate-100 font-semibold")}>{r.final_score != null ? num(r.final_score, 1) : "—"}</td>
                      <td><ScoreBar score={r.score} /></td>
                      <td className={clsx("num", r.confluence?.conflict && "text-amber-300")} title={r.confluence ? `${r.confluence.strategies.join(", ")}${r.confluence.conflict ? " — direction conflict" : ""}` : ""}>{r.confluence ? `${r.confluence.count}${r.confluence.conflict ? "!" : ""}` : "—"}</td>
                      <td title={r.gates ? Object.values(r.gates).filter((g) => g.applies).map((g) => `${g.passed ? "PASS" : "FAIL"} ${g.name}: ${g.detail}`).join("\n") : ""}>
                        {r.blocked_by?.length ? <span className="text-amber-300">✕ {r.blocked_by.length}</span> : r.blocked ? <span className="text-slate-500">{r.action}</span> : <span className="text-up">✓</span>}</td>
                      <td>{r.risk_level}</td>
                      <td>{r.liquidity.rating}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Panel>
          {selected && (
            <Panel title="Drill-down" right={<span className="flex gap-2">
              {selected.signal_id && <Link className="text-sky-300 hover:underline" to={`/signals/${selected.signal_id}`}>Full detail →</Link>}
              <Link className="text-sky-300 hover:underline" to={`/charts/${selected.exchange}/${encodeURIComponent(selected.symbol)}?tf=${selected.timeframe}&strategy=${selected.strategy}${selected.signal_id ? `&signal=${selected.signal_id}` : ""}`}>Chart →</Link>
              <button className="text-slate-400 hover:text-slate-200" onClick={() => setSelected(null)}>✕</button></span>}
              bodyClass="overflow-auto max-h-[70vh]">
              <RecommendationDetail rec={selected} compact />
              <div className="mt-3 border-t border-ink-700 pt-3"><PaperTradeBox key={selected.signal_id} rec={selected} /></div>
            </Panel>
          )}
        </div>
      )}

      {d && d.blocked.length > 0 && (
        <Panel title={`NO TRADE — data quality (${d.blocked.length})`}>
          <table className="tbl">
            <thead><tr><th>Symbol</th><th>Timeframe</th><th>Reason</th><th>Issues</th><th>Strategies blocked</th></tr></thead>
            <tbody>{d.blocked.map((b) => (
              <tr key={`${b.exchange}:${b.symbol}:${b.timeframe}`}><td>{b.symbol}</td><td>{b.timeframe}</td><td>{b.reason}</td>
                <td className="whitespace-normal text-slate-400">{b.issues.join("; ")}</td><td>{b.strategies.join(", ")}</td></tr>
            ))}</tbody>
          </table>
        </Panel>
      )}
      <div className="text-[10px] text-slate-600">Scores are transparent rule-based rankings (see weights in Data & Integrations / Risk settings), not probabilities of profit. WAIT = conditions incomplete or conflicting; NO TRADE = data or risk constraints fail.</div>
    </div>
  );
}
