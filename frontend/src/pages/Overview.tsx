import clsx from "clsx";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type Regime } from "../lib/api";
import { crore, inr, istTime, num, pct, tone } from "../lib/format";
import { DataLabel, Empty, ErrorBox, Loading, NotConnected, Panel, Spark, Stat } from "../components/ui";
import { LiveChange, LivePrice } from "../components/Live";
import { useLiveTick } from "../lib/live";

interface Row {
  symbol: string; exchange: string; name: string; sector: string | null; last: number; change_pct: number;
  change_1w_pct: number | null; vol_ratio: number | null; atr_pct: number | null; turnover_cr: number;
}
interface IndexRow {
  symbol: string; exchange: string; name: string; last: number; change: number; change_pct: number;
  change_1w_pct: number | null; change_1m_pct: number | null; spark: number[]; headline: boolean; ts: string; market_open: boolean;
}
interface SectorRow { sector: string; count: number; change_pct: number | null; change_1w_pct: number | null; change_1m_pct: number | null; advances: number }
interface OverviewResp {
  generated_at: string; data_label: string; provider: string; provider_note: string | null; market_session: string;
  indices: IndexRow[]; breadth: { advances: number; declines: number; unchanged: number; total: number; ad_ratio: number | null };
  sectors: SectorRow[]; gainers: Row[]; losers: Row[]; volume_leaders: Row[]; volatility_leaders: Row[];
  regime: Regime | null; announcements: { status: string; message: string };
  feed: { status: string; connected: boolean; message: string; latency_ms: number | null; last_daily_bar: string | null; expected_daily_bar: string | null; instruments: number };
}

function heatColor(v: number | null) {
  if (v === null) return "#151b25";
  const a = Math.min(Math.abs(v) / 2.5, 1);
  return v >= 0 ? `rgba(34,197,94,${0.12 + a * 0.55})` : `rgba(239,68,68,${0.12 + a * 0.55})`;
}

