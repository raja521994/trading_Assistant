"""Strategy library catalogue and tournament endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..quant.library.catalog import CATALOG, FAMILIES
from ..validation import tournament as tn

router = APIRouter(prefix="/api")


@router.get("/library")
def library() -> dict[str, Any]:
    return {"families": FAMILIES, "strategies": [e.as_dict() for e in CATALOG],
            "counts": {k: sum(e.kind == k for e in CATALOG) for k in
                       ("event", "portfolio", "pairs", "context", "not_testable")}}


class RunRequest(BaseModel):
    force: bool = False
    only: list[str] | None = None


@router.post("/tournament/run")
def run(req: RunRequest) -> dict[str, Any]:
    return tn.start_background(req.force, req.only)


@router.get("/tournament/status")
def status() -> dict[str, Any]:
    return tn.status()


@router.get("/tournament/runs")
def runs() -> list[dict[str, Any]]:
    return tn.list_runs()


def _report(run_id: str | None) -> dict[str, Any]:
    rep = tn.load_report(run_id)
    if rep is None:
        raise HTTPException(404, "No tournament report yet - run the tournament first")
    return rep


@router.get("/tournament/report")
def report(run_id: str | None = None, details: bool = False) -> dict[str, Any]:
    rep = _report(run_id)
    if not details:
        rep = {**rep, "details": {k: {x: v.get(x) for x in ("equity_curve", "drawdown_curve", "stability", "checks",
                                                            "regimes", "subperiods", "bootstrap", "benchmark",
                                                            "double_costs", "folds", "composite", "exit_reasons")}
                                  for k, v in rep["details"].items()}}
    return rep


@router.get("/tournament/report.csv", response_class=PlainTextResponse)
def report_csv(run_id: str | None = None) -> PlainTextResponse:
    rep = _report(run_id)
    return PlainTextResponse(tn.leaderboard_csv(rep), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="tournament-{rep["run_id"]}.csv"'})


@router.get("/tournament/report.html", response_class=HTMLResponse)
def report_html(run_id: str | None = None) -> HTMLResponse:
    from ..validation.report_html import render

    return HTMLResponse(render(_report(run_id)))


@router.get("/tournament/config")
def get_config() -> dict[str, Any]:
    c = tn.load_config()
    return {"criteria": c["criteria"], "score_weights": c["score_weights"], "walk_forward": c["walk_forward"],
            "universe": c["universe"], "intraday_universe": c["intraday_universe"]}


class ConfigUpdate(BaseModel):
    criteria: dict[str, Any] | None = None
    score_weights: dict[str, float] | None = None


@router.put("/tournament/config")
def put_config(req: ConfigUpdate) -> dict[str, Any]:
    if req.score_weights and any(v < 0 for v in req.score_weights.values()):
        raise HTTPException(400, "Weights must be >= 0")
    tn.save_config({k: v for k, v in req.model_dump().items() if v})
    return get_config()


# ------------------------------------------------------------------ decision engine (Part 6)

@router.get("/decision")
def decision(db: Session = Depends(get_db)) -> dict[str, Any]:
    from ..decision.engine import decide

    return decide(db)


@router.get("/regime")
def regime(db: Session = Depends(get_db)) -> dict[str, Any]:
    from ..decision.engine import load_config as dconf, regime_state
    from ..providers.sample import now_ist

    return regime_state(db, now_ist(), dconf())


@router.get("/decision/config")
def decision_config() -> dict[str, Any]:
    from ..decision.engine import load_config

    return load_config()


class WeightsUpdate(BaseModel):
    weights: dict[str, float]


@router.put("/decision/weights")
def decision_weights(req: WeightsUpdate) -> dict[str, Any]:
    from ..decision.engine import save_weights

    if any(v < 0 for v in req.weights.values()):
        raise HTTPException(400, "Weights must be >= 0")
    return save_weights(req.weights)


# ------------------------------------------------------------------ evidence policy & ML meta-model

class PolicyUpdate(BaseModel):
    policy: str


@router.get("/policy")
def get_policy(db: Session = Depends(get_db)) -> dict[str, Any]:
    from ..decision import policy as pol
    from ..decision.engine import load_config as dconf, regime_state
    from ..providers.sample import now_ist
    from ..risk.limits import load_risk_config

    trend = regime_state(db, now_ist(), dconf())["trend"]
    return pol.summary(trend, load_risk_config(db).evidence_policy)


@router.put("/policy")
def put_policy(req: PolicyUpdate, db: Session = Depends(get_db)) -> dict[str, Any]:
    from ..decision import policy as pol
    from ..jobs.scheduler import _lock
    from ..paper.broker import audit
    from ..risk.limits import load_risk_config, save_risk_config
    from ..signals.engine import run_scan_and_persist

    if req.policy not in pol.POLICY_MIN_TIER:
        raise HTTPException(400, f"policy must be one of {', '.join(pol.POLICY_MIN_TIER)}")
    cfg = load_risk_config(db)
    old = cfg.evidence_policy
    cfg.evidence_policy = req.policy
    save_risk_config(db, cfg)
    audit(db, "evidence_policy_changed", {"from": old, "to": req.policy})
    db.commit()
    with _lock:
        run_scan_and_persist(db)
        db.commit()
    return {"policy": req.policy, "previous": old}


@router.get("/ml/report")
def ml_report() -> dict[str, Any]:
    from ..ml import meta

    return {"report": meta.report(), "status": meta.status()}


@router.post("/ml/train")
def ml_train() -> dict[str, Any]:
    from ..ml import meta

    return meta.start_background()
