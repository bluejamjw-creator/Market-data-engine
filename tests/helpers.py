"""Deterministic synthetic price series for tests. Never used by the application."""
import numpy as np
import pandas as pd


def frame_from_closes(closes, volume=None, index=None, start="2025-01-01", freq="D", wick=0.004):
    closes = np.asarray(closes, dtype=float)
    if index is None:
        index = pd.date_range(start, periods=len(closes), freq=freq, tz="UTC")
    opens = np.r_[closes[0], closes[:-1]]
    highs = np.maximum(opens, closes) * (1 + wick)
    lows = np.minimum(opens, closes) * (1 - wick)
    if volume is None:
        change = np.r_[0.0, np.diff(closes)]
        volume = np.where(change >= 0, 6_000_000.0, 3_500_000.0)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volume}, index=index
    )


def uptrend_closes(bars=240, start=100.0, drift=0.003, wobble=0.004, seed=7):
    rng = np.random.default_rng(seed)
    return start * np.cumprod(1 + drift + rng.normal(0, wobble, bars))


def base_under_pivot_closes(bars=240, seed=7):
    """A steady uptrend, a pivot high, then a tight base with higher lows just under it."""
    trend = uptrend_closes(bars, seed=seed)
    pivot = trend[-1] * 1.02
    base = pivot * np.array([
        1.000, 0.982, 0.962, 0.950, 0.958, 0.968, 0.962, 0.974, 0.982, 0.986, 0.991, 0.994, 0.992, 0.998,
    ])
    return np.r_[trend, base]


def downtrend_closes(bars=240, seed=3):
    rng = np.random.default_rng(seed)
    return 100.0 * np.cumprod(1 - 0.004 + rng.normal(0, 0.006, bars))


def benchmark_closes(bars, seed=11):
    rng = np.random.default_rng(seed)
    return 400.0 * np.cumprod(1 + 0.0012 + rng.normal(0, 0.003, bars))
