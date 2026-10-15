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

function UpstoxConnect({ provider }: { provider?: Provider }) {
  const qc = useQueryClient();
  const [token, setToken] = useState("");
  const save = useMutation({
    mutationFn: () => api.post<{ message: string; token_hint: string }>("/integrations/upstox/token", { token }),
    onSuccess: () => { setToken(""); qc.invalidateQueries(); },
  });
  const remove = useMutation({ mutationFn: () => api.delete("/integrations/upstox/token"), onSuccess: () => qc.invalidateQueries() });
  const connected = !!provider?.enabled;
  return (
    <Panel title="Live market data — Upstox (real-time, read-only)" right={<DataLabel label={connected ? (provider?.health.connected ? "LIVE" : "UNAVAILABLE") : "UNAVAILABLE"} />}>
      {connected ? (
        <div className="space-y-2 text-xs">
          <div className={provider?.health.connected ? "text-emerald-300" : "text-red-300"}>{provider?.health.message}</div>
          <div className="text-slate-400">The token is stored only on the server (backend/.env). It is read-only and cannot place orders.</div>
          <button className="btn btn-danger" disabled={remove.isPending} onClick={() => remove.mutate()}>Disconnect (switch back to SIMULATED data)</button>
        </div>
      ) : (
        <div className="space-y-3 text-xs">
          <div className="text-slate-300">Real-time NSE/BSE prices, candles and holidays via Upstox's free, read-only <b>Analytics Token</b> (valid 1 year, no daily login, cannot trade).</div>
          <ol className="list-decimal pl-4 space-y-1 text-slate-300">
            <li>Open a free Upstox account if you don't have one (<a className="text-sky-300 underline" href="https://upstox.com/open-demat-account/" target="_blank" rel="noreferrer">upstox.com</a>).</li>
            <li>Go to <a className="text-sky-300 underline" href="https://account.upstox.com/developer/apps#analytics" target="_blank" rel="noreferrer">account.upstox.com/developer/apps → Analytics</a>.</li>
            <li>Click <b>Generate Token</b> → <b>Confirm</b>, then copy the full token.</li>
            <li>Paste it below and click Connect. It is validated with one read-only quote call, then stored server-side only.</li>
          </ol>
          <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
            <input className="input flex-1" type="password" autoComplete="off" placeholder="Paste Upstox Analytics Token" value={token} onChange={(e) => setToken(e.target.value)} />
            <button className="btn btn-primary" disabled={token.trim().length < 20 || save.isPending}>{save.isPending ? "Validating…" : "Connect"}</button>
          </form>
          {save.error && <ErrorBox error={save.error} />}
          {save.isSuccess && <div className="text-emerald-300">{save.data.message}</div>}
          <div className="text-[10px] text-slate-500">Never share this token. Your Upstox password/PIN/TOTP is never requested by this app.</div>
        </div>
      )}
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
      <ProviderSelector providers={d.providers} active={d.active_provider} />
      <UpstoxConnect provider={d.providers.find((p) => p.name === "upstox")} />
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
        {d.providers.map((p) => (
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
          <tbody>{d.coverage.map((c, i) => (
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