function MoverTable({ rows, metric }: { rows: Row[]; metric: "change" | "volume" | "volatility" }) {
  if (!rows.length) return <Empty>No data</Empty>;
  return (
    <table className="tbl">
      <thead><tr><th>Symbol</th><th className="text-right">Last</th><th className="text-right">1D</th>
        <th className="text-right">{metric === "volume" ? "Vol x avg" : metric === "volatility" ? "ATR %" : "1W"}</th></tr></thead>
      <tbody>
        {rows.map((r) => (
          <tr key={`${r.exchange}:${r.symbol}`}>
            <td><Link className="text-sky-300 hover:underline" to={`/charts/${r.exchange}/${encodeURIComponent(r.symbol)}`}>{r.symbol}</Link>
              <span className="ml-1 text-[10px] text-slate-500">{r.exchange}</span></td>
            <td className="text-right"><LivePrice exchange={r.exchange} symbol={r.symbol} fallback={r.last} /></td>
            <td className="text-right"><LiveChange exchange={r.exchange} symbol={r.symbol} fallback={r.change_pct} /></td>
            <td className="num text-right">{metric === "volume" ? `${num(r.vol_ratio)}x` : metric === "volatility" ? `${num(r.atr_pct)}%` : <span className={tone(r.change_1w_pct)}>{pct(r.change_1w_pct)}</span>}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function IndexChange({ i }: { i: IndexRow }) {
  const t = useLiveTick(i.exchange, i.symbol);
  const ch = t?.ch ?? i.change;
  const chp = t?.chp ?? i.change_pct;
  return <div className={clsx("num text-xs", tone(ch))}>{ch > 0 ? "+" : ""}{inr(ch)} ({pct(chp)})</div>;
}

export default function Overview() {
  const q = useQuery({ queryKey: ["overview"], queryFn: () => api.get<OverviewResp>("/market/overview"), refetchInterval: 15_000 });
  if (q.isLoading) return <Loading what="Loading market overview" />;
  if (q.error) return <ErrorBox error={q.error} />;
  const d = q.data!;
  const reg = d.regime?.current;
  const b = d.breadth;
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold">Market Overview</h1>
        <DataLabel label={d.data_label} />
        <span className="text-xs text-slate-500">Provider: {d.provider} · Session: {d.market_session} · Updated {istTime(d.generated_at)} IST</span>
      </div>
      {d.provider_note && <div className="rounded border border-amber-700 bg-amber-950/30 p-2 text-xs text-amber-200">{d.provider_note}</div>}

      <div className="grid grid-cols-2 xl:grid-cols-4 gap-3">
        {d.indices.filter((i) => i.headline).map((i) => (
          <Panel key={i.symbol} bodyClass="p-3">
            <div className="flex items-start justify-between">
              <div>
                <div className="text-xs text-slate-400">{i.symbol} <span className="text-[10px] text-slate-600">{i.exchange}</span></div>
                <div className="text-xl font-semibold text-slate-100"><LivePrice exchange={i.exchange} symbol={i.symbol} fallback={i.last} /></div>
                <IndexChange i={i} />
              </div>
              <Spark values={i.spark} />
            </div>
            <div className="mt-2 flex gap-4 text-[11px] text-slate-500">
              <span>1W <span className={clsx("num", tone(i.change_1w_pct))}>{pct(i.change_1w_pct)}</span></span>
              <span>1M <span className={clsx("num", tone(i.change_1m_pct))}>{pct(i.change_1m_pct)}</span></span>
              <span className="ml-auto">{i.market_open ? "intraday" : "close"}</span>
            </div>
          </Panel>
        ))}
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-3">
        <Panel title="Market regime" right={<span className="text-slate-500">benchmark NIFTY 50</span>}>
          {reg ? (
            <div className="space-y-2">
              <div className="text-sm font-semibold text-slate-100">{reg.label}</div>
              <div className="grid grid-cols-3 gap-2">
                <Stat label="Trend" value={reg.trend} valueClass={reg.trend === "UPTREND" ? "text-up" : reg.trend === "DOWNTREND" ? "text-down" : "text-amber-300"} />
                <Stat label="Volatility" value={reg.volatility} />
                <Stat label="Breadth >50DMA" value={`${num(reg.breadth_pct_above_sma50, 0)}%`} />
              </div>
              <div className="label pt-1">What changed (20 sessions)</div>
              <ul className="text-xs text-slate-300 space-y-0.5">{d.regime!.changes.map((c, i) => <li key={i}>• {c}</li>)}</ul>
              <div className="text-[10px] text-slate-500">Rules: close vs SMA50/200 + ADX≥20; 20d realised-vol percentile; % of universe above 50-day average.</div>
            </div>
          ) : <Empty>Regime unavailable</Empty>}
        </Panel>
        <Panel title="Breadth" right={<span className="text-slate-500">{b.total} equities</span>}>
          <div className="grid grid-cols-4 gap-2">
            <Stat label="Advances" value={b.advances} valueClass="text-up" />
            <Stat label="Declines" value={b.declines} valueClass="text-down" />
            <Stat label="Unchanged" value={b.unchanged} />
            <Stat label="A/D ratio" value={num(b.ad_ratio)} />
          </div>
          <div className="mt-3 flex h-2 overflow-hidden rounded">
            <div className="bg-up" style={{ width: `${(b.advances / Math.max(b.total, 1)) * 100}%` }} />
            <div className="bg-slate-600" style={{ width: `${(b.unchanged / Math.max(b.total, 1)) * 100}%` }} />
            <div className="bg-down" style={{ width: `${(b.declines / Math.max(b.total, 1)) * 100}%` }} />
          </div>
          <div className="mt-4 label">Sector & other indices</div>
          <table className="tbl mt-1">
            <tbody>
              {d.indices.filter((i) => !i.headline).map((i) => (
                <tr key={i.symbol}><td>{i.symbol}</td><td className="text-right"><LivePrice exchange={i.exchange} symbol={i.symbol} fallback={i.last} /></td>
                  <td className="text-right"><LiveChange exchange={i.exchange} symbol={i.symbol} fallback={i.change_pct} /></td>
                  <td className={clsx("num text-right", tone(i.change_1m_pct))}>{pct(i.change_1m_pct)} 1M</td></tr>
              ))}
            </tbody>
          </table>
        </Panel>
        <Panel title="Feed health & events">
          <div className="space-y-1 text-xs">
            <div className="flex items-center gap-2"><DataLabel label={d.feed.status} /><span className="text-slate-300">{d.feed.message}</span></div>
            <div className="text-slate-400">Last daily bar: <span className="num text-slate-200">{istTime(d.feed.last_daily_bar)}</span></div>
            <div className="text-slate-400">Expected daily bar: <span className="num text-slate-200">{istTime(d.feed.expected_daily_bar)}</span></div>
            <div className="text-slate-400">Instruments tracked: <span className="num text-slate-200">{d.feed.instruments}</span></div>
          </div>
          <div className="mt-3">
            <NotConnected title="Announcements & events" reason={d.announcements.message} />
          </div>
        </Panel>
      </div>

      <Panel title="Sector heatmap (1D, simple average of constituents)">
        <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-7 gap-2">
          {d.sectors.map((s) => (
            <div key={s.sector} className="rounded p-2" style={{ background: heatColor(s.change_pct) }}>
              <div className="text-xs font-semibold text-slate-100 truncate">{s.sector}</div>
              <div className="num text-sm text-white">{pct(s.change_pct)}</div>
              <div className="text-[10px] text-slate-200/80">1W {pct(s.change_1w_pct)} · 1M {pct(s.change_1m_pct)}</div>
              <div className="text-[10px] text-slate-200/70">{s.advances}/{s.count} up</div>
            </div>
          ))}
        </div>
      </Panel>

      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-4 gap-3">
        <Panel title="Top gainers"><MoverTable rows={d.gainers} metric="change" /></Panel>
        <Panel title="Top losers"><MoverTable rows={d.losers} metric="change" /></Panel>
        <Panel title="Volume leaders"><MoverTable rows={d.volume_leaders} metric="volume" /></Panel>
        <Panel title="Volatility leaders"><MoverTable rows={d.volatility_leaders} metric="volatility" /></Panel>
      </div>
      <div className="text-[10px] text-slate-600">Liquidity values: 20-session average traded value, e.g. {crore(d.gainers[0]?.turnover_cr)} for {d.gainers[0]?.symbol}.</div>
    </div>
  );
}
