"""Self-contained HTML tournament report (inline SVG charts; print to PDF from the browser)."""

from __future__ import annotations

import html
from typing import Any

PALETTE = ["#38bdf8", "#22c55e", "#f59e0b", "#a78bfa", "#f472b6", "#2dd4bf", "#fb7185", "#facc15", "#60a5fa", "#4ade80"]


def _e(x: Any) -> str:
    return html.escape("—" if x is None else str(x))


def _svg_lines(series: list[tuple[str, list[list[Any]], str]], w: int = 900, h: int = 260) -> str:
    pts = [p for _, s, _ in series for p in s]
    if not pts:
        return "<p class=muted>No curve data.</p>"
    dates = sorted({p[0] for p in pts})
    pos = {d: i for i, d in enumerate(dates)}
    vals = [p[1] for p in pts]
    lo, hi = min(vals), max(vals)
    hi = hi if hi > lo else lo + 1
    x = lambda d: 40 + (w - 50) * pos[d] / max(len(dates) - 1, 1)  # noqa: E731
    y = lambda v: 10 + (h - 30) * (1 - (v - lo) / (hi - lo))  # noqa: E731
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" style="background:#0b1220;border:1px solid #1e293b">']
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        out.append(f'<text x="2" y="{y(v) + 4:.0f}" fill="#64748b" font-size="10">{v:.2f}</text>')
        out.append(f'<line x1="40" x2="{w - 10}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="#1e293b"/>')
    for name, s, color in series:
        if s:
            d = " ".join(f"{x(p[0]):.1f},{y(p[1]):.1f}" for p in s)
            out.append(f'<polyline fill="none" stroke="{color}" stroke-width="1.5" points="{d}"/>')
    lx = 50
    for name, _, color in series:
        out.append(f'<text x="{lx}" y="{h - 4}" fill="{color}" font-size="11">{_e(name)}</text>')
        lx += 9 * len(name) + 20
    out.append(f'<text x="{w - 160}" y="{h - 4}" fill="#64748b" font-size="10">{dates[0]} → {dates[-1]}</text></svg>')
    return "".join(out)


