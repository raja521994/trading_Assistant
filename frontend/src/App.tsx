import clsx from "clsx";
import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Meta } from "./lib/api";
import { istTime, pct, tone } from "./lib/format";
import { DataLabel } from "./components/ui";
import Overview from "./pages/Overview";
import Scanner from "./pages/Scanner";
import SignalDetail from "./pages/SignalDetail";
import Charts from "./pages/Charts";
import StrategyLab from "./pages/StrategyLab";
import Portfolio from "./pages/Portfolio";
import RiskPerformance from "./pages/RiskPerformance";
import Assistant from "./pages/Assistant";
import Integrations from "./pages/Integrations";

const NAV = [
  { to: "/overview", label: "Market Overview", icon: "◧" },
  { to: "/scanner", label: "AI Scanner", icon: "◎" },
  { to: "/charts", label: "Charts", icon: "▤" },
  { to: "/lab", label: "Strategy Lab", icon: "⚗" },
  { to: "/portfolio", label: "Paper Portfolio", icon: "▣" },
  { to: "/risk", label: "Risk & Performance", icon: "◬" },
  { to: "/assistant", label: "AI Assistant", icon: "✦" },
  { to: "/integrations", label: "Data & Integrations", icon: "⚙" },
];

const NOT_CONNECTED = ["F&O terminal", "Fundamentals", "News & events", "Broker holdings", "ML models"];

interface Tick {
  ts: string;
  market_session: string;
  data_label: string;
  quotes: { symbol: string; last: number; change_pct: number }[];
  jobs: { startup: string; evidence: string };
}

// Zerodha redirects back to the registered URL (http://127.0.0.1:5173) with ?request_token=...&status=success.
// Captured once at load (before routing rewrites the URL) and exchanged server-side for today's access token.
const INITIAL_PARAMS = new URLSearchParams(window.location.search);
let kiteCallbackHandled = false;

function useKiteCallback() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const hasCallback = INITIAL_PARAMS.has("request_token") || INITIAL_PARAMS.get("action") === "login";
  const [state, setState] = useState<{ status: "idle" | "working" | "ok" | "error"; msg?: string }>(
    hasCallback ? { status: "working", msg: "Completing Zerodha login…" } : { status: "idle" });
  useEffect(() => {
    if (!hasCallback || kiteCallbackHandled) return;
    kiteCallbackHandled = true;
    window.history.replaceState({}, "", "/integrations"); // drop the one-time token from the address bar
    const rt = INITIAL_PARAMS.get("request_token");
    if (INITIAL_PARAMS.get("status") !== "success" || !rt) {
      setState({ status: "error", msg: "Zerodha login was cancelled or did not succeed." });
      navigate("/integrations", { replace: true });
      return;
    }
    api.post<{ message: string; user_name?: string }>("/integrations/kite/session", { request_token: rt })
      .then((r) => { setState({ status: "ok", msg: `${r.user_name ? r.user_name + ": " : ""}${r.message}` }); qc.invalidateQueries(); })
      .catch((e: Error) => setState({ status: "error", msg: e.message }))
      .finally(() => navigate("/integrations", { replace: true }));
  }, [hasCallback, navigate, qc]);
  return state;
}

function useTicker() {
  const [tick, setTick] = useState<Tick | null>(null);
  const [connected, setConnected] = useState(false);
  useEffect(() => {
    const es = new EventSource("/api/stream");
    es.addEventListener("tick", (e) => {
      setConnected(true);
      setTick(JSON.parse((e as MessageEvent).data));
    });
    es.onerror = () => setConnected(false);
    return () => es.close();
  }, []);
  return { tick, connected };
}

