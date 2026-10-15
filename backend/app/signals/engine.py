"""Signal engine: persists scanner output as stateful signals and advances states from prices/time."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Signal
from ..providers.registry import active_provider
from ..providers.sample import now_ist
from ..quant.scanner import scan
from .state_machine import (
    ACTIVE,
    EXITED,
    EXPIRED,
    INVALIDATED,
    SETUP,
    TRIGGERED,
    WATCH,
    create_signal,
    transition,
)

log = logging.getLogger(__name__)
_last_scan: dict[str, Any] = {}


def last_scan() -> dict[str, Any]:
    return _last_scan


def _uid(rec: dict[str, Any]) -> str:
    base = f"{rec['strategy']}:{rec['exchange']}:{rec['symbol']}"
    return f"{base}:watch:{rec['signal_bar_ts']}" if rec["state"] == WATCH else f"{base}:{rec['signal_bar_ts']}"


def run_scan_and_persist(session: Session, now: datetime | None = None) -> dict[str, Any]:
    result = scan(session, now)
    now_epoch = int(time.time())
    active = {(s.strategy, s.exchange, s.symbol): s for s in session.scalars(
        select(Signal).where(Signal.state.in_(ACTIVE), Signal.origin == "live_scan"))}
    seen: set[tuple[str, str, str]] = set()
    created = updated = 0
    for rec in result["recommendations"]:
        key = (rec["strategy"], rec["exchange"], rec["symbol"])
        seen.add(key)
        uid = _uid(rec)
        sig = active.get(key)
        if sig is not None and sig.uid != uid and rec["state"] in (SETUP, TRIGGERED):
            if sig.state == WATCH:
                transition(session, sig, rec["state"], "Entry conditions completed", rec["reference_price"])
                sig.uid = uid
            else:
                transition(session, sig, EXPIRED if sig.state == SETUP else EXITED,
                           "Superseded by a newer setup", rec["reference_price"])
                if sig.state == EXITED:
                    sig.outcome = {"result": "superseded", "exit_price": rec["reference_price"]}
                sig = None
        if sig is not None and sig.state in (SETUP, TRIGGERED) and rec["state"] == WATCH:
            continue  # never downgrade an active setup to watch
        if sig is None:
            if session.scalar(select(Signal.id).where(Signal.uid == uid)) is not None:
                continue  # already recorded and resolved
            sig = create_signal(session, uid, rec)
            created += 1
        else:
            if sig.state == SETUP and rec["state"] == TRIGGERED:
                transition(session, sig, TRIGGERED, "Entry reference traded at next open", rec["entry"]["reference"])
            sig.payload = {**rec, "state": sig.state}
            sig.action = rec["action"]
            sig.score = rec["score"]
            sig.data_ts = rec["data_ts_epoch"]
            sig.expires_at = rec["expires_at_epoch"]
            sig.updated_at = now_epoch
            updated += 1
        rec["signal_id"] = sig.id
        rec["state"] = sig.state
    for key, sig in active.items():
        if key in seen:
            continue
        if sig.state == WATCH:
            transition(session, sig, EXPIRED, "Watch conditions no longer met")
    session.flush()
    update_signal_states(session)
    session.flush()
    result["persisted"] = {"created": created, "updated": updated}
    _last_scan.clear()
    _last_scan.update(result)
    log.info("scan: %d recommendations (%d new), %d blocked, %.2fs", len(result["recommendations"]), created,
             len(result["blocked"]), result["seconds"])
    return result


def update_signal_states(session: Session, now: datetime | None = None) -> int:
    """Advance SETUP/TRIGGERED signals using the latest (possibly SIMULATED) quotes and expiry times."""
    provider = active_provider()
    now = now or now_ist()
    now_epoch = int(now.timestamp())
    changed = 0
    sigs = session.scalars(select(Signal).where(Signal.state.in_([SETUP, TRIGGERED]),
                                                Signal.origin == "live_scan")).all()
    for sig in sigs:
        p = sig.payload
        q = provider.quote(sig.symbol, sig.exchange)
        if q is None:
            continue
        px = q.last_price
        long = sig.direction != "SHORT"
        stop = p.get("stop_loss")
        targets = p.get("targets") or []
        tgt = targets[0] if targets else None
        entry = (p.get("entry") or {}).get("reference")
        if sig.state == SETUP:
            if stop is not None and ((long and px <= stop) or (not long and px >= stop)):
                transition(session, sig, INVALIDATED, f"Price {px:.2f} reached invalidation {stop:.2f} before entry", px)
            elif now_epoch > sig.expires_at:
                transition(session, sig, EXPIRED, "Setup window lapsed without entry", px)
            elif q.market_open and entry is not None:
                transition(session, sig, TRIGGERED, f"Market open; entry reference {entry:.2f} (quote {px:.2f})", px)
            else:
                continue
            changed += 1
            continue
        reason = None
        if stop is not None and ((long and px <= stop) or (not long and px >= stop)):
            reason, result = f"Stop {stop:.2f} reached (quote {px:.2f})", "stop"
        elif tgt is not None and ((long and px >= tgt) or (not long and px <= tgt)):
            reason, result = f"Target {tgt:.2f} reached (quote {px:.2f})", "target"
        elif now_epoch > sig.expires_at:
            reason, result = "Holding-period / session time limit reached", "time"
        if reason:
            transition(session, sig, EXITED, reason, px)
            risk = abs(entry - stop) if entry is not None and stop is not None else None
            r_mult = None
            if risk:
                r_mult = round(((px - entry) if long else (entry - px)) / risk, 3)
            sig.outcome = {"result": result, "exit_price": px, "entry_price": entry, "r_multiple": r_mult,
                           "exit_ts": now.isoformat(), "data_label": q.status.value}
            changed += 1
    return changed
