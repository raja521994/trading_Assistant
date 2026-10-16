"""Pure trade-setup state machine shared by the live scanner, the paper engine and the backtester.

    WATCH -> SETUP -> TRIGGERED -> ACTIVE -> EXITED
                \\-> INVALIDATED / EXPIRED     \\-> INVALIDATED (entry gapped through the stop)

- SETUP: every entry rule was met on the close of the signal bar. The entry TRIGGER is a price level
  (breakout/trend strategies: the signal bar's high for longs / low for shorts; mean-reversion: the signal close).
- TRIGGERED: a later CLOSED bar closes at/above the trigger (long) or at/below it (short). Optionally (intraday) a
  live tick may trigger. The entry is executed at the NEXT bar's open (no same-bar fills, no look-ahead).
- ACTIVE: the entry is filled. Exits: stop, target, strategy exit rule, time limit -> EXITED.
- INVALIDATED: the stop is hit before the trigger, or the entry open gaps beyond the stop.
- EXPIRED: not triggered within `expiry_bars` bars after the signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field

WATCH, SETUP, TRIGGERED, ACTIVE, EXITED, INVALIDATED, EXPIRED = (
    "WATCH", "SETUP", "TRIGGERED", "ACTIVE", "EXITED", "INVALIDATED", "EXPIRED")


@dataclass
class Plan:
    direction: str  # LONG | SHORT
    trigger: float
    stop: float
    expiry_bars: int = 3
    state: str = SETUP
    bars_waited: int = 0
    fill: float | None = None
    events: list[tuple[str, str]] = field(default_factory=list)

    @property
    def long(self) -> bool:
        return self.direction == "LONG"


def on_bar_close(plan: Plan, high: float, low: float, close: float) -> str:
    """Advance a SETUP using one newly CLOSED bar (after the signal bar)."""
    if plan.state != SETUP:
        return plan.state
    hit_stop = low <= plan.stop if plan.long else high >= plan.stop
    crossed = close >= plan.trigger if plan.long else close <= plan.trigger
    if hit_stop:
        plan.state = INVALIDATED
        plan.events.append((INVALIDATED, f"stop {plan.stop:.2f} hit before trigger"))
    elif crossed:
        plan.state = TRIGGERED
        plan.events.append((TRIGGERED, f"bar closed {close:.2f} {'>=' if plan.long else '<='} trigger {plan.trigger:.2f}"))
    else:
        plan.bars_waited += 1
        if plan.bars_waited >= plan.expiry_bars:
            plan.state = EXPIRED
            plan.events.append((EXPIRED, f"not triggered within {plan.expiry_bars} bars"))
    return plan.state


def on_tick(plan: Plan, price: float, allow_tick_trigger: bool = False) -> str:
    """Live tick update for a SETUP: invalidation always; trigger only when tick-triggering is enabled (intraday)."""
    if plan.state != SETUP:
        return plan.state
    if (price <= plan.stop) if plan.long else (price >= plan.stop):
        plan.state = INVALIDATED
        plan.events.append((INVALIDATED, f"price {price:.2f} reached stop {plan.stop:.2f} before trigger"))
    elif allow_tick_trigger and ((price >= plan.trigger) if plan.long else (price <= plan.trigger)):
        plan.state = TRIGGERED
        plan.events.append((TRIGGERED, f"tick {price:.2f} crossed trigger {plan.trigger:.2f}"))
    return plan.state


def on_next_open(plan: Plan, open_price: float, slippage: float = 0.0) -> str:
    """Execute a TRIGGERED plan at the next bar's open (adverse slippage as a fraction)."""
    if plan.state != TRIGGERED:
        return plan.state
    fill = open_price * (1 + slippage) if plan.long else open_price * (1 - slippage)
    if (fill <= plan.stop) if plan.long else (fill >= plan.stop):
        plan.state = INVALIDATED
        plan.events.append((INVALIDATED, f"open {open_price:.2f} gapped beyond stop {plan.stop:.2f}"))
    else:
        plan.state, plan.fill = ACTIVE, fill
        plan.events.append((ACTIVE, f"filled at {fill:.2f}"))
    return plan.state


def trigger_level(rule: str, direction: str, high: float, low: float, close: float) -> float:
    if rule == "signal_close":
        return close
    return high if direction == "LONG" else low
