"""Read-only HTTP endpoints: health, status and the latest research output."""
import logging
import threading
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Response
from sqlalchemy import func, select

from app.config import get_settings
from app.database import check_connection, describe_database_target, session_scope
from app.models import (
    CandidateLevel,
    Instrument,
    ScanCandidate,
    ScanRun,
    SystemHealth,
)
from app.scheduler import describe_jobs, scheduler_running
from app.services.scan_service import candle_stats, last_completed_run, run_scan, scan_is_running
from app.timeutil import LONDON, as_utc, utcnow

log = logging.getLogger(__name__)
router = APIRouter()

DISCLAIMER = "Research output only. This service does not place trades or give financial advice."


def _iso(value) -> Optional[str]:
    return as_utc(value).isoformat() if value is not None else None


def _run_dict(run: Optional[ScanRun]) -> Optional[dict]:
    if run is None:
        return None
    return {
        "id": run.id, "scan_type": run.scan_type, "slot": run.slot_key, "scope": run.scope,
        "status": run.status, "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at),
        "instruments_attempted": run.instruments_attempted,
        "instruments_collected": run.instruments_collected,
        "new_candles_stored": run.candles_stored,
        "candidates": run.candidates_total,
        "ready": run.ready_count, "near_ready": run.near_ready_count,
        "preparing": run.preparing_count, "rejected": run.rejected_count,
        "error": run.error, "summary": run.summary,
    }


@router.get("/")
def root() -> dict:
    return {
        "service": "market-data-engine",
        "purpose": "Market-data collection and swing-trading research (1-10 day horizon).",
        "note": DISCLAIMER,
        "endpoints": ["/health", "/health/db", "/status", "/scans/latest", "/candidates"],
    }


@router.get("/health")
def health() -> dict:
    """Liveness. Always answers 200 while the process is up; `status` shows degradation."""
    db_ok, db_detail = check_connection()
    now = utcnow()
    return {
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "error",
        "database_detail": None if db_ok else db_detail,
        "scheduler": "running" if scheduler_running() else "stopped",
        "scan_in_progress": scan_is_running(),
        "time_utc": now.isoformat(),
        "time_london": now.astimezone(LONDON).isoformat(),
        "timezone": get_settings().timezone,
    }


@router.get("/health/db")
def health_db(response: Response) -> dict:
    ok, detail = check_connection()
    if not ok:
        response.status_code = 503
    return {"database": "ok" if ok else "error", "detail": detail, "target": describe_database_target()}


@router.get("/status")
def status(response: Response) -> dict:
    ok, detail = check_connection()
    if not ok:
        response.status_code = 503
        return {"database": "error", "detail": detail, "schedule": describe_jobs()}
    with session_scope() as session:
        stats = candle_stats(session)
        instruments_total = session.scalar(select(func.count()).select_from(Instrument)) or 0
        latest_ids = (
            select(SystemHealth.component, func.max(SystemHealth.id).label("id"))
            .group_by(SystemHealth.component).subquery()
        )
        components = session.scalars(
            select(SystemHealth).join(latest_ids, SystemHealth.id == latest_ids.c.id)
            .order_by(SystemHealth.component)
        ).all()
        last_run = session.scalar(select(ScanRun).order_by(ScanRun.started_at.desc()).limit(1))
        last_good = last_completed_run(session)
        return {
            "database": "ok",
            "instruments_defined": int(instruments_total),
            **stats,
            "last_scan": _run_dict(last_run),
            "last_completed_scan_at": _iso(last_good.finished_at) if last_good else None,
            "scan_in_progress": scan_is_running(),
            "scheduler": "running" if scheduler_running() else "stopped",
            "schedule": describe_jobs(),
            "components": [
                {"component": c.component, "status": c.status, "detail": c.detail, "recorded_at": _iso(c.recorded_at)}
                for c in components
            ],
            "note": DISCLAIMER,
        }


def _candidate_dict(candidate: ScanCandidate, instrument: Instrument, levels) -> dict:
    return {
        "symbol": instrument.symbol, "name": instrument.name, "region": instrument.region,
        "asset_class": instrument.asset_class, "currency": instrument.currency,
        "classification": candidate.classification,
        "do_not_chase": candidate.do_not_chase, "do_not_chase_reason": candidate.do_not_chase_reason,
        "setup_type": candidate.setup_type, "setup": candidate.setup_description,
        "score": candidate.score_total, "score_max_verified": candidate.score_max_verified,
        "score_is_provisional": candidate.provisional,
        "components": candidate.component_status, "reasons": candidate.reasons,
        "price": candidate.price, "price_basis": "close of the latest completed daily candle",
        "price_candle_end": _iso(candidate.price_time),
        "trigger": candidate.trigger_price,
        "entry_zone": [candidate.entry_low, candidate.entry_high] if candidate.entry_low is not None else None,
        "stop": candidate.stop_price, "target": candidate.target_price,
        "reward_risk": candidate.reward_risk, "estimated_cost_pct": candidate.est_cost_pct,
        "data_quality": candidate.data_quality_status,
        "expires_at": _iso(candidate.expires_at), "invalidation": candidate.invalidation,
        "levels": [{"type": l.level_type, "price": l.price, "basis": l.basis} for l in levels],
    }


def _candidates(session, run_id: int, classification: Optional[str], limit: int) -> list:
    query = (
        select(ScanCandidate, Instrument)
        .join(Instrument, Instrument.id == ScanCandidate.instrument_id)
        .where(ScanCandidate.scan_run_id == run_id)
    )
    if classification:
        query = query.where(ScanCandidate.classification == classification.upper())
    else:
        query = query.where(ScanCandidate.classification != "REJECTED")
    rows = session.execute(
        query.order_by(ScanCandidate.score_total.desc().nulls_last()).limit(limit)
    ).all()
    ids = [candidate.id for candidate, _ in rows]
    levels_by_candidate: dict = {}
    if ids:
        for level in session.scalars(select(CandidateLevel).where(CandidateLevel.candidate_id.in_(ids))).all():
            levels_by_candidate.setdefault(level.candidate_id, []).append(level)
    return [_candidate_dict(c, i, levels_by_candidate.get(c.id, [])) for c, i in rows]


@router.get("/scans/latest")
def latest_scan(response: Response) -> dict:
    with session_scope() as session:
        run = last_completed_run(session)
        if run is None:
            response.status_code = 404
            return {"detail": "No completed scan yet."}
        return {"scan": _run_dict(run), "candidates": _candidates(session, run.id, None, 100), "note": DISCLAIMER}


@router.get("/candidates")
def candidates(
    response: Response,
    classification: Optional[str] = Query(None, description="READY, NEAR-READY, PREPARING or REJECTED"),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    with session_scope() as session:
        run = last_completed_run(session)
        if run is None:
            response.status_code = 404
            return {"detail": "No completed scan yet."}
        return {
            "scan_id": run.id, "scan_finished_at": _iso(run.finished_at), "scope": run.scope,
            "candidates": _candidates(session, run.id, classification, limit), "note": DISCLAIMER,
        }


@router.post("/admin/scan")
def trigger_scan(x_admin_token: Optional[str] = Header(None)) -> dict:
    """Start a manual scan. Disabled unless ADMIN_TOKEN is configured."""
    token = get_settings().admin_token
    if not token:
        raise HTTPException(status_code=404, detail="Not found")
    if x_admin_token != token:
        raise HTTPException(status_code=401, detail="Invalid admin token")
    if scan_is_running():
        return {"started": False, "reason": "a scan is already running"}
    threading.Thread(target=run_scan, args=("manual",), name="manual-scan", daemon=True).start()
    return {"started": True}
