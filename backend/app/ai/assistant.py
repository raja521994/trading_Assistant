"""AI research assistant: routes questions to deterministic retrieval + calculations, then (optionally)
lets a configured LLM phrase an interpretation of those facts. Never invents data; never places orders."""

from __future__ import annotations

import re
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..backtest.service import latest_evidence
from ..config import get_settings
from ..models import Signal
from ..providers.registry import active_provider
from ..quant.regime import classify_regime
from ..quant.strategies.base import load_specs
from ..quant.universe import instrument_map, load_benchmark, load_universe
from ..signals.state_machine import ACTIVE
from .openai_compat import OpenAICompatibleProvider
from .provider import Explanation, LLMProvider
from .template_explainer import TemplateExplainer, explain_recommendation

SUGGESTED = [
    "Why is RELIANCE appearing in the scanner?",
    "Which strategy generated this setup?",
    "What evidence contradicts the bullish signal on TCS?",
    "What is the downside risk if the stop-loss is reached on INFY?",
    "How did the Donchian strategy perform out of sample?",
    "Which stocks show improving relative strength?",
    "What changed in the current market regime?",
]


def get_llm() -> LLMProvider:
    s = get_settings()
    if s.llm_provider == "openai_compatible" and s.llm_base_url and s.llm_api_key and s.llm_model:
        return OpenAICompatibleProvider(s.llm_base_url, s.llm_api_key.get_secret_value(), s.llm_model,
                                        s.llm_timeout_seconds)
    return TemplateExplainer()


def llm_status() -> dict[str, Any]:
    s = get_settings()
    p = get_llm()
    return {"configured_provider": s.llm_provider, "active_provider": p.name,
            "model": s.llm_model if p.name != "template" else None,
            "note": "Deterministic template explainer (no external AI calls)" if p.name == "template"
            else "OpenAI-compatible endpoint; outputs with unsupported numbers are rejected"}


def _find_symbol(q: str, symbols: list[str]) -> str | None:
    up = q.upper()
    for s in sorted(symbols, key=len, reverse=True):
        if re.search(rf"(?<![A-Z0-9&-]){re.escape(s)}(?![A-Z0-9&-])", up):
            return s
    return None


def _find_strategy(q: str) -> str | None:
    ql = q.lower()
    for sid, spec in load_specs().items():
        words = [sid.replace("_", " "), spec["name"].lower(), sid.split("_")[0]]
        if sid == "rsi2_meanrev":
            words += ["rsi", "mean reversion", "mean-reversion"]
        if sid == "orb_vwap":
            words += ["opening range", "orb", "vwap"]
        if sid == "supertrend_trend":
            words += ["supertrend"]
        if sid == "ema_cross":
            words += ["ema", "crossover"]
        if any(w in ql for w in words):
            return sid
    return None


def _signal_for(session: Session, symbol: str | None, signal_id: int | None) -> Signal | None:
    if signal_id is not None:
        return session.get(Signal, signal_id)
    if symbol is None:
        return None
    sigs = session.scalars(select(Signal).where(Signal.symbol == symbol, Signal.origin == "live_scan",
                                               Signal.provider == active_provider().name)
                           .order_by(Signal.updated_at.desc())).all()
    active = [s for s in sigs if s.state in ACTIVE]
    pick = active or sigs
    if not pick:
        return None
    rank = {"TRIGGERED": 0, "SETUP": 1, "WATCH": 2}
    return sorted(pick, key=lambda s: (rank.get(s.state, 3), -s.score))[0]


