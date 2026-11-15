"""Time helpers: timeframes, candle completeness, business days."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
LONDON = ZoneInfo("Europe/London")

TIMEFRAMES = ("1d", "4h")
_DELTAS = {"1d": timedelta(days=1), "4h": timedelta(hours=4)}


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def timeframe_delta(timeframe: str) -> timedelta:
    return _DELTAS[timeframe]


def is_weekend_london(now: datetime) -> bool:
    return as_utc(now).astimezone(LONDON).weekday() >= 5


def previous_business_day(day: date) -> date:
    """Most recent Mon-Fri strictly before `day` (public holidays not modelled)."""
    current = day - timedelta(days=1)
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current


def candle_is_complete(
    asset_class: str, timeframe: str, open_time: datetime, now: datetime, exchange_tz: str
) -> bool:
    """True when the candle can no longer change.

    Crypto trades continuously, so a candle is complete once its full span has
    elapsed. For exchange-traded instruments a daily candle is treated as
    complete only once the exchange's calendar date has moved on, which is
    deliberately conservative (it never marks a still-trading day complete).
    """
    open_time = as_utc(open_time)
    now = as_utc(now)
    if asset_class == "crypto":
        return open_time + timeframe_delta(timeframe) <= now
    if timeframe == "1d":
        local_today = now.astimezone(ZoneInfo(exchange_tz)).date()
        return open_time.date() < local_today
    return open_time + timeframe_delta(timeframe) <= now
