"""Zerodha-style Indian equity transaction-cost model (ESTIMATES - verify against the broker's current charges page).

Rates are configurable; defaults reflect publicly listed equity charges as understood at build time and are
labelled as estimates everywhere they are shown. Product types: CNC (delivery) and MIS (intraday).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Product = Literal["CNC", "MIS"]
Side = Literal["BUY", "SELL"]


class CostModel(BaseModel):
    brokerage_delivery_pct: float = 0.0
    brokerage_intraday_pct: float = 0.03
    brokerage_cap_per_order: float = 20.0
    stt_delivery_pct: float = 0.1  # on buy and sell
    stt_intraday_sell_pct: float = 0.025  # sell side only
    exchange_txn_pct_nse: float = 0.00297
    exchange_txn_pct_bse: float = 0.00375
    sebi_fee_per_crore: float = 10.0
    stamp_delivery_buy_pct: float = 0.015
    stamp_intraday_buy_pct: float = 0.003
    gst_pct: float = 18.0
    dp_charge_per_sell: float = 15.93  # delivery sell, per scrip per day, incl. GST
    note: str = ("Estimated Zerodha-style equity charges; verify current rates at https://zerodha.com/charges "
                 "before relying on them. Excludes income tax on gains.")


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
