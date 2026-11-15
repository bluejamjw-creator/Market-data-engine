"""Indicator calculations, including refusal to compute from too little data."""
import numpy as np
import pandas as pd
import pytest

from app.indicators.core import (
    atr, bollinger, compute_indicators, ema, macd, relative_strength, rsi, support_resistance, swing_points,
)
from tests.helpers import benchmark_closes, frame_from_closes, uptrend_closes


def test_rsi_matches_wilder_reference_values():
    close = pd.Series([44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
                       45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64])
    values = rsi(close).round(2).tolist()
    assert np.isnan(values[13])
    assert values[14:] == [70.46, 66.25, 66.48, 69.35, 66.29, 57.92]


def test_ema_and_macd_need_enough_bars():
    close = pd.Series(np.arange(1.0, 61.0))
    e20 = ema(close, 20)
    assert e20.iloc[:19].isna().all() and not np.isnan(e20.iloc[19])
    assert ema(close, 200).isna().all()
    lines = macd(close)
    assert lines["macd"].iloc[:25].isna().all() and lines["macd_signal"].iloc[:33].isna().all()
    assert lines["macd_hist"].iloc[-1] == pytest.approx(lines["macd"].iloc[-1] - lines["macd_signal"].iloc[-1])


def test_atr_of_constant_range_series():
    index = pd.date_range("2026-01-01", periods=40, freq="D", tz="UTC")
    frame = pd.DataFrame({"open": 100.0, "high": 102.0, "low": 98.0, "close": 100.0, "volume": 1.0}, index=index)
    values = atr(frame)
    assert values.iloc[:14].isna().all()
    assert values.iloc[-1] == pytest.approx(4.0)


def test_bollinger_bands_are_symmetric_around_the_average():
    close = pd.Series(np.linspace(100, 120, 40))
    bands = bollinger(close).iloc[-1]
    assert bands["bb_middle"] == pytest.approx(close.iloc[-20:].mean())
    assert bands["bb_upper"] - bands["bb_middle"] == pytest.approx(bands["bb_middle"] - bands["bb_lower"])
    assert bollinger(close)["bb_upper"].iloc[:19].isna().all()


def test_swing_points_are_only_confirmed_after_k_bars():
    closes = [10, 11, 12, 13, 14, 13, 12, 11, 10, 11, 12, 13, 14, 15, 16]
    frame = frame_from_closes(closes, wick=0.0)
    highs, lows = swing_points(frame, k=3)
    assert [pos for pos, _ in highs] == [4]
    assert [pos for pos, _ in lows] == [8]
    # the last three bars can never be swings: their confirmation is in the future
    assert all(pos <= len(frame) - 4 for pos, _ in highs + lows)
    assert support_resistance(12.5, highs, lows, len(frame) - 1) == (10.0, 14.0)


def test_relative_strength_against_benchmark():
    index = pd.date_range("2026-01-01", periods=30, freq="D", tz="UTC")
    own = pd.Series(np.linspace(100, 120, 30), index=index)
    bench = pd.Series(np.linspace(100, 110, 30), index=index)
    value = relative_strength(own, bench, 20).iloc[-1]
    expected = ((own.iloc[-1] / own.iloc[-21] - 1) - (bench.iloc[-1] / bench.iloc[-21] - 1)) * 100
    assert value == pytest.approx(expected) and value > 0


def test_compute_indicators_full_and_insufficient_history():
    frame = frame_from_closes(uptrend_closes(240))
    bench = frame_from_closes(benchmark_closes(240))
    out = compute_indicators(frame, bench["close"])
    last = out.iloc[-1]
    for column in ("ema9", "ema20", "ema50", "ema200", "rsi14", "macd_hist", "atr14", "adx14",
                   "bb_width", "rel_volume", "last_swing_high", "last_swing_low", "rs_20", "rs_60"):
        assert not np.isnan(last[column]), column
    assert last["ema20"] > last["ema50"] > last["ema200"]  # it is an uptrend
    short = compute_indicators(frame.head(40))
    assert np.isnan(short["ema50"].iloc[-1]) and np.isnan(short["ema200"].iloc[-1])
    assert np.isnan(short["rs_20"].iloc[-1])  # no benchmark supplied -> unknown, not zero


def test_missing_volume_gives_unknown_volume_indicators():
    frame = frame_from_closes(uptrend_closes(120))
    frame["volume"] = np.nan
    out = compute_indicators(frame)
    assert out["avg_volume20"].isna().all() and out["rel_volume"].isna().all()
