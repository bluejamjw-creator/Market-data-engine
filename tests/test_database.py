"""Database connection, URL handling, duplicate protection and migrations."""
from datetime import datetime, timezone

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine, func, inspect, select  # noqa: E402

from app.database import describe_database_target, normalise_database_url, upsert_rows  # noqa: E402

UTC = timezone.utc


@pytest.mark.parametrize("raw,expected", [
    ("postgres://u:p@host:5432/db", "postgresql+psycopg://u:p@host:5432/db"),
    ("postgresql://u:p@postgres.railway.internal:5432/railway",
     "postgresql+psycopg://u:p@postgres.railway.internal:5432/railway"),
    ("postgresql+psycopg2://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
    ("  'postgresql://u:p@host/db?sslmode=require'  ", "postgresql+psycopg://u:p@host/db?sslmode=require"),
    ("postgresql+psycopg://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
    ("sqlite:///x.db", "sqlite:///x.db"),
])
def test_railway_database_urls_are_normalised_for_psycopg3(raw, expected):
    assert normalise_database_url(raw) == expected


def test_missing_database_url_is_an_explicit_error():
    with pytest.raises(ValueError):
        normalise_database_url("")


def test_database_target_description_never_contains_credentials():
    text = describe_database_target("postgresql+psycopg://user:topsecret@db.example:5432/railway")
    assert "topsecret" not in text and "user" not in text
    assert text == "postgresql://db.example:5432/railway"


def test_database_connection_check(db):
    ok, detail = db.check_connection()
    assert ok and detail == "ok"


def test_connection_failure_is_reported_without_raising(monkeypatch, tmp_path):
    from app import database
    from app.config import get_settings

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/missing-dir/none.db")
    get_settings.cache_clear()
    database.reset_engine()
    try:
        ok, detail = database.check_connection()
        assert not ok and detail
    finally:
        database.reset_engine()
        get_settings.cache_clear()


def _candle(instrument_id, close, complete=True):
    return dict(
        instrument_id=instrument_id, provider="binance", timeframe="1d",
        open_time=datetime(2026, 11, 10, tzinfo=UTC), open=10.0, high=12.0, low=9.0, close=close,
        volume=None, is_complete=complete, retrieved_at=datetime(2026, 11, 11, tzinfo=UTC),
    )


def test_duplicate_candles_are_updated_not_duplicated(db):
    from app.models import Exchange, Instrument, MarketCandle

    with db.session_scope() as session:
        exchange = Exchange(code="CRYPTO", name="Crypto", region="CRYPTO", timezone="UTC")
        session.add(exchange)
        session.flush()
        instrument = Instrument(symbol="BTC", name="Bitcoin", asset_class="crypto", region="CRYPTO",
                                exchange_id=exchange.id)
        session.add(instrument)
        session.flush()
        keys = ["instrument_id", "provider", "timeframe", "open_time"]
        updates = ["open", "high", "low", "close", "volume", "is_complete", "retrieved_at"]
        upsert_rows(session, MarketCandle, [_candle(instrument.id, 11.0, complete=False)], keys, updates)
        upsert_rows(session, MarketCandle, [_candle(instrument.id, 11.5)], keys, updates)
        upsert_rows(session, MarketCandle, [_candle(instrument.id, 11.5)], keys, updates)
        assert session.scalar(select(func.count()).select_from(MarketCandle)) == 1
        stored = session.scalars(select(MarketCandle)).one()
        assert stored.close == 11.5 and stored.is_complete is True
        assert stored.volume is None  # unknown volume is stored as NULL, never 0
        assert stored.open_time == datetime(2026, 11, 10, tzinfo=UTC) and stored.open_time.tzinfo is not None


def test_unique_constraint_blocks_duplicate_inserts(db):
    from sqlalchemy.exc import IntegrityError

    from app.models import Exchange, Instrument, MarketCandle

    with pytest.raises(IntegrityError):
        with db.session_scope() as session:
            exchange = Exchange(code="CRYPTO", name="Crypto", region="CRYPTO", timezone="UTC")
            session.add(exchange)
            session.flush()
            instrument = Instrument(symbol="BTC", name="Bitcoin", asset_class="crypto", region="CRYPTO",
                                    exchange_id=exchange.id)
            session.add(instrument)
            session.flush()
            session.add(MarketCandle(**_candle(instrument.id, 11.0)))
            session.add(MarketCandle(**_candle(instrument.id, 11.2)))


REQUIRED_TABLES = {
    "instruments", "exchanges", "market_candles", "market_snapshots", "technical_indicators",
    "provider_requests", "data_quality_checks", "scan_runs", "scan_candidates", "candidate_levels",
    "candidate_outcomes", "system_health",
}


def test_migrations_create_the_schema_the_models_expect(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config

    from app import database
    from app.config import get_settings
    from app.models import Base

    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    database.reset_engine()
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "head")
        engine = create_engine(url)
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert REQUIRED_TABLES <= tables
        for table in Base.metadata.sorted_tables:
            migrated = {column["name"] for column in inspector.get_columns(table.name)}
            assert migrated == {column.name for column in table.columns}, table.name
        uniques = {tuple(u["column_names"]) for u in inspector.get_unique_constraints("market_candles")}
        assert ("instrument_id", "provider", "timeframe", "open_time") in uniques
        command.upgrade(config, "head")  # running migrations twice is harmless
        command.downgrade(config, "base")
        assert not (REQUIRED_TABLES & set(inspect(create_engine(url)).get_table_names()))
        engine.dispose()
    finally:
        database.reset_engine()
        get_settings.cache_clear()
