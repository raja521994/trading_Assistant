"""Background jobs: startup ingest/scan, walk-forward evidence, periodic ingest, quote-driven updates."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from ..backtest.service import compute_evidence, evidence_is_current, replay_recent_signals
from ..config import get_settings
from ..data.ingest import ingest
from ..db import session_scope
from ..paper.broker import update_open_trades
from ..providers.registry import active_provider
from ..signals.engine import run_scan_and_persist, update_signal_states

log = logging.getLogger(__name__)
status: dict[str, Any] = {"startup": "pending", "evidence": "pending", "last_ingest": None, "last_scan": None,
                          "last_tick": None, "errors": []}
_lock = threading.Lock()


def _err(msg: str) -> None:
    status["errors"] = (status["errors"] + [f"{time.strftime('%H:%M:%S')} {msg}"])[-20:]


def startup_sync() -> None:
    """Ingest available data and run a first scan (blocking; fast with sample data)."""
    with _lock, session_scope() as s:
        stats = ingest(s, active_provider())
        status["last_ingest"] = {**stats, "at": int(time.time())}
    with _lock, session_scope() as s:
        r = run_scan_and_persist(s)
        status["last_scan"] = {"at": int(time.time()), "count": len(r["recommendations"])}
    status["startup"] = "done"


def evidence_job(force: bool = False) -> None:
    try:
        status["evidence"] = "running"
        with session_scope() as s:
            current = evidence_is_current(s)
        if force or not current:
            with session_scope() as s:
                compute_evidence(s)
            with session_scope() as s:
                n = replay_recent_signals(s)
                log.info("historical replay recorded %d signals", n)
        with _lock, session_scope() as s:
            r = run_scan_and_persist(s)
            status["last_scan"] = {"at": int(time.time()), "count": len(r["recommendations"])}
        status["evidence"] = "ready"
    except Exception as exc:  # noqa: BLE001
        log.exception("evidence job failed")
        status["evidence"] = "error"
        _err(f"evidence: {type(exc).__name__}: {exc}")


def tick() -> None:
    with _lock, session_scope() as s:
        update_signal_states(s)
        update_open_trades(s)
    status["last_tick"] = int(time.time())


def ingest_and_rescan() -> None:
    with _lock, session_scope() as s:
        stats = ingest(s, active_provider())
        status["last_ingest"] = {**stats, "at": int(time.time())}
    if stats["candles_inserted"]:
        with _lock, session_scope() as s:
            r = run_scan_and_persist(s)
            status["last_scan"] = {"at": int(time.time()), "count": len(r["recommendations"])}


async def _loop(fn, every: float, name: str) -> None:  # noqa: ANN001
    while True:
        await asyncio.sleep(every)
        try:
            await asyncio.to_thread(fn)
        except Exception as exc:  # noqa: BLE001
            log.exception("%s job failed", name)
            _err(f"{name}: {type(exc).__name__}: {exc}")


def start_background(tasks: list[asyncio.Task]) -> None:
    s = get_settings()
    threading.Thread(target=evidence_job, name="evidence", daemon=True).start()
    tasks.append(asyncio.create_task(_loop(tick, s.quote_refresh_seconds, "tick")))
    tasks.append(asyncio.create_task(_loop(ingest_and_rescan, s.ingest_refresh_seconds, "ingest")))
