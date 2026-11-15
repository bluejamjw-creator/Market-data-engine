"""Candidate scoring, READY eligibility (minimum 2:1 reward/risk) and missing data."""
import numpy as np
import pytest

from app.indicators.core import compute_indicators
from app.scanners.engine import (
    MIN_REWARD_RISK, NEAR_READY, READY, REJECTED, Context, evaluate,
)
from tests.helpers import (
    base_under_pivot_closes, benchmark_closes, downtrend_closes, frame_from_closes, uptrend_closes,
)


@pytest.fixture
def setup():
    closes = base_under_pivot_closes()
    daily = frame_from_closes(closes)
    bench = frame_from_closes(benchmark_closes(len(closes)))
    return {
        "daily": daily,
        "ind": compute_indicators(daily, bench["close"]),
        "h4": compute_indicators(frame_from_closes(uptrend_closes(120, seed=5), freq="4h")),
        "bench": compute_indicators(bench),
    }


def ctx(**overrides):
    values = dict(asset_class="equity", region="US", cost_pct=0.10, quality_status="PASS")
    values.update(overrides)
    return Context(**values)


def test_ready_candidate_has_a_complete_verifiable_plan(setup):
    result = evaluate(setup["daily"], setup["ind"], setup["h4"], setup["bench"], ctx())
    assert result.classification == READY
    assert result.setup_type == "BREAKOUT_PREP"
    assert not result.provisional and not result.do_not_chase
    plan = result.plan
    assert plan.valid and plan.reward_risk >= MIN_REWARD_RISK
    assert plan.stop < plan.entry_ref < plan.target
    # every level is a real price taken from the candles
    assert plan.trigger == pytest.approx(setup["daily"]["high"].iloc[-20:].max())
    assert plan.stop == pytest.approx(setup["daily"]["low"].iloc[-5:].min())
    assert plan.trigger_basis and plan.stop_basis and plan.target_basis
    assert result.invalidation


def test_score_is_transparent_and_adds_up(setup):
    result = evaluate(setup["daily"], setup["ind"], setup["h4"], setup["bench"], ctx())
    maxima = {"pattern": 25, "technical": 25, "rs_regime": 20, "liquidity_quality": 15, "reward_risk": 15}
    assert {k: c.full_max for k, c in result.components.items()} == maxima
    assert result.score_total == pytest.approx(sum(c.points for c in result.components.values()))
    assert result.score_max_verified == 100
    for component in result.components.values():
        assert 0 <= component.points <= component.maximum <= component.full_max
        assert component.notes  # each component explains itself
    assert len(result.reasons) >= 6


def test_reward_risk_below_two_to_one_can_never_be_ready(setup):
    # higher estimated costs shrink reward/risk below 2:1 on the same chart
    result = evaluate(setup["daily"], setup["ind"], setup["h4"], setup["bench"], ctx(cost_pct=0.6))
    assert result.plan.valid and result.plan.reward_risk < MIN_REWARD_RISK
    assert result.classification != READY
    assert any("reward/risk" in reason for reason in result.reasons)


def test_missing_four_hour_data_is_pending_not_zero(setup):
    full = evaluate(setup["daily"], setup["ind"], setup["h4"], setup["bench"], ctx())
    result = evaluate(setup["daily"], setup["ind"], None, setup["bench"], ctx())
    technical = result.components["technical"]
    assert technical.status == "PARTIAL" and technical.maximum == 22  # 3 points not counted
    assert any("PENDING" in note for note in technical.notes)
    assert result.provisional and result.classification == NEAR_READY
    # the unverified part is excluded from the denominator rather than scored as a failure
    assert result.score_max_verified == 97
    assert result.score_pct >= full.score_pct - 3


def test_missing_benchmark_is_unverified_and_blocks_ready(setup):
    ind = compute_indicators(setup["daily"])  # no benchmark series available
    result = evaluate(setup["daily"], ind, setup["h4"], None, ctx())
    rs = result.components["rs_regime"]
    assert rs.status == "UNVERIFIED" and rs.points == 0 and rs.maximum == 0
    assert result.provisional and result.classification != READY
    assert result.score_max_verified == 80


def test_missing_volume_is_unverified_and_blocks_ready(setup):
    daily = setup["daily"].copy()
    daily["volume"] = np.nan
    bench = frame_from_closes(benchmark_closes(len(daily)))
    result = evaluate(daily, compute_indicators(daily, bench["close"]), setup["h4"], setup["bench"],
                      ctx(quality_status="WARN", quality_notes=["missing_volume [WARN]"]))
    assert result.components["liquidity_quality"].status == "PARTIAL"
    assert result.components["pattern"].status == "PARTIAL"
    assert result.provisional and result.classification != READY


def test_data_quality_warning_blocks_ready(setup):
    result = evaluate(setup["daily"], setup["ind"], setup["h4"], setup["bench"],
                      ctx(quality_status="WARN", quality_notes=["missing_candles [WARN]: 1 gap"]))
    assert result.classification == NEAR_READY
    assert any("data quality is WARN" in reason for reason in result.reasons)


def test_data_quality_failure_and_short_history_are_rejected_without_a_plan(setup):
    failed = evaluate(setup["daily"], setup["ind"], setup["h4"], setup["bench"], ctx(quality_status="FAIL"))
    assert failed.classification == REJECTED and failed.plan is None and failed.score_total is None
    assert all(c.status == "UNVERIFIED" for c in failed.components.values())
    short = setup["daily"].tail(40)
    result = evaluate(short, compute_indicators(short), None, None, ctx())
    assert result.classification == REJECTED and "insufficient history" in result.reasons[0]


def test_downtrend_is_rejected():
    daily = frame_from_closes(downtrend_closes())
    result = evaluate(daily, compute_indicators(daily), None, None, ctx())
    assert result.classification == REJECTED and "downtrend" in result.reasons[0]


def test_extended_move_gets_do_not_chase_warning():
    closes = uptrend_closes(230)
    closes[-5:] = closes[-6] * np.array([1.06, 1.13, 1.21, 1.30, 1.42])
    daily = frame_from_closes(closes)
    result = evaluate(daily, compute_indicators(daily), None, None, ctx())
    assert result.do_not_chase and "ATR" in result.do_not_chase_reason
    assert result.classification != READY


def test_benchmark_instrument_is_not_scored_against_itself(setup):
    result = evaluate(setup["daily"], setup["ind"], setup["h4"], None, ctx(is_benchmark=True))
    rs = result.components["rs_regime"]
    assert rs.maximum == 8 and any("not applicable" in note for note in rs.notes)
