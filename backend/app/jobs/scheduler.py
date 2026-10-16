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
status: dict[str, Any] = {"startup": "pending", "evidence": "pending", "backfill": "pending", "last_ingest": None, "last_scan": None,
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


def backfill_job() -> None:
    """One-off extension of stored history (daily to KITE_HISTORY_START, 5-minute for NIFTY 50 names) used by the
    strategy tournament. Idempotent; runs after the first scan so the dashboard is usable meanwhile."""
    from ..data.constituents import load as load_constituents
    from ..data.ingest import backfill

    p = active_provider()
    if p.name != "kite":
        status["backfill"] = "not needed"
        return
    s_ = get_settings()
    status["backfill"] = "running"
    try:
        intraday = [r["symbol"] for r in load_constituents("nifty50", allow_network=False)]

        def prog(i: int, n: int, what: str) -> None:
            status["backfill"] = f"running {i}/{n} ({what})"

        with session_scope() as s:
            stats = backfill(s, p, s_.kite_history_start, intraday, s_.research_intraday_days, prog, _lock)
        status["backfill"] = "done"
        status["last_backfill"] = {**stats, "at": int(time.time())}
    except Exception as exc:  # noqa: BLE001
        log.exception("backfill failed")
        status["backfill"] = "error"
        _err(f"backfill: {type(exc).__name__}: {exc}")


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
    if status["startup"] != "done":
        return  # initial load still running (or failed and will be retried by a provider switch)
    with _lock, session_scope() as s:
        stats = ingest(s, active_provider())
        status["last_ingest"] = {**stats, "at": int(time.time())}
    if stats.get("candles_written", stats["candles_inserted"]):
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


def initial_load() -> None:
    """Startup ingest + scan, then walk-forward evidence/replay. Runs in a background thread so the API
    is responsive immediately (live providers may need ~1 minute to download history)."""
    from ..realtime.service import service as realtime

    try:
        status["startup"] = "running"
        startup_sync()
        realtime.restart_threadsafe()  # (re)subscribe the live feed to the freshly synced instruments
    except Exception as exc:  # noqa: BLE001
        log.exception("startup load failed")
        status["startup"] = "error"
        _err(f"startup: {type(exc).__name__}: {exc}")
        return
    backfill_job()
    evidence_job()


def switch_provider() -> None:
    """Called after credentials change: rebuild providers and reload everything for the new active provider."""
    from ..data.repository import candle_store
    from ..providers.registry import reset_providers
    from ..signals import engine

    reset_providers()
    candle_store.invalidate()
    engine.last_scan().clear()
    status.update({"startup": "pending", "evidence": "pending", "last_scan": None})
    threading.Thread(target=initial_load, name="provider-switch", daemon=True).start()


def weekly_tournament() -> None:
    """Re-run the strategy tournament weekly, outside market hours, once history is backfilled."""
    from ..validation import tournament as tn

    p = active_provider()
    if p.name == "sample" or status.get("backfill") != "done" or status.get("startup") != "done":
        return
    from ..providers.sample import now_ist

    if p.session_state(now_ist()) == "OPEN" or tn.status()["state"] == "running":
        return
    runs = tn.list_runs()
    if runs:
        last = runs[0]["generated_at"]
        try:
            from datetime import datetime

            if (datetime.now().astimezone() - datetime.fromisoformat(last)).days < 7:
                return
        except ValueError:
            pass
    log.info("starting the scheduled weekly strategy tournament")
    tn.start_background()


def start_background(tasks: list[asyncio.Task]) -> None:
    s = get_settings()
    threading.Thread(target=initial_load, name="initial-load", daemon=True).start()
    tasks.append(asyncio.create_task(_loop(tick, s.quote_refresh_seconds, "tick")))
    tasks.append(asyncio.create_task(_loop(ingest_and_rescan, s.ingest_refresh_seconds, "ingest")))
    tasks.append(asyncio.create_task(_loop(weekly_tournament, 3600, "tournament-schedule")))
