"""FastAPI application entry point.

Startup: configure logging, verify the database, seed the universe, start
the Europe/London scheduler and - if no scan has completed recently - run
one scan in the background so a fresh deployment collects data immediately.
"""
import logging
import threading
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI

from app.config import get_settings
from app.database import check_connection, describe_database_target, session_scope
from app.health import router
from app.logging_setup import configure_logging
from app.scheduler import start_scheduler, stop_scheduler
from app.timeutil import as_utc, utcnow

log = logging.getLogger("app.main")


def _startup_tasks() -> None:
    """Runs in a background thread so the web server can answer health checks at once."""
    from app.services.scan_service import last_completed_run, run_scan
    from app.universe import seed_universe

    settings = get_settings()
    ok, detail = check_connection()
    if not ok:
        log.error("Database is not reachable at startup: %s", detail)
        return
    try:
        with session_scope() as session:
            seed_universe(session)
            last = last_completed_run(session)
            finished = as_utc(last.finished_at) if last and last.finished_at else None
    except Exception:  # noqa: BLE001
        log.exception("Startup database tasks failed (have migrations been applied?)")
        return
    max_age = timedelta(hours=settings.startup_scan_max_age_hours)
    if finished is not None and utcnow() - finished < max_age:
        log.info("Last completed scan was at %s; no startup scan needed", finished.isoformat())
        return
    log.info("No completed scan in the last %.0f hours; running a startup scan", settings.startup_scan_max_age_hours)
    try:
        run_scan("startup")
    except Exception:  # noqa: BLE001
        log.exception("Startup scan crashed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    present = {
        "DATABASE_URL": bool(settings.database_url),
        "TWELVE_DATA_API_KEY": bool(settings.twelve_data_api_key),
        "ALPHA_VANTAGE_API_KEY": bool(settings.alpha_vantage_api_key),
        "COINMARKETCAP_API_KEY": bool(settings.coinmarketcap_api_key),
    }
    log.info("Starting market-data-engine; database target %s", describe_database_target())
    log.info("Configuration present (values hidden): %s",
             ", ".join(f"{name}={'set' if value else 'MISSING'}" for name, value in present.items()))
    if settings.disable_scheduler:
        log.info("Scheduler and startup scan disabled by MDE_DISABLE_SCHEDULER")
    else:
        start_scheduler()
        threading.Thread(target=_startup_tasks, name="startup-tasks", daemon=True).start()
    yield
    stop_scheduler()
    log.info("market-data-engine stopped")


app = FastAPI(
    title="Market Data Engine",
    description="Market-data collection and swing-trading research. Read-only; places no trades.",
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(router)
