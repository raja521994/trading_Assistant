import clsx from "clsx";
import { useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { inr, istTime, num } from "../lib/format";
import { DataLabel, Empty, ErrorBox, Loading, Panel, StateBadge, ActionBadge } from "../components/ui";
import { CandleChart, LinesChart, type LinePoint, type Marker, type Overlay, type PriceLevel } from "../components/Charts";

interface Instrument { symbol: string; exchange: string; name: string; type: string; sector: string | null }
interface ChartResp {
  symbol: string; exchange: string; name: string; timeframe: string; data_label: string; time_note: string | null;
  candles: { time: string | number; open: number; high: number; low: number; close: number }[];
  volume: LinePoint[]; indicators: Record<string, LinePoint[]>;
  levels: { price: number; kind: string; ts: string; method: string }[];
  markers: Marker[];
  strategy_trades: { entry_ts: string; exit_ts: string; direction: string; entry_price: number; exit_price: number; exit_reason: string; net_pnl: number; r_multiple: number }[];
  signals: { id: number; strategy: string; state: string; action: string; stop_loss: number | null; targets: number[] | null; entry: { reference: number } | null; timeframe: string }[];
  quality: { status: string; freshness: string; issues: string[] };
  quote: { last_price: number; prev_close: number; change_pct: number; market_open: boolean; status: string; last_trade_time: string | null; note: string } | null;
}
interface Strategy { id: string; name: string; timeframe: string }

const OVERLAYS: { key: string; label: string; color: string; dashed?: boolean; tf?: string }[] = [
  { key: "ema20", label: "EMA20", color: "#38bdf8" },
  { key: "ema50", label: "EMA50", color: "#f59e0b" },
  { key: "ema200", label: "EMA200", color: "#a78bfa" },
  { key: "bb_upper", label: "BB upper", color: "#64748b", dashed: true },
  { key: "bb_lower", label: "BB lower", color: "#64748b", dashed: true },
  { key: "supertrend", label: "Supertrend", color: "#f472b6" },
  { key: "vwap", label: "VWAP", color: "#eab308", tf: "5m" },
];

function last(points?: LinePoint[]) {
  return points && points.length ? points[points.length - 1].value : null;
}

function MtfRow({ exchange, symbol, tf }: { exchange: string; symbol: string; tf: string }) {
  const q = useQuery({ queryKey: ["chart", exchange, symbol, tf, "mtf"], queryFn: () => api.get<ChartResp>(`/chart/${exchange}/${encodeURIComponent(symbol)}?timeframe=${tf}&bars=60`) });
  if (!q.data) return <tr><td>{tf}</td><td colSpan={4} className="text-slate-500">{q.error ? "unavailable" : "…"}</td></tr>;
  const c = q.data.candles[q.data.candles.length - 1]?.close;
  const e20 = last(q.data.indicators.ema20), e50 = last(q.data.indicators.ema50), rsi = last(q.data.indicators.rsi14);
  const trend = e20 !== null && e50 !== null ? (c > e20 && e20 > e50 ? "UP" : c < e20 && e20 < e50 ? "DOWN" : "MIXED") : "n/a";
  return (
    <tr><td>{tf}</td><td className="num">{inr(c)}</td>
      <td className={trend === "UP" ? "text-up" : trend === "DOWN" ? "text-down" : "text-amber-300"}>{trend}</td>
      <td className="num">{num(rsi, 1)}</td><td className="text-slate-500">{q.data.quality.freshness}</td></tr>
  );
}

export default function Charts() {
  const params = useParams();
  const [sp, setSp] = useSearchParams();
  const navigate = useNavigate();
  const exchange = params.exchange ?? "NSE";
  const symbol = params.symbol ?? "NIFTY 50";
  const tf = sp.get("tf") ?? "1d";
  const strategy = sp.get("strategy") ?? "";
  const [on, setOn] = useState<Record<string, boolean>>({ ema20: true, ema50: true, ema200: true, supertrend: false, bb_upper: false, bb_lower: false, vwap: true, levels: true, signal: true });

  const inst = useQuery({ queryKey: ["instruments"], queryFn: () => api.get<Instrument[]>("/instruments") });
  const strats = useQuery({ queryKey: ["strategies"], queryFn: () => api.get<Strategy[]>("/strategies") });
  const q = useQuery({
    queryKey: ["chart", exchange, symbol, tf, strategy],
    queryFn: () => api.get<ChartResp>(`/chart/${exchange}/${encodeURIComponent(symbol)}?timeframe=${tf}&bars=${tf === "1d" ? 400 : 750}${strategy ? `&strategy=${strategy}` : ""}`),
    refetchInterval: tf === "5m" ? 30_000 : 60_000,
  });
  const d = q.data;
  const intraday = tf !== "1d";

  const overlays: Overlay[] = useMemo(() => !d ? [] : OVERLAYS.filter((o) => on[o.key] && d.indicators[o.key] && (!o.tf || o.tf === tf))
    .map((o) => ({ name: o.label, color: o.color, dashed: o.dashed, data: d.indicators[o.key] })), [d, on, tf]);
  const levels: PriceLevel[] = useMemo(() => {
    if (!d) return [];
    const out: PriceLevel[] = [];
    if (d.quote) out.push({ price: d.quote.last_price, color: "#eab308", title: d.quote.market_open ? "LTP" : "Close (official)" });
    if (on.levels) d.levels.forEach((l) => out.push({ price: l.price, color: l.kind === "support" ? "#15803d" : "#b91c1c", title: l.kind === "support" ? "S" : "R", dashed: true }));
    const sig = d.signals.find((s) => s.timeframe === tf && ["SETUP", "TRIGGERED"].includes(s.state));
    if (on.signal && sig) {
      if (sig.stop_loss) out.push({ price: sig.stop_loss, color: "#ef4444", title: `Stop (${sig.strategy})` });
      if (sig.entry) out.push({ price: sig.entry.reference, color: "#38bdf8", title: "Entry" });
      (sig.targets ?? []).forEach((t) => out.push({ price: t, color: "#22c55e", title: "Target" }));
    }
    return out;
  }, [d, on, tf]);
  const rsiLines: Overlay[] = useMemo(() => d ? [{ name: "RSI14", color: "#38bdf8", data: d.indicators.rsi14 }] : [], [d]);
  const macdLines: Overlay[] = useMemo(() => d ? [
    { name: "MACD", color: "#38bdf8", data: d.indicators.macd }, { name: "Signal", color: "#f59e0b", data: d.indicators.macd_signal },
    { name: "Hist", color: "#64748b", data: d.indicators.macd_hist }] : [], [d]);

  const symbols = inst.data ?? [];
  const tfStrategies = (strats.data ?? []).filter((s) => s.timeframe === tf);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold">Charts</h1>
        <select className="input" value={`${exchange}:${symbol}`} onChange={(e) => {
          const [ex, ...rest] = e.target.value.split(":");
          navigate(`/charts/${ex}/${encodeURIComponent(rest.join(":"))}?${sp.toString()}`);
        }}>
          {symbols.map((i) => <option key={`${i.exchange}:${i.symbol}`} value={`${i.exchange}:${i.symbol}`}>{i.symbol} ({i.exchange}{i.type === "INDEX" ? ", index" : ""})</option>)}
        </select>
        <div className="flex gap-1">
          {["1d", "5m"].map((t) => (
            <button key={t} className={clsx("btn", t === tf && "btn-primary")} onClick={() => { sp.set("tf", t); sp.delete("strategy"); setSp(sp); }}>{t}</button>
          ))}
        </div>
        <select className="input" value={strategy} onChange={(e) => { if (e.target.value) sp.set("strategy", e.target.value); else sp.delete("strategy"); setSp(sp); }}>
          <option value="">No strategy markers</option>
          {tfStrategies.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
        {d && <DataLabel label={d.data_label} />}
        {d?.quote && (
          <span className="text-sm" title={d.quote.note}>
            <span className="text-slate-400">{d.quote.market_open ? "LTP" : "Official close"} </span>
            <span className="num font-semibold text-slate-100">{inr(d.quote.last_price)}</span>{" "}
            <span className={clsx("num", d.quote.change_pct >= 0 ? "text-up" : "text-down")}>{d.quote.change_pct >= 0 ? "+" : ""}{num(d.quote.change_pct)}%</span>
            {d.quote.last_trade_time && <span className="text-[10px] text-slate-500"> · last trade {d.quote.last_trade_time}</span>}
          </span>
        )}
        {d && <span className="text-xs text-slate-500">{d.name} · data {d.quality.status}/{d.quality.freshness}{d.time_note ? ` · ${d.time_note}` : ""}</span>}
      </div>
      <div className="flex flex-wrap gap-3 text-xs">
        {OVERLAYS.filter((o) => !o.tf || o.tf === tf).map((o) => (
          <label key={o.key} className="flex items-center gap-1"><input type="checkbox" checked={!!on[o.key]} onChange={(e) => setOn({ ...on, [o.key]: e.target.checked })} />
            <span style={{ color: o.color }}>{o.label}</span></label>
        ))}
        <label className="flex items-center gap-1"><input type="checkbox" checked={on.levels} onChange={(e) => setOn({ ...on, levels: e.target.checked })} />Support/resistance (swing pivots)</label>
        <label className="flex items-center gap-1"><input type="checkbox" checked={on.signal} onChange={(e) => setOn({ ...on, signal: e.target.checked })} />Active signal levels</label>
      </div>

      {q.isLoading && <Loading what="Loading chart" />}
      {q.error && <ErrorBox error={q.error} />}
      {d && (
        <div className="grid grid-cols-1 2xl:grid-cols-[minmax(0,1fr)_380px] gap-3">
          <div className="space-y-3 min-w-0">
            <Panel bodyClass="p-1"><CandleChart candles={d.candles} volume={d.volume} overlays={overlays} levels={levels} markers={d.markers} intraday={intraday} height={460} /></Panel>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              <Panel title="RSI(14)" bodyClass="p-1"><LinesChart lines={rsiLines} height={150} intraday={intraday} /></Panel>
              <Panel title="MACD(12,26,9)" bodyClass="p-1"><LinesChart lines={macdLines} height={150} intraday={intraday} histogram /></Panel>
            </div>
          </div>
          <div className="space-y-3">
            <Panel title="Multi-timeframe">
              <table className="tbl"><thead><tr><th>TF</th><th>Close</th><th>Trend</th><th>RSI</th><th>Data</th></tr></thead>
                <tbody><MtfRow exchange={exchange} symbol={symbol} tf="1d" /><MtfRow exchange={exchange} symbol={symbol} tf="5m" /></tbody></table>
              <div className="mt-1 text-[10px] text-slate-500">Trend = close vs EMA20 vs EMA50 on each timeframe.</div>
            </Panel>
            <Panel title="Signals for this symbol">
              {d.signals.length === 0 ? <Empty>No scanner signals recorded.</Empty> : d.signals.map((s) => (
                <Link key={s.id} to={`/signals/${s.id}`} className="flex items-center gap-2 py-1 text-xs hover:bg-ink-850">
                  <StateBadge state={s.state} /><ActionBadge action={s.action} /><span className="text-slate-300">{s.strategy}</span>
                  <span className="ml-auto text-slate-500">{s.timeframe}</span>
                </Link>
              ))}
            </Panel>
            <Panel title="Support / resistance">
              {d.levels.length === 0 ? <Empty>No confirmed swing levels.</Empty> : (
                <table className="tbl"><tbody>{d.levels.map((l, i) => (
                  <tr key={i}><td className={l.kind === "support" ? "text-up" : "text-down"}>{l.kind}</td><td className="num">{inr(l.price)}</td>
                    <td className="text-slate-500">{istTime(l.ts)}</td></tr>))}</tbody></table>
              )}
              <div className="text-[10px] text-slate-500 mt-1">{d.levels[0]?.method ?? ""} — algorithmic hypothesis, not observed order flow.</div>
            </Panel>
            {strategy && (
              <Panel title={`Historical ${strategy} trades on this chart`} bodyClass="p-0 max-h-72 overflow-auto">
                {d.strategy_trades.length === 0 ? <Empty>No trades in the visible window.</Empty> : (
                  <table className="tbl"><thead><tr><th>Entry</th><th>Dir</th><th className="text-right">In</th><th className="text-right">Out</th><th>Exit</th><th className="text-right">R</th></tr></thead>
                    <tbody>{d.strategy_trades.map((t, i) => (
                      <tr key={i}><td>{istTime(t.entry_ts)}</td><td>{t.direction}</td><td className="num text-right">{inr(t.entry_price)}</td>
                        <td className="num text-right">{inr(t.exit_price)}</td><td className="text-slate-400">{t.exit_reason}</td>
                        <td className={clsx("num text-right", t.r_multiple >= 0 ? "text-up" : "text-down")}>{num(t.r_multiple)}</td></tr>))}</tbody></table>
                )}
              </Panel>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
