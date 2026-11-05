"""Strategy tournament: validates every testable strategy in background worker processes, applies the pass
criteria (config/tournament.yaml) including multiple-testing control, ranks ONLY passed strategies, runs a
random-walk control (nothing may pass), and writes JSON / CSV / HTML reports keyed by data + config hash.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from ..config import BACKEND_DIR
from ..quant.library.catalog import CATALOG, FAMILIES
from . import stats
from .evaluate import Bundle, evaluate_in_worker, init_worker

log = logging.getLogger(__name__)
CONFIG_FILE = BACKEND_DIR / "config" / "tournament.yaml"
OUT_DIR = Path(os.environ.get("TOURNAMENT_DIR") or BACKEND_DIR / "var" / "tournament")
TOURNAMENT_VERSION = "1.0"
NO_TRADE_MSG = "NO TRADE: no setup has a validated edge in the current regime"

_state: dict[str, Any] = {"state": "idle", "progress": 0, "total": 0, "current": [], "started_at": None,
                          "message": None, "run_id": None, "error": None}
_lock = threading.Lock()


# ------------------------------------------------------------------ config

def load_config() -> dict[str, Any]:
    return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))


def save_config(updates: dict[str, Any]) -> dict[str, Any]:
    conf = load_config()
    for sect in ("criteria", "score_weights"):
        if sect in updates and isinstance(updates[sect], dict):
            for k, v in updates[sect].items():
                if k in conf[sect] and not isinstance(conf[sect][k], list):
                    conf[sect][k] = type(conf[sect][k])(v) if conf[sect][k] is not None else v
    CONFIG_FILE.write_text(yaml.safe_dump(conf, sort_keys=False), encoding="utf-8")
    return conf


# ------------------------------------------------------------------ data bundle

def total_return_index(close: pd.Series, div_points: pd.Series | None) -> tuple[pd.Series, str]:
    """NIFTY 50 total-return proxy: price index + daily increments of the NIFTY50 Dividend Points index
    (which resets every year), reinvested. Falls back to the price index (stated) if unavailable."""
    if div_points is None or len(div_points) < 50:
        return close / close.iloc[0], "NIFTY 50 price index (dividends not available)"
    dp = div_points.reindex(close.index).ffill().fillna(0.0)
    inc = dp.diff()
    inc = inc.where(inc >= 0, dp)  # yearly reset: the new value is the increment
    inc.iloc[0] = 0.0
    growth = (close + inc) / close.shift(1)
    growth.iloc[0] = 1.0
    return growth.cumprod(), "NIFTY 50 total return (price + NIFTY50 Dividend Points)"


def regime_frame(close: pd.Series, vix: pd.Series | None) -> pd.DataFrame:
    sma50, sma200 = close.rolling(50).mean(), close.rolling(200).mean()
    trend = np.where((close > sma200) & (sma50 > sma200), "bull",
                     np.where((close < sma200) & (sma50 < sma200), "bear", "sideways"))
    df = pd.DataFrame({"above200": close > sma200, "trend": trend}, index=close.index)
    df.loc[sma200.isna(), "trend"] = "unknown"
    if vix is not None and len(vix) > 60:
        v = vix.reindex(close.index).ffill()
        med = v.rolling(252, min_periods=60).median()
        df["vix"] = np.where(v.isna() | med.isna(), None, np.where(v > med, "high", "low"))
    else:
        df["vix"] = None
    return df


def build_bundle(session, conf: dict[str, Any]) -> Bundle:  # noqa: ANN001
    from ..data.constituents import INDUSTRY_SECTOR_INDEX, load as load_constituents
    from ..providers.registry import active_provider
    from ..quant.universe import instrument_map, load_benchmark, load_universe
    from ..risk.limits import load_risk_config

    label = active_provider().capabilities().data_status.value
    imap = instrument_map(session)
    want = {r["symbol"] for r in load_constituents(conf["universe"], allow_network=False)}
    daily = {k: df for k, df in load_universe(session, "1d").items()
             if (not want or k[1] in want or label == "SIMULATED") and len(df) >= conf["min_daily_bars"]}
    iwant = {r["symbol"] for r in load_constituents(conf["intraday_universe"], allow_network=False)}
    intraday = {}
    for k, df in load_universe(session, "5m").items():
        if (iwant and k[1] not in iwant and label != "SIMULATED") or df.empty:
            continue
        if len(set(df.index.date)) >= conf["min_intraday_sessions"]:
            intraday[k] = df
    idx = {sym: df["close"] for (_, sym), df in load_universe(session, "1d", "INDEX").items()}
    bench_df = load_benchmark(session)
    bench = None if bench_df is None else bench_df["close"]
    tri, tri_label = (None, "unavailable") if bench is None else total_return_index(bench, idx.get("NIFTY50 DIV POINT"))
    regimes = None if bench is None else regime_frame(bench, idx.get("INDIA VIX"))
    sector_of = {sym: INDUSTRY_SECTOR_INDEX.get(inst.sector or "") for (_, sym), inst in imap.items()
                 if inst.instrument_type == "EQ"}
    rc = load_risk_config(session)
    return Bundle(daily, intraday, bench, tri, tri_label, regimes, idx, sector_of,
                  {m: rc.max_stop_pct(m) for m in ("intraday", "swing", "positional")}, label)


def random_walk_bundle(b: Bundle, n: int, seed: int) -> Bundle:
    """Zero-drift random walks with each symbol's own volatility and real volumes. No strategy should pass."""
    rng = np.random.default_rng(seed)
    out = {}
    for k, df in list(b.daily.items())[:n]:
        lr = np.log(df["close"]).diff().dropna()
        sig = float(lr.std()) or 0.015
        r = rng.normal(-0.5 * sig * sig, sig, len(df))
        close = 100 * np.exp(np.cumsum(r))
        gap = rng.normal(0, sig * 0.3, len(df))
        open_ = np.r_[100.0, close[:-1]] * np.exp(gap)
        hi = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0, sig * 0.5, len(df))))
        lo = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0, sig * 0.5, len(df))))
        out[("NSE", f"RW{len(out):03d}")] = pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close,
                                                         "volume": df["volume"].to_numpy()}, index=df.index)
    closes = pd.DataFrame({k: v["close"] for k, v in out.items()})
    bench = (closes / closes.iloc[0]).mean(axis=1) * 10000
    return Bundle(out, {}, bench, bench / bench.iloc[0], "random-walk equal-weight index", regime_frame(bench, None),
                  {}, {}, b.max_stop_pct, "RANDOM WALK (control)")


