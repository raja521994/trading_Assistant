import clsx from "clsx";
import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type Recommendation } from "../lib/api";
import { crore, inr, istTime, newIdempotencyKey, num, pct, titleCase } from "../lib/format";
import { ActionBadge, BulletList, DataLabel, KV, Pill, ScoreBar, Section, StateBadge } from "./ui";
import { LivePrice } from "./Live";

export function ScoreBreakdown({ rec }: { rec: Recommendation }) {
  const sb = rec.score_breakdown;
  return (
    <div>
      <table className="tbl">
        <thead><tr><th>Component</th><th className="text-right">Value</th><th className="text-right">Weight</th><th>Detail</th></tr></thead>
        <tbody>
          {Object.entries(sb.components).map(([k, c]) => (
            <tr key={k} className={clsx(!sb.used.includes(k) && "opacity-50")}>
              <td>{titleCase(k)}</td>
              <td className={clsx("num text-right", c.value === null ? "text-slate-500" : c.value > 0 ? "text-up" : c.value < 0 ? "text-down" : "")}>{c.value === null ? "n/a" : num(c.value)}</td>
              <td className="num text-right">{num(sb.weights[k] ?? 0)}</td>
              <td className="whitespace-normal text-slate-400">{c.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="mt-1 text-[10px] text-slate-500">{sb.note}</div>
    </div>
  );
}

export function PaperTradeBox({ rec }: { rec: Recommendation }) {
  const qc = useQueryClient();
  const [key] = useState(newIdempotencyKey);
  const [confirming, setConfirming] = useState(false);
  const validated = rec.action === "BUY" || rec.action === "SELL";
  const m = useMutation({
    mutationFn: () => api.post<{ trade: { id: number; qty: number; entry_price: number }; note: string }>("/paper/orders", {
      signal_id: rec.signal_id, idempotency_key: key, acknowledge_unvalidated: !validated,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["paper"] });
      qc.invalidateQueries({ queryKey: ["performance"] });
    },
  });
  const eligible = ["SETUP", "TRIGGERED", "ACTIVE"].includes(rec.state) && rec.signal_id && rec.direction_code !== "NEUTRAL";
  if (!eligible) {
    return <div className="text-xs text-slate-500">Paper trading needs a SETUP, TRIGGERED or ACTIVE signal with an entry and stop. This signal is {rec.state}.</div>;
  }
  const ps = rec.position_size;
  const err = m.error instanceof ApiError ? m.error : null;
  const decision = err && err.detail && typeof err.detail === "object" ? (err.detail as { decision?: { violations?: string[] } }).decision : undefined;
  return (
    <div className="space-y-2 text-xs">
      {!confirming && !m.isSuccess && (
        <button className={clsx("btn", validated ? "btn-primary" : "")} onClick={() => setConfirming(true)}>
          {validated ? `Paper ${rec.action === "BUY" ? "buy" : "sell"} (simulated)…` : `Paper ${rec.direction_code === "SHORT" ? "sell" : "buy"} anyway (not a validated ${rec.action})…`}</button>
      )}
      {confirming && !m.isSuccess && (
        <div className="rounded border border-sky-800 bg-sky-950/30 p-2 space-y-1">
          <div className="font-semibold text-sky-200">Order preview — PAPER ONLY, nothing is sent to a broker</div>
          {!validated && <div className="rounded border border-amber-700 bg-amber-950/40 p-1 text-amber-200">This setup is <b>{rec.action}</b>, not a validated BUY/SELL: {rec.action_reasons.join("; ")}. The paper trade is recorded as unvalidated (for learning).</div>}
          <KV k="Instrument" v={`${rec.exchange}:${rec.symbol}`} />
          <KV k="Side" v={rec.direction_code} />
          <KV k="Suggested quantity" v={ps?.qty ?? "—"} />
          <KV k="Fill" v="Current (simulated) quote ± configured slippage" mono={false} />
          <KV k="Stop / target" v={`${inr(rec.stop_loss)} / ${rec.targets.length ? inr(rec.targets[0]) : "rule-based"}`} />
          <KV k="Risk if stopped" v={`₹${inr(ps?.risk_amount, 0)}`} />
          <div className="text-[10px] text-slate-400">The risk engine re-checks every limit at submission and will reject the trade if any limit is breached.</div>
          <div className="flex gap-2 pt-1">
            <button className="btn btn-primary" disabled={m.isPending} onClick={() => m.mutate()}>{m.isPending ? "Submitting…" : "Confirm paper trade"}</button>
            <button className="btn" onClick={() => setConfirming(false)}>Cancel</button>
          </div>
        </div>
      )}
      {m.isSuccess && (
        <div className="rounded border border-emerald-800 bg-emerald-950/30 p-2 text-emerald-200">
          Paper trade #{m.data.trade.id}: {m.data.trade.qty} @ {inr(m.data.trade.entry_price)}. {m.data.note}{" "}
          <Link className="underline" to="/portfolio">View portfolio</Link>
        </div>
      )}
      {m.error && (
        <div className="rounded border border-red-800 bg-red-950/40 p-2 text-red-200">
          {err?.message ?? String(m.error)}
          {decision?.violations?.length ? <ul className="mt-1 list-disc pl-4">{decision.violations.map((v, i) => <li key={i}>{v}</li>)}</ul> : null}
        </div>
      )}
    </div>
  );
}

export function RecommendationDetail({ rec, compact = false }: { rec: Recommendation; compact?: boolean }) {
  const oos = rec.oos_performance;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-base font-semibold text-slate-100">{rec.symbol}</span>
        <span className="text-xs text-slate-500">{rec.exchange} · {rec.name} · {rec.sector ?? "—"} · {rec.instrument_type}</span>
        <ActionBadge action={rec.action} /><StateBadge state={rec.state} />
        <DataLabel label={rec.freshness.data_label} />
        <Pill>{rec.research_only ? "Research candidate (not executable)" : "Executable"}</Pill>
      </div>
      <div className="text-xs text-slate-400">{rec.strategy_name} v{rec.strategy_version} · {rec.mode} · {rec.timeframe} · direction <span className={rec.direction === "bullish" ? "text-up" : rec.direction === "bearish" ? "text-down" : ""}>{rec.direction}</span></div>
      <div className="rounded border border-ink-700 bg-ink-850 p-2 text-xs text-slate-300">
        <span className="font-semibold">Action rationale: </span>{rec.action_reasons.join("; ")}
        <div className="mt-1 text-[10px] text-amber-300/80">{rec.research_only_reason}</div>
      </div>
      {rec.gates && (
        <div className="flex flex-wrap gap-2 text-[11px]">
          {Object.values(rec.gates).filter((g) => g.applies).map((g) => (
            <span key={g.name} title={g.detail} className={clsx("rounded border px-1.5 py-0.5", g.passed ? "border-emerald-800 text-emerald-300" : "border-amber-700 text-amber-300")}>
              {g.passed ? "✓" : "✕"} {g.name}
            </span>
          ))}
        </div>
      )}

      <div className={clsx("grid gap-3", compact ? "grid-cols-1" : "grid-cols-1 lg:grid-cols-3")}>
        <Section title="Trade plan">
          <KV k="Reference price" v={<LivePrice exchange={rec.exchange} symbol={rec.symbol} fallback={rec.reference_price} />} />
          {rec.entry.trigger != null && <KV k="Entry trigger" v={<span title="A CLOSED bar must cross this level; the entry fills at the next bar's open">{inr(rec.entry.trigger)}</span>} />}
          {rec.entry.filled != null && <KV k="Filled (next open)" v={inr(rec.entry.filled)} />}
          <KV k="Entry reference" v={inr(rec.entry.reference)} />
          <KV k="Entry zone" v={rec.entry.zone ? `${inr(rec.entry.zone[0])} – ${inr(rec.entry.zone[1])}` : "—"} />
          <KV k="Stop-loss" v={<span className="text-down">{inr(rec.stop_loss)}</span>} />
          {rec.stop_info && (
            <KV k="Stop distance" v={<span className={rec.stop_info.within_cap ? "" : "text-amber-300"} title={rec.stop_info.rule}>
              {num(rec.stop_info.stop_pct)}%{rec.stop_info.atr_multiple != null ? ` · ${num(rec.stop_info.atr_multiple, 1)}×ATR` : ""} · cap {num(rec.stop_info.cap_pct, 1)}%{rec.stop_info.within_cap ? "" : " — EXCEEDS CAP"}
            </span>} />
          )}
          <KV k="Target(s)" v={rec.targets.length ? rec.targets.map((t) => inr(t)).join(", ") : rec.target_note ?? "—"} />
          {rec.target_info && <div className="text-[10px] text-slate-500">Targets: {rec.target_info.method}{rec.target_info.p50_r != null ? ` (p50 ${num(rec.target_info.p50_r)}R, p75 ${num(rec.target_info.p75_r ?? null)}R, n=${rec.target_info.n_trades})` : ""}</div>}
          <KV k="Risk : reward" v={rec.risk_reward ? `1 : ${num(rec.risk_reward)}` : "n/a (trailing exit)"} />
          <KV k="Holding period" v={rec.holding_period} mono={false} />
          <KV k="Risk level" v={rec.risk_level} />
          <div className="mt-1 text-[11px] text-slate-500">{rec.entry.note}</div>
          <div className="mt-1 text-[11px] text-slate-400"><span className="text-slate-500">Invalidation:</span> {rec.invalidation}</div>
        </Section>
        <Section title="Position size & costs">
          {rec.position_size ? (
            <>
              <KV k="Quantity" v={rec.position_size.qty} />
              <KV k="Capital required" v={`₹${inr(rec.position_size.capital_required, 0)}`} />
              <KV k="Allocation" v={pct(rec.position_size.allocation_pct, 2, false)} />
              <KV k="Risk at stop" v={`₹${inr(rec.position_size.risk_amount, 0)}`} />
              <KV k="Limited by" v={rec.position_size.limiting_factor} mono={false} />
            </>
          ) : <div className="text-xs text-slate-500">No sizing for watch-only items.</div>}
          {rec.estimated_costs && (
            <>
              <KV k="Est. round-trip costs" v={`₹${inr(rec.estimated_costs.total)} (${num(rec.estimated_costs.pct_of_turnover, 3)}%)`} />
              <div className="text-[10px] text-slate-500">{rec.estimated_costs.basis}</div>
            </>
          )}
          <div className="mt-2"><KV k="Liquidity" v={`${rec.liquidity.rating} · ${crore(rec.liquidity.avg_turnover_cr)}`} /></div>
          {rec.liquidity.spread && <div className="text-[10px] text-slate-500">Spread: {rec.liquidity.spread}</div>}
        </Section>
        <Section title="Data & timing">
          <KV k="Signal bar" v={istTime(rec.signal_bar_ts)} />
          <KV k="Latest data" v={istTime(rec.data_timestamp)} />
          <KV k="Generated" v={`${istTime(rec.generated_at)} IST`} />
          <KV k="Freshness" v={`${rec.freshness.status} · ${rec.freshness.quality}`} />
          <KV k="Expires" v={istTime(rec.expires_at)} />
          <KV k="Composite score" v={<ScoreBar score={rec.score} />} />
          {rec.final_score != null && (
            <KV k="Final score" v={<span title={`composite × regime ${num(rec.regime_multiplier ?? null)} × gates ${num(rec.gate_multiplier ?? null)}`}>
              {num(rec.final_score, 1)}{rec.blocked ? <span className="ml-1 text-amber-300">(watch only{rec.blocked_by?.length ? `: ${rec.blocked_by.join(", ")}` : ""})</span> : null}
            </span>} />
          )}
          {rec.confluence && (
            <KV k="Confluence" v={<span className={rec.confluence.conflict ? "text-amber-300" : ""} title={rec.confluence.all_strategies.join(", ")}>
              {rec.confluence.count} strateg{rec.confluence.count === 1 ? "y" : "ies"} agree{rec.confluence.conflict ? " · DIRECTION CONFLICT" : ""}
            </span>} />
          )}
          <KV k="Regime" v={rec.regime ?? "—"} mono={false} />
        </Section>
      </div>

      <div className={clsx("grid gap-3", compact ? "grid-cols-1" : "grid-cols-1 lg:grid-cols-2")}>
        <Section title="Supporting evidence"><BulletList items={rec.supporting_evidence} tone="good" /></Section>
        <Section title="Conflicting evidence"><BulletList items={rec.conflicting_evidence} tone="bad" empty="No component scored strongly against this setup." /></Section>
        <Section title="Event risks"><BulletList items={rec.event_risks} /></Section>
        <Section title="Strategy out-of-sample evidence (walk-forward)">
          {oos.status === "ready" ? (
            <div className="text-xs">
              <div className="grid grid-cols-3 gap-2">
                <KV k="Trades" v={oos.num_trades} /><KV k="Win rate" v={pct(oos.win_rate_pct, 1, false)} /><KV k="PF" v={num(oos.profit_factor)} />
                <KV k="Expectancy" v={`${num(oos.expectancy_r, 3)}R`} /><KV k="Net" v={pct(oos.net_return_pct)} /><KV k="Max DD" v={pct(oos.max_drawdown_pct)} />
              </div>
              <div className="text-[10px] text-slate-500">{oos.period_start} → {oos.period_end} · {oos.data_label}{oos.low_sample ? " · LOW SAMPLE" : ""}</div>
            </div>
          ) : <div className="text-xs text-slate-500">Evidence {oos.status} — computed in the background after startup.</div>}
        </Section>
      </div>

      <Section title="Indicator values (latest bar)">
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
          {Object.entries(rec.indicators).map(([k, v]) => <span key={k}><span className="text-slate-500">{k}</span> <span className="num">{v === null ? "—" : num(v, 2)}</span></span>)}
        </div>
      </Section>
      {!compact && <Section title="Score breakdown"><ScoreBreakdown rec={rec} /></Section>}
    </div>
  );
}
