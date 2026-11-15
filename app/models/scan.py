"""Scan runs, candidates, their price levels and tracked outcomes."""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPK, UTCDateTime, utcnow


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # One row per scheduled slot, e.g. "2026-11-16:morning" - makes jobs idempotent.
    slot_key: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)  # all | crypto_only
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # running|success|partial|failed
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    instruments_attempted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    instruments_collected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candles_stored: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidates_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ready_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    near_ready_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    preparing_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text)
    summary: Mapped[Optional[dict]] = mapped_column(JSON)


class ScanCandidate(Base):
    __tablename__ = "scan_candidates"
    __table_args__ = (
        UniqueConstraint("scan_run_id", "instrument_id", name="uq_scan_candidates_run_instrument"),
        Index("ix_scan_candidates_classification", "classification", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    scan_run_id: Mapped[int] = mapped_column(ForeignKey("scan_runs.id"), nullable=False)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), nullable=False)
    classification: Mapped[str] = mapped_column(String(15), nullable=False)
    do_not_chase: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    do_not_chase_reason: Mapped[Optional[str]] = mapped_column(Text)
    setup_type: Mapped[Optional[str]] = mapped_column(String(30))
    setup_description: Mapped[Optional[str]] = mapped_column(Text)
    # Points earned from verified components, and the maximum those components allow.
    score_total: Mapped[Optional[float]] = mapped_column(Float)
    score_max_verified: Mapped[Optional[float]] = mapped_column(Float)
    provisional: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pattern_score: Mapped[Optional[float]] = mapped_column(Float)
    technical_score: Mapped[Optional[float]] = mapped_column(Float)
    rs_regime_score: Mapped[Optional[float]] = mapped_column(Float)
    liquidity_quality_score: Mapped[Optional[float]] = mapped_column(Float)
    reward_risk_score: Mapped[Optional[float]] = mapped_column(Float)
    component_status: Mapped[Optional[dict]] = mapped_column(JSON)
    reasons: Mapped[Optional[list]] = mapped_column(JSON)
    price: Mapped[Optional[float]] = mapped_column(Float)
    price_time: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    signal_candle_time: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    timeframe: Mapped[Optional[str]] = mapped_column(String(5))
    trigger_price: Mapped[Optional[float]] = mapped_column(Float)
    entry_low: Mapped[Optional[float]] = mapped_column(Float)
    entry_high: Mapped[Optional[float]] = mapped_column(Float)
    stop_price: Mapped[Optional[float]] = mapped_column(Float)
    target_price: Mapped[Optional[float]] = mapped_column(Float)
    reward_risk: Mapped[Optional[float]] = mapped_column(Float)
    est_cost_pct: Mapped[Optional[float]] = mapped_column(Float)
    data_quality_status: Mapped[Optional[str]] = mapped_column(String(15))
    expires_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    invalidation: Mapped[Optional[str]] = mapped_column(Text)
    # True for the first candidate recorded for this instrument and signal candle;
    # only these are outcome-tracked, so repeat scans are not double counted.
    is_first_for_candle: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)


class CandidateLevel(Base):
    __tablename__ = "candidate_levels"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        BigIntPK, ForeignKey("scan_candidates.id"), nullable=False, index=True
    )
    level_type: Mapped[str] = mapped_column(String(20), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    basis: Mapped[Optional[str]] = mapped_column(Text)


class CandidateOutcome(Base):
    __tablename__ = "candidate_outcomes"
    __table_args__ = (
        UniqueConstraint("candidate_id", "horizon_days", name="uq_candidate_outcomes_key"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        BigIntPK, ForeignKey("scan_candidates.id"), nullable=False
    )
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    ref_price: Mapped[float] = mapped_column(Float, nullable=False)
    close_price: Mapped[float] = mapped_column(Float, nullable=False)
    return_pct: Mapped[float] = mapped_column(Float, nullable=False)
    mfe_pct: Mapped[float] = mapped_column(Float, nullable=False)
    mae_pct: Mapped[float] = mapped_column(Float, nullable=False)
    trigger_hit: Mapped[Optional[bool]] = mapped_column(Boolean)
    stop_hit: Mapped[Optional[bool]] = mapped_column(Boolean)
    target_hit: Mapped[Optional[bool]] = mapped_column(Boolean)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    bars_to_outcome: Mapped[Optional[int]] = mapped_column(Integer)
