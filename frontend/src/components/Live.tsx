import clsx from "clsx";
import { useEffect, useRef, useState } from "react";
import { STALE_MS, live, tickAgeMs, useClock, useLiveTick } from "../lib/live";
import { inr, pct, tone } from "../lib/format";

/** Live last price with Kite-style green/red flash on change. Falls back to `fallback` until the first tick. */
export function LivePrice({ exchange, symbol, fallback, className, showChange = false, digits = 2 }: {
  exchange: string; symbol: string; fallback?: number | null; className?: string; showChange?: boolean; digits?: number;
}) {
  const t = useLiveTick(exchange, symbol);
  const [flash, setFlash] = useState<"up" | "down" | null>(null);
  const last = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (!t) return;
    if (last.current !== undefined && t.lp !== last.current) {
      setFlash(t.lp > last.current ? "up" : "down");
      const id = setTimeout(() => setFlash(null), 450);
      last.current = t.lp;
      return () => clearTimeout(id);
    }
    last.current = t.lp;
  }, [t]);
  const price = t?.lp ?? fallback ?? null;
  return (
    <span className={clsx("num rounded px-0.5 transition-colors duration-300",
      flash === "up" && "bg-emerald-500/30", flash === "down" && "bg-red-500/30", className)}>
      {inr(price, digits)}
      {showChange && t?.chp != null && <span className={clsx("ml-1 text-[0.85em]", tone(t.chp))}>{pct(t.chp)}</span>}
    </span>
  );
}

export function LiveChange({ exchange, symbol, fallback }: { exchange: string; symbol: string; fallback?: number | null }) {
  const t = useLiveTick(exchange, symbol);
  const v = t?.chp ?? fallback ?? null;
  return <span className={clsx("num", tone(v))}>{pct(v)}</span>;
}

/** Red STALE badge when no tick for > 5 s while the feed should be streaming (market hours). */
export function StaleBadge({ exchange, symbol, marketOpen }: { exchange: string; symbol: string; marketOpen: boolean }) {
  useClock(1000);
  const t = useLiveTick(exchange, symbol);
  const age = tickAgeMs(t);
  const streaming = live.connected && (live.status?.state === "connected" || live.status?.state === "replaying");
  if (!marketOpen || !streaming) return null;
  if (age === null || age > STALE_MS) {
    return <span className="ml-1 rounded border border-red-700 bg-red-900/40 px-1 text-[9px] font-bold text-red-300"
      title={age === null ? "No tick received" : `Last tick ${(age / 1000).toFixed(1)} s ago`}>STALE</span>;
  }
  return null;
}

export function TickAge({ exchange, symbol }: { exchange: string; symbol: string }) {
  useClock(1000);
  const age = tickAgeMs(useLiveTick(exchange, symbol));
  if (age === null) return <span className="text-slate-600">—</span>;
  return <span className={clsx("num", age > STALE_MS ? "text-red-400" : "text-slate-400")}>{(age / 1000).toFixed(1)}s</span>;
}
