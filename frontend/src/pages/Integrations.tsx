import clsx from "clsx";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { istTime, num } from "../lib/format";
import { DataLabel, ErrorBox, Loading, NotConnected, Panel, Pill } from "../components/ui";

interface Provider {
  name: string; display_name: string; enabled: boolean; active: boolean;
  health: { connected: boolean; status: string; message: string; latency_ms: number | null; last_update: string | null; errors: string[] };
  capabilities: Record<string, unknown> & { timeframes: string[]; exchanges: string[]; instrument_types: string[]; missing_fields: string[]; requires: string[]; needs_verification: string[]; history_note: string; data_status: string };
  missing_requirements: string[];
}
interface Coverage {
  provider: string; provider_name: string; active: boolean; exchange: string; instrument_type: string; timeframe: string; instruments: number;
  history_start: string | null; history_end: string | null; bars: number; status: string; connected: boolean; last_update: string | null;
  latency_ms: number | null; missing_fields: string[]; note: string;
}
interface Integrations {
  configured_provider: string; active_provider: string; fallback_note: string | null; providers: Provider[]; coverage: Coverage[];
  not_connected: { name: string; reason: string }[]; llm: { active_provider: string; model: string | null; note: string };
  jobs: { startup: string; evidence: string; last_ingest: { candles_inserted: number; at: number; errors: string[] } | null; last_scan: { at: number; count: number } | null; errors: string[] };
  execution: { live_trading_enabled: boolean; paper_trading: boolean; note: string }; kite_setup: string[];
}

const CAP_FLAGS = ["supports_quotes", "supports_streaming", "supports_depth", "supports_oi", "supports_option_chain", "supports_fundamentals", "supports_corporate_actions", "supports_news", "supports_orders"];

interface KiteStatus {
  keys_saved: boolean; api_key_hint: string | null; logged_in: boolean; token_created: string | null;
  missing: string[]; login_url: string | null; redirect_url_hint: string;
}

function ZerodhaConnect() {
  const qc = useQueryClient();
  const st = useQuery({ queryKey: ["kite-status"], queryFn: () => api.get<KiteStatus>("/integrations/kite"), refetchInterval: 15_000 });
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [editing, setEditing] = useState(false);
  const saveKeys = useMutation({
    mutationFn: () => api.post<KiteStatus>("/integrations/kite/keys", { api_key: apiKey.trim(), api_secret: apiSecret.trim() }),
    onSuccess: () => { setApiKey(""); setApiSecret(""); setEditing(false); qc.invalidateQueries(); },
  });
  const logout = useMutation({ mutationFn: () => api.delete("/integrations/kite/session"), onSuccess: () => qc.invalidateQueries() });
  const s = st.data;
  if (!s) return <Panel title="Zerodha Kite Connect"><Loading /></Panel>;
  const showForm = !s.keys_saved || editing;
  return (
    <Panel title="Live market data — Zerodha Kite Connect (official, read-only)"
      right={<DataLabel label={s.logged_in ? "LIVE" : "UNAVAILABLE"} />}>
      <div className="space-y-3 text-xs">
        {s.logged_in ? (
          <div className="rounded border border-emerald-800 bg-emerald-950/30 p-2 text-emerald-200">
            Connected to Zerodha (logged in {istTime(s.token_created)} IST). Zerodha resets sessions daily at ~06:00 IST — log in again each trading day.
          </div>
        ) : s.missing.length > 0 && (
          <div className="rounded border border-amber-700 bg-amber-950/30 p-2 text-amber-200">{s.missing.join("; ")}</div>
        )}

        {showForm ? (
          <form className="space-y-2" onSubmit={(e) => { e.preventDefault(); saveKeys.mutate(); }}>
            <div className="text-slate-300">Step 1 — paste your Kite Connect app's credentials (from developers.kite.trade → your app). They are stored only on the server (backend/.env); the secret is never shown again.</div>
            <div className="flex flex-wrap gap-2">
              <input className="input w-56" placeholder="API key" autoComplete="off" value={apiKey} onChange={(e) => setApiKey(e.target.value)} />
              <input className="input w-72" type="password" placeholder="API secret" autoComplete="off" value={apiSecret} onChange={(e) => setApiSecret(e.target.value)} />
              <button className="btn btn-primary" disabled={apiKey.trim().length < 6 || apiSecret.trim().length < 6 || saveKeys.isPending}>
                {saveKeys.isPending ? "Saving…" : "Save keys"}</button>
              {s.keys_saved && <button type="button" className="btn" onClick={() => setEditing(false)}>Cancel</button>}
            </div>
            {saveKeys.error && <ErrorBox error={saveKeys.error} />}
          </form>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-slate-400">API key {s.api_key_hint} saved.</span>
            {s.login_url && (
              <a className="btn btn-primary" href={s.login_url}>{s.logged_in ? "Re-login with Zerodha" : "Step 2 — Login with Zerodha"}</a>
            )}
            {s.logged_in && <button className="btn btn-danger" disabled={logout.isPending} onClick={() => logout.mutate()}>Logout</button>}
            <button className="btn" onClick={() => setEditing(true)}>Change keys</button>
          </div>
        )}
        <ul className="list-disc pl-4 text-[11px] text-slate-500 space-y-0.5">
          <li>{s.redirect_url_hint} (exactly, no trailing path). After you sign in on kite.zerodha.com you'll be sent back here automatically.</li>
          <li>Your Kite Connect plan must include market data / historical data; otherwise Zerodha returns a permission error, shown here.</li>
          <li>Read-only: this app never calls Zerodha's order APIs and never asks for your Kite password, PIN or TOTP.</li>
        </ul>
      </div>
    </Panel>
  );
}

