"""Settings, secret redaction and time helpers."""
import logging
from datetime import date, datetime, timezone

from app.config import get_settings
from app.logging_setup import RedactingFilter, redact
from app.timeutil import is_weekend_london, previous_business_day

UTC = timezone.utc


def test_secrets_are_redacted_from_text():
    text = ("GET https://www.alphavantage.co/query?function=X&apikey=av-test-key-0002 failed; "
            "db postgresql://user:hunter2pass@host:5432/railway; header X-CMC_PRO_API_KEY: cmc-test-key-0003")
    cleaned = redact(text)
    for secret in ("av-test-key-0002", "hunter2pass", "cmc-test-key-0003"):
        assert secret not in cleaned
    assert "***" in cleaned


def test_log_records_are_redacted():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "key=%s", ("td-test-key-0001",), None)
    assert RedactingFilter().filter(record)
    assert "td-test-key-0001" not in record.getMessage()


def test_timezone_is_europe_london_and_costs_are_per_market():
    settings = get_settings()
    assert settings.timezone == "Europe/London"
    assert settings.cost_pct("UK", "equity") > settings.cost_pct("UK", "etf")
    assert settings.cost_pct("CRYPTO", "crypto") == settings.cost_pct_crypto


def test_weekend_and_business_day_helpers():
    assert is_weekend_london(datetime(2026, 11, 14, 10, tzinfo=UTC))       # Saturday
    assert not is_weekend_london(datetime(2026, 11, 13, 23, 30, tzinfo=UTC))  # Friday night
    # 23:30 UTC on a summer Friday is already Saturday in London (BST)
    assert is_weekend_london(datetime(2026, 7, 10, 23, 30, tzinfo=UTC))
    assert previous_business_day(date(2026, 11, 16)) == date(2026, 11, 13)
