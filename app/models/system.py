"""Operational records: provider calls, data-quality checks, health."""
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPK, UTCDateTime, utcnow


class ProviderRequest(Base):
    __tablename__ = "provider_requests"
    __table_args__ = (
        Index("ix_provider_requests_lookup", "provider", "symbol", "timeframe", "requested_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(60), nullable=False)
    symbol: Mapped[Optional[str]] = mapped_column(String(60))
    timeframe: Mapped[Optional[str]] = mapped_column(String(5))
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status_code: Mapped[Optional[int]] = mapped_column(Integer)
    error_kind: Mapped[Optional[str]] = mapped_column(String(30))
    error_message: Mapped[Optional[str]] = mapped_column(Text)  # always redacted
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    rows_returned: Mapped[Optional[int]] = mapped_column(Integer)
    duration_ms: Mapped[Optional[float]] = mapped_column(Float)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)


class DataQualityCheck(Base):
    __tablename__ = "data_quality_checks"
    __table_args__ = (Index("ix_data_quality_checks_run", "scan_run_id"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    scan_run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("scan_runs.id"))
    instrument_id: Mapped[Optional[int]] = mapped_column(ForeignKey("instruments.id"))
    provider: Mapped[Optional[str]] = mapped_column(String(30))
    timeframe: Mapped[Optional[str]] = mapped_column(String(5))
    check_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)  # PASS | WARN | FAIL
    detail: Mapped[Optional[str]] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)


class SystemHealth(Base):
    __tablename__ = "system_health"
    __table_args__ = (Index("ix_system_health_component", "component", "recorded_at"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    component: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(15), nullable=False)  # ok | degraded | down | skipped
    detail: Mapped[Optional[str]] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
