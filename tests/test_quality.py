"""Invalid market data, duplicates, gaps and stale data."""
from datetime import datetime, timedelta, timezone

import numpy as np

from app.providers.parsers import Candle
from app.services.quality import assess_series, candle_problem, overall_status, split_valid_candles
from app.timeutil import candle_is_complete
from tests.helpers import frame_from_closes, uptrend_closes

UTC = timezone.utc
T0 = datetime(2026, 11, 10, tzinfo=UTC)


def candle(day=0, o=10.0, h=11.0, l=9.0, c=10.5, v=1000.0):
    return Candle(T0 + timedelta(days=day), o, h, l, c, v)


def test_invalid_ohlc_and_negative_volume_are_detected():
    assert candle_problem(candle()) == ""
    assert "high below" in candle_problem(candle(h=10.2, c=10.5))
    assert "low above" in candle_problem(candle(l=10.2, o=10.0))
    assert "non-positive" in candle_problem(candle(c=0.0))
    assert "negative volume" in candle_problem(candle(v=-1.0))
    assert candle_problem(candle(v=None)) == ""  # unknown volume is allowed, and stays unknown


def test_invalid_and_duplicate_candles_are_dropped_and_reported():
    candles = [candle(0), candle(0), candle(1, h=9.0), candle(2, v=-5.0), candle(3)]
    valid, issues = split_valid_candles(candles)
    assert [c.open_time.day for c in valid] == [10, 13]
    kinds = {i.check_type: i.status for i in issues}
    assert kinds == {"duplicate_candles": "WARN", "invalid_ohlc": "FAIL", "negative_volume": "FAIL"}


def test_clean_series_passes_and_short_series_fails():
    frame = frame_from_closes(uptrend_closes(200))
    now = frame.index[-1].to_pydatetime() + timedelta(days=1, hours=2)
    assert overall_status(assess_series(frame, "crypto", "1d", now)) == "PASS"
    short = assess_series(frame.tail(30), "crypto", "1d", now)
    assert any(i.check_type == "insufficient_history" and i.status == "FAIL" for i in short)
    assert overall_status(assess_series(None, "crypto", "1d", now)) == "FAIL"


def test_stale_and_missing_candles_are_flagged():
    frame = frame_from_closes(uptrend_closes(200))
    late = frame.index[-1].to_pydatetime() + timedelta(days=9)
    assert any(i.check_type == "stale_data" for i in assess_series(frame, "crypto", "1d", late))
    holed = frame.drop(frame.index[-10:-5])
    now = holed.index[-1].to_pydatetime() + timedelta(days=1, hours=1)
    gaps = [i for i in assess_series(holed, "crypto", "1d", now) if i.check_type == "missing_candles"]
    assert gaps and gaps[0].status == "FAIL" and "5 missing" in gaps[0].detail


def test_missing_volume_is_a_warning_not_a_zero():
    frame = frame_from_closes(uptrend_closes(200))
    frame["volume"] = np.nan
    now = frame.index[-1].to_pydatetime() + timedelta(days=1, hours=1)
    issues = assess_series(frame, "crypto", "1d", now)
    assert any(i.check_type == "missing_volume" and i.status == "WARN" for i in issues)
    assert frame["volume"].isna().all()


def test_incomplete_candles_are_recognised():
    now = datetime(2026, 11, 12, 18, 0, tzinfo=UTC)
    assert candle_is_complete("crypto", "4h", datetime(2026, 11, 12, 12, tzinfo=UTC), now, "UTC")
    assert not candle_is_complete("crypto", "4h", datetime(2026, 11, 12, 16, tzinfo=UTC), now, "UTC")
    assert not candle_is_complete("crypto", "1d", datetime(2026, 11, 12, tzinfo=UTC), now, "UTC")
    # a US daily candle is only complete once New York's date has moved on
    assert not candle_is_complete("equity", "1d", datetime(2026, 11, 12, tzinfo=UTC), now, "America/New_York")
    assert candle_is_complete("equity", "1d", datetime(2026, 11, 11, tzinfo=UTC), now, "America/New_York")
    early = datetime(2026, 11, 13, 2, 0, tzinfo=UTC)  # still 12 Nov in New York
    assert not candle_is_complete("equity", "1d", datetime(2026, 11, 12, tzinfo=UTC), early, "America/New_York")
