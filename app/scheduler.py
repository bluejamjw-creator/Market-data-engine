"""Scan schedule, in Europe/London wall-clock time (daylight saving handled by the zone).

Three cron jobs and nothing else - there is no continuous polling.
Each job has max_instances=1 and the scan service holds its own locks,
so runs cannot overlap; each scheduled slot is recorded once.
"""
import logging
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings

log = logging.getLogger(__name__)

SCHEDULE = (
    {"id": "morning", "hour": 7, "minute": 30, "label": "Morning scan"},
    {"id": "us_premarket", "hour": 14, "minute": 15, "label": "US pre-market scan"},
    {"id": "review", "hour": 18, "minute": 0, "label": "Review scan"},
)

_scheduler: Optional[BackgroundScheduler] = None


def schedule_timezone() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def build_trigger(entry: dict) -> CronTrigger:
    return CronTrigger(hour=entry["hour"], minute=entry["minute"], timezone=schedule_timezone())


def _run_job(scan_type: str) -> None:
    from app.services.scan_service import run_scan

    try:
        run_scan(scan_type)
    except Exception:  # noqa: BLE001 - never let a job kill the scheduler thread
        log.exception("Scheduled scan '%s' crashed", scan_type)


def create_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=schedule_timezone())
    for entry in SCHEDULE:
        scheduler.add_job(
            _run_job,
            trigger=build_trigger(entry),
            args=[entry["id"]],
            id=entry["id"],
            name=entry["label"],
            max_instances=1,
            coalesce=True,
            misfire_grace_time=900,
            replace_existing=True,
        )
    return scheduler


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = create_scheduler()
        _scheduler.start()
        for job in describe_jobs():
            log.info("Scheduled %s at %s %s; next run %s", job["id"], job["time"], job["timezone"], job["next_run"])
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def scheduler_running() -> bool:
    return _scheduler is not None and _scheduler.running


def describe_jobs() -> List[dict]:
    """Schedule as configured, plus next run times when the scheduler is live."""
    zone = schedule_timezone()
    now = datetime.now(zone)
    jobs = []
    for entry in SCHEDULE:
        next_run = None
        if _scheduler is not None:
            job = _scheduler.get_job(entry["id"])
            next_time = getattr(job, "next_run_time", None) if job else None
            next_run = next_time.isoformat() if next_time else None
        if next_run is None:
            fire = build_trigger(entry).get_next_fire_time(None, now)
            next_run = fire.isoformat() if fire else None
        jobs.append({
            "id": entry["id"],
            "label": entry["label"],
            "time": f"{entry['hour']:02d}:{entry['minute']:02d}",
            "timezone": str(zone),
            "next_run": next_run,
            "weekends": "crypto only",
        })
    return jobs