export default function App() {
  const meta = useQuery({ queryKey: ["meta"], queryFn: () => api.get<Meta>("/meta"), refetchInterval: 30_000 });
  const { tick, connected } = useTicker();
  const kite = useKiteCallback();
  const label = tick?.data_label ?? meta.data?.data_label ?? "…";
  const headline = (tick?.quotes ?? []).filter((q) => ["NIFTY 50", "NIFTY BANK", "NIFTY FIN SERVICE", "SENSEX"].includes(q.symbol));

  return (
    <div className="flex h-full">
      <aside className="hidden lg:flex w-52 shrink-0 border-r border-ink-700 bg-ink-900 flex-col">
        <div className="px-4 py-3 border-b border-ink-700">
          <div className="text-sm font-bold tracking-wide text-slate-100">AI Trading Desk</div>
          <div className="text-[10px] text-slate-500">NSE / BSE · research & paper trading</div>
        </div>
        <nav className="flex-1 py-2">
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to}
              className={({ isActive }) => clsx("flex items-center gap-2 px-4 py-2 text-xs",
                isActive ? "bg-ink-800 text-sky-300 border-l-2 border-sky-400" : "text-slate-400 hover:text-slate-200 hover:bg-ink-850 border-l-2 border-transparent")}>
              <span className="w-4 text-center">{n.icon}</span>{n.label}
            </NavLink>
          ))}
          <div className="mt-4 px-4">
            <div className="label mb-1">Not connected</div>
            {NOT_CONNECTED.map((n) => (
              <div key={n} className="py-0.5 text-[11px] text-slate-600" title="No provider configured — see Data & Integrations">{n}</div>
            ))}
          </div>
        </nav>
        <div className="border-t border-ink-700 p-3 text-[10px] text-slate-500 space-y-1">
          <div>Live orders: <span className="text-red-300 font-semibold">DISABLED</span></div>
          <div>AI: {meta.data?.llm.active_provider ?? "…"}</div>
          <div>v{meta.data?.version ?? ""}</div>
        </div>
      </aside>

      <div className="flex-1 flex flex-col min-w-0">
        <header className="border-b border-ink-700 bg-ink-900">
          {kite.status !== "idle" && (
            <div className={clsx("border-b px-4 py-1 text-[11px]",
              kite.status === "error" ? "bg-red-500/10 border-red-800 text-red-200" :
              kite.status === "ok" ? "bg-emerald-500/10 border-emerald-800 text-emerald-200" : "bg-sky-500/10 border-sky-800 text-sky-200")}>
              Zerodha: {kite.msg}
            </div>
          )}
          {label === "SIMULATED" && (
            <div className="bg-amber-500/10 border-b border-amber-700/50 px-4 py-1 text-[11px] text-amber-200">
              SIMULATED DATA — Zerodha live data is not connected (or today's session has expired), so all prices are synthetic sample data.{" "}
              <NavLink to="/integrations" className="underline font-semibold">Link / login with Zerodha →</NavLink>
            </div>
          )}
          {label === "DELAYED" && meta.data?.jobs.startup === "done" && (
            <div className="bg-sky-500/5 border-b border-sky-800/50 px-4 py-1 text-[11px] text-sky-200/90">
              REAL market data from {meta.data?.provider_name} (may be delayed). Research & paper trading only.
            </div>
          )}
          {meta.data && label !== "SIMULATED" && meta.data.jobs.startup !== "done" && (
            <div className="bg-sky-500/10 border-b border-sky-700/50 px-4 py-1 text-[11px] text-sky-200">
              Loading live market history from {meta.data?.provider_name ?? "provider"}… pages fill in automatically (about a minute on first connect).
            </div>
          )}
          <nav className="lg:hidden flex overflow-x-auto border-b border-ink-700 px-2">
            {NAV.map((n) => (
              <NavLink key={n.to} to={n.to}
                className={({ isActive }) => clsx("whitespace-nowrap px-2.5 py-2 text-xs", isActive ? "text-sky-300 border-b-2 border-sky-400" : "text-slate-400")}>
                {n.label}
              </NavLink>
            ))}
            <span className="whitespace-nowrap px-2.5 py-2 text-[10px] text-red-300">Live orders DISABLED</span>
          </nav>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2 text-xs">
            <DataLabel label={label} />
            <span className="text-slate-400">Market: <span className="text-slate-200">{tick?.market_session ?? meta.data?.market_session ?? "…"}</span></span>
            {headline.map((q) => (
              <span key={q.symbol} className="whitespace-nowrap">
                <span className="text-slate-400">{q.symbol}</span>{" "}
                <span className="num text-slate-100">{q.last.toLocaleString("en-IN", { maximumFractionDigits: 2 })}</span>{" "}
                <span className={clsx("num", tone(q.change_pct))}>{pct(q.change_pct)}</span>
              </span>
            ))}
            <span className="ml-auto flex items-center gap-3 whitespace-nowrap text-slate-500">
              <span>Evidence: {tick?.jobs.evidence ?? meta.data?.jobs.evidence ?? "…"}</span>
              <span className="flex items-center gap-1"><span className={clsx("h-2 w-2 rounded-full", connected ? "bg-emerald-500" : "bg-red-500")} />stream</span>
              <span className="num">{istTime(tick?.ts ?? meta.data?.server_time_ist)} IST</span>
            </span>
          </div>
        </header>
        <main className="flex-1 overflow-auto p-2 sm:p-4">
          <Routes>
            <Route path="/" element={<Navigate to="/overview" replace />} />
            <Route path="/overview" element={<Overview />} />
            <Route path="/scanner" element={<Scanner />} />
            <Route path="/signals/:id" element={<SignalDetail />} />
            <Route path="/charts" element={<Charts />} />
            <Route path="/charts/:exchange/:symbol" element={<Charts />} />
            <Route path="/lab" element={<StrategyLab />} />
            <Route path="/portfolio" element={<Portfolio />} />
            <Route path="/risk" element={<RiskPerformance />} />
            <Route path="/assistant" element={<Assistant />} />
            <Route path="/integrations" element={<Integrations />} />
            <Route path="*" element={<Navigate to="/overview" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
