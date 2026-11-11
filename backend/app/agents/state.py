"""Persistent agent configuration and run state (stored in app_settings; no schema migration needed)."""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AppSetting, PaperTrade

AGENT_KEY = "paper_agent"
KEY_PREFIX = "agent-"
MODES = ("intraday", "swing", "positional")


class AgentConfig(BaseModel):
    modes: list[str] = Field(default_factory=lambda: list(MODES))
    # Share of max_open_positions reserved for each mode
    mode_allocation_pct: dict[str, float] = Field(
        default_factory=lambda: {"intraday": 30.0, "swing": 40.0, "positional": 30.0})
    max_new_trades_per_cycle: int = Field(3, ge=1, le=20)
    # Also trade scanner setups the evidence gates marked WAIT / NO TRADE (learning mode, flagged on every trade)
    allow_unvalidated: bool = False
    require_market_open: bool = True
    # Move the stop to breakeven once price has moved this many R in favour (0 disables)
    breakeven_at_r: float = Field(1.0, ge=0, le=10)
    max_hold_days: dict[str, int] = Field(default_factory=lambda: {"swing": 15, "positional": 90})
    cycle_seconds: int = Field(60, ge=15, le=3600)

    @field_validator("modes")
    @classmethod
    def _modes(cls, v: list[str]) -> list[str]:
        bad = [m for m in v if m not in MODES]
        if bad or not v:
            raise ValueError(f"modes must be a non-empty subset of {MODES}")
        return list(dict.fromkeys(v))


class AgentState(BaseModel):
    running: bool = False
    session_id: str | None = None
    started_at: int | None = None
    stopped_at: int | None = None
    starting_equity: float | None = None
    config: AgentConfig = Field(default_factory=AgentConfig)
    cycles: int = 0
    last_cycle: dict[str, Any] | None = None


def load_state(session: Session) -> AgentState:
    row = session.get(AppSetting, AGENT_KEY)
    return AgentState.model_validate(row.value) if row else AgentState()


def save_state(session: Session, st: AgentState) -> AgentState:
    row = session.get(AppSetting, AGENT_KEY)
    data = st.model_dump()
    if row is None:
        session.add(AppSetting(key=AGENT_KEY, value=data, updated_at=int(time.time())))
    else:
        row.value = data
        row.updated_at = int(time.time())
    session.flush()
    return st


def idempotency_key(session_id: str, signal_id: int) -> str:
    return f"{KEY_PREFIX}{session_id}-{signal_id}"


def agent_trades(session: Session, session_id: str | None = None, status: str | None = None) -> list[PaperTrade]:
    """Paper trades placed by the agent (all runs, or one run when session_id is given)."""
    prefix = f"{KEY_PREFIX}{session_id}-" if session_id else KEY_PREFIX
    q = select(PaperTrade).where(PaperTrade.idempotency_key.like(f"{prefix}%"))
    if status:
        q = q.where(PaperTrade.status == status)
    return list(session.scalars(q.order_by(PaperTrade.id)))
