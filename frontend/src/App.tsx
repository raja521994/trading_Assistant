import clsx from "clsx";
import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
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
          {label === "SIMULATED" && (
            <div className="bg-amber-500/10 border-b border-amber-700/50 px-4 py-1 text-[11px] text-amber-200">
              SIMULATED DATA — all prices are synthetic sample data, not real market data. Research and paper trading only; no orders are sent to any broker.
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
