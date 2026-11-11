import clsx from "clsx";
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { inr, istTime, num, pct, tone } from "../lib/format";
import { Empty, ErrorBox, Loading, Panel, Stat } from "../components/ui";
import { LinesChart, type Overlay } from "../components/Charts";

const MODES = ["intraday", "swing", "positional"] as const;
type Mode = (typeof MODES)[number];

interface AgentConfig {
  modes: Mode[];
  mode_allocation_pct: Record<string, number>;
  max_new_trades_per_cycle: number;
  allow_unvalidated: boolean;
  require_market_open: boolean;
  breakeven_at_r: number;
  max_hold_days: Record<string, number>;
  cycle_seconds: number;
}

interface Executed {
  kind: string; symbol: string; mode: string; direction: string | null; strategy: string | null; reason: string;
  status: string; detail?: string; trade_id?: number; fill?: number; qty?: number; net_pnl?: number;
}

interface Cycle {
  at: number; generated_at: string; market_open: boolean; regime: string | null; decision: string | null;
  regime_multiplier: number | null; candidates_seen: number; instructions: number; executed: Executed[]; notes: string[];
  rejected: { symbol: string; source: string; mode: string; strategy: string; direction: string; action: string; reason: string }[];
  portfolio: { equity: number; cash: number; open_positions: number; realized_today: number; slots: Record<string, number> };
}

interface AgentStatus {
  running: boolean; session_id: string | null; started_at_ist: string | null; stopped_at_ist: string | null;
  starting_equity: number | null; config: AgentConfig; cycles: number; last_cycle: Cycle | null;
}

interface Bucket {
  trades: number; open: number; closed: number; wins: number; win_rate_pct: number | null; realized_pnl: number;
  unrealized_pnl: number; total_pnl: number; avg_r: number | null;
}

interface AgentTrade {
  id: number; symbol: string; strategy: string; mode: string; direction: string; qty: number; entry_price: number;
  entry_time: string; stop: number; target: number | null; status: string; last_price: number | null;
  exit_price: number | null; exit_time: string | null; exit_reason: string | null; net_pnl: number | null;
  unrealized_pnl: number | null; r_multiple: number | null; rationale: string | null; unvalidated: boolean;
}

interface Report {
  session_id: string | null; running: boolean; started_at: string | null; days_running: number; starting_equity: number;
  totals: Bucket; return_pct: number | null; by_mode: Record<string, Bucket>; by_strategy: (Bucket & { strategy: string })[];
  daily: { date: string; pnl: number; cumulative: number }[]; trades: AgentTrade[]; note: string;
}