const SHOWN_PROVIDERS = ["kite", "sample"];

function ProviderSelector({ providers, active }: { providers: Provider[]; active: string }) {
  const qc = useQueryClient();
  const m = useMutation({ mutationFn: (name: string) => api.post("/integrations/provider", { name }), onSuccess: () => qc.invalidateQueries() });
  return (
    <Panel title="Active market-data source">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        {providers.map((p) => (
          <button key={p.name} className={clsx("btn", p.name === active && "btn-primary")} disabled={!p.enabled || p.name === active || m.isPending}
            title={p.enabled ? "" : "Needs credentials"} onClick={() => m.mutate(p.name)}>
            {p.display_name}{p.name === active ? " — active" : !p.enabled ? " (needs credentials)" : ""}
          </button>
        ))}
      </div>
      {m.error && <div className="mt-2"><ErrorBox error={m.error} /></div>}
      <div className="mt-2 text-[10px] text-slate-500">Switching reloads history from the new source in the background; signals from different sources are kept separate.</div>
    </Panel>
  );
}

interface RealtimeHealthResp {
  feed: { source: string | null; state: string; connected_since?: string | null; last_reconnect?: string | null; reconnects?: number; last_error?: string | null; subscribed?: Record<string, number>; subscribed_total?: number };
  mode: string; market_open: boolean; clients: number; instruments_with_ticks: number; registered: number;
  last_tick_age_s: number | null; stale_threshold_s: number; stale_symbols: string[];
  ticks_per_sec: number; exchange_to_server_p50_s: number | null; exchange_to_server_p95_s: number | null;
  tick_to_screen_p50_s: number | null; tick_to_screen_p95_s: number | null; latency_note: string;
  historical_api: { calls_total: Record<string, number>; historical_last_60s: number; quote_last_60s: number; limits: string } | null;
}

