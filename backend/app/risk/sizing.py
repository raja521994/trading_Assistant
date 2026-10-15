"""Position sizing from equity, entry, stop distance and instrument constraints."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SizeResult:
    qty: int
    risk_per_unit: float
    risk_amount: float
    capital_required: float
    allocation_pct: float
    limiting_factor: str
    assumptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def position_size(equity: float, entry: float, stop: float, risk_pct: float, max_alloc_pct: float,
                  cash_available: float | None = None, lot_size: int = 1, max_leverage: float = 1.0) -> SizeResult:
    assumptions = [
        f"Account equity Rs {equity:,.0f} (paper account / configured value)",
        f"Risk per trade {risk_pct:.2f}% of equity = Rs {equity * risk_pct / 100:,.0f}",
        f"Max allocation per instrument {max_alloc_pct:.1f}% of equity",
        f"Lot size {lot_size}; leverage cap {max_leverage:.1f}x (no margin products used)",
        "Stop is assumed to fill at the stop price; gaps can cause larger losses",
    ]
    risk_per_unit = abs(entry - stop)
    if entry <= 0 or risk_per_unit <= 0 or equity <= 0:
        return SizeResult(0, risk_per_unit, 0.0, 0.0, 0.0, "invalid entry/stop", assumptions)
    budget = equity * risk_pct / 100.0
    caps = {
        "risk budget": math.floor(budget / risk_per_unit),
        "max allocation": math.floor(equity * max_alloc_pct / 100.0 / entry),
        "leverage cap": math.floor(equity * max_leverage / entry),
    }
    if cash_available is not None:
        caps["available cash"] = math.floor(max(cash_available, 0.0) / entry)
    limiting = min(caps, key=lambda k: caps[k])
    qty = max(caps[limiting], 0)
    if lot_size > 1:
        qty = (qty // lot_size) * lot_size
    capital = qty * entry
    return SizeResult(qty, round(risk_per_unit, 4), round(qty * risk_per_unit, 2), round(capital, 2),
                      round(capital / equity * 100.0, 2) if equity else 0.0, limiting, assumptions)