def answer(session: Session, question: str, signal_id: int | None = None) -> dict[str, Any]:
    q = question.strip()
    ql = q.lower()
    imap = instrument_map(session)
    symbols = [k[1] for k, v in imap.items() if v.instrument_type == "EQ"]
    symbol = _find_symbol(q, symbols)
    intent = "help"
    exp = Explanation()

    if "regime" in ql:
        intent = "regime"
        exp = _regime(session)
    elif "relative strength" in ql or re.search(r"\brs\b", ql):
        intent = "relative_strength"
        exp = _relative_strength(session)
    elif any(w in ql for w in ("out of sample", "out-of-sample", "oos", "walk-forward", "walk forward", "perform")):
        intent = "oos_performance"
        sid = _find_strategy(q)
        if sid is None and (symbol or signal_id):
            sig = _signal_for(session, symbol, signal_id)
            sid = sig.strategy if sig else None
        exp = _oos(session, sid)
    elif symbol or signal_id:
        sig = _signal_for(session, symbol, signal_id)
        if sig is None:
            intent = "no_signal"
            exp = _no_signal(session, symbol)
        else:
            rec = {**sig.payload, "signal_id": sig.id, "state": sig.state}
            full = explain_recommendation(rec)
            if any(w in ql for w in ("contradict", "against", "conflict", "bear", "risk factor", "wrong")):
                intent = "conflicting_evidence"
                conf = rec.get("conflicting_evidence") or ["No component scored strongly against the setup."]
                exp = Explanation(facts=[full.facts[0], full.facts[1]] + [f"Conflicting: {c}" for c in conf],
                                  assumptions=full.assumptions[-2:],
                                  interpretation=[i for i in full.interpretation if "fails" in i or "UNKNOWN" in i],
                                  sources=full.sources)
            elif any(w in ql for w in ("downside", "stop", "lose", "loss", "risk")):
                intent = "downside_risk"
                exp = Explanation(facts=full.facts[:3], calculations=full.calculations[:4],
                                  assumptions=full.assumptions, sources=full.sources,
                                  interpretation=["Gaps through the stop can produce a larger loss than the "
                                                  "calculated amount; costs are added to the loss."])
            elif "which strategy" in ql or "what strategy" in ql:
                intent = "strategy"
                spec = load_specs()[sig.strategy]
                exp = Explanation(facts=[full.facts[0], f"Category: {spec['category']}. Reference: {spec['reference']}",
                                         f"Entry rule: {spec['entry']}", f"Stop rule: {spec['stop_loss']}",
                                         f"Exit rule: {spec['exit']}"],
                                  sources=full.sources, interpretation=[full.interpretation[0]])
            else:
                intent = "explain_signal"
                exp = full
    else:
        exp = Explanation(facts=["I answer from computed scanner, backtest and market data only."],
                          interpretation=["Try one of the suggested questions, naming a symbol or strategy."])
    llm = get_llm()
    if intent not in ("help", "no_signal"):
        exp = llm.interpret(q, exp)
    return {"question": q, "intent": intent, "symbol": symbol, "answer": exp.to_dict(), "provider": exp.provider,
            "suggested": suggested_questions()}


def suggested_questions() -> list[str]:
    """Suggestions that reference symbols actually present in the latest scan."""
    from ..signals.engine import last_scan

    recs = last_scan().get("recommendations", [])
    actionable = [r for r in recs if r["action"] in ("BUY", "SELL")]
    conflicted = [r for r in recs if r.get("conflicting_evidence")]
    out = list(SUGGESTED)
    if actionable:
        out[0] = f"Why is {actionable[0]['symbol']} appearing in the scanner?"
        out[3] = f"What is the downside risk if the stop-loss is reached on {actionable[-1]['symbol']}?"
    if recs:
        out[1] = f"Which strategy generated the setup on {recs[0]['symbol']}?"
    if conflicted:
        out[2] = f"What evidence contradicts the signal on {conflicted[0]['symbol']}?"
    elif actionable:
        out[2] = f"What evidence contradicts the signal on {actionable[0]['symbol']}?"
    return out


def _no_signal(session: Session, symbol: str | None) -> Explanation:
    from ..quant.universe import features, last_row

    facts = [f"{symbol} has no active scanner signal from any of the {len(load_specs())} strategies."]
    imap = instrument_map(session)
    key = next((k for k in imap if k[1] == symbol), None)
    data = load_universe(session, "1d")
    if key and key in data:
        f = last_row(features(key, data[key]))
        ts = data[key].index[-1].isoformat()
        facts.append(f"Latest daily bar {ts}: close {f['close']:.2f}, EMA50 {f['ema50']:.2f}, EMA200 "
                     f"{f['ema200']:.2f}, RSI(14) {f['rsi14']:.1f}, ADX(14) {f['adx14']:.1f}, ATR% {f['atr_pct']:.2f}.")
    return Explanation(facts=facts, interpretation=[
        "The scanner only lists instruments where a strategy's entry or watch conditions are met on validated "
        "data; none are met for this symbol on the latest bar."], sources=[f"universe:daily:{symbol}"])


