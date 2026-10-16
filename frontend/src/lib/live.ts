// Live market data client: ONE WebSocket per browser tab to the backend (/api/ws). The backend holds the single
// Kite WebSocket connection and pushes only changed symbols ~4x per second. Nothing here polls for prices.
import { useEffect, useSyncExternalStore } from "react";

export interface LiveTick {
  e: string; s: string; lp: number; pc: number | null; o: number | null; h: number | null; l: number | null;
  v: number | null; et: number | null; rt: number; ch: number | null; chp: number | null; src: string;
  prev?: number; // previous ltp seen by this client (for flash direction)
  seenAt: number; // client receive time (ms)
}
export interface LiveBar { ts: number; open: number; high: number; low: number; close: number; volume: number; partial: boolean }
export interface FeedStatus { feed: string | null; state: string; mode: string; last_tick_ts: number | null; stale_after_s: number }

export const STALE_MS = 5000;
type Listener = () => void;
type BarListener = (bar: LiveBar, closed: boolean) => void;

class LiveFeed {
  ticks = new Map<string, LiveTick>();
  status: FeedStatus | null = null;
  connected = false;
  version = 0;
  private ws: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private keyListeners = new Map<string, Set<Listener>>();
  private barListeners = new Set<{ key: string; fn: BarListener; bars?: (b: LiveBar[]) => void }>();
  private dirty = new Set<string>();
  private raf = 0;
  private chartSub: { exchange: string; symbol: string; tf: string } | null = null;
  private latencySamples: number[] = [];
  private retry = 0;
  private latencyTimer = 0;

  start() {
    if (this.ws) return;
    if (!this.latencyTimer) this.latencyTimer = window.setInterval(() => this.flushLatency(), 5000);
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/api/ws`);
    this.ws = ws;
    ws.onopen = () => {
      this.connected = true; this.retry = 0; this.bump();
      if (this.chartSub) ws.send(JSON.stringify({ op: "chart", ...this.chartSub }));
    };
    ws.onclose = () => {
      this.connected = false; this.ws = null; this.bump();
      const wait = Math.min(1000 * 2 ** this.retry++, 15000);
      setTimeout(() => this.start(), wait);
    };
    ws.onmessage = (ev) => this.onMessage(JSON.parse(ev.data));
  }

  private onMessage(m: { type: string; [k: string]: unknown }) {
    const now = Date.now();
    if (m.type === "snapshot" || m.type === "ticks") {
      for (const t of m.data as LiveTick[]) {
        const key = `${t.e}:${t.s}`;
        const old = this.ticks.get(key);
        this.ticks.set(key, { ...t, prev: old?.lp, seenAt: now });
        this.dirty.add(key);
        if (t.et && m.type === "ticks") this.latencySamples.push(Math.max(0, now / 1000 - t.et));
      }
      if (m.status) this.status = m.status as FeedStatus;
      this.schedule();
    } else if (m.type === "status") {
      this.status = m as unknown as FeedStatus;
      this.bump();
    } else if (m.type === "bar" || m.type === "bars") {
      const key = `${m.e}:${m.s}:${m.tf}`;
      for (const l of this.barListeners) {
        if (l.key !== key) continue;
        if (m.type === "bar") l.fn(m.bar as LiveBar, Boolean(m.closed));
        else l.bars?.(m.bars as LiveBar[]);
      }
    }
  }

  private schedule() {
    if (this.raf) return;
    this.raf = requestAnimationFrame(() => {
      this.raf = 0;
      const keys = [...this.dirty];
      this.dirty.clear();
      this.version++;
      for (const k of keys) this.keyListeners.get(k)?.forEach((l) => l());
      this.listeners.forEach((l) => l());
    });
  }

  private bump() { this.version++; this.listeners.forEach((l) => l()); }

  private flushLatency() {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN || !this.latencySamples.length) return;
    this.ws.send(JSON.stringify({ op: "latency", samples: this.latencySamples.slice(-200) }));
    this.latencySamples = [];
  }

  subscribe(fn: Listener) { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; }
  subscribeKey(key: string, fn: Listener) {
    if (!this.keyListeners.has(key)) this.keyListeners.set(key, new Set());
    this.keyListeners.get(key)!.add(fn);
    return () => { this.keyListeners.get(key)?.delete(fn); };
  }

  watchChart(exchange: string, symbol: string, tf: string, fn: BarListener, bars?: (b: LiveBar[]) => void) {
    const l = { key: `${exchange}:${symbol}:${tf}`, fn, bars };
    this.barListeners.add(l);
    this.chartSub = { exchange, symbol, tf };
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ op: "chart", exchange, symbol, tf }));
    return () => {
      this.barListeners.delete(l);
      if (this.chartSub?.symbol === symbol && this.chartSub.tf === tf) {
        this.chartSub = null;
        if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ op: "unchart" }));
      }
    };
  }
}

export const live = new LiveFeed();

export function useLiveTick(exchange: string | undefined, symbol: string | undefined): LiveTick | undefined {
  const key = `${exchange}:${symbol}`;
  return useSyncExternalStore(
    (fn) => live.subscribeKey(key, fn),
    () => live.ticks.get(key),
  );
}

export function useLiveVersion(): number {
  return useSyncExternalStore((fn) => live.subscribe(fn), () => live.version);
}

/** Re-render every second so tick ages / STALE badges update even when no ticks arrive. */
export function useClock(intervalMs = 1000): number {
  return useSyncExternalStore(
    (fn) => { const id = setInterval(fn, intervalMs); return () => clearInterval(id); },
    () => Math.floor(Date.now() / intervalMs),
  );
}

export function tickAgeMs(t: LiveTick | undefined): number | null {
  return t ? Date.now() - t.rt * 1000 : null;
}

export function useStartLive() {
  useEffect(() => { live.start(); }, []);
}
