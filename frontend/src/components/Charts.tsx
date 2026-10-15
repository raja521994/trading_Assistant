import { useEffect, useRef } from "react";
import {
  ColorType,
  CrosshairMode,
  LineStyle,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type SeriesMarker,
  type Time,
} from "lightweight-charts";

const BASE_OPTIONS = {
  layout: { background: { type: ColorType.Solid, color: "#0b0f15" }, textColor: "#94a3b8", fontSize: 11 },
  grid: { vertLines: { color: "#151b25" }, horzLines: { color: "#151b25" } },
  crosshair: { mode: CrosshairMode.Normal },
  rightPriceScale: { borderColor: "#1e2632" },
  timeScale: { borderColor: "#1e2632" },
};

export interface LinePoint { time: string | number; value: number }
export interface Candle { time: string | number; open: number; high: number; low: number; close: number }
export interface Overlay { name: string; color: string; data: LinePoint[]; dashed?: boolean }
export interface PriceLevel { price: number; color: string; title: string; dashed?: boolean }
export interface Marker { time: string | number; position: "aboveBar" | "belowBar" | "inBar"; color: string; shape: "arrowUp" | "arrowDown" | "circle" | "square"; text?: string }

function useChart(height: number, intraday: boolean) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  useEffect(() => {
    if (!ref.current) return;
    const chart = createChart(ref.current, {
      ...BASE_OPTIONS,
      width: ref.current.clientWidth,
      height,
      timeScale: { ...BASE_OPTIONS.timeScale, timeVisible: intraday, secondsVisible: false },
    });
    chartRef.current = chart;
    const ro = new ResizeObserver(() => {
      if (ref.current) chart.applyOptions({ width: ref.current.clientWidth });
    });
    ro.observe(ref.current);
    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
    };
  }, [height, intraday]);
  return { ref, chartRef };
}

export function CandleChart({ candles, volume, overlays = [], levels = [], markers = [], height = 420, intraday = false }: {
  candles: Candle[]; volume?: LinePoint[]; overlays?: Overlay[]; levels?: PriceLevel[]; markers?: Marker[];
  height?: number; intraday?: boolean;
}) {
  const { ref, chartRef } = useChart(height, intraday);
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const series: ISeriesApi<"Candlestick" | "Histogram" | "Line">[] = [];
    const cs = chart.addCandlestickSeries({
      upColor: "#22c55e", downColor: "#ef4444", borderVisible: false, wickUpColor: "#22c55e", wickDownColor: "#ef4444",
    });
    cs.setData(candles.map((c) => ({ ...c, time: c.time as Time })));
    series.push(cs);
    if (volume?.length) {
      const vs = chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol", color: "#334155" });
      vs.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
      vs.setData(volume.map((v, i) => ({
        time: v.time as Time, value: v.value,
        color: candles[i] && candles[i].close >= candles[i].open ? "rgba(34,197,94,0.35)" : "rgba(239,68,68,0.35)",
      })));
      series.push(vs);
    }
    overlays.forEach((o) => {
      const ls = chart.addLineSeries({
        color: o.color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        lineStyle: o.dashed ? LineStyle.Dashed : LineStyle.Solid, title: o.name,
      });
      ls.setData(o.data.map((p) => ({ time: p.time as Time, value: p.value })));
      series.push(ls);
    });
    levels.forEach((l) =>
      cs.createPriceLine({ price: l.price, color: l.color, lineWidth: 1, lineStyle: l.dashed ? LineStyle.Dashed : LineStyle.Solid, axisLabelVisible: true, title: l.title }),
    );
    if (markers.length) cs.setMarkers(markers.map((m) => ({ ...m, time: m.time as Time })) as SeriesMarker<Time>[]);
    chart.timeScale().fitContent();
    return () => {
      series.forEach((s) => {
        try { chart.removeSeries(s); } catch { /* chart disposed */ }
      });
    };
  }, [chartRef, candles, volume, overlays, levels, markers]);
  return <div ref={ref} className="w-full" />;
}

export function LinesChart({ lines, height = 220, intraday = false, histogram = false }: {
  lines: Overlay[]; height?: number; intraday?: boolean; histogram?: boolean;
}) {
  const { ref, chartRef } = useChart(height, intraday);
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const series: ISeriesApi<"Line" | "Histogram">[] = [];
    lines.forEach((o, idx) => {
      if (histogram && idx === lines.length - 1) {
        const hs = chart.addHistogramSeries({ color: o.color, priceLineVisible: false, lastValueVisible: false });
        hs.setData(o.data.map((p) => ({ time: p.time as Time, value: p.value, color: p.value >= 0 ? "rgba(34,197,94,0.5)" : "rgba(239,68,68,0.5)" })));
        series.push(hs);
        return;
      }
      const ls = chart.addLineSeries({
        color: o.color, lineWidth: 2, priceLineVisible: false, lastValueVisible: true, title: o.name,
        lineStyle: o.dashed ? LineStyle.Dashed : LineStyle.Solid,
      });
      ls.setData(o.data.map((p) => ({ time: p.time as Time, value: p.value })));
      series.push(ls);
    });
    chart.timeScale().fitContent();
    return () => {
      series.forEach((s) => {
        try { chart.removeSeries(s); } catch { /* chart disposed */ }
      });
    };
  }, [chartRef, lines, histogram]);
  return <div ref={ref} className="w-full" />;
}
