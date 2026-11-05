"""Evidence policy: how much out-of-sample proof a strategy needs before its live setups become BUY / SELL.

Every tested strategy gets an evidence TIER for the current market regime (computed from the latest tournament):

  VALIDATED    passed every tournament criterion (incl. multiple-testing) AND worked in the current regime
  PROVISIONAL  statistically positive out-of-sample edge after costs: >= 30 OOS trades, PF >= 1.2, expectancy CI
               lower bound > 0, still profitable with 2x costs, survives the survivorship re-test, AND PF >= 1.2 over
               >= 20 OOS trades in the current regime. Did not clear every test (usually multiple testing).
  SPECULATIVE  positive out-of-sample expectancy and PF >= 1.1 after costs, survives 2x costs, and did not lose
               money in the current regime (when >= 5 OOS trades exist there). Weak evidence.
  NONE         no demonstrated edge (or a cross-sectional strategy that cannot be validated without point-in-time
               constituents)

The user's policy (Risk & Performance / Scanner) sets the minimum tier: strict -> VALIDATED, balanced (default) ->
PROVISIONAL, aggressive -> SPECULATIVE. Tiers are shown on every recommendation so weak evidence is never hidden.
"""

from __future__ import annotations

from typing import Any

TIERS = ["NONE", "SPECULATIVE", "PROVISIONAL", "VALIDATED"]
POLICY_MIN_TIER = {"strict": "VALIDATED", "balanced": "PROVISIONAL", "aggressive": "SPECULATIVE"}
# Minimum exposure budget (% of normal risk per trade) when a strategy qualifies under a non-strict policy in a
# regime whose default budget is lower (e.g. bear = 0%): trades are still taken, at reduced size.
BUDGET_FLOOR = {"strict": 0.0, "balanced": 50.0, "aggressive": 50.0}
POLICY_INFO = {
    "strict": "Only strategies that passed EVERY tournament test (incl. multiple-testing control). Usually NO TRADE.",
    "balanced": "Strategies with a statistically positive out-of-sample edge after costs that also worked in the "
                "current market regime (PROVISIONAL). Default.",
    "aggressive": "Also strategies with weaker positive out-of-sample evidence (SPECULATIVE). More trades, lower "
                  "confidence.",
}

_cache: dict[str, Any] = {"run": None, "report": None}


def report() -> dict[str, Any] | None:
    from ..validation.tournament import OUT_DIR, load_report

    latest = OUT_DIR / "latest.txt"
    run = latest.read_text().strip() if latest.exists() else None
    if run != _cache["run"]:
        _cache["report"], _cache["run"] = load_report(run) if run else None, run
    return _cache["report"]


def trend_from_facts(facts: dict[str, Any] | None) -> str:
    f = facts or {}
    c, s50, s200 = f.get("close"), f.get("sma50"), f.get("sma200")
    if None in (c, s50, s200):
        return "unknown"
    if c > s200 and s50 > s200:
        return "bull"
    if c < s200 and s50 < s200:
        return "bear"
    return "sideways"


def _rank(t: str) -> int:
    return TIERS.index(t)


