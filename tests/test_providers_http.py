"""Provider adapters over a mocked HTTP transport: retries, limits, fallbacks, secrecy."""
from datetime import datetime, timezone

import pytest

httpx = pytest.importorskip("httpx")

from app.providers.alpha_vantage import AlphaVantageProvider  # noqa: E402
from app.providers.binance import BinanceProvider  # noqa: E402
from app.providers.coinmarketcap import CoinMarketCapProvider  # noqa: E402
from app.providers.parsers import ProviderError  # noqa: E402
from app.providers.twelve_data import TwelveDataProvider  # noqa: E402
from tests.mock_market import FAKE_KEYS, MockMarket  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 11, 12, 18, 0, tzinfo=UTC)


def no_sleep(_seconds):
    return None


def test_twelve_data_fetches_candles_with_header_auth_only():
    market = MockMarket(NOW)
    provider = TwelveDataProvider("td-test-key-0001", transport=market.transport, sleep=no_sleep)
    candles = provider.fetch_candles("AAPL", "1d")
    assert len(candles) == 260 and candles[0].open_time < candles[-1].open_time
    request = market.requests[-1]
    assert "td-test-key-0001" not in str(request.url)  # key travels in a header, not the URL
    assert request.url.params["timezone"] == "UTC" and request.url.params["interval"] == "1day"
    record = provider.records[-1]
    assert record.success and record.rows_returned == 260 and record.attempts == 1


def test_twelve_data_reads_plan_limits():
    market = MockMarket(NOW)
    provider = TwelveDataProvider("td-test-key-0001", transport=market.transport, sleep=no_sleep)
    usage = provider.refresh_usage()
    assert usage["plan_limit"] == 8
    assert provider.plan_limit_per_minute == 8 and provider.plan_daily_limit == 800
    assert provider.remaining_daily_credits() == 800 - 12 - 1


def test_plan_restricted_symbol_is_unsupported_and_not_retried():
    market = MockMarket(NOW)
    provider = TwelveDataProvider("td-test-key-0001", transport=market.transport, sleep=no_sleep)
    with pytest.raises(ProviderError) as info:
        provider.fetch_candles("SHEL", "1d", mic_code="XLON")
    assert info.value.kind == "unsupported"
    assert market.calls["td:SHEL:1day"] == 1  # one request, no retries
    assert provider.available  # an unsupported symbol does not take the provider down


def test_transient_server_errors_are_retried_with_backoff():
    attempts, delays = [], []

    def handler(request):
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503, text="upstream unavailable")
        return httpx.Response(200, json={"values": [
            {"datetime": "2026-11-11", "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "10"}]})

    provider = TwelveDataProvider("td-test-key-0001", transport=httpx.MockTransport(handler), sleep=delays.append)
    provider.set_rate_limit(0)
    assert len(provider.fetch_candles("AAPL", "1d")) == 1
    assert len(attempts) == 3 and len(delays) == 2 and delays[1] > delays[0]  # exponential backoff
    assert provider.records[-1].attempts == 3 and provider.records[-1].success


def test_timeouts_give_up_after_the_retry_limit_and_are_recorded():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    provider = TwelveDataProvider("td-test-key-0001", transport=httpx.MockTransport(handler), sleep=no_sleep)
    provider.set_rate_limit(0)
    with pytest.raises(ProviderError) as info:
        provider.fetch_candles("AAPL", "1d")
    assert info.value.kind == "timeout"
    record = provider.records[-1]
    assert not record.success and record.attempts == 3 and record.error_kind == "timeout"


def test_per_minute_rate_limit_waits_then_retries_once():
    calls, delays = [], []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"code": 429, "status": "error",
                                             "message": "You have run out of API credits for the current minute."})
        return httpx.Response(200, json={"values": [
            {"datetime": "2026-11-11", "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "10"}]})

    provider = TwelveDataProvider("td-test-key-0001", transport=httpx.MockTransport(handler), sleep=delays.append)
    provider.set_rate_limit(0)
    assert len(provider.fetch_candles("AAPL", "1d")) == 1
    assert len(calls) == 2 and 61 in delays


def test_daily_quota_exhaustion_disables_the_provider_for_the_run():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, json={"code": 429, "status": "error",
                                         "message": "You have run out of API credits for the day."})

    provider = TwelveDataProvider("td-test-key-0001", transport=httpx.MockTransport(handler), sleep=no_sleep)
    provider.set_rate_limit(0)
    with pytest.raises(ProviderError):
        provider.fetch_candles("AAPL", "1d")
    assert not provider.available
    with pytest.raises(ProviderError):
        provider.fetch_candles("MSFT", "1d")
    assert len(calls) == 1  # no further quota is spent once the limit is hit


def test_missing_credentials_make_no_requests():
    market = MockMarket(NOW)
    for provider in (
        TwelveDataProvider(None, transport=market.transport),
        AlphaVantageProvider(None, transport=market.transport),
        CoinMarketCapProvider(None, transport=market.transport),
    ):
        assert not provider.available and "missing_credentials" in provider.disabled_reason
    with pytest.raises(ProviderError):
        TwelveDataProvider(None, transport=market.transport).fetch_candles("AAPL", "1d")
    assert not market.requests


def test_binance_falls_back_to_the_next_host_when_one_is_region_blocked():
    market = MockMarket(NOW, binance_blocked_hosts={"data-api.binance.vision"})
    provider = BinanceProvider(transport=market.transport, sleep=no_sleep)
    candles = provider.fetch_candles("BTCUSDT", "4h")
    assert len(candles) == 300 and provider.available
    assert market.calls["binance_host:data-api.binance.vision"] == 1
    assert market.calls["binance_host:data.binance.com"] == 1
    provider.fetch_candles("ETHUSDT", "4h")
    assert market.calls["binance_host:data-api.binance.vision"] == 1  # the blocked host is not tried again


def test_binance_reports_blocked_when_every_host_refuses():
    market = MockMarket(NOW, binance_blocked_hosts={
        "data-api.binance.vision", "data.binance.com", "api.binance.com"})
    provider = BinanceProvider(transport=market.transport, sleep=no_sleep)
    with pytest.raises(ProviderError) as info:
        provider.fetch_candles("BTCUSDT", "1d")
    assert info.value.kind == "blocked" and not provider.available


def test_alpha_vantage_and_coinmarketcap_parse_and_never_record_keys():
    market = MockMarket(NOW)
    av = AlphaVantageProvider("av-test-key-0002", transport=market.transport, sleep=no_sleep)
    cmc = CoinMarketCapProvider("cmc-test-key-0003", transport=market.transport, sleep=no_sleep)
    assert len(av.fetch_daily("AAPL")) == 100
    quotes = cmc.fetch_quotes(["BTC", "ETH"])
    assert {q.symbol for q in quotes} == {"BTC", "ETH"} and quotes[0].market_cap_rank == 1

    def failing(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    broken = AlphaVantageProvider("av-test-key-0002", transport=httpx.MockTransport(failing), sleep=no_sleep)
    with pytest.raises(ProviderError):
        broken.fetch_daily("AAPL")
    for record in av.records + cmc.records + broken.records:
        text = f"{record.endpoint} {record.symbol} {record.error_message}"
        assert not any(key in text for key in FAKE_KEYS)