function RealtimeHealth() {
  const q = useQuery({ queryKey: ["realtime-health"], queryFn: () => api.get<RealtimeHealthResp>("/realtime/health"), refetchInterval: 3_000 });
  const h = q.data;
  if (!h) return null;
  const ok = h.feed.state === "connected" || h.feed.state === "replaying";
  const s = (v: number | null) => (v == null ? "—" : `${num(v, 2)} s`);
  return (
    <Panel title="Real-time feed health (WebSocket)" right={<span className={clsx("font-semibold", ok ? "text-up" : "text-amber-300")}>{h.feed.source ?? "off"} · {h.feed.state}</span>}>
      <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-8 gap-3 text-xs">
        <div><div className="label">Ticks / s</div><div className="num text-slate-100">{num(h.ticks_per_sec, 1)}</div></div>
        <div><div className="label">Exchange→server p50 / p95</div><div className="num text-slate-100">{s(h.exchange_to_server_p50_s)} / {s(h.exchange_to_server_p95_s)}</div></div>
        <div><div className="label">Tick→screen p50 / p95</div><div className="num text-slate-100">{s(h.tick_to_screen_p50_s)} / {s(h.tick_to_screen_p95_s)}</div></div>
        <div><div className="label">Last tick</div><div className="num text-slate-100">{h.last_tick_age_s == null ? "—" : `${num(h.last_tick_age_s, 1)} s ago`}</div></div>
        <div><div className="label">Subscribed</div><div className="num text-slate-100">{h.feed.subscribed_total ?? 0} <span className="text-slate-500">({Object.entries(h.feed.subscribed ?? {}).map(([k, v]) => `${k} ${v}`).join(", ")})</span></div></div>
        <div><div className="label">With ticks / registered</div><div className="num text-slate-100">{h.instruments_with_ticks} / {h.registered}</div></div>
        <div><div className="label">Reconnects</div><div className="num text-slate-100">{h.feed.reconnects ?? 0}</div></div>
        <div><div className="label">Browser clients</div><div className="num text-slate-100">{h.clients}</div></div>
      </div>
      <div className="mt-2 space-y-0.5 text-[11px] text-slate-400">
        <div>{h.mode}{h.feed.connected_since ? ` · connected since ${h.feed.connected_since}` : ""}{h.feed.last_reconnect ? ` · last reconnect ${h.feed.last_reconnect}` : ""}</div>
        {h.feed.last_error && <div className="text-amber-300">Last error: {h.feed.last_error}</div>}
        {h.market_open && h.stale_symbols.length > 0 && <div className="text-red-300">STALE (&gt; {h.stale_threshold_s} s without a tick): {h.stale_symbols.join(", ")}</div>}
        {!h.market_open && <div>Market closed — no ticks expected; prices show the last traded / official close.</div>}
        {h.historical_api && <div>Historical REST API: {h.historical_api.historical_last_60s} candle calls and {h.historical_api.quote_last_60s} quote calls in the last 60 s · totals {Object.entries(h.historical_api.calls_total).map(([k, v]) => `${k} ${v}`).join(", ") || "0"} · {h.historical_api.limits}</div>}
        <div className="text-slate-500">{h.latency_note}</div>
      </div>
    </Panel>
  );
}

