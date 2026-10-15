"""Deterministic explainer: builds explanations exclusively from computed results. Default provider."""

from __future__ import annotations

from typing import Any

from ..quant.strategies.base import load_specs
from .provider import Explanation, LLMProvider


def _fmt(x: Any, nd: int = 2) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, (int, float)):
        return f"{x:,.{nd}f}"
    return str(x)


def explain_recommendation(rec: dict[str, Any]) -> Explanation:
    spec = load_specs().get(rec["strategy"], {})
    e = Explanation(sources=[f"signal:{rec.get('signal_id', 'n/a')}", f"strategy_spec:{rec['strategy']}@{rec['strategy_version']}"])
    e.facts += [
        f"{rec['symbol']} ({rec['exchange']}, {rec['instrument_type']}) - {rec['strategy_name']} v{rec['strategy_version']}, "
        f"{rec['mode']} mode, {rec['timeframe']} bars.",
        f"State {rec['state']}; action {rec['action']}; direction {rec['direction']}.",
        f"Latest bar {rec['data_timestamp']} ({rec['freshness']['data_label']}, freshness {rec['freshness']['status']}); "
        f"reference price {_fmt(rec['reference_price'])}.",
    ]
    ind = rec.get("indicators") or {}
    if ind:
        e.facts.append("Indicator values: " + ", ".join(f"{k}={_fmt(v)}" for k, v in ind.items() if v is not None) + ".")
    for s in rec.get("supporting_evidence", []):
        e.facts.append(f"Supporting: {s}")
    for s in rec.get("conflicting_evidence", []):
        e.facts.append(f"Conflicting: {s}")

    entry = rec.get("entry") or {}
    stop = rec.get("stop_loss")
    if stop is not None and entry.get("reference") is not None:
        risk_u = abs(entry["reference"] - stop)
        e.calculations.append(f"Risk per share = |entry {_fmt(entry['reference'])} - stop {_fmt(stop)}| = {_fmt(risk_u)} "
                              f"({risk_u / entry['reference'] * 100:.2f}% of entry).")
        if rec.get("targets"):
            e.calculations.append(f"Target {_fmt(rec['targets'][0])} = entry +/- {_fmt(rec.get('risk_reward'))} x risk per share "
                                  f"(risk-reward {_fmt(rec.get('risk_reward'))}).")
    ps = rec.get("position_size")
    if ps:
        e.calculations.append(f"Position size {ps['qty']} shares: limited by {ps['limiting_factor']}; capital "
                              f"Rs {_fmt(ps['capital_required'], 0)} ({_fmt(ps['allocation_pct'])}% of equity); "
                              f"loss if stop is hit approx. Rs {_fmt(ps['risk_amount'], 0)} before costs.")
    c = rec.get("estimated_costs")
    if c:
        e.calculations.append(f"Estimated round-trip costs Rs {_fmt(c['total'])} ({_fmt(c['pct_of_turnover'], 3)}% of turnover).")
    sb = rec.get("score_breakdown") or {}
    if sb:
        parts = [f"{k}={_fmt(v['value'], 2)}x{_fmt(sb['weights'].get(k, 0), 2)}" for k, v in sb["components"].items()
                 if k in sb.get("used", [])]
        e.calculations.append(f"Composite score {_fmt(rec['score'], 1)} = 50 + 50 x weighted mean of [{'; '.join(parts)}]. "
                              f"Excluded (unavailable or zero weight): {', '.join(sb.get('excluded', [])) or 'none'}.")
    oos = rec.get("oos_performance") or {}
    if oos.get("status") == "ready":
        e.calculations.append(
            f"Walk-forward OOS: {oos.get('num_trades')} trades, win rate {_fmt(oos.get('win_rate_pct'))}%, profit factor "
            f"{_fmt(oos.get('profit_factor'))}, expectancy {_fmt(oos.get('expectancy_r'), 3)}R, max drawdown "
            f"{_fmt(oos.get('max_drawdown_pct'))}% ({oos.get('period_start')} to {oos.get('period_end')}, {oos.get('data_label')}).")
    if ps:
        e.assumptions += ps.get("assumptions", [])
    e.assumptions += [
        entry.get("note") or "",
        "Transaction costs are estimates from a configurable Zerodha-style model.",
        "The score is a rule-based ranking, not a probability of profit.",
    ]
    if rec["freshness"]["data_label"] == "SIMULATED":
        e.assumptions.append("All prices are SIMULATED sample data - not real market data.")
    e.assumptions = [a for a in e.assumptions if a]

    if rec["state"] == "WATCH":
        e.interpretation.append("This is a watch-list item only: the strategy's entry rules are not yet complete, so there is no trade.")
    else:
        e.interpretation.append(f"The setup qualifies because every entry rule of {rec['strategy_name']} was met on "
                                f"{rec['signal_bar_ts']} (see supporting evidence).")
        e.interpretation.append(f"It is invalidated if: {rec.get('invalidation')}.")
    if rec.get("action_reasons"):
        e.interpretation.append(f"Action {rec['action']}: " + "; ".join(rec["action_reasons"]) + ".")
    fm = spec.get("failure_modes") or []
    if fm:
        e.interpretation.append("Known ways this methodology fails: " + " ".join(fm))
    e.interpretation.append("Event risk (earnings, corporate actions, news) is UNKNOWN because no event feed is connected.")
    e.interpretation.append(f"{rec['research_only_reason']}.")
    return e


class TemplateExplainer(LLMProvider):
    name = "template"

    def interpret(self, question: str, base: Explanation) -> Explanation:
        return base
