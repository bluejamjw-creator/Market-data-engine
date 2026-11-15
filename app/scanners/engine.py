"""Opportunity detection, transparent scoring and classification.

Pure functions over completed candles and their indicators. Every price
level in a trade plan is an actual high, low or swing point taken from the
stored candles, and each is labelled with where it came from. When a level
cannot be derived, the plan is marked invalid and the candidate cannot be
READY - nothing is estimated to make a setup look complete.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from app.indicators.core import swing_points

READY, NEAR_READY, PREPARING, REJECTED = "READY", "NEAR-READY", "PREPARING", "REJECTED"
VERIFIED, PARTIAL, UNVERIFIED, PENDING = "VERIFIED", "PARTIAL", "UNVERIFIED", "PENDING"

MIN_BARS = 60
MIN_REWARD_RISK = 2.0


@dataclass
class Context:
    asset_class: str
    region: str
    cost_pct: float            # estimated round-trip cost, percent of price (an assumption)
    quality_status: str        # PASS | WARN | FAIL
    quality_notes: List[str] = field(default_factory=list)
    is_benchmark: bool = False
    value_scale: float = 1.0   # 0.01 for prices quoted in pence


@dataclass
class Component:
    name: str
    full_max: float
    points: float = 0.0
    maximum: float = 0.0       # portion of full_max that could be verified
    status: str = VERIFIED
    notes: List[str] = field(default_factory=list)

    def add(self, points: float, maximum: float, note: str) -> None:
        self.points += points
        self.maximum += maximum
        self.notes.append(f"{note} ({points:g}/{maximum:g})")

    def pending(self, maximum: float, note: str, status: str = PENDING) -> None:
        self.notes.append(f"{note} - {status}, not scored (0 of {maximum:g} counted)")
        if self.status == VERIFIED:
            self.status = PARTIAL

    def as_dict(self) -> dict:
        return {
            "points": round(self.points, 2),
            "max_verified": round(self.maximum, 2),
            "max": self.full_max,
            "status": self.status,
            "notes": self.notes,
        }


@dataclass
class Plan:
    trigger: float
    entry_low: float
    entry_high: float
    entry_ref: float
    stop: float
    target: Optional[float]
    trigger_basis: str
    stop_basis: str
    target_basis: str
    reward_risk: Optional[float] = None
    valid: bool = False
    problem: str = ""


@dataclass
class Evaluation:
    classification: str
    reasons: List[str]
    components: Dict[str, Component]
    score_total: Optional[float] = None
    score_max_verified: Optional[float] = None
    provisional: bool = False
    do_not_chase: bool = False
    do_not_chase_reason: Optional[str] = None
    setup_type: Optional[str] = None
    setup_description: Optional[str] = None
    plan: Optional[Plan] = None
    price: Optional[float] = None
    support: Optional[float] = None
    resistance: Optional[float] = None
    invalidation: Optional[str] = None

    @property
    def score_pct(self) -> Optional[float]:
        if not self.score_max_verified:
            return None
        return 100.0 * (self.score_total or 0.0) / self.score_max_verified


def fmt(value: Optional[float]) -> str:
    if value is None or value != value:
        return "n/a"
    magnitude = abs(value)
    if magnitude >= 100:
        return f"{value:,.2f}"
    if magnitude >= 1:
        return f"{value:.3f}"
    return f"{value:.6g}"


def _empty_components() -> Dict[str, Component]:
    return {
        "pattern": Component("Pattern quality", 25, status=UNVERIFIED),
        "technical": Component("Technical confirmation", 25, status=UNVERIFIED),
        "rs_regime": Component("Relative strength and market regime", 20, status=UNVERIFIED),
        "liquidity_quality": Component("Liquidity and data quality", 15, status=UNVERIFIED),
        "reward_risk": Component("Realistic reward/risk", 15, status=UNVERIFIED),
    }


def _reject(reason: str, price: Optional[float] = None, extra: Optional[List[str]] = None) -> Evaluation:
    return Evaluation(REJECTED, [reason] + (extra or []), _empty_components(), provisional=True, price=price)


class _Features:
    """Arrays and derived values shared by the pattern detectors."""

    def __init__(self, daily: pd.DataFrame, ind: pd.DataFrame):
        self.h = daily["high"].to_numpy(dtype=float)
        self.l = daily["low"].to_numpy(dtype=float)
        self.c = daily["close"].to_numpy(dtype=float)
        self.v = daily["volume"].to_numpy(dtype=float)
        self.n = len(daily)
        self.ind = ind
        last = ind.iloc[-1]
        self.close = float(self.c[-1])
        self.atr = float(last["atr14"])
        self.e20 = float(last["ema20"])
        self.e50 = float(last["ema50"])
        self.e200 = float(last["ema200"]) if not pd.isna(last["ema200"]) else None
        self.rsi = float(last["rsi14"]) if not pd.isna(last["rsi14"]) else None
        self.swing_highs, self.swing_lows = swing_points(daily)
        recent_lows = [p for i, p in self.swing_lows if i >= self.n - 60]
        self.ascending_lows = 0
        for a, b in zip(reversed(recent_lows[:-1]), reversed(recent_lows[1:])):
            if b > a:
                self.ascending_lows += 1
            else:
                break
        self.base_high = float(self.h[-15:].max())
        self.base_low = float(self.l[-15:].min())
        self.tightness = (self.base_high - self.base_low) / self.atr
        self.has_volume = bool(np.isfinite(self.v[-20:]).all())

    def next_resistance(self, above: float) -> Optional[float]:
        """Lowest confirmed swing high in the last 250 bars that sits above `above`."""
        levels = [p for i, p in self.swing_highs if i >= self.n - 250 and p > above]
        return min(levels) if levels else None


def _finish_plan(plan: Plan, f: _Features, cost_pct: float) -> Plan:
    if plan.target is None:
        plan.problem = "no verifiable target level above the entry"
        return plan
    if not (plan.stop < plan.entry_ref < plan.target):
        plan.problem = "levels are not ordered stop < entry < target"
        return plan
    if plan.entry_ref - plan.stop < 0.5 * f.atr:
        plan.problem = "stop is closer than half an ATR to the entry (inside normal noise)"
        return plan
    cost = plan.entry_ref * cost_pct / 100.0
    reward = plan.target - plan.entry_ref - cost
    risk = plan.entry_ref - plan.stop + cost
    if reward <= 0 or risk <= 0:
        plan.problem = "no reward left after estimated costs"
        return plan
    plan.reward_risk = round(reward / risk, 2)
    plan.valid = True
    return plan


def _target(f: _Features, level: float, measured: float, measured_basis: str) -> Tuple[float, str]:
    resistance = f.next_resistance(level + 0.5 * f.atr)
    if resistance is not None and resistance < measured:
        return resistance, f"next confirmed swing high above the trigger ({fmt(resistance)})"
    return measured, measured_basis


# --------------------------------------------------------------------- patterns

def _breakout_retest(f: _Features, cost_pct: float) -> Optional[dict]:
    for i in range(f.n - 2, max(f.n - 12, 31), -1):
        level = float(f.h[i - 30:i].max())
        if not (f.c[i] > level and f.c[i - 1] <= level):
            continue
        after_close, after_low = f.c[i + 1:], f.l[i + 1:]
        if len(after_close) == 0:
            return None
        held = after_close.min() >= level - 0.25 * f.atr
        retested = after_low.min() <= level + 0.75 * f.atr
        in_zone = level - 0.25 * f.atr <= f.close <= level + 1.0 * f.atr
        if not (held and retested and in_zone):
            return None
        stop = float(f.l[i:].min())
        base_low = float(f.l[max(0, i - 15):i].min())
        measured = level + (level - base_low)
        target, basis = _target(f, max(level, f.close), measured,
                                f"measured move: breakout level plus the height of the prior base ({fmt(base_low)} to {fmt(level)})")
        plan = Plan(
            trigger=level, entry_low=level, entry_high=level + 0.5 * f.atr, entry_ref=max(level, f.close),
            stop=stop, target=target,
            trigger_basis=f"breakout level: highest high of the 30 bars before the breakout ({fmt(level)})",
            stop_basis=f"lowest low since the breakout bar ({fmt(stop)})",
            target_basis=basis,
        )
        return dict(
            type="BREAKOUT_RETEST", plan=_finish_plan(plan, f, cost_pct),
            description=(f"Closed above {fmt(level)} {f.n - 1 - i} bar(s) ago, has pulled back to retest "
                         f"that level and is holding it."),
        )
    return None


def _breakout_prep(f: _Features, cost_pct: float) -> Optional[dict]:
    window = f.h[-20:]
    pivot = float(window.max())
    pivot_age = len(window) - 1 - int(window.argmax())
    if pivot_age < 3 or f.close > pivot:
        return None
    if not (f.close > f.e50 and f.e20 >= f.e50 * 0.995):
        return None
    if f.tightness > 6.5 or (pivot - f.close) / f.atr > 2.5:
        return None
    # Invalidation is the most recent low inside the base (the "handle"), not the
    # bottom of the whole base: a break of the latest higher low ends the setup.
    stop = float(f.l[-5:].min())
    measured = pivot + (pivot - f.base_low)
    target, basis = _target(f, pivot, measured,
                            f"measured move: pivot plus the height of the 15-bar base ({fmt(f.base_low)} to {fmt(pivot)})")
    plan = Plan(
        trigger=pivot, entry_low=pivot, entry_high=pivot + 0.3 * f.atr, entry_ref=pivot,
        stop=stop, target=target,
        trigger_basis=f"pivot: highest high of the last 20 bars ({fmt(pivot)})",
        stop_basis=f"lowest low of the last 5 completed bars ({fmt(stop)})",
        target_basis=basis,
    )
    return dict(
        type="BREAKOUT_PREP", plan=_finish_plan(plan, f, cost_pct),
        description=(f"Consolidating {f.tightness:.1f} ATR wide below the {fmt(pivot)} pivot; "
                     f"price is {(pivot - f.close) / f.atr:.1f} ATR under it."),
    )


def _ma_pullback(f: _Features, cost_pct: float) -> Optional[dict]:
    e50_series = f.ind["ema50"].to_numpy(dtype=float)
    if f.n < 12 or not np.isfinite(e50_series[-11]):
        return None
    if not (f.e20 > f.e50 and f.close > f.e50 and e50_series[-1] > e50_series[-11]):
        return None
    window = f.h[-20:]
    high = float(window.max())
    age = len(window) - 1 - int(window.argmax())
    if not 3 <= age <= 15:
        return None
    pullback_low = float(f.l[-age:].min())
    depth = (high - pullback_low) / f.atr
    touched = f.l[-3:].min() <= f.e20 + 0.3 * f.atr
    if not (1.0 <= depth <= 4.0 and touched and f.close >= f.e20 - 0.5 * f.atr):
        return None
    trigger = float(f.h[-1])
    plan = Plan(
        trigger=trigger, entry_low=trigger, entry_high=trigger + 0.3 * f.atr, entry_ref=trigger,
        stop=pullback_low, target=high if high > trigger else None,
        trigger_basis=f"high of the latest completed bar ({fmt(trigger)})",
        stop_basis=f"low of the pullback ({fmt(pullback_low)})",
        target_basis=f"prior high before the pullback ({fmt(high)})",
    )
    return dict(
        type="MA_PULLBACK", plan=_finish_plan(plan, f, cost_pct),
        description=(f"Uptrend pulled back {depth:.1f} ATR from {fmt(high)} to its 20-period average "
                     f"({fmt(f.e20)})."),
    )


def _early_trend(f: _Features, cost_pct: float) -> Optional[dict]:
    e50_series = f.ind["ema50"].to_numpy(dtype=float)
    e20_series = f.ind["ema20"].to_numpy(dtype=float)
    hist = f.ind["macd_hist"].to_numpy(dtype=float)
    recent_c, recent_e50 = f.c[-15:], e50_series[-15:]
    if not np.isfinite(recent_e50).all() or not np.isfinite(hist[-1]):
        return None
    was_below = bool((recent_c[:-1] <= recent_e50[:-1]).any())
    if not (f.close > f.e50 and was_below and e20_series[-1] > e20_series[-4]
            and hist[-1] > 0 and f.ascending_lows >= 1):
        return None
    trigger = float(f.h[-5:].max())
    lows_below = [p for i, p in f.swing_lows if i >= f.n - 40 and p < f.close]
    if not lows_below:
        return None
    stop = lows_below[-1]
    target = f.next_resistance(trigger + f.atr)
    plan = Plan(
        trigger=trigger, entry_low=trigger, entry_high=trigger + 0.3 * f.atr, entry_ref=trigger,
        stop=stop, target=target,
        trigger_basis=f"highest high of the last 5 bars ({fmt(trigger)})",
        stop_basis=f"most recent confirmed swing low ({fmt(stop)})",
        target_basis=(f"next confirmed swing high above the trigger ({fmt(target)})"
                      if target is not None else "none found"),
    )
    return dict(
        type="EARLY_TREND", plan=_finish_plan(plan, f, cost_pct),
        description=("Reclaimed its 50-period average within the last 15 bars with rising momentum "
                     "and higher lows - an early-stage trend."),
    )


# ---------------------------------------------------------------------- scoring

def _score_pattern(f: _Features, setup: dict) -> Component:
    comp = Component("Pattern quality", 25)
    plan: Plan = setup["plan"]
    if setup["type"] == "MA_PULLBACK":
        comp.add(8, 8, "orderly pullback to the 20-period average in an uptrend")
    else:
        t = f.tightness
        pts = 8 if t <= 3 else 6 if t <= 4 else 4 if t <= 5 else 2 if t <= 6.5 else 0
        comp.add(pts, 8, f"15-bar range is {t:.1f} ATR wide")
    pts = 6 if f.ascending_lows >= 2 else 4 if f.ascending_lows == 1 else 0
    comp.add(pts, 6, f"{f.ascending_lows + 1 if f.ascending_lows else 0} consecutive higher swing lows")
    distance = (plan.trigger - f.close) / f.atr
    if distance <= 0:
        pts = 6 if f.close <= plan.entry_high else 2
        comp.add(pts, 6, "price is at/above the trigger" + ("" if pts == 6 else " and past the entry zone"))
    else:
        pts = 6 if distance <= 0.5 else 4 if distance <= 1 else 3 if distance <= 1.5 else 1 if distance <= 2.5 else 0
        comp.add(pts, 6, f"price is {distance:.1f} ATR below the trigger")
    if f.has_volume:
        closes, volumes = f.c[-21:], f.v[-20:]
        change = np.diff(closes)
        up, down = volumes[change > 0].sum(), volumes[change < 0].sum()
        ratio = up / down if down > 0 else float("inf")
        pts = 3 if ratio >= 1.5 else 2 if ratio >= 1.15 else 0
        comp.add(pts, 3, f"up-day vs down-day volume over 20 bars is {min(ratio, 99):.2f}x")
        dry = volumes[-5:].mean() / volumes.mean() if volumes.mean() > 0 else 1.0
        comp.add(2 if dry <= 0.85 else 0, 2, f"last-5-bar volume is {dry:.2f}x the 20-bar average")
    else:
        comp.pending(5, "volume accumulation: volume not reported by the provider", UNVERIFIED)
    return comp


def _score_technical(f: _Features, h4_ind: Optional[pd.DataFrame]) -> Component:
    comp = Component("Technical confirmation", 25)
    last = f.ind.iloc[-1]
    comp.add((2 if f.close > f.e20 else 0) + (3 if f.e20 > f.e50 else 0), 5,
             "price above EMA20 / EMA20 above EMA50")
    if f.e200 is not None:
        comp.add(3 if f.e50 > f.e200 else 0, 3, "EMA50 above EMA200")
    else:
        comp.pending(3, "EMA50 vs EMA200: fewer than 200 completed daily bars")
    if f.rsi is not None:
        pts = 5 if 50 <= f.rsi <= 68 else 3 if 45 <= f.rsi < 50 or 68 < f.rsi <= 72 else 0
        comp.add(pts, 5, f"RSI(14) {f.rsi:.1f}")
    else:
        comp.pending(5, "RSI(14)")
    hist = f.ind["macd_hist"].to_numpy(dtype=float)
    if np.isfinite(hist[-3:]).all():
        rising = 3 if hist[-1] > hist[-2] > hist[-3] else 2 if hist[-1] > hist[-2] else 0
        comp.add((2 if hist[-1] > 0 else 0) + rising, 5, "MACD histogram positive / rising")
    else:
        comp.pending(5, "MACD")
    adx_value, plus_di, minus_di = last["adx14"], last["plus_di"], last["minus_di"]
    if not (pd.isna(adx_value) or pd.isna(plus_di) or pd.isna(minus_di)):
        pts = 0
        if plus_di > minus_di:
            pts = 4 if adx_value >= 25 else 2 if adx_value >= 18 else 0
        comp.add(pts, 4, f"ADX(14) {adx_value:.1f} with +DI {'above' if plus_di > minus_di else 'below'} -DI")
    else:
        comp.pending(4, "ADX(14)")
    if h4_ind is not None and len(h4_ind) >= 3 and not pd.isna(h4_ind["ema20"].iloc[-1]) \
            and np.isfinite(h4_ind["macd_hist"].to_numpy(dtype=float)[-2:]).all():
        h4 = h4_ind.iloc[-1]
        hist4 = h4_ind["macd_hist"].to_numpy(dtype=float)
        pts = (2 if h4["close"] > h4["ema20"] else 0) + (1 if hist4[-1] > hist4[-2] else 0)
        comp.add(pts, 3, "4-hour: price above EMA20 / momentum rising")
    else:
        comp.pending(3, "4-hour confirmation: no verified 4-hour candles")
    return comp


def _score_rs_regime(f: _Features, bench_ind: Optional[pd.DataFrame], ctx: Context) -> Component:
    comp = Component("Relative strength and market regime", 20)
    last = f.ind.iloc[-1]
    if ctx.is_benchmark:
        comp.notes.append("relative strength not applicable: this instrument is the benchmark (0 of 12 counted)")
        bench_ind = f.ind
    elif bench_ind is None or len(bench_ind) == 0:
        comp.status = UNVERIFIED
        comp.notes.append("benchmark data unavailable - UNVERIFIED, not scored (0 of 20 counted)")
        return comp
    else:
        rs20, rs60 = last.get("rs_20"), last.get("rs_60")
        if rs20 is not None and not pd.isna(rs20):
            pts = 8 if rs20 > 5 else 6 if rs20 > 2 else 4 if rs20 > 0 else 2 if rs20 > -2 else 0
            comp.add(pts, 8, f"20-bar return vs benchmark {rs20:+.1f} pts")
        else:
            comp.pending(8, "20-bar relative strength")
        if rs60 is not None and not pd.isna(rs60):
            comp.add(4 if rs60 > 5 else 2 if rs60 > 0 else 0, 4, f"60-bar return vs benchmark {rs60:+.1f} pts")
        else:
            comp.pending(4, "60-bar relative strength")
    bench = bench_ind.iloc[-1]
    if pd.isna(bench["ema50"]) or pd.isna(bench["ema20"]):
        comp.pending(8, "market regime: benchmark has too little history")
        return comp
    pts = (4 if bench["close"] > bench["ema50"] else 0) + (2 if bench["ema20"] > bench["ema50"] else 0)
    maximum = 6
    if not pd.isna(bench["ema200"]):
        pts += 2 if bench["close"] > bench["ema200"] else 0
        maximum = 8
    else:
        comp.pending(2, "benchmark vs EMA200")
    comp.add(pts, maximum, "benchmark trend (above EMA50 / EMA20 above EMA50 / above EMA200)")
    return comp


def _score_liquidity_quality(f: _Features, ctx: Context) -> Tuple[Component, Optional[float]]:
    comp = Component("Liquidity and data quality", 15)
    traded = None
    if f.has_volume:
        traded = float((f.c[-20:] * f.v[-20:]).mean() * ctx.value_scale)
        pts = 8 if traded >= 100e6 else 6 if traded >= 25e6 else 4 if traded >= 5e6 else 2 if traded >= 1e6 else 0
        comp.add(pts, 8, f"average daily value traded {traded / 1e6:,.1f}m (local currency)")
    else:
        comp.pending(8, "liquidity: volume not reported by the provider", UNVERIFIED)
    comp.add(7 if ctx.quality_status == "PASS" else 4, 7, f"data quality {ctx.quality_status}")
    comp.notes.extend(ctx.quality_notes)
    return comp, traded


def _score_reward_risk(plan: Plan, cost_pct: float) -> Component:
    comp = Component("Realistic reward/risk", 15)
    if not plan.valid or plan.reward_risk is None:
        comp.status = UNVERIFIED
        comp.notes.append(f"no valid trade plan ({plan.problem}) - UNVERIFIED, not scored (0 of 15 counted)")
        return comp
    rr = plan.reward_risk
    pts = 15 if rr >= 3 else 12 if rr >= 2.5 else 10 if rr >= 2 else 5 if rr >= 1.5 else 0
    comp.add(pts, 15, f"reward/risk {rr:.2f} after an estimated {cost_pct:.2f}% round-trip cost")
    return comp


def _do_not_chase(f: _Features) -> Optional[str]:
    reasons = []
    extension = (f.close - f.e20) / f.atr
    if extension > 2.5:
        reasons.append(f"price is {extension:.1f} ATR above its 20-period average")
    if f.rsi is not None and f.rsi > 78:
        reasons.append(f"RSI(14) is {f.rsi:.0f}")
    if f.n > 6:
        gain = f.close - float(f.c[-6])
        if gain > 4.0 * f.atr:
            reasons.append(f"up {gain / f.atr:.1f} ATR in 5 bars")
    last_range = float(f.h[-1] - f.l[-1])
    if last_range > 3.0 * f.atr and f.c[-1] > f.c[-2]:
        reasons.append(f"latest bar spans {last_range / f.atr:.1f} ATR")
    return "; ".join(reasons) if reasons else None


# --------------------------------------------------------------------- evaluate

def evaluate(
    daily: pd.DataFrame,
    ind: pd.DataFrame,
    h4_ind: Optional[pd.DataFrame],
    bench_ind: Optional[pd.DataFrame],
    ctx: Context,
) -> Evaluation:
    """Classify one instrument from its completed daily candles."""
    price = float(daily["close"].iloc[-1]) if daily is not None and len(daily) else None
    if ctx.quality_status == "FAIL":
        return _reject("data quality FAIL - not analysed", price, ctx.quality_notes)
    if daily is None or len(daily) < MIN_BARS:
        return _reject(f"insufficient history ({0 if daily is None else len(daily)} completed daily bars)", price)
    last = ind.iloc[-1]
    if pd.isna(last["atr14"]) or pd.isna(last["ema20"]) or pd.isna(last["ema50"]) or last["atr14"] <= 0:
        return _reject("core indicators unavailable (ATR/EMA) - UNVERIFIED", price)

    f = _Features(daily, ind)
    chase = _do_not_chase(f)

    setups = [s for s in (
        _breakout_retest(f, ctx.cost_pct), _breakout_prep(f, ctx.cost_pct),
        _ma_pullback(f, ctx.cost_pct), _early_trend(f, ctx.cost_pct),
    ) if s is not None]
    if not setups:
        if f.close < f.e50 and f.e20 < f.e50:
            reason = "downtrend: price and EMA20 are below EMA50"
        else:
            reason = "no qualifying structure (no tight base, retest, pullback or early trend)"
        result = _reject(reason, price)
        result.do_not_chase, result.do_not_chase_reason = chase is not None, chase
        return result

    scored = []
    for setup in setups:
        pattern = _score_pattern(f, setup)
        scored.append((pattern.points + (5 if setup["plan"].valid else 0), setup, pattern))
    scored.sort(key=lambda item: item[0], reverse=True)
    _, setup, pattern = scored[0]
    plan: Plan = setup["plan"]

    liquidity, traded = _score_liquidity_quality(f, ctx)
    components = {
        "pattern": pattern,
        "technical": _score_technical(f, h4_ind),
        "rs_regime": _score_rs_regime(f, bench_ind, ctx),
        "liquidity_quality": liquidity,
        "reward_risk": _score_reward_risk(plan, ctx.cost_pct),
    }
    total = sum(c.points for c in components.values())
    max_verified = sum(c.maximum for c in components.values())
    provisional = any(c.status != VERIFIED for c in components.values())
    pct = 100.0 * total / max_verified if max_verified else 0.0

    reasons: List[str] = [setup["description"]]
    for comp in components.values():
        reasons.append(f"{comp.name}: {comp.points:g}/{comp.maximum:g} verified "
                       f"(of {comp.full_max:g}) [{comp.status}]")
    if provisional:
        reasons.append("Score is PROVISIONAL: one or more inputs are unverified or pending.")

    distance = (plan.trigger - f.close) / f.atr
    past_zone = f.close > plan.entry_high
    liquid = traded is not None and traded >= 5e6
    rr_ok = plan.valid and plan.reward_risk is not None and plan.reward_risk >= MIN_REWARD_RISK

    if (plan.valid and rr_ok and not provisional and total >= 70 and chase is None
            and ctx.quality_status == "PASS" and liquid and distance <= 0.5 and not past_zone):
        classification = READY
    elif plan.valid and pct >= 60 and distance <= 1.5 and not past_zone and chase is None \
            and plan.reward_risk is not None and plan.reward_risk >= 1.5:
        classification = NEAR_READY
        blockers = []
        if not rr_ok:
            blockers.append(f"reward/risk {plan.reward_risk:.2f} is below {MIN_REWARD_RISK:g}")
        if provisional:
            blockers.append("score is provisional")
        if total < 70:
            blockers.append(f"verified score {total:.0f} is below 70")
        if distance > 0.5:
            blockers.append(f"price is {distance:.1f} ATR below the trigger")
        if not liquid:
            blockers.append("liquidity is unverified or below 5m a day")
        if ctx.quality_status != "PASS":
            blockers.append(f"data quality is {ctx.quality_status}")
        if blockers:
            reasons.append("Not READY because: " + "; ".join(blockers) + ".")
    elif pct >= 45:
        classification = PREPARING
        if past_zone:
            reasons.append("Price is already past the entry zone - wait for a retest rather than chase.")
        if not plan.valid:
            reasons.append(f"No valid trade plan yet: {plan.problem}.")
    else:
        classification = REJECTED
        reasons.insert(0, f"score {pct:.0f}% of verified points is below the 45% threshold")

    supports = [p for i, p in f.swing_lows if i >= f.n - 120 and p < f.close]
    resistances = [p for i, p in f.swing_highs if i >= f.n - 120 and p > f.close]
    return Evaluation(
        classification=classification,
        reasons=reasons,
        components=components,
        score_total=round(total, 2),
        score_max_verified=round(max_verified, 2),
        provisional=provisional,
        do_not_chase=chase is not None,
        do_not_chase_reason=chase,
        setup_type=setup["type"],
        setup_description=setup["description"],
        plan=plan,
        price=f.close,
        support=max(supports) if supports else None,
        resistance=min(resistances) if resistances else None,
        invalidation=(f"A daily close below {fmt(plan.stop)} ({plan.stop_basis}) invalidates the setup."),
    )