def evaluate(strategy_id: str, trend: str, rep: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Evidence tier of a strategy in the current regime, with the reasons. None if it was not in the tournament."""
    rep = rep if rep is not None else report()
    if not rep:
        return None
    row = next((r for r in rep["leaderboard"] if r["id"] == strategy_id), None)
    if row is None:
        return None
    det = rep["details"].get(strategy_id) or {}
    checks = {c["name"]: c.get("passed") for c in det.get("checks") or []}
    o = det.get("oos") or {}
    n, pf, exp = o.get("num_trades") or 0, o.get("profit_factor") or 0.0, o.get("expectancy_r")
    b = (det.get("regimes") or {}).get(f"trend:{trend}") or {}
    bn, bpf = b.get("num_trades") or 0, b.get("profit_factor")
    reg_txt = f"{trend} regime: PF {bpf} over {bn} OOS trades"
    base = {"strategy": strategy_id, "name": row.get("name"), "run_id": rep.get("run_id"), "trend": trend,
            "oos_trades": n, "profit_factor": pf, "expectancy_r": exp, "regime_trades": bn, "regime_pf": bpf,
            "pf_ci": (det.get("bootstrap") or {}).get("pf_ci"),
            "expectancy_ci": (det.get("bootstrap") or {}).get("expectancy_r_ci"), "tournament_result": row["result"]}
    if row.get("kind") in ("portfolio", "pairs") or checks.get("point_in_time_universe") is False:
        return {**base, "tier": "NONE", "reasons": ["Cross-sectional / pairs strategy: cannot be validated without "
                                                    "point-in-time index constituents"]}
    common = n >= 30 and checks.get("double_costs") is True
    regime_good = bn >= 20 and (bpf or 0) >= 1.2
    regime_not_bad = bn < 5 or (bpf or 0) >= 1.0
    surv_ok = checks.get("survivorship_sensitivity") is not False
    tier, why = "NONE", []
    if row["result"] == "PASS" and regime_good:
        tier = "VALIDATED"
        why.append(f"Passed every tournament test; {reg_txt}")
    elif common and pf >= 1.2 and checks.get("expectancy") is True and surv_ok and regime_good:
        tier = "PROVISIONAL"
        why.append(f"Positive OOS edge after costs (PF {pf}, expectancy CI {base['expectancy_ci']}), survives 2x "
                   f"costs; {reg_txt}. Did not pass: {row.get('first_failed')}")
    elif common and pf >= 1.1 and (exp or 0) > 0 and regime_not_bad:
        tier = "SPECULATIVE"
        why.append(f"Weak positive OOS evidence (PF {pf}, expectancy {exp}R); {reg_txt}")
    else:
        miss = []
        if n < 30:
            miss.append(f"only {n} OOS trades")
        if pf < 1.1:
            miss.append(f"OOS PF {pf} < 1.1")
        if (exp or 0) <= 0:
            miss.append(f"OOS expectancy {exp}R <= 0")
        if checks.get("double_costs") is not True:
            miss.append("not profitable with 2x costs")
        if not regime_not_bad:
            miss.append(f"lost money in the current regime ({reg_txt})")
        why.append("No demonstrated edge: " + ("; ".join(miss) or f"failed {row.get('first_failed')}"))
    return {**base, "tier": tier, "reasons": why}


def accepted(tier: str, policy: str) -> bool:
    return _rank(tier) >= _rank(POLICY_MIN_TIER.get(policy, "PROVISIONAL")) and tier != "NONE"


def qualify(strategy_id: str, trend: str, policy: str, rep: dict[str, Any] | None = None) -> dict[str, Any] | None:
    ev = evaluate(strategy_id, trend, rep)
    if ev is None:
        return None
    ok = accepted(ev["tier"], policy)
    need = POLICY_MIN_TIER.get(policy, "PROVISIONAL")
    detail = (f"Evidence {ev['tier']} (policy '{policy}' needs {need} or better): " + "; ".join(ev["reasons"]))
    return {**ev, "ok": ok, "policy": policy, "detail": detail}


def qualifying_library_ids(trend: str, policy: str) -> list[str]:
    """Library event strategies (not the 5 YAML strategies) whose evidence tier is accepted under the policy."""
    rep = report()
    if not rep:
        return []
    from ..quant.library.catalog import BY_ID

    out = []
    for r in rep["leaderboard"]:
        e = BY_ID.get(r["id"])
        if e is None or e.kind != "event":
            continue
        q = qualify(r["id"], trend, policy, rep)
        if q and q["ok"]:
            out.append(r["id"])
    return out


def summary(trend: str, policy: str) -> dict[str, Any]:
    rep = report()
    rows = []
    if rep:
        for r in rep["leaderboard"]:
            ev = evaluate(r["id"], trend, rep)
            if ev:
                rows.append({**{k: ev[k] for k in ("strategy", "name", "tier", "oos_trades", "profit_factor",
                                                   "expectancy_r", "regime_trades", "regime_pf")},
                             "accepted": accepted(ev["tier"], policy), "reason": "; ".join(ev["reasons"])})
    rows.sort(key=lambda x: (-_rank(x["tier"]), -(x["profit_factor"] or 0)))
    return {"policy": policy, "info": POLICY_INFO, "min_tier": POLICY_MIN_TIER[policy], "trend": trend,
            "strategies": rows, "tournament_run": rep.get("run_id") if rep else None}


def tournament_evidence(strategy_id: str, data_label: str) -> dict[str, Any] | None:
    """Out-of-sample evidence in the shape the scanner's scoring expects, from the latest tournament."""
    rep = report()
    det = (rep or {}).get("details", {}).get(strategy_id)
    if not det or not det.get("oos"):
        return None
    o = det["oos"]
    return {"status": "ready", "strategy": strategy_id, "num_trades": o.get("num_trades"),
            "profit_factor": o.get("profit_factor"), "expectancy_r": o.get("expectancy_r"),
            "win_rate_pct": o.get("win_rate_pct"), "net_return_pct": o.get("net_return_pct"),
            "max_drawdown_pct": o.get("max_drawdown_pct"), "sharpe": o.get("sharpe"),
            "period_start": o.get("period_start"), "period_end": o.get("period_end"),
            "mfe_r_quantiles": det.get("mfe_r_quantiles") or {}, "data_label": data_label,
            "low_sample": (o.get("num_trades") or 0) < 30, "method": f"tournament {rep.get('run_id')}"}
