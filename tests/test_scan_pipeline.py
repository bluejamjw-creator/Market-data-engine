"""The whole scan against a fake market API: collect, store, analyse, re-run."""
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("httpx")

from sqlalchemy import func, select  # noqa: E402

from app.collectors.candles import needs_fetch  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models import (  # noqa: E402
    CandidateLevel, CandidateOutcome, DataQualityCheck, Exchange, Instrument, MarketCandle,
    MarketSnapshot, ProviderRequest, ScanCandidate, ScanRun, SystemHealth, TechnicalIndicator,
)
from app.services.scan_service import build_providers, run_scan, update_outcomes  # noqa: E402
from app.universe import build_universe, seed_universe  # noqa: E402
from tests.helpers import frame_from_closes, uptrend_closes  # noqa: E402
from tests.mock_market import FAKE_KEYS, MockMarket  # noqa: E402

UTC = timezone.utc
THURSDAY = datetime(2026, 11, 12, 18, 0, tzinfo=UTC)
SATURDAY = datetime(2026, 11, 14, 7, 30, tzinfo=UTC)


def providers_for(market):
    return build_providers(get_settings(), transport=market.transport, sleep=lambda _s: None)


def count(session, model, *where):
    return session.scalar(select(func.count()).select_from(model).where(*where))


@pytest.fixture
def seeded(db):
    with db.session_scope() as session:
        seed_universe(session)
    return db


def test_universe_seeding_is_idempotent(seeded):
    universe = build_universe()
    with seeded.session_scope() as session:
        assert seed_universe(session) == {"exchanges_added": 0, "instruments_added": 0}
        assert count(session, Instrument) == len(universe)
        regions = {r for (r,) in session.execute(select(Instrument.region).distinct())}
        assert regions == {"US", "UK", "EU", "CRYPTO"}
        for region in regions:  # every market has exactly one benchmark
            assert count(session, Instrument, Instrument.region == region, Instrument.is_benchmark.is_(True)) == 1


def test_weekday_scan_collects_stores_and_classifies(seeded):
    market = MockMarket(THURSDAY)
    result = run_scan("review", now=THURSDAY, providers=providers_for(market))
    assert result["status"] == "success", result
    universe = build_universe()
    us_and_crypto = [u for u in universe if u["region"] in ("US", "CRYPTO")]

    with seeded.session_scope() as session:
        run = session.scalars(select(ScanRun)).one()
        assert run.slot_key == "2026-11-12:review" and run.scope == "all" and run.finished_at is not None
        assert run.instruments_attempted == len(universe)
        assert run.instruments_collected == len(us_and_crypto)
        assert run.candles_stored > 10_000

        # genuine provider candles were stored, once each
        total = count(session, MarketCandle)
        distinct = session.scalar(select(func.count()).select_from(
            select(MarketCandle.instrument_id, MarketCandle.provider, MarketCandle.timeframe,
                   MarketCandle.open_time).distinct().subquery()))
        assert total == distinct > 10_000
        assert count(session, MarketCandle, MarketCandle.is_complete.is_(False)) > 0
        btc = session.scalars(select(Instrument).where(Instrument.symbol == "BTC")).one()
        assert btc.primary_provider == "binance"
        aapl = session.scalars(select(Instrument).where(Instrument.symbol == "AAPL")).one()
        assert aapl.primary_provider == "twelve_data"

        # one candidate per instrument, with a recorded reason
        assert count(session, ScanCandidate) == len(universe) == run.candidates_total
        assert run.ready_count + run.near_ready_count + run.preparing_count + run.rejected_count == len(universe)
        for candidate in session.scalars(select(ScanCandidate)).all():
            assert candidate.reasons and candidate.classification in ("READY", "NEAR-READY", "PREPARING", "REJECTED")
            assert candidate.is_first_for_candle is True
            if candidate.signal_candle_time is not None:
                assert candidate.signal_candle_time < THURSDAY  # signals use completed candles only
            if candidate.classification == "READY":
                assert candidate.reward_risk >= 2 and not candidate.provisional and not candidate.do_not_chase
                assert candidate.stop_price < candidate.trigger_price < candidate.target_price
        planned = session.scalars(select(ScanCandidate).where(ScanCandidate.trigger_price.is_not(None))).all()
        assert planned, "the fixture market should produce at least one candidate with a trade plan"
        level_types = {t for (t,) in session.execute(
            select(CandidateLevel.level_type).where(CandidateLevel.candidate_id == planned[0].id))}
        assert {"trigger", "entry_low", "entry_high", "stop", "target"} <= level_types

        # markets the data plan does not cover are reported, not invented
        shel = session.scalars(select(Instrument).where(Instrument.symbol == "SHEL")).one()
        assert "twelve_data" in shel.unsupported_providers
        shel_candidate = session.scalars(select(ScanCandidate).where(ScanCandidate.instrument_id == shel.id)).one()
        assert shel_candidate.classification == "REJECTED" and shel_candidate.data_quality_status == "FAIL"
        assert shel_candidate.price is None and shel_candidate.trigger_price is None
        assert count(session, MarketCandle, MarketCandle.instrument_id == shel.id) == 0

        assert count(session, TechnicalIndicator) > 0
        assert count(session, MarketSnapshot) == 15
        assert count(session, DataQualityCheck, DataQualityCheck.check_type == "provider_cross_check",
                     DataQualityCheck.status == "PASS") == 5
        assert count(session, SystemHealth, SystemHealth.component == "provider:twelve_data") == 1
        assert count(session, ProviderRequest, ProviderRequest.success.is_(True)) > 90
        for request in session.scalars(select(ProviderRequest)).all():
            assert not any(key in f"{request.symbol} {request.error_message}" for key in FAKE_KEYS)

    # quota discipline: one refused request per unsupported exchange, not one per symbol
    for mic in ("XLON", "XETR", "XPAR", "XAMS"):
        assert market.calls[f"td_mic:{mic}"] == 1, mic
    assert market.calls["av"] == 5 and market.calls["cmc"] == 1


