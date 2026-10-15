"""Risk configuration and pre-trade risk checks. Risk limits are never bypassed (AI has no override path)."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..models import AppSetting
from .costs import CostModel


class RiskConfig(BaseModel):
    account_equity: float = Field(1_000_000.0, gt=0)
    risk_per_trade_pct: float = Field(1.0, gt=0, le=5)
    max_daily_loss_pct: float = Field(2.0, gt=0, le=20)
    max_drawdown_pct: float = Field(10.0, gt=0, le=50)
    max_open_positions: int = Field(8, ge=1, le=100)
    max_sector_exposure_pct: float = Field(30.0, gt=0, le=100)
    max_alloc_per_instrument_pct: float = Field(15.0, gt=0, le=100)
    max_leverage: float = Field(1.0, ge=0.1, le=1.0)
    min_avg_turnover_cr: float = Field(5.0, ge=0)
    allow_overnight: bool = True
    allow_intraday_short: bool = True
    min_risk_reward: float = Field(1.5, ge=0)
    min_score: float = Field(55.0, ge=0, le=100)
    slippage_bps: float = Field(5.0, ge=0, le=100)
    kill_switch: bool = False
    event_risk_policy: str = "warn"  # event data not connected -> always disclosed as UNKNOWN
    costs: CostModel = Field(default_factory=CostModel)


@dataclass
class TradeProposal:
    symbol: str
    exchange: str
    sector: str | None
    direction: str
    mode: str
    entry: float
    stop: float
    target: float | None
    qty: int
    avg_turnover_cr: float | None


@dataclass
class OpenPosition:
    symbol: str
    sector: str | None
    value: float
    risk: float


@dataclass
class PortfolioState:
    equity: float
    cash: float
    peak_equity: float
    realized_pnl_today: float
    unrealized_pnl: float
    open_positions: list[OpenPosition] = field(default_factory=list)


@dataclass
class RiskCheck:
    name: str
    passed: bool
    detail: str
    severity: str = "block"  # block | warn


@dataclass
class RiskDecision:
    approved: bool
    checks: list[RiskCheck]

    @property
    def violations(self) -> list[str]:
        return [c.detail for c in self.checks if not c.passed and c.severity == "block"]

    @property
    def warnings(self) -> list[str]:
        return [c.detail for c in self.checks if not c.passed and c.severity == "warn"]

    def to_dict(self) -> dict[str, Any]:
        return {"approved": self.approved, "violations": self.violations, "warnings": self.warnings,
                "checks": [asdict(c) for c in self.checks]}


def check_trade(p: TradeProposal, state: PortfolioState, cfg: RiskConfig) -> RiskDecision:
    checks: list[RiskCheck] = []

    def add(name: str, passed: bool, detail: str, severity: str = "block") -> None:
        checks.append(RiskCheck(name, bool(passed), detail, severity))

    add("kill_switch", not cfg.kill_switch, "Kill switch is ON - all new trades blocked" if cfg.kill_switch
        else "Kill switch off")
    add("quantity", p.qty >= 1, f"Quantity {p.qty} (minimum 1)")
    long = p.direction == "LONG"
    stop_ok = (p.stop < p.entry) if long else (p.stop > p.entry)
    add("stop_side", stop_ok, f"Stop {p.stop:.2f} must be {'below' if long else 'above'} entry {p.entry:.2f}"
        if not stop_ok else f"Stop {p.stop:.2f} on the correct side of entry {p.entry:.2f}")
    if p.target is not None:
        tgt_ok = (p.target > p.entry) if long else (p.target < p.entry)
        add("target_side", tgt_ok, f"Target {p.target:.2f} must be {'above' if long else 'below'} entry"
            if not tgt_ok else f"Target {p.target:.2f} on the correct side of entry")
        risk = abs(p.entry - p.stop)
        rr = abs(p.target - p.entry) / risk if risk > 0 else 0.0
        add("risk_reward", rr >= cfg.min_risk_reward, f"Risk-reward {rr:.2f} vs minimum {cfg.min_risk_reward:.2f}")

    risk_amt = abs(p.entry - p.stop) * p.qty
    max_risk = state.equity * cfg.risk_per_trade_pct / 100.0
    add("risk_per_trade", risk_amt <= max_risk * 1.001,
        f"Risk Rs {risk_amt:,.0f} vs limit Rs {max_risk:,.0f} ({cfg.risk_per_trade_pct}% of equity)")

    value = p.entry * p.qty
    max_alloc = state.equity * cfg.max_alloc_per_instrument_pct / 100.0
    add("allocation", value <= max_alloc * 1.001,
        f"Position value Rs {value:,.0f} vs limit Rs {max_alloc:,.0f} ({cfg.max_alloc_per_instrument_pct}%)")

    gross = sum(o.value for o in state.open_positions) + value
    add("leverage", gross <= state.equity * cfg.max_leverage * 1.001,
        f"Gross exposure Rs {gross:,.0f} vs limit Rs {state.equity * cfg.max_leverage:,.0f} "
        f"({cfg.max_leverage:.1f}x equity)")
    if long or p.mode != "intraday":
        add("cash", value <= state.cash * 1.001, f"Required Rs {value:,.0f} vs available cash Rs {state.cash:,.0f}")

    add("open_positions", len(state.open_positions) < cfg.max_open_positions,
        f"Open positions {len(state.open_positions)} vs limit {cfg.max_open_positions}")
    add("duplicate", all(o.symbol != p.symbol for o in state.open_positions),
        f"An open paper position in {p.symbol} already exists (duplicate/correlated exposure)"
        if any(o.symbol == p.symbol for o in state.open_positions) else f"No existing open position in {p.symbol}")

    if p.sector:
        sec_val = sum(o.value for o in state.open_positions if o.sector == p.sector) + value
        lim = state.equity * cfg.max_sector_exposure_pct / 100.0
        add("sector_exposure", sec_val <= lim * 1.001,
            f"{p.sector} exposure Rs {sec_val:,.0f} vs limit Rs {lim:,.0f} ({cfg.max_sector_exposure_pct}%)")

    day_pnl = state.realized_pnl_today + min(state.unrealized_pnl, 0.0)
    loss_lim = state.equity * cfg.max_daily_loss_pct / 100.0
    add("daily_loss", -day_pnl < loss_lim, f"Today's loss Rs {max(-day_pnl, 0):,.0f} vs limit Rs {loss_lim:,.0f}")

    dd = (state.peak_equity - state.equity) / state.peak_equity * 100.0 if state.peak_equity > 0 else 0.0
    add("drawdown", dd < cfg.max_drawdown_pct, f"Drawdown {dd:.2f}% vs limit {cfg.max_drawdown_pct}%")

    if p.avg_turnover_cr is not None:
        add("liquidity", p.avg_turnover_cr >= cfg.min_avg_turnover_cr,
            f"Avg daily turnover Rs {p.avg_turnover_cr:,.1f} cr vs minimum Rs {cfg.min_avg_turnover_cr} cr")
    else:
        add("liquidity", False, "Liquidity unknown (no volume data)", "warn")

    if not long:
        add("short_allowed", p.mode == "intraday" and cfg.allow_intraday_short,
            "Short selling is only allowed intraday in the cash segment" if p.mode != "intraday"
            else "Intraday shorts disabled in risk settings")
    if p.mode != "intraday":
        add("overnight", cfg.allow_overnight, "Overnight positions disabled in risk settings" if not cfg.allow_overnight
            else "Overnight positions allowed")

    add("event_risk", False, "Event/earnings calendar not connected - event risk UNKNOWN", "warn")
    approved = all(c.passed for c in checks if c.severity == "block")
    return RiskDecision(approved, checks)


RISK_KEY = "risk_config"


def load_risk_config(session: Session) -> RiskConfig:
    row = session.get(AppSetting, RISK_KEY)
    return RiskConfig.model_validate(row.value) if row else RiskConfig()


def save_risk_config(session: Session, cfg: RiskConfig) -> RiskConfig:
    row = session.get(AppSetting, RISK_KEY)
    data = cfg.model_dump()
    if row is None:
        session.add(AppSetting(key=RISK_KEY, value=data, updated_at=int(time.time())))
    else:
        row.value = data
        row.updated_at = int(time.time())
    session.flush()
    return cfg