export default function IntegrationsPage() {
  const q = useQuery({ queryKey: ["integrations"], queryFn: () => api.get<Integrations>("/integrations"), refetchInterval: 30_000 });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const d = q.data!;
  const epoch = (s?: number) => (s ? istTime(new Date(s * 1000).toISOString()) : "—");
  return (
    <div className="space-y-3">
      <h1 className="text-lg font-semibold">Data & Integration Settings</h1>
      {d.fallback_note && <div className="rounded border border-amber-700 bg-amber-950/30 p-2 text-xs text-amber-200">{d.fallback_note}</div>}
      <ProviderSelector providers={d.providers.filter((p) => SHOWN_PROVIDERS.includes(p.name))} active={d.active_provider} />
      <ZerodhaConnect />
      <RealtimeHealth />
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
        {d.providers.filter((p) => SHOWN_PROVIDERS.includes(p.name)).map((p) => (
          <Panel key={p.name} title={p.display_name} right={<span className="flex items-center gap-2">{p.active && <Pill className="text-sky-300">ACTIVE</Pill>}<DataLabel label={p.health.status} /></span>}>
            <div className="space-y-2 text-xs">
              <div className="flex items-center gap-2"><span className={clsx("h-2 w-2 rounded-full", p.health.connected ? "bg-emerald-500" : "bg-red-500")} />
                <span className="text-slate-300">{p.health.message}</span></div>
              <div className="text-slate-400">Latency: {p.health.latency_ms === null ? "—" : `${num(p.health.latency_ms, 0)} ms`} · Last update: {istTime(p.health.last_update)}</div>
              <div className="text-slate-400">Timeframes: {p.capabilities.timeframes.join(", ")} · Exchanges: {p.capabilities.exchanges.join(", ")} · Types: {p.capabilities.instrument_types.join(", ")}</div>
              <div className="text-slate-400">History: {p.capabilities.history_note}</div>
              <div className="flex flex-wrap gap-1">{CAP_FLAGS.map((f) => (
                <span key={f} className={clsx("rounded border px-1.5 py-0.5 text-[10px]", p.capabilities[f] ? "border-emerald-700 text-emerald-300" : "border-ink-600 text-slate-500 line-through")}>{f.replace("supports_", "")}</span>))}</div>
              {p.capabilities.missing_fields.length > 0 && <div className="text-slate-500">Missing fields: {p.capabilities.missing_fields.join(", ")}</div>}
              {p.capabilities.requires.length > 0 && <div className="text-slate-500">Subscription dependencies: {p.capabilities.requires.join("; ")}</div>}
              {p.missing_requirements.length > 0 && <div className="text-amber-300">Not enabled: {p.missing_requirements.join("; ")}</div>}
              {p.capabilities.needs_verification.length > 0 && (
                <div><div className="label">Capabilities that must be verified with the account</div>
                  <ul className="list-disc pl-4 text-slate-400">{p.capabilities.needs_verification.map((n, i) => <li key={i}>{n}</li>)}</ul></div>)}
              {p.health.errors.length > 0 && <div><div className="label">Recent API errors</div><ul className="text-red-300">{p.health.errors.map((e, i) => <li key={i}>{e}</li>)}</ul></div>}
            </div>
          </Panel>
        ))}
      </div>

      <Panel title="Instrument coverage matrix (what is actually stored)" bodyClass="p-0 overflow-auto">
        <table className="tbl">
          <thead><tr><th>Provider</th><th>Exchange</th><th>Type</th><th>TF</th><th className="text-right">Instruments</th><th className="text-right">Bars</th><th>History start</th><th>History end</th><th>Status</th><th>Latency</th><th>Missing fields</th></tr></thead>
          <tbody>{d.coverage.filter((c) => SHOWN_PROVIDERS.includes(c.provider)).map((c, i) => (
            <tr key={i}><td>{c.provider}{c.active && <span className="ml-1 text-sky-300">●</span>}</td><td>{c.exchange}</td><td>{c.instrument_type}</td><td>{c.timeframe}</td>
              <td className="num text-right">{c.instruments}</td><td className="num text-right">{c.bars.toLocaleString("en-IN")}</td>
              <td>{istTime(c.history_start)}</td><td>{istTime(c.history_end)}</td><td><DataLabel label={c.status} /></td>
              <td className="num">{c.latency_ms === null ? "—" : `${num(c.latency_ms, 0)} ms`}</td>
              <td className="whitespace-normal text-[10px] text-slate-500 max-w-md">{c.missing_fields.join(", ")}</td></tr>))}</tbody>
        </table>
      </Panel>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-3">
        <Panel title="Not connected in this build">
          <div className="space-y-2">{d.not_connected.map((n) => <NotConnected key={n.name} title={n.name} reason={n.reason} />)}</div>
        </Panel>
        <Panel title="Zerodha Kite Connect setup (read-only)">
          <ol className="list-decimal pl-4 text-xs text-slate-300 space-y-1">{d.kite_setup.map((s, i) => <li key={i}>{s}</li>)}</ol>
          <div className="mt-2 text-[10px] text-slate-500">Credentials are read from environment variables on the server only and are never sent to the browser or logged.</div>
        </Panel>
        <Panel title="Execution, AI & jobs">
          <div className="space-y-1 text-xs">
            <div>Live trading: <span className="font-bold text-red-300">{d.execution.live_trading_enabled ? "ENABLED" : "DISABLED"}</span> — {d.execution.note}</div>
            <div>Paper trading: <span className="text-emerald-300">{d.execution.paper_trading ? "enabled" : "off"}</span></div>
            <div>AI provider: {d.llm.active_provider}{d.llm.model ? ` (${d.llm.model})` : ""} — {d.llm.note}</div>
            <div className="pt-2 label">Background jobs</div>
            <div>Startup: {d.jobs.startup} · Walk-forward evidence: {d.jobs.evidence}</div>
            <div>Last ingest: {epoch(d.jobs.last_ingest?.at)} ({d.jobs.last_ingest?.candles_inserted ?? 0} new bars)</div>
            <div>Last scan: {epoch(d.jobs.last_scan?.at)} ({d.jobs.last_scan?.count ?? 0} candidates)</div>
            {d.jobs.errors.length > 0 && <ul className="text-red-300">{d.jobs.errors.map((e, i) => <li key={i}>{e}</li>)}</ul>}
            {(d.jobs.last_ingest?.errors ?? []).length > 0 && <ul className="text-red-300">{d.jobs.last_ingest!.errors.map((e, i) => <li key={i}>{e}</li>)}</ul>}
          </div>
        </Panel>
      </div>
    </div>
  );
}
