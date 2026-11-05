"""Machine-learning meta-labelling (M. Lopez de Prado, "Advances in Financial Machine Learning", ch. 3).

The rule-based strategies decide WHEN a setup exists (side and stop); a secondary model estimates the PROBABILITY
that this particular setup ends with a profit, from causal features known at the signal close (trend, momentum,
volatility, volume, relative strength, stop distance, market regime, India VIX). It never creates trades on its own.

Validation is walk-forward by calendar year: for each test year the model is trained only on setups whose trades
EXITED at least 5 days before the year starts (purge + embargo), then scored on that year's setups. The model is
used by the decision engine only if this out-of-sample test shows real lift (AUC and expectancy of the selected
setups above all setups, with a bootstrap check); otherwise it is reported and ignored.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..config import BACKEND_DIR

log = logging.getLogger(__name__)
ML_DIR = Path(os.environ.get("ML_DIR") or BACKEND_DIR / "var" / "ml")
MODEL_FILE = ML_DIR / "meta_model.joblib"
REPORT_FILE = ML_DIR / "meta_report.json"
MODEL_VERSION = "1.0"
EMBARGO = pd.Timedelta(days=5)
FEATURES = ["c_ema20", "c_ema50", "c_ema200", "ema50_200", "rsi14", "macd_hist_pct", "adx14", "atr_pct", "vol_ratio",
            "bb_width", "ret_21", "ret_63", "atr_pct_rank", "dist_52w_high", "stop_pct", "rs_63", "nifty_c_sma200",
            "nifty_ret_21", "vix", "vix_rel", "strategy_code"]

_state: dict[str, Any] = {"state": "idle", "message": None, "error": None, "started_at": None}
_lock = threading.Lock()
_model_cache: dict[str, Any] = {"mtime": None, "obj": None}
_mf_cache: dict[str, Any] = {"key": None, "mf": None}


# ------------------------------------------------------------------ features

def market_frame(bench: pd.Series | None, vix: pd.Series | None) -> pd.DataFrame:
    if bench is None or bench.empty:
        return pd.DataFrame()
    sma200 = bench.rolling(200).mean()
    out = pd.DataFrame({"nifty_c_sma200": bench / sma200 - 1, "nifty_ret_21": bench.pct_change(21),
                        "nifty_ret_63": bench.pct_change(63)}, index=bench.index)
    if vix is not None and len(vix) > 60:
        v = vix.reindex(out.index).ffill()
        out["vix"] = v
        out["vix_rel"] = v / v.rolling(252, min_periods=60).median() - 1
    else:
        out["vix"] = np.nan
        out["vix_rel"] = np.nan
    return out


def stock_frame(feat: pd.DataFrame) -> pd.DataFrame:
    """Per-bar causal stock features from the common feature frame (quant/features.py)."""
    c = feat["close"]
    f = pd.DataFrame(index=feat.index)
    f["c_ema20"] = c / feat["ema20"] - 1
    f["c_ema50"] = c / feat["ema50"] - 1
    f["c_ema200"] = c / feat["ema200"] - 1
    f["ema50_200"] = feat["ema50"] / feat["ema200"] - 1
    f["rsi14"] = feat["rsi14"]
    f["macd_hist_pct"] = feat["macd_hist"] / c * 100
    f["adx14"] = feat["adx14"]
    f["atr_pct"] = feat["atr_pct"]
    f["vol_ratio"] = feat["vol_ratio"].clip(upper=10)
    f["bb_width"] = feat["bb_width"]
    f["ret_21"] = feat["ret_21"] if "ret_21" in feat else c.pct_change(21)
    f["ret_63"] = feat["ret_63"] if "ret_63" in feat else c.pct_change(63)
    f["atr_pct_rank"] = feat["atr_pct_rank"] if "atr_pct_rank" in feat else np.nan
    f["dist_52w_high"] = c / feat["high"].rolling(252, min_periods=60).max() - 1
    return f


def feature_row(sf: pd.DataFrame, mf: pd.DataFrame, ts: pd.Timestamp, entry: float, stop: float,
                strategy_code: int) -> dict[str, float] | None:
    """Features as of the signal bar `ts` (daily close). Market features use the same day's index close."""
    pos = sf.index.searchsorted(ts, side="right") - 1
    if pos < 0:
        return None
    row = sf.iloc[pos].to_dict()
    m = None
    if not mf.empty:
        mpos = mf.index.searchsorted(ts, side="right") - 1
        m = mf.iloc[mpos] if mpos >= 0 else None
    row["stop_pct"] = abs(entry - stop) / entry * 100 if entry else np.nan
    row["rs_63"] = row.get("ret_63", np.nan) - (float(m["nifty_ret_63"]) if m is not None else np.nan)
    for k in ("nifty_c_sma200", "nifty_ret_21", "vix", "vix_rel"):
        row[k] = float(m[k]) if m is not None and m[k] == m[k] else np.nan
    row["strategy_code"] = strategy_code
    return {k: (float(row[k]) if row.get(k) is not None else np.nan) for k in FEATURES}


