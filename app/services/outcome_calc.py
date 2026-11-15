"""Forward outcome of a candidate, measured only on candles after its signal.

No look-ahead: the function receives the daily candles that completed
*after* the candidate's signal candle, and uses exactly the first
`horizon` of them. If fewer exist, there is no result yet.
"""
from dataclasses import dataclass
from typing import Optional

import pandas as pd

HORIZONS = (1, 3, 5, 10, 20)


@dataclass(frozen=True)
class Outcome:
    horizon_days: int
    ref_price: float
    close_price: float
    return_pct: float
    mfe_pct: float
    mae_pct: float
    trigger_hit: Optional[bool]
    stop_hit: Optional[bool]
    target_hit: Optional[bool]
    outcome: str
    bars_to_outcome: Optional[int]


def evaluate_outcome(
    future: pd.DataFrame,
    horizon: int,
    ref_price: float,
    trigger: Optional[float] = None,
    stop: Optional[float] = None,
    target: Optional[float] = None,
) -> Optional[Outcome]:
    """Outcome after `horizon` trading days, or None if not enough candles yet.

    MFE/MAE are the best and worst excursions from the price at the scan.
    With a trade plan, the trigger must be reached before the stop or target
    can count. If a single daily bar spans both stop and target, the order
    is unknowable from daily data and the result is recorded as AMBIGUOUS.
    """
    if future is None or len(future) < horizon or ref_price <= 0:
        return None
    window = future.iloc[:horizon]
    highs, lows = window["high"].to_numpy(dtype=float), window["low"].to_numpy(dtype=float)
    close = float(window["close"].iloc[-1])
    mfe = (highs.max() / ref_price - 1.0) * 100.0
    mae = (lows.min() / ref_price - 1.0) * 100.0
    ret = (close / ref_price - 1.0) * 100.0

    trigger_hit = stop_hit = target_hit = None
    label, bars = "NO_PLAN", None
    if trigger is not None and stop is not None and target is not None:
        trigger_hit, stop_hit, target_hit = False, False, False
        label = "NOT_TRIGGERED"
        active = ref_price >= trigger  # already at/above the trigger when scanned
        if active:
            trigger_hit = True
            label = "OPEN"
        for i in range(len(window)):
            if not active:
                if lows[i] <= stop and highs[i] < trigger:
                    label, bars = "INVALIDATED_BEFORE_TRIGGER", i + 1
                    stop_hit = True
                    break
                if highs[i] >= trigger:
                    active, trigger_hit, label = True, True, "OPEN"
                else:
                    continue
            hit_stop, hit_target = lows[i] <= stop, highs[i] >= target
            if hit_stop and hit_target:
                stop_hit = target_hit = True
                label, bars = "AMBIGUOUS", i + 1
                break
            if hit_stop:
                stop_hit, label, bars = True, "STOPPED", i + 1
                break
            if hit_target:
                target_hit, label, bars = True, "TARGET", i + 1
                break
    return Outcome(
        horizon_days=horizon, ref_price=float(ref_price), close_price=close,
        return_pct=round(float(ret), 4), mfe_pct=round(float(mfe), 4), mae_pct=round(float(mae), 4),
        trigger_hit=trigger_hit, stop_hit=stop_hit, target_hit=target_hit,
        outcome=label, bars_to_outcome=bars,
    )
