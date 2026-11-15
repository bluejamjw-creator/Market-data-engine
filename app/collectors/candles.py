"""Incremental OHLCV collection.

For each instrument and timeframe the collector looks at what is already
stored and only calls a provider when a new completed candle can exist.
Fetched candles are validated, then upserted on the unique key
(instrument, provider, timeframe, open_time), so nothing is duplicated.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import upsert_rows
from app.models import Instrument, MarketCandle, ProviderRequest
from app.providers.parsers import Candle, ProviderError
from app.services.quality import Issue, split_valid_candles
from app.timeutil import as_utc, candle_is_complete, previous_business_day, timeframe_delta

log = logging.getLogger(__name__)

HISTORY_BARS = {"1d": 400, "4h": 300}
UNSUPPORTED_RECHECK = timedelta(days=7)
_UNSUPPORTED_KINDS = {"unsupported", "not_found"}


@dataclass
class CollectResult:
    instrument_id: int
    timeframe: str
    provider: Optional[str] = None
    status: str = "skipped"      # fetched | up_to_date | failed | unavailable
    candles_stored: int = 0
    detail: str = ""
    issues: List[Issue] = field(default_factory=list)


def provider_chain(instrument: Instrument) -> List[str]:
    if instrument.asset_class == "crypto":
        chain = []
        if instrument.binance_symbol:
            chain.append("binance")
        if instrument.td_symbol:
            chain.append("twelve_data")
        return chain
    return ["twelve_data"] if instrument.td_symbol else []


def _unsupported(instrument: Instrument, provider: str, now: datetime) -> Optional[str]:
    entry = (instrument.unsupported_providers or {}).get(provider)
    if not entry:
        return None
    try:
        checked = as_utc(datetime.fromisoformat(entry["checked_at"]))
    except (KeyError, ValueError):
        return None
    if now - checked < UNSUPPORTED_RECHECK:
        return entry.get("reason", "unsupported")
    return None


def mark_unsupported(instrument: Instrument, provider: str, reason: str, now: datetime) -> None:
    current = dict(instrument.unsupported_providers or {})
    current[provider] = {"reason": reason[:300], "checked_at": now.isoformat()}
    instrument.unsupported_providers = current  # reassign so the JSON change is tracked


def clear_unsupported(instrument: Instrument, provider: str) -> None:
    if instrument.unsupported_providers and provider in instrument.unsupported_providers:
        current = dict(instrument.unsupported_providers)
        current.pop(provider, None)
        instrument.unsupported_providers = current or None


def _last_complete(session: Session, instrument_id: int, provider: str, timeframe: str) -> Optional[datetime]:
    return session.scalar(
        select(func.max(MarketCandle.open_time)).where(
            MarketCandle.instrument_id == instrument_id,
            MarketCandle.provider == provider,
            MarketCandle.timeframe == timeframe,
            MarketCandle.is_complete.is_(True),
        )
    )


def _last_success(session: Session, provider: str, symbol: str, timeframe: str) -> Optional[datetime]:
    return session.scalar(
        select(func.max(ProviderRequest.requested_at)).where(
            ProviderRequest.provider == provider,
            ProviderRequest.symbol == symbol,
            ProviderRequest.timeframe == timeframe,
            ProviderRequest.success.is_(True),
        )
    )


def needs_fetch(
    asset_class: str,
    timeframe: str,
    last_complete: Optional[datetime],
    last_success: Optional[datetime],
    now: datetime,
    exchange_tz: str,
) -> bool:
    """Could the provider have a completed candle we do not hold yet?"""
    if last_complete is None:
        return True
    last_complete, now = as_utc(last_complete), as_utc(now)
    delta = timeframe_delta(timeframe)
    if asset_class == "crypto":
        # the candle after the last stored one must itself have closed
        return last_complete + 2 * delta <= now
    local_now = now.astimezone(ZoneInfo(exchange_tz))
    prev_day = previous_business_day(local_now.date())
    since = now - as_utc(last_success) if last_success is not None else None
    if timeframe == "1d":
        if last_complete.date() >= prev_day:
            return False
        # behind (or a market holiday): ask again, but not more than every 6 hours
        return since is None or since > timedelta(hours=6)
    if local_now.weekday() >= 5 and last_complete.date() >= prev_day:
        return False
    return since is None or since > timedelta(hours=3, minutes=30)


def _fetch(provider_name: str, providers: Dict[str, object], instrument: Instrument,
           timeframe: str, start: Optional[datetime]) -> List[Candle]:
    provider = providers[provider_name]
    if provider_name == "binance":
        return provider.fetch_candles(instrument.binance_symbol, timeframe, start=start,
                                      limit=HISTORY_BARS[timeframe])
    return provider.fetch_candles(instrument.td_symbol, timeframe, mic_code=instrument.td_mic_code,
                                  start=start, outputsize=HISTORY_BARS[timeframe])


def provider_symbol(instrument: Instrument, provider_name: str) -> str:
    return instrument.binance_symbol if provider_name == "binance" else instrument.td_symbol


def collect_series(
    session: Session,
    instrument: Instrument,
    timeframe: str,
    providers: Dict[str, object],
    now: datetime,
    exchange_tz: str,
) -> CollectResult:
    """Bring one instrument/timeframe up to date from the first provider that can serve it."""
    result = CollectResult(instrument_id=instrument.id, timeframe=timeframe)
    reasons: List[str] = []
    for name in provider_chain(instrument):
        provider = providers.get(name)
        if provider is None or not provider.available:
            reasons.append(f"{name}: {getattr(provider, 'disabled_reason', None) or 'not configured'}")
            continue
        why = _unsupported(instrument, name, now)
        if why:
            reasons.append(f"{name}: {why}")
            continue
        symbol = provider_symbol(instrument, name)
        last = _last_complete(session, instrument.id, name, timeframe)
        last = as_utc(last) if last is not None else None
        if not needs_fetch(instrument.asset_class, timeframe, last,
                           _last_success(session, name, symbol, timeframe), now, exchange_tz):
            result.provider, result.status, result.detail = name, "up_to_date", "no new completed candle possible yet"
            return result
        try:
            candles = _fetch(name, providers, instrument, timeframe, last)
        except ProviderError as exc:
            reasons.append(f"{name}: {exc.kind}")
            if exc.kind in _UNSUPPORTED_KINDS:
                mark_unsupported(instrument, name, f"{exc.kind}: {exc}", now)
                log.info("%s does not serve %s %s (%s)", name, instrument.symbol, timeframe, exc.kind)
            else:
                log.warning("%s failed for %s %s: %s", name, instrument.symbol, timeframe, exc.kind)
            continue
        clear_unsupported(instrument, name)
        valid, issues = split_valid_candles(candles)
        result.issues = issues
        rows = [
            dict(
                instrument_id=instrument.id, provider=name, timeframe=timeframe,
                open_time=c.open_time, open=c.open, high=c.high, low=c.low, close=c.close,
                volume=c.volume,
                is_complete=candle_is_complete(instrument.asset_class, timeframe, c.open_time, now, exchange_tz),
                retrieved_at=now,
            )
            for c in valid
        ]
        upsert_rows(
            session, MarketCandle, rows,
            conflict_cols=["instrument_id", "provider", "timeframe", "open_time"],
            update_cols=["open", "high", "low", "close", "volume", "is_complete", "retrieved_at"],
        )
        if instrument.primary_provider != name:
            instrument.primary_provider = name
        new_rows = [r for r in rows if last is None or as_utc(r["open_time"]) > as_utc(last)]
        result.provider, result.status = name, "fetched"
        result.candles_stored = len(new_rows)
        result.detail = f"{len(rows)} candle(s) received, {len(new_rows)} new"
        return result
    result.status = "unavailable" if reasons else "skipped"
    result.detail = "; ".join(reasons) or "no provider mapping"
    return result


def load_candles(
    session: Session, instrument: Instrument, timeframe: str, limit: int = 420
) -> Optional[pd.DataFrame]:
    """Completed candles for analysis, oldest first, from the instrument's primary provider."""
    provider = instrument.primary_provider
    if provider is None:
        return None
    rows = session.execute(
        select(
            MarketCandle.open_time, MarketCandle.open, MarketCandle.high,
            MarketCandle.low, MarketCandle.close, MarketCandle.volume,
        )
        .where(
            MarketCandle.instrument_id == instrument.id,
            MarketCandle.provider == provider,
            MarketCandle.timeframe == timeframe,
            MarketCandle.is_complete.is_(True),
        )
        .order_by(MarketCandle.open_time.desc())
        .limit(limit)
    ).all()
    if not rows:
        return None
    frame = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume"])
    frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True)
    frame = frame.sort_values("open_time").set_index("open_time")
    return frame.astype(float)
