"""Initial schema: reference data, candles, indicators, scans, outcomes, health.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-11-15
"""
import sqlalchemy as sa
from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None

# 64-bit ids on PostgreSQL; plain INTEGER on SQLite so it auto-increments.
BigId = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
Ts = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "exchanges",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("region", sa.String(10), nullable=False),
        sa.Column("country", sa.String(50)),
        sa.Column("timezone", sa.String(50), nullable=False),
        sa.Column("currency", sa.String(10)),
        sa.Column("mic_code", sa.String(10)),
        sa.UniqueConstraint("code", name="uq_exchanges_code"),
    )
    op.create_table(
        "instruments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(30), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("asset_class", sa.String(10), nullable=False),
        sa.Column("region", sa.String(10), nullable=False),
        sa.Column("exchange_id", sa.Integer(), sa.ForeignKey("exchanges.id"), nullable=False),
        sa.Column("currency", sa.String(10)),
        sa.Column("td_symbol", sa.String(30)),
        sa.Column("td_mic_code", sa.String(10)),
        sa.Column("av_symbol", sa.String(30)),
        sa.Column("binance_symbol", sa.String(30)),
        sa.Column("cmc_symbol", sa.String(30)),
        sa.Column("benchmark_symbol", sa.String(30)),
        sa.Column("is_benchmark", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("primary_provider", sa.String(30)),
        sa.Column("unsupported_providers", sa.JSON()),
        sa.Column("created_at", Ts, nullable=False),
        sa.Column("updated_at", Ts, nullable=False),
        sa.UniqueConstraint("symbol", "region", name="uq_instruments_symbol_region"),
    )
    op.create_table(
        "market_candles",
        sa.Column("id", BigId, primary_key=True, autoincrement=True),
        sa.Column("instrument_id", sa.Integer(), sa.ForeignKey("instruments.id"), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("timeframe", sa.String(5), nullable=False),
        sa.Column("open_time", Ts, nullable=False),
        sa.Column("open", sa.Float(), nullable=False),
        sa.Column("high", sa.Float(), nullable=False),
        sa.Column("low", sa.Float(), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("volume", sa.Float()),
        sa.Column("is_complete", sa.Boolean(), nullable=False),
        sa.Column("retrieved_at", Ts, nullable=False),
        sa.UniqueConstraint("instrument_id", "provider", "timeframe", "open_time", name="uq_market_candles_key"),
    )
    op.create_index("ix_market_candles_lookup", "market_candles", ["instrument_id", "timeframe", "open_time"])
    op.create_table(
        "market_snapshots",
        sa.Column("id", BigId, primary_key=True, autoincrement=True),
        sa.Column("instrument_id", sa.Integer(), sa.ForeignKey("instruments.id"), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("snapshot_time", Ts, nullable=False),
        sa.Column("price", sa.Float()),
        sa.Column("volume_24h", sa.Float()),
        sa.Column("market_cap", sa.Float()),
        sa.Column("market_cap_rank", sa.Integer()),
        sa.Column("pct_change_24h", sa.Float()),
        sa.Column("pct_change_7d", sa.Float()),
        sa.Column("retrieved_at", Ts, nullable=False),
        sa.UniqueConstraint("instrument_id", "provider", "snapshot_time", name="uq_market_snapshots_key"),
    )
    op.create_table(
        "technical_indicators",
        sa.Column("id", BigId, primary_key=True, autoincrement=True),
        sa.Column("instrument_id", sa.Integer(), sa.ForeignKey("instruments.id"), nullable=False),
        sa.Column("timeframe", sa.String(5), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("candle_time", Ts, nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("ema9", sa.Float()),
        sa.Column("ema20", sa.Float()),
        sa.Column("ema50", sa.Float()),
        sa.Column("ema200", sa.Float()),
        sa.Column("rsi14", sa.Float()),
        sa.Column("macd", sa.Float()),
        sa.Column("macd_signal", sa.Float()),
        sa.Column("macd_hist", sa.Float()),
        sa.Column("atr14", sa.Float()),
        sa.Column("adx14", sa.Float()),
        sa.Column("plus_di", sa.Float()),
        sa.Column("minus_di", sa.Float()),
        sa.Column("bb_upper", sa.Float()),
        sa.Column("bb_middle", sa.Float()),
        sa.Column("bb_lower", sa.Float()),
        sa.Column("bb_width", sa.Float()),
        sa.Column("avg_volume20", sa.Float()),
        sa.Column("rel_volume", sa.Float()),
        sa.Column("support", sa.Float()),
        sa.Column("resistance", sa.Float()),
        sa.Column("last_swing_high", sa.Float()),
        sa.Column("last_swing_low", sa.Float()),
        sa.Column("trend_strength", sa.Float()),
        sa.Column("rs_20", sa.Float()),
        sa.Column("rs_60", sa.Float()),
        sa.Column("benchmark_symbol", sa.String(30)),
        sa.Column("structure", sa.Text()),
        sa.Column("computed_at", Ts, nullable=False),
        sa.UniqueConstraint("instrument_id", "timeframe", "candle_time", name="uq_technical_indicators_key"),
    )
    op.create_table(
        "scan_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scan_type", sa.String(20), nullable=False),
        sa.Column("slot_key", sa.String(60), nullable=False),
        sa.Column("scope", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("started_at", Ts, nullable=False),
        sa.Column("finished_at", Ts),
        sa.Column("instruments_attempted", sa.Integer(), nullable=False),
        sa.Column("instruments_collected", sa.Integer(), nullable=False),
        sa.Column("candles_stored", sa.Integer(), nullable=False),
        sa.Column("candidates_total", sa.Integer(), nullable=False),
        sa.Column("ready_count", sa.Integer(), nullable=False),
        sa.Column("near_ready_count", sa.Integer(), nullable=False),
        sa.Column("preparing_count", sa.Integer(), nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("summary", sa.JSON()),
        sa.UniqueConstraint("slot_key", name="uq_scan_runs_slot_key"),
    )
    op.create_table(
        "scan_candidates",
        sa.Column("id", BigId, primary_key=True, autoincrement=True),
        sa.Column("scan_run_id", sa.Integer(), sa.ForeignKey("scan_runs.id"), nullable=False),
        sa.Column("instrument_id", sa.Integer(), sa.ForeignKey("instruments.id"), nullable=False),
        sa.Column("classification", sa.String(15), nullable=False),
        sa.Column("do_not_chase", sa.Boolean(), nullable=False),
        sa.Column("do_not_chase_reason", sa.Text()),
        sa.Column("setup_type", sa.String(30)),
        sa.Column("setup_description", sa.Text()),
        sa.Column("score_total", sa.Float()),
        sa.Column("score_max_verified", sa.Float()),
        sa.Column("provisional", sa.Boolean(), nullable=False),
        sa.Column("pattern_score", sa.Float()),
        sa.Column("technical_score", sa.Float()),
        sa.Column("rs_regime_score", sa.Float()),
        sa.Column("liquidity_quality_score", sa.Float()),
        sa.Column("reward_risk_score", sa.Float()),
        sa.Column("component_status", sa.JSON()),
        sa.Column("reasons", sa.JSON()),
        sa.Column("price", sa.Float()),
        sa.Column("price_time", Ts),
        sa.Column("signal_candle_time", Ts),
        sa.Column("timeframe", sa.String(5)),
        sa.Column("trigger_price", sa.Float()),
        sa.Column("entry_low", sa.Float()),
        sa.Column("entry_high", sa.Float()),
        sa.Column("stop_price", sa.Float()),
        sa.Column("target_price", sa.Float()),
        sa.Column("reward_risk", sa.Float()),
        sa.Column("est_cost_pct", sa.Float()),
        sa.Column("data_quality_status", sa.String(15)),
        sa.Column("expires_at", Ts),
        sa.Column("invalidation", sa.Text()),
        sa.Column("is_first_for_candle", sa.Boolean(), nullable=False),
        sa.Column("created_at", Ts, nullable=False),
        sa.UniqueConstraint("scan_run_id", "instrument_id", name="uq_scan_candidates_run_instrument"),
    )
    op.create_index("ix_scan_candidates_classification", "scan_candidates", ["classification", "created_at"])
    op.create_table(
        "candidate_levels",
        sa.Column("id", BigId, primary_key=True, autoincrement=True),
        sa.Column("candidate_id", BigId, sa.ForeignKey("scan_candidates.id"), nullable=False),
        sa.Column("level_type", sa.String(20), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("basis", sa.Text()),
    )
    op.create_index("ix_candidate_levels_candidate_id", "candidate_levels", ["candidate_id"])
    op.create_table(
        "candidate_outcomes",
        sa.Column("id", BigId, primary_key=True, autoincrement=True),
        sa.Column("candidate_id", BigId, sa.ForeignKey("scan_candidates.id"), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("evaluated_at", Ts, nullable=False),
        sa.Column("ref_price", sa.Float(), nullable=False),
        sa.Column("close_price", sa.Float(), nullable=False),
        sa.Column("return_pct", sa.Float(), nullable=False),
        sa.Column("mfe_pct", sa.Float(), nullable=False),
        sa.Column("mae_pct", sa.Float(), nullable=False),
        sa.Column("trigger_hit", sa.Boolean()),
        sa.Column("stop_hit", sa.Boolean()),
        sa.Column("target_hit", sa.Boolean()),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("bars_to_outcome", sa.Integer()),
        sa.UniqueConstraint("candidate_id", "horizon_days", name="uq_candidate_outcomes_key"),
    )
    op.create_table(
        "provider_requests",
        sa.Column("id", BigId, primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("endpoint", sa.String(60), nullable=False),
        sa.Column("symbol", sa.String(60)),
        sa.Column("timeframe", sa.String(5)),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("status_code", sa.Integer()),
        sa.Column("error_kind", sa.String(30)),
        sa.Column("error_message", sa.Text()),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("rows_returned", sa.Integer()),
        sa.Column("duration_ms", sa.Float()),
        sa.Column("requested_at", Ts, nullable=False),
    )
    op.create_index(
        "ix_provider_requests_lookup", "provider_requests", ["provider", "symbol", "timeframe", "requested_at"]
    )
    op.create_table(
        "data_quality_checks",
        sa.Column("id", BigId, primary_key=True, autoincrement=True),
        sa.Column("scan_run_id", sa.Integer(), sa.ForeignKey("scan_runs.id")),
        sa.Column("instrument_id", sa.Integer(), sa.ForeignKey("instruments.id")),
        sa.Column("provider", sa.String(30)),
        sa.Column("timeframe", sa.String(5)),
        sa.Column("check_type", sa.String(40), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("detail", sa.Text()),
        sa.Column("checked_at", Ts, nullable=False),
    )
    op.create_index("ix_data_quality_checks_run", "data_quality_checks", ["scan_run_id"])
    op.create_table(
        "system_health",
        sa.Column("id", BigId, primary_key=True, autoincrement=True),
        sa.Column("component", sa.String(40), nullable=False),
        sa.Column("status", sa.String(15), nullable=False),
        sa.Column("detail", sa.Text()),
        sa.Column("recorded_at", Ts, nullable=False),
    )
    op.create_index("ix_system_health_component", "system_health", ["component", "recorded_at"])


def downgrade() -> None:
    for table in (
        "system_health",
        "data_quality_checks",
        "provider_requests",
        "candidate_outcomes",
        "candidate_levels",
        "scan_candidates",
        "scan_runs",
        "technical_indicators",
        "market_snapshots",
        "market_candles",
        "instruments",
        "exchanges",
    ):
        op.drop_table(table)