def test_rerunning_does_not_refetch_or_duplicate(seeded):
    market = MockMarket(THURSDAY)
    providers = providers_for(market)
    run_scan("review", now=THURSDAY, providers=providers)
    calls_after_first = market.candle_calls()
    with seeded.session_scope() as session:
        candles_after_first = count(session, MarketCandle)

    # the same scheduled slot again: nothing happens at all
    again = run_scan("review", now=THURSDAY + timedelta(minutes=5), providers=providers)
    assert again["status"] == "skipped"

    # a different scan ten minutes later: data is already current, so no candle requests
    later = THURSDAY + timedelta(minutes=10)
    market.now = later
    second = run_scan("manual", now=later, providers=providers)
    assert second["status"] == "success"
    assert market.candle_calls() == calls_after_first
    with seeded.session_scope() as session:
        assert count(session, MarketCandle) == candles_after_first
        assert count(session, ScanRun) == 2
        repeat = session.scalars(
            select(ScanCandidate).join(ScanRun, ScanRun.id == ScanCandidate.scan_run_id)
            .where(ScanRun.scan_type == "manual")).all()
        # same signal candle as the first scan, so these are not outcome-tracked twice
        assert repeat and not any(c.is_first_for_candle for c in repeat if c.signal_candle_time is not None)


def test_incremental_collection_only_adds_new_candles(seeded):
    market = MockMarket(THURSDAY)
    providers = providers_for(market)
    run_scan("review", now=THURSDAY, providers=providers)
    with seeded.session_scope() as session:
        btc = session.scalars(select(Instrument).where(Instrument.symbol == "BTC")).one()
        before = count(session, MarketCandle, MarketCandle.instrument_id == btc.id, MarketCandle.timeframe == "4h")
    later = THURSDAY + timedelta(hours=8)  # two more 4-hour candles have closed
    market.now = later
    run_scan("manual", now=later, providers=providers)
    with seeded.session_scope() as session:
        after = count(session, MarketCandle, MarketCandle.instrument_id == btc.id, MarketCandle.timeframe == "4h")
    assert after == before + 2
    assert market.calls["binance:BTCUSDT:4h"] == 2


def test_weekend_scan_is_crypto_only(seeded):
    market = MockMarket(SATURDAY)
    result = run_scan("morning", now=SATURDAY, providers=providers_for(market))
    assert result["status"] == "success" and result["scope"] == "crypto_only"
    assert not any(key.startswith("td:") for key in market.calls)  # no equity requests at the weekend
    assert market.calls["av"] == 0
    with seeded.session_scope() as session:
        classes = {c for (c,) in session.execute(
            select(Instrument.asset_class).join(ScanCandidate, ScanCandidate.instrument_id == Instrument.id).distinct())}
        assert classes == {"crypto"}
        assert count(session, ScanCandidate) == 15


