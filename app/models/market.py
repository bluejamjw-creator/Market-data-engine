"""Reference data, candles, snapshots and indicators."""
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


class Exchange(Base):
    __tablename__ = "exchanges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    region: Mapped[str] = mapped_column(String(10), nullable=False)  # US, UK, EU, CRYPTO
    country: Mapped[Optional[str]] = mapped_column(String(50))
    timezone: Mapped[str] = mapped_column(String(50), nullable=False)
    currency: Mapped[Optional[str]] = mapped_column(String(10))
    mic_code: Mapped[Optional[str]] = mapped_column(String(10))


class Instrument(Base):
    __tablename__ = "instruments"
    __table_args__ = (UniqueConstraint("symbol", "region", name="uq_instruments_symbol_region"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(10), nullable=False)  # equity, etf, crypto
    region: Mapped[str] = mapped_column(String(10), nullable=False)
    exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id"), nullable=False)
    currency: Mapped[Optional[str]] = mapped_column(String(10))
    # Provider symbol mappings (None = not available from that provider).
    td_symbol: Mapped[Optional[str]] = mapped_column(String(30))
    td_mic_code: Mapped[Optional[str]] = mapped_column(String(10))
    av_symbol: Mapped[Optional[str]] = mapped_column(String(30))
    binance_symbol: Mapped[Optional[str]] = mapped_column(String(30))
    cmc_symbol: Mapped[Optional[str]] = mapped_column(String(30))
    benchmark_symbol: Mapped[Optional[str]] = mapped_column(String(30))
    is_benchmark: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Provider that actually supplied this instrument's candles.
    primary_provider: Mapped[Optional[str]] = mapped_column(String(30))
    # {provider: {"reason": str, "checked_at": iso8601}} for providers that
    # refused this symbol, so quota is not spent retrying every scan.
    unsupported_providers: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)


class MarketCandle(Base):
    __tablename__ = "market_candles"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "provider", "timeframe", "open_time", name="uq_market_candles_key"
        ),
        Index("ix_market_candles_lookup", "instrument_id", "timeframe", "open_time"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(5), nullable=False)
    open_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    # NULL means the provider did not report volume. It is never stored as 0.
    volume: Mapped[Optional[float]] = mapped_column(Float)
    is_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    retrieved_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "provider", "snapshot_time", name="uq_market_snapshots_key"
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    snapshot_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    price: Mapped[Optional[float]] = mapped_column(Float)
    volume_24h: Mapped[Optional[float]] = mapped_column(Float)
    market_cap: Mapped[Optional[float]] = mapped_column(Float)
    market_cap_rank: Mapped[Optional[int]] = mapped_column(Integer)
    pct_change_24h: Mapped[Optional[float]] = mapped_column(Float)
    pct_change_7d: Mapped[Optional[float]] = mapped_column(Float)
    retrieved_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)


class TechnicalIndicator(Base):
    __tablename__ = "technical_indicators"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "timeframe", "candle_time", name="uq_technical_indicators_key"
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(5), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    candle_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    # NULL = not enough verified data to calculate (never a placeholder value).
    ema9: Mapped[Optional[float]] = mapped_column(Float)
    ema20: Mapped[Optional[float]] = mapped_column(Float)
    ema50: Mapped[Optional[float]] = mapped_column(Float)
    ema200: Mapped[Optional[float]] = mapped_column(Float)
    rsi14: Mapped[Optional[float]] = mapped_column(Float)
    macd: Mapped[Optional[float]] = mapped_column(Float)
    macd_signal: Mapped[Optional[float]] = mapped_column(Float)
    macd_hist: Mapped[Optional[float]] = mapped_column(Float)
    atr14: Mapped[Optional[float]] = mapped_column(Float)
    adx14: Mapped[Optional[float]] = mapped_column(Float)
    plus_di: Mapped[Optional[float]] = mapped_column(Float)
    minus_di: Mapped[Optional[float]] = mapped_column(Float)
    bb_upper: Mapped[Optional[float]] = mapped_column(Float)
    bb_middle: Mapped[Optional[float]] = mapped_column(Float)
    bb_lower: Mapped[Optional[float]] = mapped_column(Float)
    bb_width: Mapped[Optional[float]] = mapped_column(Float)
    avg_volume20: Mapped[Optional[float]] = mapped_column(Float)
    rel_volume: Mapped[Optional[float]] = mapped_column(Float)
    support: Mapped[Optional[float]] = mapped_column(Float)
    resistance: Mapped[Optional[float]] = mapped_column(Float)
    last_swing_high: Mapped[Optional[float]] = mapped_column(Float)
    last_swing_low: Mapped[Optional[float]] = mapped_column(Float)
    trend_strength: Mapped[Optional[float]] = mapped_column(Float)
    rs_20: Mapped[Optional[float]] = mapped_column(Float)
    rs_60: Mapped[Optional[float]] = mapped_column(Float)
    benchmark_symbol: Mapped[Optional[str]] = mapped_column(String(30))
    structure: Mapped[Optional[str]] = mapped_column(Text)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