def _regime(session: Session) -> Explanation:
    bench = load_benchmark(session)
    r = classify_regime(bench, load_universe(session, "1d")) if bench is not None else None
    if not r or not r.get("current"):
        return Explanation(facts=["Benchmark data unavailable - regime cannot be classified."])
    cur, prev = r["current"], r["previous"]
    return Explanation(
        facts=[f"Current regime ({cur['as_of']}): {cur['label']}.", f"Previous ({prev['as_of']}): {prev['label']}.",
               "Inputs now: " + ", ".join(f"{k}={v}" for k, v in cur["facts"].items())],
        calculations=r["changes"],
        assumptions=["Trend: close vs SMA50/SMA200 with ADX(14) >= 20; volatility: 20-day realised volatility "
                     "percentile over 1 year; breadth: % of universe closing above its 50-day average."],
        interpretation=["Trend-following setups historically align better with an UPTREND regime; mean-reversion "
                        "longs are riskier when the regime turns to DOWNTREND or HIGH volatility."],
        sources=["benchmark:NSE:NIFTY 50", "universe:daily"])


def _relative_strength(session: Session) -> Explanation:
    bench = load_benchmark(session)
    data = load_universe(session, "1d")
    if bench is None or not data:
        return Explanation(facts=["Benchmark or universe data unavailable."])
    b = bench["close"]
    rows = []
    for (exch, sym), df in data.items():
        rs = (df["close"] / b.reindex(df.index)).dropna()
        if len(rs) < 64:
            continue
        chg21 = rs.iloc[-1] / rs.iloc[-22] - 1.0
        chg63 = rs.iloc[-1] / rs.iloc[-64] - 1.0
        rows.append((sym, exch, chg21, chg63))
    rows.sort(key=lambda r: r[2], reverse=True)
    top = [r for r in rows if r[2] > 0][:8]
    return Explanation(
        facts=[f"{s} ({e}): RS vs NIFTY 50 changed {c21 * 100:+.2f}% over 21 sessions, {c63 * 100:+.2f}% over 63 sessions"
               for s, e, c21, c63 in top] or ["No instrument shows improving relative strength over 21 sessions."],
        calculations=["RS ratio = stock close / NIFTY 50 close; improvement = RS(t) / RS(t-21) - 1, ranked descending."],
        assumptions=["Price-only relative strength; dividends and corporate actions not adjusted."],
        interpretation=["Improving relative strength identifies leadership candidates; it is not an entry signal by itself."],
        sources=["universe:daily", "benchmark:NSE:NIFTY 50"])


def _oos(session: Session, sid: str | None) -> Explanation:
    ev = latest_evidence(session)
    ids = [sid] if sid else list(ev.keys())
    if not ids:
        return Explanation(facts=["Walk-forward evidence has not been computed yet."])
    facts, calcs = [], []
    for s in ids:
        e = ev.get(s)
        name = load_specs()[s]["name"]
        if not e:
            facts.append(f"{name}: evidence pending.")
            continue
        facts.append(f"{name} v{e['version']}: {e['num_trades']} OOS trades {e['period_start']} to {e['period_end']} "
                     f"({e['data_label']}).")
        calcs.append(f"{name}: net return {e['net_return_pct']}%, win rate {e['win_rate_pct']}%, profit factor "
                     f"{e['profit_factor']}, expectancy {e['expectancy_r']}R, max drawdown {e['max_drawdown_pct']}%, "
                     f"Sharpe {e['sharpe']}, benchmark {e.get('benchmark_return_pct')}%.")
        for f in e.get("folds", []):
            calcs.append(f"  fold {f['fold']} params {f['params']}: IS Sharpe {f['is'].get('sharpe')} -> OOS Sharpe "
                         f"{f['oos'].get('sharpe')}, OOS trades {f['oos'].get('num_trades')}")
    return Explanation(facts=facts, calculations=calcs,
                       assumptions=[ev[ids[0]].get("method") or "" if ids[0] in ev else ""],
                       interpretation=["Only out-of-sample segments are reported as evidence. A gap between IS and "
                                       "OOS Sharpe indicates overfitting risk.",
                                       "Results on SIMULATED data say nothing about real-market profitability."],
                       sources=[f"evidence:{s}" for s in ids])
