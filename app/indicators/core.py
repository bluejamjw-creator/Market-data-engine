"""Technical indicators as pure pandas functions.

Input: a DataFrame of *completed* candles indexed by open time (ascending)
with columns open, high, low, close, volume. Every indicator returns NaN
until it has enough bars - a value is never produced from too little data.
"""
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

SWING_K = 3  # bars on each side that define a swing point


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def _wilder(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing: seeded with the simple average of the first `period` values."""
    values = series.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    if len(values) >= period and np.isfinite(values[:period]).all():
        out[period - 1] = values[:period].mean()
        for i in range(period, len(values)):
            if not np.isfinite(values[i]):
                break  # a hole in the data: stop rather than smooth across it
            out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return pd.Series(out, index=series.index)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = _wilder(gain.iloc[1:], period).reindex(close.index)
    avg_loss = _wilder(loss.iloc[1:], period).reindex(close.index)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    # no losses in the window -> RSI is 100 by definition
    out = out.where(~((avg_loss == 0.0) & avg_gain.notna()), 100.0)
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    )
    tr = ranges.max(axis=1)
    tr.iloc[0] = np.nan  # needs a previous close
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = true_range(df)
    return _wilder(tr.iloc[1:], period).reindex(df.index)


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr_s = _wilder(true_range(df).iloc[1:], period).reindex(df.index)
    plus_di = 100.0 * _wilder(plus_dm.iloc[1:], period).reindex(df.index) / tr_s
    minus_di = 100.0 * _wilder(minus_dm.iloc[1:], period).reindex(df.index) / tr_s
    total = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / total
    adx_line = _wilder(dx.dropna(), period).reindex(df.index)
    return pd.DataFrame({"adx14": adx_line, "plus_di": plus_di, "minus_di": minus_di})


def bollinger(close: pd.Series, period: int = 20, width: float = 2.0) -> pd.DataFrame:
    middle = close.rolling(period, min_periods=period).mean()
    std = close.rolling(period, min_periods=period).std(ddof=0)
    upper = middle + width * std
    lower = middle - width * std
    return pd.DataFrame(
        {"bb_upper": upper, "bb_middle": middle, "bb_lower": lower, "bb_width": (upper - lower) / middle}
    )


def volume_stats(volume: pd.Series, period: int = 20) -> pd.DataFrame:
    """Average volume of the previous `period` bars and today's multiple of it."""
    avg = volume.rolling(period, min_periods=period).mean().shift(1)
    return pd.DataFrame({"avg_volume20": avg, "rel_volume": volume / avg.replace(0.0, np.nan)})


def swing_points(df: pd.DataFrame, k: int = SWING_K) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """Confirmed swing highs and lows as (position, price).

    A swing needs `k` later bars to confirm it, so the most recent `k` bars
    can never be swings - this avoids using information from the future.
    """
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    swing_highs: List[Tuple[int, float]] = []
    swing_lows: List[Tuple[int, float]] = []
    for i in range(k, len(df) - k):
        window_h = highs[i - k:i + k + 1]
        window_l = lows[i - k:i + k + 1]
        # on equal highs/lows (e.g. a double top) the first bar is the swing
        if int(window_h.argmax()) == k:
            swing_highs.append((i, float(highs[i])))
        if int(window_l.argmin()) == k:
            swing_lows.append((i, float(lows[i])))
    return swing_highs, swing_lows


def support_resistance(
    close: float,
    swing_highs: List[Tuple[int, float]],
    swing_lows: List[Tuple[int, float]],
    last_pos: int,
    lookback: int = 120,
) -> Tuple[Optional[float], Optional[float]]:
    """Nearest confirmed swing low below price and swing high above it."""
    floor = last_pos - lookback
    below = [p for i, p in swing_lows if i >= floor and p < close]
    above = [p for i, p in swing_highs if i >= floor and p > close]
    return (max(below) if below else None, min(above) if above else None)


def trend_strength(row: pd.Series, ema20_prev: Optional[float]) -> Optional[float]:
    """0-100 composite of moving-average alignment, slope and ADX. None if unknown."""
    close, e20, e50, e200 = row.get("close"), row.get("ema20"), row.get("ema50"), row.get("ema200")
    if pd.isna(e20) or pd.isna(e50):
        return None
    points, maximum = 0.0, 0.0
    maximum += 15
    points += 15 if close > e20 else 0
    maximum += 20
    points += 20 if e20 > e50 else 0
    if not pd.isna(e200):
        maximum += 20
        points += 20 if e50 > e200 else 0
    if ema20_prev is not None and not pd.isna(ema20_prev):
        maximum += 15
        points += 15 if e20 > ema20_prev else 0
    adx_value, plus_di, minus_di = row.get("adx14"), row.get("plus_di"), row.get("minus_di")
    if not pd.isna(adx_value) and not pd.isna(plus_di) and not pd.isna(minus_di):
        maximum += 30
        if plus_di > minus_di:
            points += 30 * min(float(adx_value), 40.0) / 40.0
    return round(100.0 * points / maximum, 1) if maximum else None


def relative_strength(close: pd.Series, benchmark_close: pd.Series, bars: int) -> pd.Series:
    """Instrument return minus benchmark return over `bars`, in percentage points."""
    bench = benchmark_close.reindex(close.index, method="ffill", limit=3)
    own = close / close.shift(bars) - 1.0
    ref = bench / bench.shift(bars) - 1.0
    return (own - ref) * 100.0


def compute_indicators(df: pd.DataFrame, benchmark_close: Optional[pd.Series] = None) -> pd.DataFrame:
    """All per-bar indicators for `df`. Columns are NaN where data is insufficient."""
    out = pd.DataFrame(index=df.index)
    close = df["close"]
    out["close"] = close
    for span in (9, 20, 50, 200):
        out[f"ema{span}"] = ema(close, span)
    out["rsi14"] = rsi(close)
    out = out.join(macd(close))
    out["atr14"] = atr(df)
    out = out.join(adx(df))
    out = out.join(bollinger(close))
    if df["volume"].notna().any():
        out = out.join(volume_stats(df["volume"]))
    else:
        out["avg_volume20"] = np.nan
        out["rel_volume"] = np.nan
    flags_h = pd.Series(np.nan, index=df.index)
    flags_l = pd.Series(np.nan, index=df.index)
    swing_highs, swing_lows = swing_points(df)
    for pos, price in swing_highs:
        flags_h.iloc[pos] = price
    for pos, price in swing_lows:
        flags_l.iloc[pos] = price
    # a swing is only known SWING_K bars after it prints
    out["last_swing_high"] = flags_h.shift(SWING_K).ffill()
    out["last_swing_low"] = flags_l.shift(SWING_K).ffill()
    if benchmark_close is not None and len(benchmark_close) > 0:
        out["rs_20"] = relative_strength(close, benchmark_close, 20)
        out["rs_60"] = relative_strength(close, benchmark_close, 60)
    else:
        out["rs_20"] = np.nan
        out["rs_60"] = np.nan
    return out
