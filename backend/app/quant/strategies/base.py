"""Strategy base class and registry. Strategies are deterministic rule sets loaded from versioned YAML specs.

Contract (prevents look-ahead): `entry(a, i)` / `exit(a, i, ...)` may only read array elements <= i.
Indicators in `prepare` are causal. Fills happen at the NEXT bar's open (handled by the backtester/signal engine).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from ..features import add_common_features

SPEC_DIR = Path(__file__).resolve().parent.parent / "specs"


@dataclass
class Setup:
    direction: str  # LONG | SHORT
    stop: float
    target_r: float | None
    reasons: list[str]
    invalidation: str
    max_hold_bars: int


@dataclass
class StrategyMeta:
    id: str
    name: str
    version: str
    category: str
    modes: list[str]
    timeframe: str
    direction: str
    contrarian_components: list[str] = field(default_factory=list)


class Strategy:
    id: str = "base"

    def __init__(self, spec: dict[str, Any], params: dict[str, Any] | None = None) -> None:
        self.spec = spec
        self.params: dict[str, Any] = {**spec.get("params", {}), **(params or {})}
        self.meta = StrategyMeta(
            id=spec["id"], name=spec["name"], version=str(spec["version"]), category=spec["category"],
            modes=list(spec["modes"]), timeframe=spec["timeframe"], direction=spec["direction"],
            contrarian_components=list(spec.get("contrarian_components", [])),
        )

    @property
    def intraday(self) -> bool:
        return self.meta.timeframe != "1d"

    @property
    def warmup(self) -> int:
        return 60

    @property
    def one_trade_per_session(self) -> bool:
        return False

    @property
    def flat_at_session_end(self) -> bool:
        return self.intraday

    def prepare(self, df: pd.DataFrame, base: pd.DataFrame | None = None) -> pd.DataFrame:
        """`base` = precomputed common features for `df` (reused across strategies)."""
        return base.copy() if base is not None else add_common_features(df, intraday=self.intraday)

    def entry(self, a: dict[str, np.ndarray], i: int) -> Setup | None:
        raise NotImplementedError

    def exit(self, a: dict[str, np.ndarray], i: int, direction: str) -> str | None:
        return None

    def watch(self, a: dict[str, np.ndarray], i: int) -> str | None:
        return None

    def snapshot(self, a: dict[str, np.ndarray], i: int) -> dict[str, float | None]:
        keys = ["close", "ema20", "ema50", "ema200", "rsi14", "macd_hist", "adx14", "atr14", "atr_pct", "vol_ratio"]
        keys += self.extra_snapshot_keys()
        return {k: _num(a[k][i]) for k in keys if k in a}

    def extra_snapshot_keys(self) -> list[str]:
        return []

    def describe(self) -> dict[str, Any]:
        return {**self.spec, "params": self.params}


def _num(x: float) -> float | None:
    return None if x is None or x != x else round(float(x), 4)


def ok(*vals: float) -> bool:
    return all(v is not None and v == v for v in vals)


@lru_cache
def load_specs() -> dict[str, dict[str, Any]]:
    specs = {}
    for p in sorted(SPEC_DIR.glob("*.yaml")):
        spec = yaml.safe_load(p.read_text(encoding="utf-8"))
        specs[spec["id"]] = spec
    return specs


def get_strategy(strategy_id: str, params: dict[str, Any] | None = None) -> Strategy:
    from . import donchian, ema_cross, orb_vwap, rsi_meanrev, supertrend_trend

    classes: dict[str, type[Strategy]] = {
        "ema_cross": ema_cross.EmaCross,
        "donchian_breakout": donchian.DonchianBreakout,
        "rsi2_meanrev": rsi_meanrev.Rsi2MeanReversion,
        "supertrend_trend": supertrend_trend.SupertrendTrend,
        "orb_vwap": orb_vwap.OrbVwap,
    }
    specs = load_specs()
    if strategy_id not in specs or strategy_id not in classes:
        raise KeyError(f"Unknown strategy '{strategy_id}'")
    return classes[strategy_id](specs[strategy_id], params)


def all_strategy_ids() -> list[str]:
    return list(load_specs().keys())
