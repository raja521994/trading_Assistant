import clsx from "clsx";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type PaperTrade } from "../lib/api";
import { inr, istTime, newIdempotencyKey, num, pct, tone } from "../lib/format";
import { Empty, ErrorBox, Loading, NotConnected, Panel, Stat } from "../components/ui";
import { LinesChart, type Overlay } from "../components/Charts";

interface Summary {
  starting_equity: number; equity: number; cash: number; realized_pnl: number; unrealized_pnl: number; open_positions: number;
  closed_trades: number; win_rate_pct: number | null; allocation: { sector: string; value: number; pct_of_equity: number }[];
  invested_pct: number; risk_at_stops: number; equity_curve: { time: string; equity: number }[]; note: string;
}

function ManualOrder() {
  const qc = useQueryClient();
  const [f, setF] = useState({ symbol: "", exchange: "NSE", direction: "LONG", mode: "swing", stop: "", target: "", qty: "" });
  const m = useMutation({
    mutationFn: () => api.post("/paper/orders", {
      symbol: f.symbol.toUpperCase(), exchange: f.exchange, direction: f.direction, mode: f.mode,
      stop: Number(f.stop), target: f.target ? Number(f.target) : null, qty: f.qty ? Number(f.qty) : null,
      idempotency_key: newIdempotencyKey(),
    }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["paper"] }),
  });
  const err = m.error instanceof ApiError ? m.error : null;
  const violations = err && err.detail && typeof err.detail === "object" ? (err.detail as { decision?: { violations?: string[] } }).decision?.violations : undefined;
  const inp = (k: keyof typeof f, label: string, w = "w-24") => (
    <label className="flex flex-col gap-0.5"><span className="label">{label}</span>
      <input className={clsx("input", w)} value={f[k]} onChange={(e) => setF({ ...f, [k]: e.target.value })} /></label>);
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-end gap-2">
        {inp("symbol", "Symbol", "w-28")}
        <label className="flex flex-col gap-0.5"><span className="label">Exchange</span>
          <select className="input" value={f.exchange} onChange={(e) => setF({ ...f, exchange: e.target.value })}><option>NSE</option><option>BSE</option></select></label>
        <label className="flex flex-col gap-0.5"><span className="label">Side</span>
          <select className="input" value={f.direction} onChange={(e) => setF({ ...f, direction: e.target.value })}><option>LONG</option><option>SHORT</option></select></label>
        <label className="flex flex-col gap-0.5"><span className="label">Mode</span>
          <select className="input" value={f.mode} onChange={(e) => setF({ ...f, mode: e.target.value })}><option>swing</option><option>positional</option><option>intraday</option></select></label>
        {inp("stop", "Stop (required)")}
        {inp("target", "Target")}
        {inp("qty", "Qty (blank = auto)")}
        <button className="btn btn-primary" disabled={!f.symbol || !f.stop || m.isPending} onClick={() => m.mutate()}>Place paper order</button>
      </div>
      <div className="text-[10px] text-slate-500">Fills at the current (simulated) quote ± slippage. All risk limits are enforced; nothing is sent to a broker.</div>
      {m.isSuccess && <div className="text-xs text-emerald-300">Paper order filled.</div>}
      {m.error && <div className="rounded border border-red-800 bg-red-950/40 p-2 text-xs text-red-200">{err?.message ?? String(m.error)}
        {violations?.length ? <ul className="list-disc pl-4 mt-1">{violations.map((v, i) => <li key={i}>{v}</li>)}</ul> : null}</div>}
    </div>
  );
}

