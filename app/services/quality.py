"""Market-data validation.

`split_valid_candles` screens provider candles before they are stored.
`assess_series` checks a stored series before it is analysed. Nothing here
repairs data: bad rows are rejected and gaps are reported, never filled.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Sequence, Tuple

import pandas as pd

from app.providers.parsers import Candle
from app.timeutil import as_utc, timeframe_delta

MIN_BARS_FOR_ANALYSIS = 60


@dataclass(frozen=True)
class Issue:
    check_type: str
    status: str  # PASS | WARN | FAIL
    detail: str


def candle_problem(candle: Candle) -> str:
    """Return '' for a valid candle, otherwise the reason it is invalid."""
    values = (candle.open, candle.high, candle.low, candle.close)
    if any(v is None or v != v or v <= 0 for v in values):
        return "non-positive or missing price"
    tolerance = 1e-9 * max(values)
    if candle.high + tolerance < max(candle.open, candle.close, candle.low):
        return "high below open/close/low"
    if candle.low - tolerance > min(candle.open, candle.close, candle.high):
        return "low above open/close/high"
    if candle.volume is not None and candle.volume < 0:
        return "negative volume"
    return ""


def split_valid_candles(candles: Sequence[Candle]) -> Tuple[List[Candle], List[Issue]]:
    """Drop invalid and duplicate candles; report what was dropped."""
    issues: List[Issue] = []
    valid: List[Candle] = []
    seen = set()
    invalid = {}
    duplicates = 0
    for candle in candles:
        if candle.open_time in seen:
            duplicates += 1
            continue
        seen.add(candle.open_time)
        problem = candle_problem(candle)
        if problem:
            invalid[problem] = invalid.get(problem, 0) + 1
            continue
        valid.append(candle)
    if duplicates:
        issues.append(Issue("duplicate_candles", "WARN", f"{duplicates} duplicate candle(s) from provider ignored"))
    for problem, count in invalid.items():
        check = "negative_volume" if problem == "negative volume" else "invalid_ohlc"
        issues.append(Issue(check, "FAIL", f"{count} candle(s) rejected: {problem}"))
    return valid, issues


def _stale_limit(asset_class: str, timeframe: str) -> timedelta:
    if asset_class == "crypto":
        return timedelta(days=2) if timeframe == "1d" else timedelta(hours=9)
    # exchanges close for weekends and holidays (up to a 4-day gap is normal)
    return timedelta(days=6)


def assess_series(df: pd.DataFrame, asset_class: str, timeframe: str, now: datetime) -> List[Issue]:
    """Quality issues for a stored series of completed candles."""
    issues: List[Issue] = []
    if df is None or len(df) == 0:
        return [Issue("no_data", "FAIL", "no completed candles stored")]
    if len(df) < MIN_BARS_FOR_ANALYSIS:
        issues.append(Issue("insufficient_history", "FAIL",
                            f"{len(df)} completed candles; {MIN_BARS_FOR_ANALYSIS} needed"))
    if df.index.has_duplicates:
        issues.append(Issue("duplicate_candles", "FAIL", "duplicate open times in stored series"))

    recent = df.tail(120)
    bad = recent[
        (recent["high"] < recent[["open", "close", "low"]].max(axis=1) - 1e-9 * recent["high"].abs())
        | (recent["low"] > recent[["open", "close", "high"]].min(axis=1) + 1e-9 * recent["high"].abs())
        | (recent[["open", "high", "low", "close"]] <= 0).any(axis=1)
    ]
    if len(bad):
        issues.append(Issue("invalid_ohlc", "FAIL", f"{len(bad)} invalid candle(s) in the last 120"))
    if (recent["volume"].dropna() < 0).any():
        issues.append(Issue("negative_volume", "FAIL", "negative volume in the last 120 candles"))
    missing_volume = int(recent["volume"].isna().sum())
    if missing_volume:
        issues.append(Issue("missing_volume", "WARN",
                            f"volume not reported for {missing_volume} of the last {len(recent)} candles"))

    last_open = as_utc(df.index[-1].to_pydatetime())
    age = as_utc(now) - (last_open + timeframe_delta(timeframe))
    limit = _stale_limit(asset_class, timeframe)
    if age > limit:
        issues.append(Issue("stale_data", "FAIL",
                            f"latest completed candle {last_open:%Y-%m-%d %H:%M} UTC is older than {limit}"))

    gaps = pd.Series(recent.index).diff().dropna()
    if asset_class == "crypto":
        missing = int(((gaps / timeframe_delta(timeframe)).round() - 1).clip(lower=0).sum())
        if missing:
            issues.append(Issue("missing_candles", "WARN" if missing <= 2 else "FAIL",
                                f"{missing} missing candle(s) in the last {len(recent)}"))
    elif timeframe == "1d":
        long_gaps = int((gaps > timedelta(days=5)).sum())
        if long_gaps:
            issues.append(Issue("missing_candles", "WARN",
                                f"{long_gaps} gap(s) longer than 5 days in the last {len(recent)} daily candles"))
    else:
        issues.append(Issue("missing_candles", "PASS",
                            "intraday gaps not checked: no exchange trading calendar available"))
    return issues


def overall_status(issues: Sequence[Issue]) -> str:
    statuses = {issue.status for issue in issues}
    if "FAIL" in statuses:
        return "FAIL"
    if "WARN" in statuses:
        return "WARN"
    return "PASS"
