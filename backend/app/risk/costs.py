"""Indian equity / F&O transaction-cost model (ESTIMATES - verify against the broker's current charges page).

Rates live in backend/config/charges.yaml with an `as_of` date so they can be updated without code changes.
Separate models: equity delivery (CNC), equity intraday (MIS), futures, options. Liquidity-based slippage helper.
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

Product = Literal["CNC", "MIS"]
Side = Literal["BUY", "SELL"]
CHARGES_FILE = Path(__file__).resolve().parents[2] / "config" / "charges.yaml"


@lru_cache
def charges_config() -> dict[str, Any]:
    try:
        return yaml.safe_load(CHARGES_FILE.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}


def _eq(key: str, default: float) -> float:
    return float((charges_config().get("equity") or {}).get(key, default))


class CostModel(BaseModel):
    brokerage_delivery_pct: float = Field(default_factory=lambda: _eq("brokerage_delivery_pct", 0.0))
    brokerage_intraday_pct: float = Field(default_factory=lambda: _eq("brokerage_intraday_pct", 0.03))
    brokerage_cap_per_order: float = Field(default_factory=lambda: _eq("brokerage_cap_per_order", 20.0))
    stt_delivery_pct: float = Field(default_factory=lambda: _eq("stt_delivery_pct", 0.1))
    stt_intraday_sell_pct: float = Field(default_factory=lambda: _eq("stt_intraday_sell_pct", 0.025))
    exchange_txn_pct_nse: float = Field(default_factory=lambda: _eq("exchange_txn_pct_nse", 0.00297))
    exchange_txn_pct_bse: float = Field(default_factory=lambda: _eq("exchange_txn_pct_bse", 0.00375))
    sebi_fee_per_crore: float = Field(default_factory=lambda: _eq("sebi_fee_per_crore", 10.0))
    stamp_delivery_buy_pct: float = Field(default_factory=lambda: _eq("stamp_delivery_buy_pct", 0.015))
    stamp_intraday_buy_pct: float = Field(default_factory=lambda: _eq("stamp_intraday_buy_pct", 0.003))
    gst_pct: float = Field(default_factory=lambda: _eq("gst_pct", 18.0))
    dp_charge_per_sell: float = Field(default_factory=lambda: _eq("dp_charge_per_sell", 15.93))
    as_of: str = Field(default_factory=lambda: str(charges_config().get("as_of", "unknown")))
    note: str = ("Estimated Zerodha-style charges from config/charges.yaml; verify current rates at "
                 "https://zerodha.com/charges before relying on them. Excludes income tax on gains.")


def liquidity_slippage_bps(avg_turnover_cr: float | None, order_value: float = 0.0) -> float:
    """Per-side slippage: liquidity tier + square-root size impact (see config/charges.yaml)."""
    cfg = charges_config().get("slippage") or {}
    tiers = cfg.get("tiers_cr_bps") or [[500, 3], [100, 5], [20, 10], [5, 20], [0, 40]]
    if avg_turnover_cr is None:
        return float(tiers[-1][1])
    base = next((float(bps) for lim, bps in tiers if avg_turnover_cr >= lim), float(tiers[-1][1]))
    adv = avg_turnover_cr * 1e7
    impact = float(cfg.get("impact_k", 2.0)) * math.sqrt(order_value / adv) * 100 if adv > 0 and order_value > 0 else 0.0
    return round(base + impact, 2)


def fo_order_costs(side: Side, value: float, instrument: Literal["FUT", "OPT"], orders: int = 1) -> dict[str, float]:
    """Futures / options charges for one order. For options, `value` is the PREMIUM turnover."""
    c = charges_config().get("futures" if instrument == "FUT" else "options") or {}
    if instrument == "FUT":
        brokerage = min(value * float(c.get("brokerage_pct", 0.03)) / 100, float(c.get("brokerage_cap_per_order", 20)))
    else:
        brokerage = float(c.get("brokerage_per_order", 20.0)) * orders
    stt = value * float(c.get("stt_sell_pct", 0.02 if instrument == "FUT" else 0.1)) / 100 if side == "SELL" else 0.0
    txn = value * float(c.get("exchange_txn_pct_nse", 0.00173 if instrument == "FUT" else 0.03503)) / 100
    sebi = value * float(c.get("sebi_fee_per_crore", 10.0)) / 1e7
    stamp = value * float(c.get("stamp_buy_pct", 0.002 if instrument == "FUT" else 0.003)) / 100 if side == "BUY" else 0.0
    gst = (brokerage + txn + sebi) * float(c.get("gst_pct", 18.0)) / 100
    total = brokerage + stt + txn + sebi + stamp + gst
    return {"brokerage": round(brokerage, 4), "stt": round(stt, 4), "exchange_txn": round(txn, 4),
            "sebi": round(sebi, 4), "stamp_duty": round(stamp, 4), "gst": round(gst, 4), "total": round(total, 4)}


def order_costs(side: Side, price: float, qty: int, product: Product, exchange: str, m: CostModel) -> dict[str, float]:
    value = abs(price * qty)
    if value <= 0:
        return {"value": 0.0, "total": 0.0}
    if product == "CNC":
        brokerage = value * m.brokerage_delivery_pct / 100.0
        stt = value * m.stt_delivery_pct / 100.0
        stamp = value * m.stamp_delivery_buy_pct / 100.0 if side == "BUY" else 0.0
        dp = m.dp_charge_per_sell if side == "SELL" else 0.0
    else:
        brokerage = min(value * m.brokerage_intraday_pct / 100.0, m.brokerage_cap_per_order)
        stt = value * m.stt_intraday_sell_pct / 100.0 if side == "SELL" else 0.0
        stamp = value * m.stamp_intraday_buy_pct / 100.0 if side == "BUY" else 0.0
        dp = 0.0
    txn_pct = m.exchange_txn_pct_bse if exchange.upper() == "BSE" else m.exchange_txn_pct_nse
    txn = value * txn_pct / 100.0
    sebi = value * m.sebi_fee_per_crore / 1e7
    gst = (brokerage + txn + sebi) * m.gst_pct / 100.0
    total = brokerage + stt + txn + sebi + stamp + gst + dp
    return {"value": round(value, 2), "brokerage": round(brokerage, 4), "stt": round(stt, 4),
            "exchange_txn": round(txn, 4), "sebi": round(sebi, 4), "stamp_duty": round(stamp, 4),
            "gst": round(gst, 4), "dp_charges": round(dp, 4), "total": round(total, 4)}


def round_trip_costs(direction: str, entry: float, exit_: float, qty: int, product: Product, exchange: str,
                     m: CostModel) -> dict[str, float]:
    buy_px, sell_px = (entry, exit_) if direction == "LONG" else (exit_, entry)
    buy = order_costs("BUY", buy_px, qty, product, exchange, m)
    sell = order_costs("SELL", sell_px, qty, product, exchange, m)
    total = buy["total"] + sell["total"]
    turnover = buy["value"] + sell["value"]
    return {"buy_side": buy["total"], "sell_side": sell["total"], "total": round(total, 2),
            "pct_of_turnover": round(total / turnover * 100.0, 4) if turnover else 0.0}


def product_for_mode(mode: str) -> Product:
    return "MIS" if mode == "intraday" else "CNC"
