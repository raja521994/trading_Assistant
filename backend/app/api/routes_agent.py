"""Autonomous paper-trading agent: start/stop, status, manual cycle, profit report and activity log."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..agents import service as agent
from ..agents.state import AgentConfig, load_state, save_state
from ..db import get_db
from ..jobs.scheduler import _lock

router = APIRouter(prefix="/api/agent")


@router.get("/status")
def agent_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    return agent.status(db)


@router.post("/start")
def agent_start(config: AgentConfig | None = None, db: Session = Depends(get_db)) -> dict[str, Any]:
    with _lock:
        agent.start(db, config)
        db.commit()
    return agent.status(db)


@router.post("/stop")
def agent_stop(db: Session = Depends(get_db)) -> dict[str, Any]:
    with _lock:
        agent.stop(db)
        db.commit()
    return agent.status(db)


@router.put("/config")
def agent_config(config: AgentConfig, db: Session = Depends(get_db)) -> dict[str, Any]:
    with _lock:
        st = load_state(db)
        st.config = config
        save_state(db, st)
        db.commit()
    return agent.status(db)


@router.post("/run-once")
def agent_run_once(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Run one orchestrator -> worker cycle now (works even while the agent is stopped)."""
    with _lock:
        summary = agent.run_cycle(db, force=True)
        db.commit()
    return {"cycle": summary, "status": agent.status(db)}


@router.get("/report")
def agent_report(session_id: str | None = None, all_runs: bool = False,
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    return agent.report(db, session_id, all_runs)


@router.get("/activity")
def agent_activity(limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return agent.activity(db, limit)
