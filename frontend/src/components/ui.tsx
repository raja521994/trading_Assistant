import clsx from "clsx";
import type { ReactNode } from "react";
import type { Action, SignalState } from "../lib/api";
import { ApiError } from "../lib/api";

export function Panel({ title, right, children, className, bodyClass }: {
  title?: ReactNode; right?: ReactNode; children: ReactNode; className?: string; bodyClass?: string;
}) {
  return (
    <section className={clsx("panel flex flex-col min-w-0", className)}>
      {title !== undefined && (
        <div className="panel-h">
          <span>{title}</span>
          <span className="normal-case tracking-normal font-normal">{right}</span>
        </div>
      )}
      <div className={clsx("p-3 min-w-0", bodyClass)}>{children}</div>
    </section>
  );
}

export function Stat({ label, value, sub, valueClass }: { label: string; value: ReactNode; sub?: ReactNode; valueClass?: string }) {
  return (
    <div className="min-w-0">
      <div className="label">{label}</div>
      <div className={clsx("num text-sm font-semibold truncate", valueClass)}>{value}</div>
      {sub !== undefined && <div className="text-[11px] text-slate-500 truncate">{sub}</div>}
    </div>
  );
}

const ACTION_STYLE: Record<string, string> = {
  BUY: "bg-emerald-600/20 text-emerald-300 border-emerald-700",
  SELL: "bg-red-600/20 text-red-300 border-red-700",
  WAIT: "bg-amber-600/15 text-amber-300 border-amber-700/70",
  "NO TRADE": "bg-slate-600/20 text-slate-300 border-slate-600",
};
const STATE_STYLE: Record<string, string> = {
  WATCH: "text-slate-300 border-slate-600",
  SETUP: "text-sky-300 border-sky-700",
  TRIGGERED: "text-emerald-300 border-emerald-700",
  ACTIVE: "bg-emerald-900/40 text-emerald-200 border-emerald-600",
  INVALIDATED: "text-red-300 border-red-800",
  EXITED: "text-violet-300 border-violet-700",
  EXPIRED: "text-slate-400 border-slate-700",
};

export function ActionBadge({ action }: { action: Action | string }) {
  return <span className={clsx("inline-block rounded border px-1.5 py-0.5 text-[10px] font-bold tracking-wide", ACTION_STYLE[action] ?? ACTION_STYLE["NO TRADE"])}>{action}</span>;
}

export function StateBadge({ state }: { state: SignalState | string }) {
  return <span className={clsx("inline-block rounded border px-1.5 py-0.5 text-[10px] font-semibold", STATE_STYLE[state] ?? STATE_STYLE.EXPIRED)}>{state}</span>;
}

export function DataLabel({ label }: { label: string }) {
  const style =
    label === "LIVE" ? "border-emerald-700 text-emerald-300" :
    label === "DELAYED" ? "border-sky-700 text-sky-300 bg-sky-500/10" :
    label === "SIMULATED" ? "border-amber-600 text-amber-300 bg-amber-500/10" :
    label === "UNAVAILABLE" ? "border-red-800 text-red-300" : "border-slate-600 text-slate-300";
  return <span className={clsx("inline-block rounded border px-1.5 py-0.5 text-[10px] font-bold tracking-wider", style)}>{label}</span>;
}

export function Pill({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={clsx("inline-block rounded bg-ink-700 px-1.5 py-0.5 text-[10px] text-slate-300", className)}>{children}</span>;
}

export function Loading({ what = "Loading" }: { what?: string }) {
  return <div className="p-6 text-xs text-slate-500 animate-pulse">{what}…</div>;
}

export function ErrorBox({ error }: { error: unknown }) {
  const msg = error instanceof ApiError ? error.message : error instanceof Error ? error.message : String(error);
  return <div className="rounded border border-red-800 bg-red-950/40 p-3 text-xs text-red-200">Error: {msg}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="p-4 text-xs text-slate-500">{children}</div>;
}

export function NotConnected({ title, reason }: { title: string; reason: string }) {
  return (
    <div className="rounded border border-dashed border-ink-600 p-3 text-xs">
      <div className="flex items-center gap-2"><span className="font-semibold text-slate-300">{title}</span><DataLabel label="UNAVAILABLE" /></div>
      <div className="mt-1 text-slate-500">{reason}</div>
    </div>
  );
}

export function Spark({ values, width = 90, height = 24 }: { values: number[]; width?: number; height?: number }) {
  if (!values || values.length < 2) return null;
  const min = Math.min(...values), max = Math.max(...values);
  const span = max - min || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * width},${height - ((v - min) / span) * height}`).join(" ");
  const up = values[values.length - 1] >= values[0];
  return (
    <svg width={width} height={height} className="overflow-visible">
      <polyline fill="none" stroke={up ? "#22c55e" : "#ef4444"} strokeWidth="1.2" points={pts} />
    </svg>
  );
}

export function ScoreBar({ score }: { score: number }) {
  const color = score >= 65 ? "bg-emerald-500" : score >= 50 ? "bg-sky-500" : "bg-amber-500";
  return (
    <div className="flex items-center gap-1.5">
      <div className="h-1.5 w-14 rounded bg-ink-700"><div className={clsx("h-1.5 rounded", color)} style={{ width: `${Math.max(2, Math.min(100, score))}%` }} /></div>
      <span className="num text-[11px]">{score.toFixed(1)}</span>
    </div>
  );
}

export function KV({ k, v, mono = true }: { k: ReactNode; v: ReactNode; mono?: boolean }) {
  return (
    <div className="flex justify-between gap-3 py-0.5 text-xs">
      <span className="text-slate-500">{k}</span>
      <span className={clsx("text-right text-slate-200", mono && "num")}>{v}</span>
    </div>
  );
}

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <div className="label mb-1">{title}</div>
      {children}
    </div>
  );
}

export function BulletList({ items, empty = "None", tone }: { items: string[]; empty?: string; tone?: "good" | "bad" | "neutral" }) {
  if (!items?.length) return <div className="text-xs text-slate-500">{empty}</div>;
  const dot = tone === "good" ? "bg-emerald-500" : tone === "bad" ? "bg-red-500" : "bg-slate-500";
  return (
    <ul className="space-y-1 text-xs">
      {items.map((t, i) => (
        <li key={i} className="flex gap-2"><span className={clsx("mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full", dot)} /><span className="text-slate-300">{t}</span></li>
      ))}
    </ul>
  );
}