# ------------------------------------------------------------------ dataset

def strategy_ids() -> list[str]:
    from ..quant.library.catalog import CATALOG

    return [e.id for e in CATALOG if e.kind == "event" and e.timeframe == "1d"]


def build_dataset(bundle: Any, progress: Any = None) -> pd.DataFrame:
    """Every setup the daily event strategies took on the full history (no portfolio cap), with its outcome."""
    from ..backtest.engine import BTConfig, run_portfolio
    from ..backtest.service import slippage_fn_for
    from ..quant.library.context import set_context
    from ..quant.strategies.base import get_strategy
    from ..quant.universe import features
    from ..risk.costs import CostModel

    set_context(bundle.benchmark, bundle.index_closes, bundle.sector_of)
    data = bundle.daily
    mf = market_frame(bundle.benchmark, bundle.index_closes.get("INDIA VIX"))
    feats = {k: features(k, df) for k, df in data.items()}
    sfs = {k: stock_frame(f) for k, f in feats.items()}
    slip = slippage_fn_for(data)
    rows = []
    ids = strategy_ids()
    for code, sid in enumerate(ids):
        if progress:
            progress(f"collecting setups: {sid} ({code + 1}/{len(ids)})")
        st = get_strategy(sid)
        cfg = BTConfig(capital=1e9, risk_pct=0.01, max_alloc_pct=100.0, max_positions=100_000, costs=CostModel(),
                       max_stop_pct=bundle.max_stop_pct.get(st.meta.modes[0], 6.0), slippage_fn=slip)
        prep = {k: st.prepare_keyed(k, df, feats[k]) for k, df in data.items()}
        res = run_portfolio(st, data, cfg, prep)
        for t in res["trades"]:
            k = (t.exchange, t.symbol)
            ts = pd.Timestamp(t.signal_ts or t.entry_ts)
            fr = feature_row(sfs[k], mf, ts, t.entry_price, t.stop, code)
            if fr is None:
                continue
            rows.append({**fr, "strategy": sid, "symbol": t.symbol, "signal_ts": ts,
                         "exit_ts": pd.Timestamp(t.exit_ts), "r": float(t.r_multiple), "win": int(t.r_multiple > 0)})
    df = pd.DataFrame(rows)
    if len(df):
        df = df.sort_values("signal_ts").reset_index(drop=True)
    return df


# ------------------------------------------------------------------ model & validation

def _new_model():
    from sklearn.ensemble import HistGradientBoostingClassifier

    return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=150, min_samples_leaf=50,
                                          l2_regularization=1.0,
                                          categorical_features=[FEATURES.index("strategy_code")], random_state=7)


def _r_stats(r: np.ndarray) -> dict[str, Any]:
    if len(r) == 0:
        return {"n": 0, "win_rate_pct": None, "expectancy_r": None, "profit_factor_r": None}
    gains, losses = r[r > 0].sum(), -r[r <= 0].sum()
    return {"n": int(len(r)), "win_rate_pct": round(float((r > 0).mean() * 100), 2),
            "expectancy_r": round(float(r.mean()), 4),
            "profit_factor_r": round(float(gains / losses), 3) if losses > 0 else None}


