"""Crypto market snapshots (CoinMarketCap) and equity cross-checks (Alpha Vantage)."""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import upsert_rows
from app.models import DataQualityCheck, Instrument, MarketCandle, MarketSnapshot
from app.providers.parsers import ProviderError
from app.timeutil import as_utc

log = logging.getLogger(__name__)

CROSS_CHECK_TOLERANCE_PCT = 0.5


def collect_crypto_snapshots(session: Session, provider, instruments: Sequence[Instrument], now: datetime) -> int:
    """Store rank / market cap / 24h volume for the crypto list. One API credit."""
    by_symbol: Dict[str, Instrument] = {i.cmc_symbol: i for i in instruments if i.cmc_symbol}
    if not by_symbol or provider is None or not provider.available:
        return 0
    try:
        quotes = provider.fetch_quotes(sorted(by_symbol))
    except ProviderError as exc:
        log.warning("CoinMarketCap snapshot failed: %s", exc.kind)
        return 0
    snapshot_time = now.replace(second=0, microsecond=0)
    rows = [
        dict(
            instrument_id=by_symbol[q.symbol].id, provider="coinmarketcap", snapshot_time=snapshot_time,
            price=q.price, volume_24h=q.volume_24h, market_cap=q.market_cap,
            market_cap_rank=q.market_cap_rank, pct_change_24h=q.pct_change_24h,
            pct_change_7d=q.pct_change_7d, retrieved_at=now,
        )
        for q in quotes
        if q.symbol in by_symbol
    ]
    upsert_rows(
        session, MarketSnapshot, rows,
        conflict_cols=["instrument_id", "provider", "snapshot_time"],
        update_cols=["price", "volume_24h", "market_cap", "market_cap_rank",
                     "pct_change_24h", "pct_change_7d", "retrieved_at"],
    )
    return len(rows)


def cross_check_equities(
    session: Session, provider, instruments: Sequence[Instrument], scan_run_id: int,
    now: datetime, max_checks: int,
) -> List[DataQualityCheck]:
    """Compare stored daily closes with Alpha Vantage for a small rotating sample.

    The free Alpha Vantage key allows about 25 calls a day, so only
    `max_checks` symbols are checked per run, least recently checked first.
    """
    if provider is None or not provider.available or max_checks <= 0:
        return []
    today_start = as_utc(now).replace(hour=0, minute=0, second=0, microsecond=0)
    done_today = session.scalar(
        select(func.count()).select_from(DataQualityCheck).where(
            DataQualityCheck.check_type == "provider_cross_check",
            DataQualityCheck.checked_at >= today_start,
        )
    ) or 0
    budget = max(0, max_checks - int(done_today))
    candidates = [i for i in instruments if i.av_symbol and i.primary_provider == "twelve_data"]
    if not candidates or budget == 0:
        return []
    last_checked = {
        instrument_id: as_utc(checked)
        for instrument_id, checked in session.execute(
            select(DataQualityCheck.instrument_id, func.max(DataQualityCheck.checked_at))
            .where(DataQualityCheck.check_type == "provider_cross_check")
            .group_by(DataQualityCheck.instrument_id)
        ).all()
        if checked is not None
    }
    oldest = as_utc(datetime(1970, 1, 1))
    candidates.sort(key=lambda i: (last_checked.get(i.id) or oldest, i.symbol))
    results: List[DataQualityCheck] = []
    for instrument in candidates[:budget]:
        if not provider.available:
            break
        try:
            av_candles = provider.fetch_daily(instrument.av_symbol)
        except ProviderError as exc:
            log.warning("Alpha Vantage cross-check failed for %s: %s", instrument.symbol, exc.kind)
            continue
        av_by_day = {c.open_time.date(): c.close for c in av_candles}
        stored = session.execute(
            select(MarketCandle.open_time, MarketCandle.close)
            .where(
                MarketCandle.instrument_id == instrument.id,
                MarketCandle.provider == "twelve_data",
                MarketCandle.timeframe == "1d",
                MarketCandle.is_complete.is_(True),
                MarketCandle.open_time >= as_utc(now) - timedelta(days=15),
            )
            .order_by(MarketCandle.open_time.desc())
            .limit(5)
        ).all()
        compared, worst, worst_day = 0, 0.0, None
        for open_time, close in stored:
            other = av_by_day.get(as_utc(open_time).date())
            if other is None or not close:
                continue
            compared += 1
            diff = abs(other - close) / close * 100.0
            if diff >= worst:
                worst, worst_day = diff, as_utc(open_time).date()
        if compared == 0:
            status, detail = "WARN", "no overlapping daily candles to compare with Alpha Vantage"
        elif worst > CROSS_CHECK_TOLERANCE_PCT:
            status = "WARN"
            detail = (f"Twelve Data and Alpha Vantage daily closes differ by {worst:.2f}% on {worst_day} "
                      f"({compared} day(s) compared)")
        else:
            status = "PASS"
            detail = f"daily closes agree within {worst:.2f}% over {compared} day(s)"
        check = DataQualityCheck(
            scan_run_id=scan_run_id, instrument_id=instrument.id, provider="alpha_vantage",
            timeframe="1d", check_type="provider_cross_check", status=status, detail=detail, checked_at=now,
        )
        session.add(check)
        results.append(check)
    return results
