"""Provider response parsing, against mocked payloads (no network, no quota)."""
from datetime import datetime, timezone

import pytest

from app.providers.parsers import (
    ProviderError,
    parse_alpha_vantage_daily,
    parse_binance_klines,
    parse_coinmarketcap_quotes,
    parse_twelve_data_time_series,
)

UTC = timezone.utc


def test_twelve_data_daily_and_intraday_are_parsed_oldest_first():
    payload = {
        "meta": {"symbol": "AAPL", "interval": "1day"},
        "values": [
            {"datetime": "2026-11-11", "open": "228.1", "high": "230.0", "low": "227.5", "close": "229.4", "volume": "51000000"},
            {"datetime": "2026-11-10", "open": "226.0", "high": "228.9", "low": "225.2", "close": "228.0", "volume": "48000000"},
        ],
        "status": "ok",
    }
    candles = parse_twelve_data_time_series(payload)
    assert [c.open_time for c in candles] == [
        datetime(2026, 11, 10, tzinfo=UTC), datetime(2026, 11, 11, tzinfo=UTC)]
    assert candles[1].close == 229.4 and candles[1].volume == 51000000.0
    intraday = parse_twelve_data_time_series(
        {"values": [{"datetime": "2026-11-11 14:30:00", "open": "1", "high": "2", "low": "0.5", "close": "1.5"}]})
    assert intraday[0].open_time == datetime(2026, 11, 11, 14, 30, tzinfo=UTC)
    assert intraday[0].volume is None  # absent volume stays unknown, never zero


@pytest.mark.parametrize("payload,kind", [
    ({"code": 403, "message": "**symbol** SHEL is available starting with Grow plan", "status": "error"}, "unsupported"),
    ({"code": 404, "message": "**symbol** not found: XXXX", "status": "error"}, "not_found"),
    ({"code": 429, "message": "You have run out of API credits for the current minute.", "status": "error"}, "rate_limited"),
    ({"code": 429, "message": "You have run out of API credits for the day.", "status": "error"}, "quota_exhausted"),
    ({"code": 401, "message": "**apikey** parameter is incorrect", "status": "error"}, "invalid_credentials"),
])
def test_twelve_data_errors_are_classified(payload, kind):
    with pytest.raises(ProviderError) as info:
        parse_twelve_data_time_series(payload)
    assert info.value.kind == kind


def test_twelve_data_rejects_non_numeric_prices_instead_of_guessing():
    with pytest.raises(ProviderError) as info:
        parse_twelve_data_time_series(
            {"values": [{"datetime": "2026-11-11", "open": "", "high": "2", "low": "1", "close": "1.5"}]})
    assert info.value.kind == "parse_error"


def test_binance_klines():
    payload = [
        [1762905600000, "67000.1", "67500.0", "66800.0", "67250.5", "1234.5", 1762919999999, "8.3e7", 1000, "600", "4e7", "0"],
        [1762891200000, "66500.0", "67100.0", "66400.0", "67000.1", "987.6", 1762905599999, "6.5e7", 900, "500", "3e7", "0"],
    ]
    candles = parse_binance_klines(payload)
    assert candles[0].open_time < candles[1].open_time
    assert candles[1].open_time == datetime.fromtimestamp(1762905600, tz=UTC)
    assert candles[1].close == 67250.5 and candles[1].volume == 1234.5


def test_binance_region_block_and_bad_symbol():
    with pytest.raises(ProviderError) as blocked:
        parse_binance_klines({"code": 0, "msg": "Service unavailable from a restricted location according to 'b. Eligibility'"})
    assert blocked.value.kind == "blocked"
    with pytest.raises(ProviderError) as missing:
        parse_binance_klines({"code": -1121, "msg": "Invalid symbol."})
    assert missing.value.kind == "not_found"


def test_alpha_vantage_daily_and_limits():
    payload = {"Meta Data": {}, "Time Series (Daily)": {
        "2026-11-11": {"1. open": "228.1", "2. high": "230.0", "3. low": "227.5", "4. close": "229.4", "5. volume": "51000000"},
        "2026-11-10": {"1. open": "226.0", "2. high": "228.9", "3. low": "225.2", "4. close": "228.0", "5. volume": "48000000"},
    }}
    candles = parse_alpha_vantage_daily(payload)
    assert [c.close for c in candles] == [228.0, 229.4]
    with pytest.raises(ProviderError) as limited:
        parse_alpha_vantage_daily({"Information": "our standard API rate limit is 25 requests per day."})
    assert limited.value.kind == "quota_exhausted"
    with pytest.raises(ProviderError) as missing:
        parse_alpha_vantage_daily({"Error Message": "Invalid API call."})
    assert missing.value.kind == "not_found"


def test_coinmarketcap_quotes_v1_and_v2_shapes():
    usd = {"price": 67000.5, "volume_24h": 3.3e10, "market_cap": 1.34e12,
           "percent_change_24h": -2.1, "percent_change_7d": -11.7, "last_updated": "2026-11-14T09:00:00.000Z"}
    v1 = {"status": {"error_code": 0}, "data": {"BTC": {"symbol": "BTC", "cmc_rank": 1, "quote": {"USD": usd}}}}
    v2 = {"status": {"error_code": 0}, "data": {"BTC": [
        {"symbol": "BTC", "cmc_rank": 4000, "quote": {"USD": {"price": 0.01}}},
        {"symbol": "BTC", "cmc_rank": 1, "quote": {"USD": usd}},
    ]}}
    for payload in (v1, v2):
        quote = parse_coinmarketcap_quotes(payload)[0]
        assert quote.symbol == "BTC" and quote.price == 67000.5 and quote.market_cap_rank == 1
        assert quote.last_updated == datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
    with pytest.raises(ProviderError) as bad_key:
        parse_coinmarketcap_quotes({"status": {"error_code": 1001, "error_message": "This API Key is invalid."}})
    assert bad_key.value.kind == "invalid_credentials"