def walk_forward(df: pd.DataFrame, min_train: int = 400) -> dict[str, Any]:
    from sklearn.metrics import brier_score_loss, roc_auc_score

    tz = df["signal_ts"].dt.tz
    years = sorted(df["signal_ts"].dt.year.unique())
    oos, folds = [], []
    for y in years:
        start = pd.Timestamp(f"{y}-01-01", tz=tz)
        end = pd.Timestamp(f"{y + 1}-01-01", tz=tz)
        train_df = df[df["exit_ts"] < start - EMBARGO]
        test = df[(df["signal_ts"] >= start) & (df["signal_ts"] < end)]
        if len(train_df) < min_train or len(test) < 30 or train_df["win"].nunique() < 2:
            continue
        m = _new_model().fit(train_df[FEATURES], train_df["win"])
        thr = float(train_df["win"].mean())
        t = test.assign(p=m.predict_proba(test[FEATURES])[:, 1], thr=thr)
        oos.append(t)
        auc = roc_auc_score(t["win"], t["p"]) if t["win"].nunique() == 2 else None
        folds.append({"test_year": int(y), "train_setups": int(len(train_df)), "test_setups": int(len(t)),
                      "auc": None if auc is None else round(float(auc), 4), "threshold": round(thr, 4),
                      "all": _r_stats(t["r"].to_numpy()), "selected": _r_stats(t.loc[t["p"] >= thr, "r"].to_numpy())})
    if not oos:
        return {"folds": [], "useful": False, "verdict": "NOT USEFUL: not enough history for walk-forward",
                "error": "not enough history for walk-forward"}
    o = pd.concat(oos)
    mask = (o["p"] >= o["thr"]).to_numpy()
    r_all = o["r"].to_numpy()
    auc = float(roc_auc_score(o["win"], o["p"])) if o["win"].nunique() == 2 else None
    base = float(o["win"].mean())
    rng = np.random.default_rng(7)
    diffs = []
    for _ in range(1000):
        idx = rng.integers(0, len(o), len(o))
        if mask[idx].sum():
            diffs.append(r_all[idx][mask[idx]].mean() - r_all[idx].mean())
    p_no_lift = float(np.mean(np.asarray(diffs) <= 0)) if diffs else 1.0
    by_strat = []
    for sid, g in o.groupby("strategy"):
        by_strat.append({"strategy": sid, "all": _r_stats(g["r"].to_numpy()),
                         "selected": _r_stats(g.loc[g["p"] >= g["thr"], "r"].to_numpy())})
    calib = []
    for lo in np.round(np.arange(0, 1, 0.1), 1):
        b = o[(o["p"] >= lo) & (o["p"] < lo + 0.1)]
        if len(b) >= 20:
            calib.append({"p_from": float(lo), "n": int(len(b)), "mean_p": round(float(b["p"].mean()), 3),
                          "actual_win_rate": round(float(b["win"].mean()), 3),
                          "expectancy_r": round(float(b["r"].mean()), 3)})
    a, s = _r_stats(r_all), _r_stats(r_all[mask])
    useful = bool(auc is not None and auc >= 0.55 and s["n"] >= 100 and s["expectancy_r"] > a["expectancy_r"]
                  and (s["profit_factor_r"] or 0) >= (a["profit_factor_r"] or 0) and p_no_lift < 0.10)
    return {"folds": folds, "oos_auc": None if auc is None else round(auc, 4),
            "oos_brier": round(float(brier_score_loss(o["win"], o["p"])), 4),
            "base_rate_brier": round(base * (1 - base), 4), "oos_setups": int(len(o)), "all": a, "selected": s,
            "p_no_lift": round(p_no_lift, 4), "by_strategy": by_strat, "calibration": calib, "useful": useful,
            "verdict": ("USEFUL: out-of-sample, the setups the model selects beat all setups - used as a filter and "
                        "for ranking" if useful else
                        "NOT USEFUL: no reliable out-of-sample lift - reported only, not used for decisions"),
            "rule": "used only if OOS AUC >= 0.55, >= 100 selected setups, higher expectancy and PF than all setups, "
                    "and bootstrap p(no lift) < 0.10"}


def train(progress: Any = None, bundle: Any = None) -> dict[str, Any]:
    import joblib

    t0 = time.time()
    if bundle is None:
        from ..db import session_scope
        from ..validation.tournament import build_bundle, load_config

        with session_scope() as s:
            bundle = build_bundle(s, load_config())
    df = build_dataset(bundle, progress)
    if len(df) < 500:
        raise ValueError(f"only {len(df)} historical setups - not enough to train")
    if progress:
        progress(f"walk-forward validation on {len(df)} setups")
    wf = walk_forward(df)
    model = _new_model().fit(df[FEATURES], df["win"])
    thr = float(df["win"].mean())
    imp = None
    try:
        from sklearn.inspection import permutation_importance

        recent = df.tail(min(len(df), 3000))
        pi = permutation_importance(model, recent[FEATURES], recent["win"], n_repeats=3, random_state=7,
                                    scoring="roc_auc")
        imp = sorted(({"feature": f, "importance": round(float(v), 4)} for f, v in
                      zip(FEATURES, pi.importances_mean, strict=True)), key=lambda x: -x["importance"])[:12]
    except Exception:  # noqa: BLE001
        imp = None
    ML_DIR.mkdir(parents=True, exist_ok=True)
    meta = {"version": MODEL_VERSION, "trained_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "setups": int(len(df)), "base_win_rate": round(thr, 4), "threshold": round(thr, 4),
            "strategies": strategy_ids(), "features": FEATURES, "data_label": bundle.label,
            "data_range": [str(df["signal_ts"].min().date()), str(df["signal_ts"].max().date())],
            "seconds": round(time.time() - t0, 1), "feature_importance": imp, **wf}
    joblib.dump({"model": model, "meta": meta}, MODEL_FILE)
    REPORT_FILE.write_text(json.dumps(meta, default=str), encoding="utf-8")
    log.info("meta-label model trained: %s setups, OOS AUC %s - %s", len(df), wf.get("oos_auc"), wf.get("verdict"))
    return meta