def bundle_hash(b: Bundle) -> str:
    h = hashlib.sha256()
    for name, data in (("d", b.daily), ("i", b.intraday)):
        for k in sorted(data):
            df = data[k]
            h.update(f"{name}{k}{len(df)}{df.index[0]}{df.index[-1]}{float(df['close'].sum()):.4f}".encode())
    if b.tri is not None:
        h.update(f"{len(b.tri)}{float(b.tri.iloc[-1]):.6f}".encode())
    return h.hexdigest()[:16]


def config_hash(conf: dict[str, Any]) -> str:
    from ..backtest.service import ENGINE_VERSION

    payload = json.dumps({"conf": conf, "engine": ENGINE_VERSION, "t": TOURNAMENT_VERSION,
                          "catalog": [e.as_dict() for e in CATALOG]}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ------------------------------------------------------------------ scoring & pass logic

def composite_score(r: dict[str, Any], weights: dict[str, float]) -> dict[str, Any]:
    o = r.get("oos") or {}
    clip = lambda x: max(0.0, min(100.0, x))  # noqa: E731
    pf, exp = o.get("profit_factor") or 0, o.get("expectancy_r") or 0
    comp = {
        "edge": clip(((min(pf, 3) - 1) / 1.0 * 100 + exp / 0.5 * 100) / 2),
        "risk_adjusted": clip((o.get("calmar") or 0) / 2 * 100),
        "robustness": clip(0.5 * (r.get("stability") or {}).get("score", 0)
                           + 0.5 * 100 * (r["subperiods"]["working"] / max(r["subperiods"]["total"], 1))),
        "significance": clip(100 * (r.get("deflated_sharpe") or 0)),
        "benchmark": clip(50 + ((o.get("annualised_return_pct") or 0) - (r.get("benchmark_annualised_pct") or 0)) * 5),
        "capacity": clip(50 * math.log10(max(o.get("capacity_cr") or 1, 1)) if o.get("capacity_cr") else 0),
    }
    tot = sum(weights.values()) or 1
    score = sum(comp[k] * weights.get(k, 0) for k in comp) / tot
    return {"score": round(score, 1), "components": {k: round(v, 1) for k, v in comp.items()}}


def finalize(results: list[dict[str, Any]], conf: dict[str, Any]) -> dict[str, Any]:
    crit, weights = conf["criteria"], conf["score_weights"]
    tested = [r for r in results if r.get("status") == "TESTED"]
    n_trials = sum(r.get("grid_size", 1) for r in tested)
    # Luck threshold: expected best Sharpe among n_trials unskilled configurations, using each strategy's null
    # sampling variance (1/T). (The cross-sectional variance of trial Sharpes is dominated by consistently losing
    # strategies and produced an unreachable bar, so it is reported but not used.)
    srs = [r["sharpe_stats"]["sr"] for r in tested if r["sharpe_stats"].get("sr") is not None]
    var_sr = float(np.var(srs, ddof=1)) if len(srs) > 1 else 0.0
    t_med = float(np.median([r["sharpe_stats"]["t"] for r in tested])) if tested else 0
    sr0 = stats.expected_max_sharpe(max(n_trials, 2), 1.0 / max(t_med, 1))
    pvals = {r["id"]: r["bootstrap"].get("p_value") for r in tested}
    bh = stats.benjamini_hochberg(pvals, crit["fdr_alpha"])
    for r in results:
        if r.get("status") != "TESTED":
            r["result"] = "ERROR" if r.get("status") == "ERROR" else "NO DATA"
            r["first_failed"] = r.get("error") or "data"
            continue
        dsr = stats.deflated_sharpe(r["sharpe_stats"], stats.expected_max_sharpe(
            max(n_trials, 2), 1.0 / max(r["sharpe_stats"].get("t") or t_med, 1)))
        r["deflated_sharpe"] = None if dsr is None else round(dsr, 4)
        r["fdr_discovery"] = r["id"] in bh
        for c in r["checks"]:
            if c["name"] == "multiple_testing":
                c["passed"] = dsr is not None and dsr >= crit["min_deflated_sharpe"]
                c["value"] = r["deflated_sharpe"]
                c["detail"] = (f"Deflated Sharpe probability {r['deflated_sharpe']} (need >= {crit['min_deflated_sharpe']}; "
                               f"{n_trials} configurations tested, luck threshold SR {sr0 * math.sqrt(252):.2f}/yr); "
                               f"BH-FDR discovery: {'yes' if r['fdr_discovery'] else 'no'}")
        failed = [c for c in r["checks"] if not c["passed"]]
        r["result"] = "PASS" if not failed else "FAIL"
        r["first_failed"] = failed[0]["name"] if failed else None
        r["first_failed_detail"] = failed[0]["detail"] if failed else None
        b = r.get("benchmark") or {}
        yrs = max((pd.Timestamp(r["oos"]["period_end"]) - pd.Timestamp(r["oos"]["period_start"])).days / 365.25, 1e-9) \
            if r["oos"].get("period_start") else 1
        r["benchmark_annualised_pct"] = None if not b else round(((1 + b["return_pct"] / 100) ** (1 / yrs) - 1) * 100, 3)
        r["composite"] = composite_score(r, weights)
        r["best_regime"] = _best_regime(r)
        r["pass_reason"] = _pass_reason(r) if r["result"] == "PASS" else None
    return {"n_trials": n_trials, "sr0_per_period": round(sr0, 5), "sr0_annualised": round(sr0 * math.sqrt(252), 3),
            "var_sr_cross_section": var_sr, "luck_threshold_method": "E[max Sharpe] of N null trials, variance 1/T",
            "tested": len(tested), "bh_discoveries": sorted(bh),
            "expected_false_positives": round(crit["fdr_alpha"] * len(tested), 2)}


def _best_regime(r: dict[str, Any]) -> str | None:
    best, best_pf = None, 0.0
    for k, v in (r.get("regimes") or {}).items():
        if k.startswith("trend:") and v["num_trades"] >= 20 and (v["profit_factor"] or 0) > best_pf:
            best, best_pf = k.split(":", 1)[1], v["profit_factor"]
    return best


def _pass_reason(r: dict[str, Any]) -> str:
    o = r["oos"]
    return (f"{o['num_trades']} OOS trades, PF {o['profit_factor']} (CI low {r['bootstrap']['pf_ci'][0]}), "
            f"expectancy {o['expectancy_r']}R, net {o['net_return_pct']}% vs NIFTY {r['benchmark'].get('return_pct')}%, "
            f"max DD {o['max_drawdown_pct']}%, stable under +/-20% parameters, survives 2x costs and "
            f"{r['subperiods']['working']}/{r['subperiods']['total']} sub-periods; deflated Sharpe {r['deflated_sharpe']}.")


def evidence_summary(results: list[dict[str, Any]], meta: dict[str, Any], control: dict[str, Any] | None) -> list[str]:
    out = []
    by_fam: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        if r.get("result") in ("PASS", "FAIL"):
            by_fam.setdefault(r["family"], []).append(r)
    for fam, rs in sorted(by_fam.items()):
        pfs = [x["oos"]["profit_factor"] for x in rs if x["oos"].get("profit_factor") is not None]
        npass = sum(x["result"] == "PASS" for x in rs)
        med = float(np.median(pfs)) if pfs else None
        verdict = ("shows a validated edge" if npass else
                   "no validated edge after costs" if med is not None and med < 1.0 else
                   "some profitable results but none survive every robustness test")
        out.append(f"{FAMILIES[fam]}: {npass} of {len(rs)} passed; median OOS profit factor "
                   f"{'n/a' if med is None else f'{med:.2f}'} - {verdict}.")
    out.append(f"Multiple testing: {meta['tested']} strategies / {meta['n_trials']} parameter configurations were "
               f"tested. At a 5% significance level about {meta['expected_false_positives']} would look 'significant' "
               f"by luck alone; the deflated Sharpe ratio raises the bar to an annualised Sharpe of "
               f"{meta['sr0_annualised']} before any result counts.")
    if control:
        out.append(f"Random-walk control: {control['passed']} of {control['tested']} strategies passed on synthetic "
                   f"zero-drift data ({'validation is discriminating' if control['passed'] == 0 else 'WARNING: validation is too lenient'}).")
    out.append("Overfitting warning: parameter grids were fixed before testing and are never tuned to make a "
               "strategy pass. Results use current index constituents (survivorship bias) and Zerodha-style "
               "charge estimates; past out-of-sample results do not guarantee future returns.")
    return out


# ------------------------------------------------------------------ running

def _workers() -> int:
    env = os.environ.get("TOURNAMENT_WORKERS")
    if env:
        return max(1, int(env))
    return max(1, min((os.cpu_count() or 2) - 2, 4))


def _run_pool(bundle: Bundle, entries: list[dict[str, Any]], conf: dict[str, Any], phase: str) -> list[dict[str, Any]]:
    results = []
    with ProcessPoolExecutor(max_workers=_workers(), initializer=init_worker, initargs=(bundle,)) as ex:
        futs = {ex.submit(evaluate_in_worker, e, conf): e for e in entries}
        with _lock:
            _state["current"] = [e["id"] for e in entries]
        for f in as_completed(futs):
            e = futs[f]
            try:
                results.append(f.result())
            except Exception as exc:  # noqa: BLE001 - e.g. worker killed (out of memory)
                results.append({"id": e["id"], "status": "ERROR", "error": f"{type(exc).__name__}: {exc}"})
            with _lock:
                _state["progress"] += 1
                _state["current"] = [x for x in _state["current"] if x != e["id"]]
                _state["message"] = f"{phase}: finished {e['id']}"
    return results


def _order(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    weight = {"portfolio": 1, "pairs": 2}
    return sorted(entries, key=lambda e: (-len(e["grid"]), weight.get(e["kind"], 0), e["timeframe"] != "1d"))


def run_tournament(force: bool = False, only: list[str] | None = None) -> dict[str, Any]:
    from ..db import session_scope

    conf = load_config()
    with session_scope() as s:
        bundle = build_bundle(s, conf)
    key = f"{bundle_hash(bundle)}-{config_hash(conf)}"
    if not force and not only:
        cached = find_report(key)
        if cached:
            with _lock:
                _state.update({"state": "done", "message": "Cached result (same data and configuration)",
                               "run_id": cached["run_id"]})
            return cached
    entries = [e.as_dict() for e in CATALOG if e.testable and (not only or e.id in only)]
    control_entries = [e for e in entries if e["timeframe"] == "1d" and e["id"] != "sector_rotation"] \
        if conf["control"].get("random_walk") and not only else []
    with _lock:
        _state.update({"progress": 0, "total": len(entries) + len(control_entries), "message": "validating strategies"})
    t0 = time.time()
    results = _run_pool(bundle, _order(entries), conf, "strategies")
    meta = finalize(results, conf)
    control = None
    if control_entries:
        rw = random_walk_bundle(bundle, int(conf["control"]["symbols"]), int(conf["control"]["seed"]))
        cres = _run_pool(rw, _order(control_entries), conf, "random-walk control")
        finalize(cres, conf)
        control = {"tested": sum(r.get("result") in ("PASS", "FAIL") for r in cres),
                   "passed": sum(r.get("result") == "PASS" for r in cres),
                   "symbols": len(rw.daily), "label": rw.label,
                   "results": [{"id": r["id"], "result": r.get("result"), "first_failed": r.get("first_failed"),
                                "profit_factor": (r.get("oos") or {}).get("profit_factor"),
                                "num_trades": (r.get("oos") or {}).get("num_trades")} for r in cres]}
    report = assemble(results, meta, control, conf, bundle, key, time.time() - t0)
    save_report(report)
    return report


def assemble(results, meta, control, conf, bundle: Bundle, key: str, seconds: float) -> dict[str, Any]:  # noqa: ANN001
    passed = sorted([r for r in results if r.get("result") == "PASS"], key=lambda r: -r["composite"]["score"])
    top = passed[:10]
    order = {"PASS": 0, "FAIL": 1, "NO DATA": 2, "ERROR": 3}
    leaderboard = sorted(results, key=lambda r: (order.get(r.get("result"), 9), -(r.get("composite") or {}).get("score", 0)))
    daily_dates = [df.index for df in bundle.daily.values()]
    intra_dates = [df.index for df in bundle.intraday.values()]
    per_regime: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        for k, v in (r.get("regimes") or {}).items():
            per_regime.setdefault(k, []).append({"id": r["id"], "name": r["name"], "result": r.get("result"), **v,
                                                 "qualifies": r.get("result") == "PASS" and v["num_trades"] >= 20
                                                 and (v["profit_factor"] or 0) >= conf["criteria"]["min_profit_factor"]})
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    return {
        "run_id": run_id, "cache_key": key, "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "seconds": round(seconds, 1), "data_label": bundle.label, "config": conf,
        "data": {"daily_symbols": len(bundle.daily), "intraday_symbols": len(bundle.intraday),
                 "daily_range": [str(min(d[0] for d in daily_dates).date()), str(max(d[-1] for d in daily_dates).date())]
                 if daily_dates else None,
                 "intraday_range": [str(min(d[0] for d in intra_dates).date()), str(max(d[-1] for d in intra_dates).date())]
                 if intra_dates else None,
                 "benchmark": bundle.tri_label,
                 "survivorship_note": "Current index constituents only (not point-in-time): survivorship bias."},
        "headline": (f"{len(passed)} of {meta['tested']} tested strategies passed every criterion."
                     + ("" if len(passed) >= 10 else f" Only {len(passed)} of 10 strategies passed.")),
        "top10": [_row(r, i + 1) for i, r in enumerate(top)],
        "leaderboard": [_row(r, None) for r in leaderboard],
        "not_testable": [{"no": e.no, "id": e.id, "name": e.name, "family": e.family_name, "missing_data": e.missing_data,
                          "note": e.note} for e in CATALOG if e.kind == "not_testable"],
        "context_inputs": [{"no": e.no, "id": e.id, "name": e.name, "missing_data": e.missing_data, "note": e.note}
                           for e in CATALOG if e.kind == "context"],
        "per_regime": per_regime, "multiple_testing": meta, "control": control,
        "summary": evidence_summary(results, meta, control),
        "details": {r["id"]: {k: r.get(k) for k in ("folds", "oos", "bootstrap", "benchmark", "double_costs", "stability",
                                                    "subperiods", "regimes", "checks", "equity_curve", "drawdown_curve",
                                                    "exit_reasons", "composite", "deflated_sharpe", "error",
                                                    "mfe_r_quantiles", "family", "family_name", "result",
                                                    "survivorship")}
                    for r in results},
    }


def _row(r: dict[str, Any], rank: int | None) -> dict[str, Any]:
    o, b = r.get("oos") or {}, r.get("benchmark") or {}
    return {"rank": rank, "no": r.get("no"), "id": r["id"], "name": r.get("name"), "family": r.get("family_name"),
            "kind": r.get("kind"), "mode": r.get("mode"), "direction": r.get("direction"), "timeframe": r.get("timeframe"),
            "result": r.get("result"), "first_failed": r.get("first_failed"),
            "first_failed_detail": r.get("first_failed_detail"), "best_regime": r.get("best_regime"),
            "oos_trades": o.get("num_trades"), "profit_factor": o.get("profit_factor"),
            "pf_ci": (r.get("bootstrap") or {}).get("pf_ci"), "expectancy_r": o.get("expectancy_r"),
            "expectancy_ci": (r.get("bootstrap") or {}).get("expectancy_r_ci"), "net_return_pct": o.get("net_return_pct"),
            "benchmark_return_pct": b.get("return_pct"), "cagr_pct": o.get("cagr_pct"),
            "annualised_return_pct": o.get("annualised_return_pct"), "max_drawdown_pct": o.get("max_drawdown_pct"),
            "calmar": o.get("calmar"), "win_rate_pct": o.get("win_rate_pct"), "avg_holding_bars": o.get("avg_holding_bars"),
            "stability_score": (r.get("stability") or {}).get("score"), "deflated_sharpe": r.get("deflated_sharpe"),
            "score": (r.get("composite") or {}).get("score"), "pass_reason": r.get("pass_reason"),
            "data_label": r.get("data_label")}


# ------------------------------------------------------------------ persistence & exports

def save_report(report: dict[str, Any]) -> Path:
    d = OUT_DIR / report["run_id"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(json.dumps(report, default=str), encoding="utf-8")
    (d / "leaderboard.csv").write_text(leaderboard_csv(report), encoding="utf-8")
    from .report_html import render

    (d / "report.html").write_text(render(report), encoding="utf-8")
    (OUT_DIR / "latest.txt").write_text(report["run_id"], encoding="utf-8")
    return d


def leaderboard_csv(report: dict[str, Any]) -> str:
    buf = io.StringIO()
    rows = report["leaderboard"]
    cols = [c for c in rows[0].keys() if c not in ("pf_ci", "expectancy_ci")] + ["pf_ci_low", "pf_ci_high",
                                                                                "exp_ci_low", "exp_ci_high"] if rows else []
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for r in rows:
        x = {k: v for k, v in r.items() if k in cols}
        pci, eci = r.get("pf_ci") or [None, None], r.get("expectancy_ci") or [None, None]
        x.update({"pf_ci_low": pci[0], "pf_ci_high": pci[1], "exp_ci_low": eci[0], "exp_ci_high": eci[1]})
        w.writerow(x)
    for nt in report["not_testable"]:
        w.writerow({"no": nt["no"], "id": nt["id"], "name": nt["name"], "family": nt["family"], "result": "NOT TESTABLE",
                    "first_failed_detail": "; ".join(nt["missing_data"])})
    return buf.getvalue()


def list_runs() -> list[dict[str, Any]]:
    if not OUT_DIR.exists():
        return []
    out = []
    for d in sorted(OUT_DIR.iterdir(), reverse=True):
        f = d / "report.json"
        if f.exists():
            try:
                j = json.loads(f.read_text(encoding="utf-8"))
                out.append({"run_id": j["run_id"], "generated_at": j["generated_at"], "headline": j["headline"],
                            "cache_key": j["cache_key"], "data_label": j["data_label"]})
            except (ValueError, KeyError):
                continue
    return out


def load_report(run_id: str | None = None) -> dict[str, Any] | None:
    if run_id is None:
        p = OUT_DIR / "latest.txt"
        if not p.exists():
            return None
        run_id = p.read_text().strip()
    if not run_id.replace("-", "").isdigit():
        return None
    f = OUT_DIR / run_id / "report.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def find_report(key: str) -> dict[str, Any] | None:
    for r in list_runs():
        if r["cache_key"] == key:
            return load_report(r["run_id"])
    return None


def status() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def start_background(force: bool = False, only: list[str] | None = None) -> dict[str, Any]:
    with _lock:
        if _state["state"] == "running":
            return dict(_state)
        _state.update({"state": "running", "progress": 0, "total": 0, "started_at": int(time.time()), "error": None,
                       "message": "loading data", "run_id": None, "current": []})

    def work() -> None:
        try:
            rep = run_tournament(force, only)
            with _lock:
                _state.update({"state": "done", "run_id": rep["run_id"], "message": rep["headline"]})
        except Exception as exc:  # noqa: BLE001
            log.exception("tournament failed")
            with _lock:
                _state.update({"state": "error", "error": f"{type(exc).__name__}: {exc}"})

    threading.Thread(target=work, name="tournament", daemon=True).start()
    return status()