def _table(rows: list[dict[str, Any]], cols: list[tuple[str, str]]) -> str:
    head = "".join(f"<th>{_e(h)}</th>" for _, h in cols)
    body = "".join("<tr>" + "".join(f"<td>{_e(r.get(k))}</td>" for k, _ in cols) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render(rep: dict[str, Any]) -> str:
    conf, data = rep["config"], rep["data"]
    top_cols = [("rank", "#"), ("name", "Strategy"), ("family", "Family"), ("mode", "Mode"), ("direction", "Direction"),
                ("timeframe", "TF"), ("best_regime", "Best regime"), ("oos_trades", "OOS trades"),
                ("profit_factor", "PF"), ("pf_ci", "PF 95% CI"), ("expectancy_r", "Exp. R"),
                ("net_return_pct", "Net %"), ("benchmark_return_pct", "NIFTY TR %"), ("cagr_pct", "CAGR %"),
                ("max_drawdown_pct", "Max DD %"), ("calmar", "Calmar"), ("win_rate_pct", "Win %"),
                ("avg_holding_bars", "Avg hold (bars)"), ("stability_score", "Stability"), ("score", "Score"),
                ("pass_reason", "Why it passed")]
    lb_cols = [("no", "#"), ("name", "Strategy"), ("family", "Family"), ("timeframe", "TF"), ("result", "Result"),
               ("first_failed", "First failed"), ("first_failed_detail", "Detail"), ("oos_trades", "Trades"),
               ("profit_factor", "PF"), ("expectancy_r", "Exp. R"), ("net_return_pct", "Net %"),
               ("benchmark_return_pct", "NIFTY TR %"), ("max_drawdown_pct", "Max DD %"),
               ("deflated_sharpe", "DSR"), ("score", "Score")]
    parts = [f"""<!doctype html><html><head><meta charset="utf-8"><title>Strategy tournament {rep['run_id']}</title>
<style>body{{font:13px system-ui,Segoe UI,sans-serif;background:#020617;color:#e2e8f0;margin:24px}}
h1{{font-size:20px}}h2{{font-size:15px;margin-top:28px;border-bottom:1px solid #1e293b;padding-bottom:4px}}
table{{border-collapse:collapse;width:100%;margin:8px 0}}th,td{{border:1px solid #1e293b;padding:3px 6px;text-align:left;
vertical-align:top}}th{{background:#0f172a;color:#94a3b8;font-weight:600}}.muted{{color:#64748b}}
.warn{{color:#fbbf24}}.pass{{color:#4ade80}}.fail{{color:#f87171}}@media print{{body{{background:#fff;color:#000}}}}</style>
</head><body><h1>Strategy tournament report</h1>
<p class=muted>Run {rep['run_id']} · generated {_e(rep['generated_at'])} · data {_e(rep['data_label'])} ·
daily {_e(data['daily_symbols'])} symbols {_e(data['daily_range'])} · 5-minute {_e(data['intraday_symbols'])} symbols
{_e(data['intraday_range'])} · benchmark {_e(data['benchmark'])} · cache key {_e(rep['cache_key'])} ·
{_e(rep['seconds'])} s</p>
<p class=warn>Research and paper trading only - not investment advice. {_e(data['survivorship_note'])}</p>
<h2>{_e(rep['headline'])}</h2>"""]
    if rep["top10"]:
        parts.append(_table(rep["top10"], top_cols))
    else:
        parts.append("<p class=warn><b>No strategy passed every criterion.</b> Nothing is padded into the Top 10.</p>")
    parts.append("<h2>What the evidence says</h2><ul>" + "".join(f"<li>{_e(s)}</li>" for s in rep["summary"]) + "</ul>")
    show = [r["id"] for r in (rep["top10"] or rep["leaderboard"])[:6] if r.get("result") in ("PASS", "FAIL")]
    series = []
    bench = None
    for i, sid in enumerate(show):
        det = rep["details"].get(sid) or {}
        series.append((sid, det.get("equity_curve") or [], PALETTE[i % len(PALETTE)]))
        bench = bench or ((det.get("benchmark") or {}).get("curve"))
    if bench:
        series.append(("NIFTY 50 TR", bench, "#e2e8f0"))
    parts.append("<h2>OOS equity curves vs NIFTY 50 (growth of 1)</h2>" + _svg_lines(series))
    dd = [(sid, (rep["details"].get(sid) or {}).get("drawdown_curve") or [], PALETTE[i % len(PALETTE)])
          for i, sid in enumerate(show)]
    parts.append("<h2>Drawdown (%)</h2>" + _svg_lines(dd, h=180))
    parts.append("<h2>Full leaderboard</h2>" + _table(rep["leaderboard"], lb_cols))
    parts.append("<h2>Parameter stability (profit factor at -20% / base / +20%)</h2>")
    rows = []
    for r in rep["leaderboard"]:
        st = (rep["details"].get(r["id"]) or {}).get("stability") or {}
        for h in st.get("heatmap") or []:
            cells = {c["shift"]: c.get("profit_factor") for c in h["cells"]}
            rows.append({"strategy": r["id"], "param": h["param"], "base": h["base_value"],
                         "-20%": cells.get("-20%"), "pf_base": cells.get("base"), "+20%": cells.get("+20%"),
                         "passed": st.get("passed")})
    parts.append(_table(rows, [("strategy", "Strategy"), ("param", "Parameter"), ("base", "Base value"),
                               ("-20%", "PF -20%"), ("pf_base", "PF base"), ("+20%", "PF +20%"), ("passed", "Stable")]))
    parts.append("<h2>Results per regime (OOS trades by regime at signal time)</h2>")
    reg_rows = []
    for k, items in sorted(rep["per_regime"].items()):
        good = [x["id"] for x in items if x["qualifies"]]
        reg_rows.append({"regime": k, "passed_and_profitable": ", ".join(good) or "none",
                         "best_pf": max((x["profit_factor"] or 0 for x in items if x["num_trades"] >= 20), default=None)})
    parts.append(_table(reg_rows, [("regime", "Regime"), ("passed_and_profitable", "Passed strategies with PF >= threshold"),
                                   ("best_pf", "Best PF any strategy (>= 20 trades)")]))
    parts.append("<h2>NOT TESTABLE (missing data)</h2>" + _table(
        [{**x, "missing_data": "; ".join(x["missing_data"])} for x in rep["not_testable"]],
        [("no", "#"), ("name", "Strategy"), ("family", "Family"), ("missing_data", "Missing data"), ("note", "Note")]))
    parts.append("<h2>Context inputs (excluded from ranking)</h2>" + _table(
        [{**x, "missing_data": "; ".join(x["missing_data"]) or "-"} for x in rep["context_inputs"]],
        [("no", "#"), ("name", "Input"), ("missing_data", "Missing data"), ("note", "Status")]))
    if rep.get("control"):
        c = rep["control"]
        parts.append(f"<h2>Random-walk control</h2><p class={'pass' if c['passed'] == 0 else 'fail'}>{c['passed']} of "
                     f"{c['tested']} strategies passed on {c['symbols']} zero-drift random walks.</p>")
    mt = rep["multiple_testing"]
    parts.append(f"<h2>Configuration</h2><p class=muted>Multiple testing: {mt['n_trials']} configurations; luck "
                 f"threshold Sharpe {mt['sr0_annualised']}/yr; BH-FDR discoveries: {_e(', '.join(mt['bh_discoveries']) or 'none')}"
                 f"</p><pre class=muted>{_e(conf)}</pre></body></html>")
    return "".join(parts)