def report() -> dict[str, Any] | None:
    if not REPORT_FILE.exists():
        return None
    return json.loads(REPORT_FILE.read_text(encoding="utf-8"))


def load_model() -> dict[str, Any] | None:
    if not MODEL_FILE.exists():
        return None
    m = MODEL_FILE.stat().st_mtime
    if _model_cache["mtime"] != m:
        import joblib

        _model_cache["obj"] = joblib.load(MODEL_FILE)
        _model_cache["mtime"] = m
    return _model_cache["obj"]


def _market_frame_cached() -> pd.DataFrame:
    from ..quant.library.context import get_context

    ctx = get_context()
    b = ctx.get("benchmark")
    v = (ctx.get("index") or {}).get("INDIA VIX")  # type: ignore[union-attr]
    key = (None if b is None else (len(b), b.index[-1], float(b.iloc[-1])),
           None if v is None else (len(v), v.index[-1], float(v.iloc[-1])))
    if _mf_cache["key"] != key:
        _mf_cache["mf"], _mf_cache["key"] = market_frame(b, v), key  # type: ignore[arg-type]
    return _mf_cache["mf"]


def score_setup(strategy_id: str, daily_feat: pd.DataFrame | None, ts: pd.Timestamp, entry: float, stop: float
                ) -> dict[str, Any] | None:
    """P(win) for a live daily setup. None when no model, unknown strategy or not enough data."""
    obj = load_model()
    if obj is None or daily_feat is None or len(daily_feat) < 60:
        return None
    meta = obj["meta"]
    if strategy_id not in meta["strategies"]:
        return None
    fr = feature_row(stock_frame(daily_feat), _market_frame_cached(), ts, entry, stop,
                     meta["strategies"].index(strategy_id))
    if fr is None:
        return None
    p = float(obj["model"].predict_proba(pd.DataFrame([fr])[FEATURES])[:, 1][0])
    bucket = next((c for c in meta.get("calibration") or [] if c["p_from"] <= p < c["p_from"] + 0.1), None)
    return {"p_win": round(p, 3), "threshold": meta["threshold"], "above_threshold": p >= meta["threshold"],
            "model_useful": bool(meta.get("useful")), "oos_auc": meta.get("oos_auc"),
            "bucket_win_rate": None if bucket is None else bucket["actual_win_rate"],
            "bucket_expectancy_r": None if bucket is None else bucket["expectancy_r"],
            "trained_at": meta["trained_at"]}


def status() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def start_background() -> dict[str, Any]:
    with _lock:
        if _state["state"] == "running":
            return dict(_state)
        _state.update({"state": "running", "message": "starting", "error": None, "started_at": int(time.time())})

    def prog(msg: str) -> None:
        with _lock:
            _state["message"] = msg

    def work() -> None:
        try:
            meta = train(prog)
            with _lock:
                _state.update({"state": "done", "message": meta["verdict"]})
        except Exception as exc:  # noqa: BLE001
            log.exception("meta-model training failed")
            with _lock:
                _state.update({"state": "error", "error": f"{type(exc).__name__}: {exc}"})

    threading.Thread(target=work, name="ml-train", daemon=True).start()
    return status()


def is_stale(days: int = 7) -> bool:
    r = report()
    if not r or r.get("version") != MODEL_VERSION:
        return True
    try:
        return (datetime.now().astimezone() - datetime.fromisoformat(r["trained_at"])).days >= days
    except (KeyError, ValueError):
        return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    out = train(lambda m: print(m, flush=True))
    print(json.dumps({k: out.get(k) for k in ("setups", "oos_auc", "oos_brier", "base_rate_brier", "all", "selected",
                                              "p_no_lift", "verdict", "feature_importance")}, indent=1, default=str))
    for f in out["folds"]:
        print(f)
    for b in out["by_strategy"]:
        print(b)
