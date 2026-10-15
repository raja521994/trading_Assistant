import clsx from "clsx";
import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, qs, type Recommendation, type ScanResponse } from "../lib/api";
import { inr, istTime, num, titleCase } from "../lib/format";
import { ActionBadge, DataLabel, Empty, ErrorBox, Loading, Panel, ScoreBar, StateBadge } from "../components/ui";
import { PaperTradeBox, RecommendationDetail } from "../components/Recommendation";

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

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold">AI Trading Scanner</h1>
        {d && <DataLabel label={d.data_label} />}
        {d && <span className="text-xs text-slate-500">{d.total} candidates · generated {istTime(d.generated_at)} IST · {Object.entries(d.counts).map(([k, v]) => `${k} ${v}`).join(" · ")}</span>}
        <button className="btn ml-auto" onClick={() => refresh.mutate()} disabled={refresh.isPending}>{refresh.isPending ? "Scanning…" : "Re-scan now"}</button>
      </div>

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

      {q.isLoading && <Loading what="Scanning universe" />}
      {q.error && <ErrorBox error={q.error} />}
      {d && d.notes.map((n, i) => <div key={i} className="rounded border border-ink-600 bg-ink-850 p-2 text-xs text-slate-400">{n}</div>)}

      {d && (
        <div className={clsx("grid gap-3", selected ? "grid-cols-1 2xl:grid-cols-[minmax(0,1fr)_560px]" : "grid-cols-1")}>
          <Panel title="Ranked candidates" right={<span className="text-slate-500">click a row for the drill-down</span>} bodyClass="p-0 overflow-auto max-h-[70vh]">
            {d.recommendations.length === 0 ? <Empty>No candidates match these filters.</Empty> : (
              <table className="tbl">
                <thead><tr>
                  <th>Symbol</th><th>Strategy</th><th>Mode</th><th>State</th><th>Action</th><th>Dir</th>
                  <th className="text-right">Ref</th><th className="text-right">Entry</th><th className="text-right">Stop</th>
                  <th className="text-right">Target</th><th className="text-right">R:R</th><th className="text-right">Qty</th>
                  <th>Score</th><th>Risk</th><th>Liq</th>
                </tr></thead>
                <tbody>
                  {d.recommendations.map((r) => (
                    <tr key={`${r.strategy}:${r.exchange}:${r.symbol}`} onClick={() => setSelected(r)}
                      className={clsx("cursor-pointer", selected?.signal_id === r.signal_id && "bg-ink-800")}>
                      <td><span className="font-semibold text-slate-100">{r.symbol}</span> <span className="text-[10px] text-slate-500">{r.exchange}</span></td>
                      <td className="text-slate-300">{r.strategy_name}</td>
                      <td>{r.mode}</td>
                      <td><StateBadge state={r.state} /></td>
                      <td><ActionBadge action={r.action} /></td>
                      <td className={r.direction === "bullish" ? "text-up" : r.direction === "bearish" ? "text-down" : "text-slate-400"}>{r.direction}</td>
                      <td className="num text-right">{inr(r.reference_price)}</td>
                      <td className="num text-right">{r.state === "WATCH" ? "—" : inr(r.entry.reference)}</td>
                      <td className="num text-right text-down">{inr(r.stop_loss)}</td>
                      <td className="num text-right text-up">{r.targets.length ? inr(r.targets[0]) : r.state === "WATCH" ? "—" : "trail"}</td>
                      <td className="num text-right">{r.risk_reward ? num(r.risk_reward, 1) : "—"}</td>
                      <td className="num text-right">{r.position_size?.qty ?? "—"}</td>
                      <td><ScoreBar score={r.score} /></td>
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
              <Link className="text-sky-300 hover:underline" to={`/charts/${selected.exchange}/${encodeURIComponent(selected.symbol)}?tf=${selected.timeframe}&strategy=${selected.strategy}`}>Chart →</Link>
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
