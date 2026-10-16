"""Statistics for honest edge testing: bootstrap confidence intervals, deflated Sharpe ratio, FDR control."""

from __future__ import annotations

import math
from statistics import NormalDist
from typing import Any

import numpy as np

N = NormalDist()
EULER_GAMMA = 0.5772156649


def profit_factor(pnl: np.ndarray) -> float:
    win, loss = pnl[pnl > 0].sum(), -pnl[pnl <= 0].sum()
    if loss <= 0:
        return float("inf") if win > 0 else float("nan")
    return float(win / loss)


def bootstrap(pnl: list[float], r: list[float], samples: int = 2000, conf: float = 0.95, seed: int = 7
              ) -> dict[str, Any]:
    """Resample the OOS trade sequence with replacement: CIs for profit factor and expectancy (R), and a one-sided
    p-value for 'mean R <= 0'."""
    p, rr = np.asarray(pnl, float), np.asarray(r, float)
    n = len(p)
    if n < 5:
        return {"n": n, "pf_ci": [None, None], "expectancy_r_ci": [None, None], "p_value": None}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(samples, n))
    ps, rs = p[idx], rr[idx]
    wins = np.where(ps > 0, ps, 0).sum(axis=1)
    losses = -np.where(ps <= 0, ps, 0).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        pf = np.where(losses > 0, wins / losses, np.inf)
    exp_r = rs.mean(axis=1)
    lo, hi = (1 - conf) / 2 * 100, (1 + conf) / 2 * 100
    pf_f = pf[np.isfinite(pf)]
    pf_ci = [float(np.percentile(pf_f, lo)), float(np.percentile(pf_f, hi))] if len(pf_f) else [None, None]
    return {"n": n, "samples": samples, "confidence": conf, "pf_ci": [_r(x) for x in pf_ci],
            "expectancy_r_ci": [_r(float(np.percentile(exp_r, lo))), _r(float(np.percentile(exp_r, hi)))],
            "p_value": _r(float((exp_r <= 0).mean()), 4)}


def _r(x: float | None, nd: int = 3) -> float | None:
    return None if x is None or not math.isfinite(x) else round(x, nd)


def sharpe_stats(daily_returns: np.ndarray) -> dict[str, float | None]:
    r = np.asarray(daily_returns, float)
    r = r[np.isfinite(r)]
    t = len(r)
    if t < 20 or r.std(ddof=1) == 0:
        return {"sr": None, "t": t, "skew": None, "kurt": None}
    m, s = r.mean(), r.std(ddof=1)
    z = (r - m) / s
    return {"sr": float(m / s), "t": t, "skew": float((z ** 3).mean()), "kurt": float((z ** 4).mean())}


def expected_max_sharpe(n_trials: int, var_sr: float) -> float:
    """E[max SR] of n_trials unskilled strategies (Bailey & Lopez de Prado, 2014), per-period units."""
    if n_trials < 2 or var_sr <= 0:
        return 0.0
    return math.sqrt(var_sr) * ((1 - EULER_GAMMA) * N.inv_cdf(1 - 1 / n_trials)
                                + EULER_GAMMA * N.inv_cdf(1 - 1 / (n_trials * math.e)))


def deflated_sharpe(st: dict[str, Any], sr0: float) -> float | None:
    """Probability that the true Sharpe exceeds sr0 (the best Sharpe expected by luck across all trials)."""
    sr, t, sk, ku = st.get("sr"), st.get("t") or 0, st.get("skew"), st.get("kurt")
    if sr is None or t < 20 or sk is None or ku is None:
        return None
    denom = 1 - sk * sr + (ku - 1) / 4 * sr * sr
    if denom <= 0:
        return None
    return float(N.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom)))


def benjamini_hochberg(pvals: dict[str, float | None], alpha: float = 0.05) -> set[str]:
    items = sorted((p, k) for k, p in pvals.items() if p is not None)
    m = len(items)
    cutoff = 0
    for rank, (p, _) in enumerate(items, start=1):
        if p <= alpha * rank / m:
            cutoff = rank
    return {k for _, k in items[:cutoff]}


def max_drawdown(curve: np.ndarray) -> float:
    c = np.asarray(curve, float)
    if len(c) < 2:
        return 0.0
    return float((c / np.maximum.accumulate(c) - 1).min() * 100)
