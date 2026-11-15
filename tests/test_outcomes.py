"""Outcome tracking: MFE/MAE, trigger/stop/target, and no look-ahead."""
import pandas as pd
import pytest

from app.services.outcome_calc import HORIZONS, evaluate_outcome


def future(rows):
    index = pd.date_range("2026-11-02", periods=len(rows), freq="D", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=index)


BARS = future([
    (100, 102, 99, 101),
    (101, 104, 100, 103),   # trigger 103 reached here
    (103, 106, 102, 105),
    (105, 111, 104, 110),   # target 110 reached here
    (110, 112, 96, 97),     # later collapse must not change the 3-day result
])


def test_horizons_are_the_required_trading_day_windows():
    assert HORIZONS == (1, 3, 5, 10, 20)


def test_no_result_until_enough_candles_exist():
    assert evaluate_outcome(BARS, 10, 100.0) is None
    assert evaluate_outcome(BARS.iloc[:0], 1, 100.0) is None


def test_mfe_mae_and_return_use_only_the_horizon_window():
    result = evaluate_outcome(BARS, 3, 100.0)
    assert result.return_pct == pytest.approx(5.0)
    assert result.mfe_pct == pytest.approx(6.0)
    assert result.mae_pct == pytest.approx(-1.0)
    assert result.outcome == "NO_PLAN" and result.trigger_hit is None


def test_trigger_then_target():
    result = evaluate_outcome(BARS, 5, 100.0, trigger=103.0, stop=98.0, target=110.0)
    assert result.trigger_hit and result.target_hit and not result.stop_hit
    assert result.outcome == "TARGET" and result.bars_to_outcome == 4


def test_not_triggered_within_horizon():
    result = evaluate_outcome(BARS, 1, 100.0, trigger=103.0, stop=98.0, target=110.0)
    assert result.outcome == "NOT_TRIGGERED" and result.trigger_hit is False


def test_stop_after_trigger_and_ambiguous_bar():
    stopped = evaluate_outcome(BARS, 5, 100.0, trigger=103.0, stop=98.5, target=120.0)
    assert stopped.outcome == "STOPPED" and stopped.stop_hit and stopped.bars_to_outcome == 5
    assert stopped.trigger_hit and not stopped.target_hit
    both = future([(100, 115, 95, 100)])
    ambiguous = evaluate_outcome(both, 1, 100.0, trigger=100.0, stop=96.0, target=110.0)
    assert ambiguous.outcome == "AMBIGUOUS" and ambiguous.stop_hit and ambiguous.target_hit


def test_invalidated_before_trigger():
    bars = future([(100, 101, 95, 96), (96, 110, 95, 109)])
    result = evaluate_outcome(bars, 2, 100.0, trigger=103.0, stop=97.0, target=108.0)
    assert result.outcome == "INVALIDATED_BEFORE_TRIGGER" and result.trigger_hit is False