def test_crypto_falls_back_to_twelve_data_when_binance_is_blocked(seeded):
    market = MockMarket(SATURDAY, binance_blocked_hosts={
        "data-api.binance.vision", "data.binance.com", "api.binance.com"})
    result = run_scan("morning", now=SATURDAY, providers=providers_for(market))
    assert result["status"] == "success"
    with seeded.session_scope() as session:
        btc = session.scalars(select(Instrument).where(Instrument.symbol == "BTC")).one()
        assert btc.primary_provider == "twelve_data"
        health = session.scalars(select(SystemHealth).where(SystemHealth.component == "provider:binance")).one()
        assert health.status == "down" and "blocked" in health.detail


def test_scan_survives_every_provider_being_unavailable(seeded, monkeypatch):
    for name in ("TWELVE_DATA_API_KEY", "ALPHA_VANTAGE_API_KEY", "COINMARKETCAP_API_KEY"):
        monkeypatch.delenv(name)
    get_settings.cache_clear()
    market = MockMarket(THURSDAY, binance_blocked_hosts={
        "data-api.binance.vision", "data.binance.com", "api.binance.com"})
    result = run_scan("review", now=THURSDAY, providers=providers_for(market))
    assert result["status"] == "failed" and "no instrument" in result["error"]
    with seeded.session_scope() as session:
        assert count(session, MarketCandle) == 0
        assert count(session, ScanCandidate, ScanCandidate.classification != "REJECTED") == 0
        skipped = session.scalars(
            select(SystemHealth).where(SystemHealth.component == "provider:twelve_data")).one()
        assert skipped.status == "skipped" and "missing_credentials" in skipped.detail


def test_needs_fetch_rules():
    tz = "America/New_York"
    now = THURSDAY
    assert needs_fetch("crypto", "4h", None, None, now, "UTC")
    assert not needs_fetch("crypto", "4h", datetime(2026, 11, 12, 12, tzinfo=UTC), None, now, "UTC")
    assert needs_fetch("crypto", "4h", datetime(2026, 11, 12, 8, tzinfo=UTC), None, now, "UTC")
    assert not needs_fetch("equity", "1d", datetime(2026, 11, 11, tzinfo=UTC), None, now, tz)
    assert needs_fetch("equity", "1d", datetime(2026, 11, 10, tzinfo=UTC), None, now, tz)
    # behind but asked recently (e.g. a market holiday): do not burn quota asking again
    assert not needs_fetch("equity", "1d", datetime(2026, 11, 10, tzinfo=UTC), now - timedelta(hours=1), now, tz)
    assert not needs_fetch("equity", "4h", datetime(2026, 11, 13, 18, 30, tzinfo=UTC), None, SATURDAY, tz)


def test_outcomes_are_recorded_once_enough_later_candles_exist(db):
    closes = uptrend_closes(60)
    frame = frame_from_closes(closes, start="2026-09-01")
    now = frame.index[-1].to_pydatetime() + timedelta(days=1, hours=1)
    with db.session_scope() as session:
        exchange = Exchange(code="CRYPTO", name="Crypto", region="CRYPTO", timezone="UTC")
        session.add(exchange)
        session.flush()
        instrument = Instrument(symbol="BTC", name="Bitcoin", asset_class="crypto", region="CRYPTO",
                                exchange_id=exchange.id, primary_provider="binance")
        run = ScanRun(scan_type="manual", slot_key="x", scope="all", status="success", started_at=now)
        session.add_all([instrument, run])
        session.flush()
        for ts, row in frame.iterrows():
            session.add(MarketCandle(
                instrument_id=instrument.id, provider="binance", timeframe="1d", open_time=ts.to_pydatetime(),
                open=row.open, high=row.high, low=row.low, close=row.close, volume=row.volume,
                is_complete=True, retrieved_at=now))
        signal = frame.index[-13]  # twelve completed candles follow the signal
        session.add(ScanCandidate(
            scan_run_id=run.id, instrument_id=instrument.id, classification="PREPARING",
            price=float(frame["close"].iloc[-13]), signal_candle_time=signal.to_pydatetime(),
            is_first_for_candle=True, created_at=now))
    with db.session_scope() as session:
        assert update_outcomes(session, now) == 4
        horizons = sorted(h for (h,) in session.execute(select(CandidateOutcome.horizon_days)))
        assert horizons == [1, 3, 5, 10]  # the 20-day result does not exist yet, so none is written
        one_day = session.scalars(select(CandidateOutcome).where(CandidateOutcome.horizon_days == 1)).one()
        expected = (frame["close"].iloc[-12] / frame["close"].iloc[-13] - 1) * 100
        assert one_day.return_pct == pytest.approx(expected, abs=1e-3)
        assert one_day.outcome == "NO_PLAN" and one_day.mfe_pct >= one_day.return_pct >= one_day.mae_pct
        assert update_outcomes(session, now) == 0  # idempotent