export default function Portfolio() {
  const qc = useQueryClient();
  const s = useQuery({ queryKey: ["paper", "summary"], queryFn: () => api.get<Summary>("/paper/summary"), refetchInterval: 5_000 });
  const t = useQuery({ queryKey: ["paper", "trades"], queryFn: () => api.get<PaperTrade[]>("/paper/trades"), refetchInterval: 5_000 });
  const close = useMutation({
    mutationFn: (id: number) => api.post(`/paper/trades/${id}/close`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["paper"] }),
  });
  const curve: Overlay[] = useMemo(() => {
    const pts = s.data?.equity_curve ?? [];
    const byDay = new Map<string, number>();
    pts.forEach((p) => byDay.set(p.time.slice(0, 10), p.equity));
    return [{ name: "Realized equity", color: "#38bdf8", data: [...byDay.entries()].map(([time, value]) => ({ time, value })) }];
  }, [s.data]);
  if (s.isLoading || t.isLoading) return <Loading />;
  if (s.error) return <ErrorBox error={s.error} />;
  const d = s.data!;
  const trades = t.data ?? [];
  const open = trades.filter((x) => x.status === "OPEN");
  const closed = trades.filter((x) => x.status === "CLOSED");

  return (
    <div className="space-y-3">
      <h1 className="text-lg font-semibold">Paper Portfolio</h1>
      <Panel>
        <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-8 gap-3">
          <Stat label="Equity" value={`₹${inr(d.equity, 0)}`} sub={`start ₹${inr(d.starting_equity, 0)}`} />
          <Stat label="Cash" value={`₹${inr(d.cash, 0)}`} />
          <Stat label="Realized P&L" value={`₹${inr(d.realized_pnl)}`} valueClass={tone(d.realized_pnl)} />
          <Stat label="Unrealized P&L" value={`₹${inr(d.unrealized_pnl)}`} valueClass={tone(d.unrealized_pnl)} />
          <Stat label="Open positions" value={d.open_positions} />
          <Stat label="Invested" value={pct(d.invested_pct, 1, false)} />
          <Stat label="Risk at stops" value={`₹${inr(d.risk_at_stops, 0)}`} />
          <Stat label="Win rate" value={pct(d.win_rate_pct, 1, false)} sub={`${d.closed_trades} closed`} />
        </div>
        <div className="mt-2 text-[10px] text-slate-500">{d.note}</div>
      </Panel>

      <Panel title={`Open positions (${open.length})`} bodyClass="p-0 overflow-auto">
        {open.length === 0 ? <Empty>No open paper positions. Use the scanner drill-down or the form below to open one.</Empty> : (
          <table className="tbl">
            <thead><tr><th>#</th><th>Symbol</th><th>Strategy</th><th>Mode</th><th>Side</th><th className="text-right">Qty</th><th className="text-right">Entry</th><th className="text-right">Last</th><th className="text-right">Stop</th><th className="text-right">Target</th><th className="text-right">Unrealized</th><th>Opened</th><th>Data</th><th></th></tr></thead>
            <tbody>{open.map((x) => (
              <tr key={x.id}><td>{x.id}</td><td className="font-semibold">{x.symbol} <span className="text-[10px] text-slate-500">{x.exchange}</span></td>
                <td>{x.signal_id ? <Link className="text-sky-300 hover:underline" to={`/signals/${x.signal_id}`}>{x.strategy}</Link> : x.strategy}</td>
                <td>{x.mode}</td><td className={x.direction === "LONG" ? "text-up" : "text-down"}>{x.direction}</td><td className="num text-right">{x.qty}</td>
                <td className="num text-right">{inr(x.entry_price)}</td><td className="num text-right">{inr(x.last_price)}</td>
                <td className="num text-right text-down">{inr(x.stop)}</td><td className="num text-right text-up">{inr(x.target)}</td>
                <td className={clsx("num text-right", tone(x.unrealized_pnl))}>{inr(x.unrealized_pnl)}</td><td>{istTime(x.entry_time)}</td>
                <td className="text-[10px] text-amber-300">{x.data_label}</td>
                <td><button className="btn btn-danger" disabled={close.isPending} onClick={() => close.mutate(x.id)}>Close</button></td></tr>))}</tbody>
          </table>
        )}
      </Panel>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-3">
        <Panel title="Sector allocation (open)">
          {d.allocation.length === 0 ? <Empty>Nothing invested.</Empty> : d.allocation.map((a) => (
            <div key={a.sector} className="mb-1.5 text-xs">
              <div className="flex justify-between"><span>{a.sector}</span><span className="num">₹{inr(a.value, 0)} · {pct(a.pct_of_equity, 1, false)}</span></div>
              <div className="h-1.5 rounded bg-ink-700"><div className="h-1.5 rounded bg-sky-500" style={{ width: `${Math.min(a.pct_of_equity, 100)}%` }} /></div>
            </div>))}
        </Panel>
        <Panel title="Realized equity curve" className="xl:col-span-2" bodyClass="p-1">
          {curve[0].data.length < 2 ? <Empty>Close at least two trades on different days to plot the curve.</Empty> : <LinesChart lines={curve} height={200} />}
        </Panel>
      </div>

      <Panel title="Manual paper order"><ManualOrder /></Panel>

      <Panel title={`Closed trades — outcome log (${closed.length})`} bodyClass="p-0 overflow-auto max-h-96">
        {closed.length === 0 ? <Empty>No closed paper trades yet.</Empty> : (
          <table className="tbl">
            <thead><tr><th>#</th><th>Symbol</th><th>Strategy</th><th>Side</th><th className="text-right">Qty</th><th className="text-right">Entry</th><th className="text-right">Exit</th><th>Reason</th><th className="text-right">Costs</th><th className="text-right">Net P&L</th><th className="text-right">R</th><th>Closed</th></tr></thead>
            <tbody>{closed.map((x) => (
              <tr key={x.id}><td>{x.id}</td><td>{x.symbol}</td><td>{x.strategy}</td><td>{x.direction}</td><td className="num text-right">{x.qty}</td>
                <td className="num text-right">{inr(x.entry_price)}</td><td className="num text-right">{inr(x.exit_price)}</td><td className="text-slate-400">{x.exit_reason}</td>
                <td className="num text-right">{inr(x.costs)}</td><td className={clsx("num text-right", tone(x.net_pnl))}>{inr(x.net_pnl)}</td>
                <td className={clsx("num text-right", tone(x.r_multiple))}>{num(x.r_multiple)}</td><td>{istTime(x.exit_time)}</td></tr>))}</tbody>
          </table>
        )}
      </Panel>
      <NotConnected title="Broker holdings & positions" reason="Zerodha account endpoints (holdings, positions, order book) are not connected in this build. Only the local paper account is shown." />
    </div>
  );
}
