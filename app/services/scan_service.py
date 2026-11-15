"""One scan run: collect -> validate -> indicators -> classify -> track outcomes.

Runs are idempotent (one row per scheduled slot) and never overlap (an
in-process lock plus a PostgreSQL advisory lock). The engine only reads
market data and writes research records; it places no orders.
"""
import logging
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.collectors.candles import collect_series, load_candles, mark_unsupported
from app.collectors.extras import collect_crypto_snapshots, cross_check_equities
from app.config import Settings, get_settings
from app.database import get_engine, new_session, upsert_rows
from app.indicators.core import SWING_K, compute_indicators, support_resistance, swing_points, trend_strength
from app.logging_setup import redact
from app.models import (
    CandidateLevel,
    CandidateOutcome,
    DataQualityCheck,
    Exchange,
    Instrument,
    MarketCandle,
    ProviderRequest,
    ScanCandidate,
    ScanRun,
    SystemHealth,
    TechnicalIndicator,
)
from app.providers.alpha_vantage import AlphaVantageProvider
from app.providers.binance import BinanceProvider
from app.providers.coinmarketcap import CoinMarketCapProvider
from app.providers.twelve_data import TwelveDataProvider
from app.scanners.engine import Context, Evaluation, evaluate
from app.services.outcome_calc import HORIZONS, evaluate_outcome
from app.services.quality import Issue, assess_series, overall_status
from app.timeutil import LONDON, TIMEFRAMES, as_utc, timeframe_delta, utcnow

log = logging.getLogger(__name__)

SCHEDULED_TYPES = ("morning", "us_premarket", "review")
ADVISORY_LOCK_KEY = 7421905533
_run_lock = threading.Lock()

_INDICATOR_COLUMNS = [
    "close", "ema9", "ema20", "ema50", "ema200", "rsi14", "macd", "macd_signal", "macd_hist",
    "atr14", "adx14", "plus_di", "minus_di", "bb_upper", "bb_middle", "bb_lower", "bb_width",
    "avg_volume20", "rel_volume", "last_swing_high", "last_swing_low", "rs_20", "rs_60",
]
_INDICATOR_UPDATE = [c for c in _INDICATOR_COLUMNS] + [
    "provider", "support", "resistance", "trend_strength", "benchmark_symbol", "structure", "computed_at",
]


def scan_is_running() -> bool:
    return _run_lock.locked()


def build_providers(settings: Settings, transport=None, sleep=time.sleep) -> Dict[str, object]:
    kwargs = dict(timeout=settings.http_timeout_seconds, transport=transport, sleep=sleep)
    return {
        "binance": BinanceProvider(**kwargs),
        "twelve_data": TwelveDataProvider(settings.twelve_data_api_key, **kwargs),
        "alpha_vantage": AlphaVantageProvider(settings.alpha_vantage_api_key, **kwargs),
        "coinmarketcap": CoinMarketCapProvider(settings.coinmarketcap_api_key, **kwargs),
    }


def scan_scope(now: datetime) -> str:
    """Weekends (Europe/London) are crypto only: equity markets are closed."""
    return "crypto_only" if as_utc(now).astimezone(LONDON).weekday() >= 5 else "all"


def slot_key_for(scan_type: str, now: datetime) -> str:
    local = as_utc(now).astimezone(LONDON)
    if scan_type in SCHEDULED_TYPES:
        return f"{local:%Y-%m-%d}:{scan_type}"
    return f"{local:%Y-%m-%d}:{scan_type}:{local:%H%M%S}"