function ConfigForm({ cfg, onChange, disabled }: { cfg: AgentConfig; onChange: (c: AgentConfig) => void; disabled: boolean }) {
  const toggleMode = (m: Mode) =>
    onChange({ ...cfg, modes: cfg.modes.includes(m) ? cfg.modes.filter((x) => x !== m) : [...cfg.modes, m] });
  const numInput = (value: number, set: (v: number) => void, w = "w-16") => (
    <input type="number" className={clsx("input", w)} value={value} disabled={disabled}
      onChange={(e) => set(Number(e.target.value))} />
  );
  return (
    <div className="space-y-3 text-xs">
      <div className="flex flex-wrap gap-4">
        {MODES.map((m) => (
          <div key={m} className="flex items-center gap-2 rounded border border-ink-700 px-2 py-1.5">
            <label className="flex items-center gap-1 capitalize">
              <input type="checkbox" checked={cfg.modes.includes(m)} disabled={disabled} onChange={() => toggleMode(m)} />{m}
            </label>
            <span className="text-slate-500">slots %</span>
            {numInput(cfg.mode_allocation_pct[m] ?? 0, (v) => onChange({ ...cfg, mode_allocation_pct: { ...cfg.mode_allocation_pct, [m]: v } }), "w-14")}
            {m !== "intraday" && <>
              <span className="text-slate-500">max hold days</span>
              {numInput(cfg.max_hold_days[m] ?? 0, (v) => onChange({ ...cfg, max_hold_days: { ...cfg.max_hold_days, [m]: v } }), "w-14")}
            </>}
          </div>
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-4">
        <label className="flex items-center gap-1">New trades / cycle {numInput(cfg.max_new_trades_per_cycle, (v) => onChange({ ...cfg, max_new_trades_per_cycle: v }))}</label>
        <label className="flex items-center gap-1">Cycle every (s) {numInput(cfg.cycle_seconds, (v) => onChange({ ...cfg, cycle_seconds: v }))}</label>
        <label className="flex items-center gap-1">Breakeven stop at (R, 0 = off) {numInput(cfg.breakeven_at_r, (v) => onChange({ ...cfg, breakeven_at_r: v }))}</label>
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={cfg.require_market_open} disabled={disabled}
            onChange={(e) => onChange({ ...cfg, require_market_open: e.target.checked })} />Enter only during market hours
        </label>
        <label className="flex items-center gap-1" title="Also trade setups the evidence gates marked WAIT / NO TRADE. Flagged as unvalidated on every trade.">
          <input type="checkbox" checked={cfg.allow_unvalidated} disabled={disabled}
            onChange={(e) => onChange({ ...cfg, allow_unvalidated: e.target.checked })} />Include unvalidated setups (learning mode)
        </label>
      </div>
    </div>
  );
}

function BucketRow({ name, b }: { name: string; b: Bucket }) {
  return (
    <tr>
      <td className="capitalize">{name}</td>
      <td className="num">{b.trades}</td><td className="num">{b.open}</td><td className="num">{b.closed}</td>
      <td className="num">{b.win_rate_pct === null ? "—" : `${b.win_rate_pct}%`}</td>
      <td className={clsx("num", tone(b.realized_pnl))}>{inr(b.realized_pnl)}</td>
      <td className={clsx("num", tone(b.unrealized_pnl))}>{inr(b.unrealized_pnl)}</td>
      <td className={clsx("num font-semibold", tone(b.total_pnl))}>{inr(b.total_pnl)}</td>
      <td className="num">{num(b.avg_r)}</td>
    </tr>
  );
}

const BUCKET_HEAD = (
  <tr><th></th><th>Trades</th><th>Open</th><th>Closed</th><th>Win rate</th><th>Realized</th><th>Unrealized</th><th>Total P&L</th><th>Avg R</th></tr>
);

export default function Agent() {
  const qc = useQueryClient();
  const [allRuns, setAllRuns] = useState(false);
  const st = useQuery({ queryKey: ["agent", "status"], queryFn: () => api.get<AgentStatus>("/agent/status"), refetchInterval: 5_000 });
  const rep = useQuery({ queryKey: ["agent", "report", allRuns], queryFn: () => api.get<Report>(`/agent/report${allRuns ? "?all_runs=true" : ""}`), refetchInterval: 10_000 });
  const [cfg, setCfg] = useState<AgentConfig | null>(null);
  useEffect(() => { if (st.data && cfg === null) setCfg(st.data.config); }, [st.data, cfg]);

  const refresh = () => qc.invalidateQueries({ queryKey: ["agent"] });
  const start = useMutation({ mutationFn: () => api.post("/agent/start", cfg), onSuccess: refresh });
  const stop = useMutation({ mutationFn: () => api.post("/agent/stop"), onSuccess: refresh });
  const save = useMutation({ mutationFn: () => api.put("/agent/config", cfg), onSuccess: refresh });
  const once = useMutation({ mutationFn: () => api.post("/agent/run-once"), onSuccess: () => { refresh(); qc.invalidateQueries({ queryKey: ["paper"] }); } });

  const curve: Overlay[] = useMemo(() => [{
    name: "Cumulative realized P&L", color: "#38bdf8",
    data: (rep.data?.daily ?? []).map((d) => ({ time: d.date, value: d.cumulative })),
  }], [rep.data]);

  if (st.error) return <ErrorBox error={st.error} />;
  if (st.isLoading || !cfg) return <Loading />;
  const s = st.data!;
  const r = rep.data;
  const c = s.last_cycle;
  const err = start.error ?? stop.error ?? save.error ?? once.error;

  return (
    <div className="space-y-3">
      <h1 className="text-lg font-semibold">Trading Agent <span className="text-xs font-normal text-slate-400">· paper portfolio only</span></h1>

      <Panel title="Control" right={
        <span className={clsx("rounded border px-2 py-0.5 text-[10px] font-bold", s.running ? "border-emerald-600 text-emerald-300" : "border-slate-600 text-slate-400")}>
          {s.running ? "RUNNING" : "STOPPED"}
        </span>}>
        <div className="space-y-3">
          <div className="text-[11px] text-slate-400">
            <b className="text-slate-200">Orchestrator agent</b> collects regime & gates from the decision engine, live strategy signals
            (intraday / swing / positional) and runs a per-stock risk assessment (sizing, stop/target, exposure, daily-loss, drawdown).
            It hands a plan to the <b className="text-slate-200">worker agent</b>, which places, closes and trails <b>paper</b> orders only. Nothing is sent to a broker.
          </div>
          <ConfigForm cfg={cfg} onChange={setCfg} disabled={false} />
          <div className="flex flex-wrap items-center gap-2">
            {!s.running
              ? <button className="btn btn-primary" disabled={start.isPending || cfg.modes.length === 0} onClick={() => start.mutate()}>Start agent</button>
              : <button className="btn btn-danger" disabled={stop.isPending} onClick={() => stop.mutate()}>Stop agent</button>}
            {s.running && <button className="btn" disabled={save.isPending} onClick={() => save.mutate()}>Save settings</button>}
            <button className="btn" disabled={once.isPending} onClick={() => once.mutate()}>{once.isPending ? "Running…" : "Run one cycle now"}</button>
            <span className="text-[11px] text-slate-500">
              Run {s.session_id ?? "—"} · started {istTime(s.started_at_ist)}{!s.running && s.stopped_at_ist ? ` · stopped ${istTime(s.stopped_at_ist)}` : ""} · {s.cycles} cycles
            </span>
          </div>
          {err && <div className="rounded border border-red-800 bg-red-950/40 p-2 text-xs text-red-200">{(err as Error).message}</div>}
          <div className="text-[10px] text-slate-500">Stopping the agent leaves open positions in place; their stops, targets and the 15:15 intraday exit keep running. Close them from Paper Portfolio if needed.</div>
        </div>
      </Panel>

      <Panel title="Profit report" right={
        <label className="flex items-center gap-1 text-[11px]"><input type="checkbox" checked={allRuns} onChange={(e) => setAllRuns(e.target.checked)} />All agent runs</label>}>
        {!r ? <Loading /> : (
          <div className="space-y-3">
            <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-8 gap-3">
              <Stat label="Starting equity" value={`₹${inr(r.starting_equity, 0)}`} />
              <Stat label="Total P&L" value={`₹${inr(r.totals.total_pnl)}`} valueClass={tone(r.totals.total_pnl)} />
              <Stat label="Return" value={pct(r.return_pct)} valueClass={tone(r.return_pct)} />
              <Stat label="Realized" value={`₹${inr(r.totals.realized_pnl)}`} valueClass={tone(r.totals.realized_pnl)} />
              <Stat label="Unrealized" value={`₹${inr(r.totals.unrealized_pnl)}`} valueClass={tone(r.totals.unrealized_pnl)} />
              <Stat label="Win rate" value={r.totals.win_rate_pct === null ? "—" : `${r.totals.win_rate_pct}%`} sub={`${r.totals.wins}/${r.totals.closed} closed`} />
              <Stat label="Trades" value={r.totals.trades} sub={`${r.totals.open} open`} />
              <Stat label="Days running" value={num(r.days_running, 1)} sub={r.started_at ? istTime(r.started_at) : undefined} />
            </div>
            <div className="grid gap-3 xl:grid-cols-2">
              <div>
                <div className="label mb-1">By trading style</div>
                <table className="tbl"><thead>{BUCKET_HEAD}</thead>
                  <tbody>{MODES.map((m) => <BucketRow key={m} name={m} b={r.by_mode[m]} />)}</tbody></table>
                <div className="label mt-3 mb-1">By strategy</div>
                {r.by_strategy.length === 0 ? <Empty>No agent trades yet.</Empty> :
                  <table className="tbl"><thead>{BUCKET_HEAD}</thead>
                    <tbody>{r.by_strategy.map((b) => <BucketRow key={b.strategy} name={b.strategy} b={b} />)}</tbody></table>}
              </div>
              <div>
                <div className="label mb-1">Cumulative realized P&L by day</div>
                {curve[0].data.length < 2 ? <Empty>Needs closed trades on at least two days.</Empty> : <LinesChart lines={curve} height={200} />}
              </div>
            </div>
            <div className="text-[10px] text-slate-500">{r.note}</div>
          </div>
        )}
      </Panel>

      {c && (
        <Panel title="Last orchestrator cycle" right={<span className="text-[11px]">{istTime(c.generated_at)}</span>}>
          <div className="space-y-2 text-xs">
            <div className="flex flex-wrap gap-4 text-slate-300">
              <span>Market: <b>{c.market_open ? "OPEN" : "CLOSED"}</b></span>
              <span>Regime: <b>{c.regime ?? "—"}</b> (size × {num(c.regime_multiplier)})</span>
              <span>Decision engine: <b>{c.decision ?? "—"}</b></span>
              <span>Signals considered: <b>{c.candidates_seen}</b></span>
              <span>Instructions: <b>{c.instructions}</b></span>
              <span>Slots: {Object.entries(c.portfolio.slots).map(([k, v]) => `${k} ${v}`).join(" · ")}</span>
            </div>
            {c.notes.length > 0 && <ul className="list-disc pl-4 text-amber-200/90">{c.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>}
            {c.executed.length > 0 && (
              <table className="tbl"><thead><tr><th>Worker action</th><th>Symbol</th><th>Style</th><th>Side</th><th>Status</th><th>Detail</th></tr></thead>
                <tbody>{c.executed.map((e, i) => (
                  <tr key={i}><td>{e.kind}</td><td>{e.symbol}</td><td>{e.mode}</td><td>{e.direction}</td>
                    <td className={clsx(e.status === "REJECTED" || e.status === "FAILED" ? "text-red-300" : "text-emerald-300")}>{e.status}</td>
                    <td className="max-w-[28rem] truncate" title={e.detail ?? e.reason}>{e.detail ?? e.reason}</td></tr>))}</tbody></table>
            )}
            {c.rejected.length > 0 && (
              <details><summary className="cursor-pointer text-slate-400">Candidates rejected by the orchestrator ({c.rejected.length})</summary>
                <table className="tbl mt-1"><thead><tr><th>Symbol</th><th>Source</th><th>Style</th><th>Strategy</th><th>Side</th><th>Scanner</th><th>Reason</th></tr></thead>
                  <tbody>{c.rejected.map((x, i) => (
                    <tr key={i}><td>{x.symbol}</td><td>{x.source === "decision_engine" ? "Decision" : "Scanner"}</td><td>{x.mode}</td><td>{x.strategy}</td><td>{x.direction}</td><td>{x.action}</td>
                      <td className="max-w-[28rem] truncate" title={x.reason}>{x.reason}</td></tr>))}</tbody></table>
              </details>
            )}
          </div>
        </Panel>
      )}

      <Panel title="Agent trades" bodyClass="p-0 overflow-auto max-h-[28rem]">
        {!r || r.trades.length === 0 ? <div className="p-3"><Empty>No agent trades yet.</Empty></div> : (
          <table className="tbl">
            <thead><tr><th>Entry</th><th>Symbol</th><th>Style</th><th>Strategy</th><th>Side</th><th>Qty</th><th>Entry ₹</th><th>Stop</th><th>Target</th><th>Status</th><th>Exit ₹</th><th>P&L</th><th>R</th><th>Exit reason</th></tr></thead>
            <tbody>{r.trades.map((t) => {
              const pnl = t.status === "OPEN" ? t.unrealized_pnl : t.net_pnl;
              return (
                <tr key={t.id} title={t.rationale ?? ""}>
                  <td>{istTime(t.entry_time)}</td>
                  <td>{t.symbol}{t.unvalidated && <span className="ml-1 text-[9px] text-amber-300">UNVALIDATED</span>}</td>
                  <td className="capitalize">{t.mode}</td><td>{t.strategy}</td><td>{t.direction}</td>
                  <td className="num">{t.qty}</td><td className="num">{inr(t.entry_price)}</td>
                  <td className="num">{inr(t.stop)}</td><td className="num">{inr(t.target)}</td><td>{t.status}</td>
                  <td className="num">{inr(t.exit_price ?? t.last_price)}</td>
                  <td className={clsx("num", tone(pnl))}>{inr(pnl)}</td><td className="num">{num(t.r_multiple)}</td>
                  <td className="max-w-[16rem] truncate" title={t.exit_reason ?? ""}>{t.exit_reason ?? "—"}</td>
                </tr>);
            })}</tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}