def _none_if_nan(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return float(value)


def _flush_provider_records(session: Session, providers: Dict[str, object]) -> None:
    for provider in providers.values():
        for record in provider.drain_records():
            session.add(ProviderRequest(**asdict(record)))


# ------------------------------------------------------------------ collection

def _collect(session: Session, run: ScanRun, instruments: List[Instrument],
             exchanges: Dict[int, Exchange], providers: Dict[str, object], now: datetime) -> Dict[int, str]:
    """Collect candles for every instrument. Returns {instrument_id: failure detail}."""
    failures: Dict[int, str] = {}
    blocked_mics: Dict[str, str] = {}
    collected = 0
    for instrument in instruments:
        timezone_name = exchanges[instrument.exchange_id].timezone
        mic = instrument.td_mic_code
        if mic and mic in blocked_mics and not (instrument.unsupported_providers or {}).get("twelve_data"):
            mark_unsupported(instrument, "twelve_data", blocked_mics[mic], now)
        ok = False
        details = []
        for timeframe in TIMEFRAMES:
            result = collect_series(session, instrument, timeframe, providers, now, timezone_name)
            run.candles_stored += result.candles_stored
            if result.status in ("fetched", "up_to_date"):
                ok = True
            else:
                details.append(f"{timeframe}: {result.detail}")
            for issue in result.issues:
                session.add(DataQualityCheck(
                    scan_run_id=run.id, instrument_id=instrument.id, provider=result.provider,
                    timeframe=timeframe, check_type=issue.check_type, status=issue.status,
                    detail=issue.detail, checked_at=now,
                ))
        entry = (instrument.unsupported_providers or {}).get("twelve_data")
        if mic and entry and mic not in blocked_mics and str(entry.get("reason", "")).startswith("unsupported"):
            blocked_mics[mic] = (f"unsupported (inferred): exchange {mic} is not available on the current "
                                 f"Twelve Data plan - first refused for {instrument.symbol}")
            log.info("Twelve Data refused %s on %s; skipping other %s symbols this week", instrument.symbol, mic, mic)
        if ok:
            collected += 1
        else:
            failures[instrument.id] = "; ".join(details) or "no data collected"
        run.instruments_attempted += 1
        run.instruments_collected = collected
        _flush_provider_records(session, providers)
        session.commit()
    return failures


# -------------------------------------------------------------------- analysis

def _store_indicators(session: Session, instrument: Instrument, timeframe: str, frame: pd.DataFrame,
                      ind: pd.DataFrame, now: datetime, rows_to_store: int = 10) -> None:
    swing_highs, swing_lows = swing_points(frame)
    rows = []
    n = len(ind)
    for pos in range(max(0, n - rows_to_store), n):
        row = ind.iloc[pos]
        if pd.isna(row["close"]):
            continue
        known_highs = [s for s in swing_highs if s[0] <= pos - SWING_K]
        known_lows = [s for s in swing_lows if s[0] <= pos - SWING_K]
        support, resistance = support_resistance(float(row["close"]), known_highs, known_lows, pos)
        previous = ind["ema20"].iloc[pos - 5] if pos >= 5 else None
        strength = trend_strength(row, previous)
        e20, e50 = row["ema20"], row["ema50"]
        if pd.isna(e20) or pd.isna(e50):
            structure = "unknown (insufficient history)"
        elif row["close"] > e50 and e20 > e50:
            structure = "uptrend"
        elif row["close"] < e50 and e20 < e50:
            structure = "downtrend"
        else:
            structure = "transition"
        record = {column: _none_if_nan(row.get(column)) for column in _INDICATOR_COLUMNS}
        record.update(
            instrument_id=instrument.id, timeframe=timeframe, provider=instrument.primary_provider,
            candle_time=ind.index[pos].to_pydatetime(), support=support, resistance=resistance,
            trend_strength=strength, benchmark_symbol=instrument.benchmark_symbol,
            structure=structure, computed_at=now,
        )
        rows.append(record)
    upsert_rows(session, TechnicalIndicator, rows,
                conflict_cols=["instrument_id", "timeframe", "candle_time"], update_cols=_INDICATOR_UPDATE)


def _store_candidate(session: Session, run: ScanRun, instrument: Instrument, evaluation: Evaluation,
                     daily: Optional[pd.DataFrame], quality: str, cost_pct: float, now: datetime) -> ScanCandidate:
    signal_time = daily.index[-1].to_pydatetime() if daily is not None and len(daily) else None
    first = True
    if signal_time is not None:
        first = session.scalar(
            select(func.count()).select_from(ScanCandidate).where(
                ScanCandidate.instrument_id == instrument.id,
                ScanCandidate.signal_candle_time == signal_time,
            )
        ) == 0
    plan = evaluation.plan
    has_plan = plan is not None and plan.valid
    components = evaluation.components
    candidate = ScanCandidate(
        scan_run_id=run.id, instrument_id=instrument.id, classification=evaluation.classification,
        do_not_chase=evaluation.do_not_chase, do_not_chase_reason=evaluation.do_not_chase_reason,
        setup_type=evaluation.setup_type, setup_description=evaluation.setup_description,
        score_total=evaluation.score_total, score_max_verified=evaluation.score_max_verified,
        provisional=evaluation.provisional,
        pattern_score=components["pattern"].points if evaluation.score_total is not None else None,
        technical_score=components["technical"].points if evaluation.score_total is not None else None,
        rs_regime_score=components["rs_regime"].points if evaluation.score_total is not None else None,
        liquidity_quality_score=(components["liquidity_quality"].points
                                 if evaluation.score_total is not None else None),
        reward_risk_score=components["reward_risk"].points if evaluation.score_total is not None else None,
        component_status={key: comp.as_dict() for key, comp in components.items()},
        reasons=evaluation.reasons,
        price=evaluation.price,
        price_time=signal_time + timeframe_delta("1d") if signal_time else None,
        signal_candle_time=signal_time, timeframe="1d",
        trigger_price=plan.trigger if has_plan else None,
        entry_low=plan.entry_low if has_plan else None,
        entry_high=plan.entry_high if has_plan else None,
        stop_price=plan.stop if has_plan else None,
        target_price=plan.target if has_plan else None,
        reward_risk=plan.reward_risk if has_plan else None,
        est_cost_pct=cost_pct, data_quality_status=quality,
        expires_at=(now + timedelta(days=5 if instrument.asset_class == "crypto" else 7)) if has_plan else None,
        invalidation=evaluation.invalidation if has_plan else None,
        is_first_for_candle=first, created_at=now,
    )
    session.add(candidate)
    session.flush()
    levels = []
    if has_plan:
        levels += [
            ("trigger", plan.trigger, plan.trigger_basis),
            ("entry_low", plan.entry_low, "bottom of the entry zone (the trigger)"),
            ("entry_high", plan.entry_high, "top of the entry zone (trigger plus a fraction of ATR)"),
            ("stop", plan.stop, plan.stop_basis),
            ("target", plan.target, plan.target_basis),
        ]
    if evaluation.support is not None:
        levels.append(("support", evaluation.support, "nearest confirmed swing low below price (120 bars)"))
    if evaluation.resistance is not None:
        levels.append(("resistance", evaluation.resistance, "nearest confirmed swing high above price (120 bars)"))
    for level_type, price, basis in levels:
        session.add(CandidateLevel(candidate_id=candidate.id, level_type=level_type, price=float(price), basis=basis))
    return candidate


def _analyse(session: Session, run: ScanRun, instruments: List[Instrument], failures: Dict[int, str],
             settings: Settings, now: datetime) -> Dict[str, int]:
    counts = {"READY": 0, "NEAR-READY": 0, "PREPARING": 0, "REJECTED": 0}
    by_key = {(i.symbol, i.region): i for i in session.scalars(select(Instrument)).all()}
    bench_cache: Dict[int, Optional[tuple]] = {}

    def benchmark_for(instrument: Instrument):
        bench = by_key.get((instrument.benchmark_symbol, instrument.region))
        if bench is None:
            return None
        if bench.id not in bench_cache:
            frame = load_candles(session, bench, "1d")
            if frame is None or len(frame) < 60:
                bench_cache[bench.id] = None
            else:
                bench_cache[bench.id] = (frame, compute_indicators(frame))
        return bench_cache[bench.id]

    for instrument in instruments:
        try:
            daily = load_candles(session, instrument, "1d")
            if daily is None:
                issues = [Issue("no_data", "FAIL", failures.get(instrument.id, "no completed daily candles stored"))]
            else:
                issues = assess_series(daily, instrument.asset_class, "1d", now)
            quality = overall_status(issues)
            for issue in issues:
                if issue.status != "PASS":
                    session.add(DataQualityCheck(
                        scan_run_id=run.id, instrument_id=instrument.id, provider=instrument.primary_provider,
                        timeframe="1d", check_type=issue.check_type, status=issue.status,
                        detail=issue.detail, checked_at=now,
                    ))
            notes = [f"{i.check_type} [{i.status}]: {i.detail}" for i in issues if i.status != "PASS"]

            bench = None if instrument.is_benchmark else benchmark_for(instrument)
            ind = None
            if daily is not None and len(daily) >= 30:
                ind = compute_indicators(daily, bench[0]["close"] if bench else None)
                _store_indicators(session, instrument, "1d", daily, ind, now)

            h4_ind = None
            h4 = load_candles(session, instrument, "4h", limit=320)
            if h4 is not None:
                h4_issues = assess_series(h4, instrument.asset_class, "4h", now)
                if overall_status(h4_issues) == "FAIL":
                    for issue in h4_issues:
                        if issue.status == "FAIL":
                            session.add(DataQualityCheck(
                                scan_run_id=run.id, instrument_id=instrument.id,
                                provider=instrument.primary_provider, timeframe="4h",
                                check_type=issue.check_type, status=issue.status,
                                detail=issue.detail, checked_at=now,
                            ))
                else:
                    h4_ind = compute_indicators(h4)
                    _store_indicators(session, instrument, "4h", h4, h4_ind, now, rows_to_store=6)

            cost_pct = settings.cost_pct(instrument.region, instrument.asset_class)
            context = Context(
                asset_class=instrument.asset_class, region=instrument.region, cost_pct=cost_pct,
                quality_status=quality, quality_notes=notes, is_benchmark=instrument.is_benchmark,
                value_scale=0.01 if instrument.currency == "GBX" else 1.0,
            )
            evaluation = evaluate(daily, ind, h4_ind, bench[1] if bench else None, context)
            _store_candidate(session, run, instrument, evaluation, daily, quality, cost_pct, now)
            counts[evaluation.classification] += 1
            session.commit()
        except Exception:  # noqa: BLE001 - one instrument must not stop the scan
            session.rollback()
            log.exception("Analysis failed for %s", instrument.symbol)
    return counts


# -------------------------------------------------------------------- outcomes

def update_outcomes(session: Session, now: datetime) -> int:
    """Fill in 1/3/5/10/20-day outcomes that have become measurable."""
    since = as_utc(now) - timedelta(days=45)
    done = dict(
        session.execute(
            select(CandidateOutcome.candidate_id, func.count())
            .group_by(CandidateOutcome.candidate_id)
        ).all()
    )
    candidates = session.scalars(
        select(ScanCandidate).where(
            ScanCandidate.is_first_for_candle.is_(True),
            ScanCandidate.signal_candle_time.is_not(None),
            ScanCandidate.price.is_not(None),
            ScanCandidate.created_at >= since,
        )
    ).all()
    pending = [c for c in candidates if done.get(c.id, 0) < len(HORIZONS)]
    if not pending:
        return 0
    instruments = {i.id: i for i in session.scalars(select(Instrument)).all()}
    frames: Dict[int, Optional[pd.DataFrame]] = {}
    existing = {
        (candidate_id, horizon)
        for candidate_id, horizon in session.execute(
            select(CandidateOutcome.candidate_id, CandidateOutcome.horizon_days)
            .where(CandidateOutcome.candidate_id.in_([c.id for c in pending]))
        ).all()
    }
    written = 0
    for candidate in pending:
        instrument = instruments.get(candidate.instrument_id)
        if instrument is None:
            continue
        if instrument.id not in frames:
            frames[instrument.id] = load_candles(session, instrument, "1d", limit=90)
        frame = frames[instrument.id]
        if frame is None:
            continue
        future = frame[frame.index > pd.Timestamp(as_utc(candidate.signal_candle_time))]
        for horizon in HORIZONS:
            if (candidate.id, horizon) in existing:
                continue
            outcome = evaluate_outcome(future, horizon, candidate.price, candidate.trigger_price,
                                       candidate.stop_price, candidate.target_price)
            if outcome is None:
                continue
            session.add(CandidateOutcome(candidate_id=candidate.id, evaluated_at=now, **asdict(outcome)))
            written += 1
    session.commit()
    return written


# ------------------------------------------------------------------------- run

def _record_health(session: Session, providers: Dict[str, object], run: ScanRun, now: datetime) -> dict:
    report = {}
    for name, provider in providers.items():
        if provider.disabled_reason:
            status = "skipped" if provider.disabled_reason.startswith("missing_credentials") else "down"
            detail = provider.disabled_reason
        elif provider.successes == 0 and provider.failures == 0:
            status, detail = "skipped", "not called in this run"
        elif provider.failures and provider.successes == 0:
            status, detail = "down", f"0 of {provider.failures} request(s) succeeded"
        elif provider.failures:
            status, detail = "degraded", f"{provider.successes} ok, {provider.failures} failed"
        else:
            status, detail = "ok", f"{provider.successes} request(s) ok"
        report[name] = {"status": status, "detail": detail}
        session.add(SystemHealth(component=f"provider:{name}", status=status, detail=detail, recorded_at=now))
    session.add(SystemHealth(
        component="scan", status="ok" if run.status == "success" else "degraded" if run.status == "partial" else "down",
        detail=f"{run.scan_type} {run.status}: {run.instruments_collected}/{run.instruments_attempted} instruments, "
               f"{run.candles_stored} new candles, {run.candidates_total} candidates",
        recorded_at=now,
    ))
    return report


def _execute(session: Session, run: ScanRun, providers: Dict[str, object], settings: Settings, now: datetime) -> None:
    exchanges = {e.id: e for e in session.scalars(select(Exchange)).all()}
    instruments = session.scalars(
        select(Instrument).where(Instrument.active.is_(True)).order_by(Instrument.id)
    ).all()
    if run.scope == "crypto_only":
        instruments = [i for i in instruments if i.asset_class == "crypto"]
    # crypto first (keyless and fast), then benchmarks, then everything else
    instruments.sort(key=lambda i: (i.asset_class != "crypto", not i.is_benchmark, i.id))

    twelve_data = providers.get("twelve_data")
    if twelve_data is not None and twelve_data.available and any(i.asset_class != "crypto" for i in instruments):
        twelve_data.refresh_usage()

    failures = _collect(session, run, instruments, exchanges, providers, now)

    crypto = [i for i in instruments if i.asset_class == "crypto"]
    snapshots = collect_crypto_snapshots(session, providers.get("coinmarketcap"), crypto, now)
    cross_checks = []
    if run.scope == "all":
        cross_checks = cross_check_equities(
            session, providers.get("alpha_vantage"), instruments, run.id, now,
            settings.alpha_vantage_daily_checks,
        )
    _flush_provider_records(session, providers)
    session.commit()

    counts = _analyse(session, run, instruments, failures, settings, now)
    outcomes = update_outcomes(session, now)

    # A symbol the provider's plan does not cover is a known limitation, not a failed run.
    unexpected = [d for d in failures.values() if "unsupported" not in d and "not_found" not in d]
    run.ready_count = counts["READY"]
    run.near_ready_count = counts["NEAR-READY"]
    run.preparing_count = counts["PREPARING"]
    run.rejected_count = counts["REJECTED"]
    run.candidates_total = sum(counts.values())
    if run.instruments_collected == 0:
        run.status = "failed"
        run.error = "no instrument could be collected from any provider"
    elif unexpected or run.candidates_total < len(instruments):
        run.status = "partial"
    else:
        run.status = "success"
    run.finished_at = utcnow()
    health = _record_health(session, providers, run, run.finished_at)
    unsupported = sorted(
        i.symbol for i in instruments if i.id in failures
    )
    run.summary = {
        "providers": health,
        "instruments_without_data": unsupported,
        "failure_details": {
            next(i.symbol for i in instruments if i.id == key): value[:300] for key, value in failures.items()
        },
        "crypto_snapshots_stored": snapshots,
        "cross_checks": len(cross_checks),
        "outcomes_written": outcomes,
        "twelve_data_plan": {
            "category": getattr(twelve_data, "plan_category", None),
            "credits_per_minute": getattr(twelve_data, "plan_limit_per_minute", None),
            "daily_limit": getattr(twelve_data, "plan_daily_limit", None),
            "daily_usage_at_start": getattr(twelve_data, "daily_usage", None),
        },
        "duration_seconds": round((run.finished_at - as_utc(run.started_at)).total_seconds(), 1),
    }
    _flush_provider_records(session, providers)
    session.commit()


def run_scan(scan_type: str, now: Optional[datetime] = None, providers: Optional[Dict[str, object]] = None) -> dict:
    """Run one scan. Safe to call concurrently or twice for the same slot."""
    now = as_utc(now) if now is not None else utcnow()
    if not _run_lock.acquire(blocking=False):
        log.warning("Scan '%s' skipped: another scan is already running", scan_type)
        return {"status": "skipped", "reason": "another scan is already running"}
    lock_connection = None
    session = None
    own_providers = providers is None
    try:
        engine = get_engine()
        if engine.dialect.name == "postgresql":
            lock_connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
            if not lock_connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": ADVISORY_LOCK_KEY}
            ).scalar():
                log.warning("Scan '%s' skipped: another process holds the scan lock", scan_type)
                lock_connection.close()
                lock_connection = None
                return {"status": "skipped", "reason": "another process holds the scan lock"}
        session = new_session()
        settings = get_settings()
        slot_key = slot_key_for(scan_type, now)
        run = session.scalar(select(ScanRun).where(ScanRun.slot_key == slot_key))
        if run is not None and run.status in ("success", "partial"):
            log.info("Scan slot %s already completed (%s); nothing to do", slot_key, run.status)
            return {"status": "skipped", "reason": f"slot {slot_key} already completed", "scan_run_id": run.id}
        if run is None:
            run = ScanRun(
                scan_type=scan_type, slot_key=slot_key, scope=scan_scope(now), status="running",
                started_at=now, instruments_attempted=0, instruments_collected=0, candles_stored=0,
                candidates_total=0, ready_count=0, near_ready_count=0, preparing_count=0, rejected_count=0,
            )
            session.add(run)
        else:  # a previous attempt at this slot failed or was interrupted: retry it cleanly
            stale = select(ScanCandidate.id).where(ScanCandidate.scan_run_id == run.id)
            session.execute(delete(CandidateLevel).where(CandidateLevel.candidate_id.in_(stale)))
            session.execute(delete(CandidateOutcome).where(CandidateOutcome.candidate_id.in_(stale)))
            session.execute(delete(ScanCandidate).where(ScanCandidate.scan_run_id == run.id))
            run.status, run.started_at, run.finished_at, run.error = "running", now, None, None
            run.instruments_attempted = run.instruments_collected = run.candles_stored = 0
            run.candidates_total = run.ready_count = run.near_ready_count = 0
            run.preparing_count = run.rejected_count = 0
            run.scope = scan_scope(now)
        session.commit()
        run_id = run.id
        log.info("Scan %s started (id %s, scope %s)", slot_key, run_id, run.scope)
        if own_providers:
            providers = build_providers(settings)
        try:
            _execute(session, run, providers, settings, now)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            log.exception("Scan %s failed", slot_key)
            run = session.get(ScanRun, run_id)
            run.status = "failed"
            run.finished_at = utcnow()
            run.error = redact(f"{type(exc).__name__}: {exc}")[:1000]
            session.add(SystemHealth(component="scan", status="down", detail=run.error, recorded_at=run.finished_at))
            _flush_provider_records(session, providers)
            session.commit()
        log.info(
            "Scan %s finished: %s - %d/%d instruments, %d new candles, READY %d, NEAR-READY %d, PREPARING %d, REJECTED %d",
            slot_key, run.status, run.instruments_collected, run.instruments_attempted, run.candles_stored,
            run.ready_count, run.near_ready_count, run.preparing_count, run.rejected_count,
        )
        return {
            "status": run.status, "scan_run_id": run_id, "slot_key": slot_key, "scope": run.scope,
            "instruments_collected": run.instruments_collected, "candles_stored": run.candles_stored,
            "candidates": run.candidates_total, "error": run.error,
        }
    finally:
        if own_providers and providers:
            for provider in providers.values():
                provider.close()
        if session is not None:
            session.close()
        if lock_connection is not None:
            try:
                lock_connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": ADVISORY_LOCK_KEY})
            finally:
                lock_connection.close()
        _run_lock.release()


def last_completed_run(session: Session) -> Optional[ScanRun]:
    return session.scalar(
        select(ScanRun).where(ScanRun.status.in_(("success", "partial")))
        .order_by(ScanRun.started_at.desc()).limit(1)
    )


def candle_stats(session: Session) -> dict:
    total, latest = session.execute(
        select(func.count(MarketCandle.id), func.max(MarketCandle.retrieved_at))
    ).one()
    with_data = session.scalar(select(func.count(func.distinct(MarketCandle.instrument_id)))) or 0
    return {
        "candles_stored": int(total or 0),
        "instruments_with_candles": int(with_data),
        "most_recent_collection": as_utc(latest).isoformat() if latest else None,
    }
