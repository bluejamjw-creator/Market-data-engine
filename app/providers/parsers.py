"""Pure functions that turn provider responses into candles and quotes.

Kept free of network code so they can be tested against recorded/mocked
payloads. A parser either returns real values from the payload or raises
`ProviderError`; it never fills gaps with invented numbers.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional

UTC = timezone.utc


class ProviderError(Exception):
    """A provider call failed. `kind` drives how the caller reacts.

    kinds: missing_credentials, invalid_credentials, rate_limited,
    quota_exhausted, unsupported, not_found, blocked, http_error,
    parse_error, timeout, network, no_data
    """

    def __init__(self, message: str, kind: str = "http_error", status_code: Optional[int] = None):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code


@dataclass(frozen=True)
class Candle:
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float]  # None = provider did not report volume


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: Optional[float]
    volume_24h: Optional[float]
    market_cap: Optional[float]
    market_cap_rank: Optional[int]
    pct_change_24h: Optional[float]
    pct_change_7d: Optional[float]
    last_updated: Optional[datetime]


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _required_float(value: Any, field: str) -> float:
    number = _to_float(value)
    if number is None:
        raise ProviderError(f"missing or non-numeric '{field}' in candle", kind="parse_error")
    return number


# --------------------------------------------------------------- Twelve Data

def classify_twelve_data_error(payload: dict) -> ProviderError:
    code = payload.get("code")
    message = str(payload.get("message", "unknown Twelve Data error"))
    lowered = message.lower()
    try:
        code_int = int(code) if code is not None else None
    except (TypeError, ValueError):
        code_int = None
    if code_int == 429 or "api credits" in lowered:
        if "day" in lowered and "minute" not in lowered:
            return ProviderError(message, kind="quota_exhausted", status_code=429)
        return ProviderError(message, kind="rate_limited", status_code=429)
    if code_int == 401 or "apikey" in lowered and ("invalid" in lowered or "incorrect" in lowered):
        return ProviderError(message, kind="invalid_credentials", status_code=401)
    if code_int == 403 or "plan" in lowered or "upgrade" in lowered or "subscription" in lowered:
        return ProviderError(message, kind="unsupported", status_code=code_int or 403)
    if code_int in (400, 404) or "not found" in lowered or "symbol" in lowered:
        return ProviderError(message, kind="not_found", status_code=code_int or 404)
    return ProviderError(message, kind="http_error", status_code=code_int)


def parse_twelve_data_time_series(payload: Any) -> List[Candle]:
    """Parse `/time_series` (requested with timezone=UTC). Oldest candle first."""
    if not isinstance(payload, dict):
        raise ProviderError("unexpected Twelve Data response shape", kind="parse_error")
    if payload.get("status") == "error" or "code" in payload and "values" not in payload:
        raise classify_twelve_data_error(payload)
    values = payload.get("values")
    if not isinstance(values, list):
        raise ProviderError("Twelve Data response has no 'values' list", kind="parse_error")
    candles: List[Candle] = []
    for row in values:
        raw_time = str(row.get("datetime", ""))
        try:
            if len(raw_time) == 10:
                open_time = datetime.strptime(raw_time, "%Y-%m-%d").replace(tzinfo=UTC)
            else:
                open_time = datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except ValueError as exc:
            raise ProviderError(f"bad datetime '{raw_time}'", kind="parse_error") from exc
        candles.append(
            Candle(
                open_time=open_time,
                open=_required_float(row.get("open"), "open"),
                high=_required_float(row.get("high"), "high"),
                low=_required_float(row.get("low"), "low"),
                close=_required_float(row.get("close"), "close"),
                volume=_to_float(row.get("volume")),
            )
        )
    candles.sort(key=lambda c: c.open_time)
    return candles


# ------------------------------------------------------------------- Binance

def parse_binance_klines(payload: Any) -> List[Candle]:
    """Parse `/api/v3/klines`: a list of arrays, open time in milliseconds UTC."""
    if isinstance(payload, dict):
        message = str(payload.get("msg", "Binance error"))
        code = payload.get("code")
        if code == -1121 or "invalid symbol" in message.lower():
            raise ProviderError(message, kind="not_found", status_code=400)
        if "restricted location" in message.lower() or "unavailable" in message.lower():
            raise ProviderError(message, kind="blocked", status_code=451)
        raise ProviderError(message, kind="http_error")
    if not isinstance(payload, list):
        raise ProviderError("unexpected Binance response shape", kind="parse_error")
    candles: List[Candle] = []
    for row in payload:
        if not isinstance(row, list) or len(row) < 6:
            raise ProviderError("malformed Binance kline row", kind="parse_error")
        try:
            open_time = datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC)
        except (TypeError, ValueError) as exc:
            raise ProviderError("bad Binance kline open time", kind="parse_error") from exc
        candles.append(
            Candle(
                open_time=open_time,
                open=_required_float(row[1], "open"),
                high=_required_float(row[2], "high"),
                low=_required_float(row[3], "low"),
                close=_required_float(row[4], "close"),
                volume=_to_float(row[5]),
            )
        )
    candles.sort(key=lambda c: c.open_time)
    return candles


# -------------------------------------------------------------- Alpha Vantage

def parse_alpha_vantage_daily(payload: Any) -> List[Candle]:
    """Parse `TIME_SERIES_DAILY`. Dates are exchange-local; stored at 00:00 UTC."""
    if not isinstance(payload, dict):
        raise ProviderError("unexpected Alpha Vantage response shape", kind="parse_error")
    if "Error Message" in payload:
        raise ProviderError(str(payload["Error Message"]), kind="not_found")
    for key in ("Note", "Information"):
        if key in payload and "Time Series (Daily)" not in payload:
            text = str(payload[key])
            lowered = text.lower()
            if "premium" in lowered and "rate limit" not in lowered and "per day" not in lowered:
                raise ProviderError(text, kind="unsupported")
            raise ProviderError(text, kind="quota_exhausted", status_code=429)
    series = payload.get("Time Series (Daily)")
    if not isinstance(series, dict):
        raise ProviderError("Alpha Vantage response has no daily series", kind="parse_error")
    candles: List[Candle] = []
    for day, row in series.items():
        try:
            open_time = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError as exc:
            raise ProviderError(f"bad date '{day}'", kind="parse_error") from exc
        candles.append(
            Candle(
                open_time=open_time,
                open=_required_float(row.get("1. open"), "open"),
                high=_required_float(row.get("2. high"), "high"),
                low=_required_float(row.get("3. low"), "low"),
                close=_required_float(row.get("4. close"), "close"),
                volume=_to_float(row.get("5. volume")),
            )
        )
    candles.sort(key=lambda c: c.open_time)
    return candles


# -------------------------------------------------------------- CoinMarketCap

def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def parse_coinmarketcap_quotes(payload: Any) -> List[Quote]:
    """Parse `/v1` or `/v2` `cryptocurrency/quotes/latest` (USD)."""
    if not isinstance(payload, dict):
        raise ProviderError("unexpected CoinMarketCap response shape", kind="parse_error")
    status = payload.get("status") or {}
    error_code = status.get("error_code")
    if error_code not in (None, 0, "0"):
        message = str(status.get("error_message") or "CoinMarketCap error")
        code = int(error_code) if str(error_code).isdigit() else 0
        if code in (1001, 1002):
            raise ProviderError(message, kind="invalid_credentials", status_code=401)
        if code in (1006,):
            raise ProviderError(message, kind="unsupported", status_code=403)
        if code in (1008, 1009, 1010, 1011):
            kind = "rate_limited" if code == 1008 else "quota_exhausted"
            raise ProviderError(message, kind=kind, status_code=429)
        raise ProviderError(message, kind="http_error")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ProviderError("CoinMarketCap response has no data", kind="parse_error")
    quotes: List[Quote] = []
    for symbol, entry in data.items():
        if isinstance(entry, list):  # v2 returns a list per symbol
            entries = [e for e in entry if isinstance(e, dict)]
            if not entries:
                continue
            # the most valuable asset with that ticker is the one we mean
            entries.sort(key=lambda e: (e.get("cmc_rank") is None, e.get("cmc_rank") or 0))
            entry = entries[0]
        if not isinstance(entry, dict):
            continue
        usd = ((entry.get("quote") or {}).get("USD")) or {}
        rank = entry.get("cmc_rank")
        quotes.append(
            Quote(
                symbol=str(entry.get("symbol") or symbol).upper(),
                price=_to_float(usd.get("price")),
                volume_24h=_to_float(usd.get("volume_24h")),
                market_cap=_to_float(usd.get("market_cap")),
                market_cap_rank=int(rank) if isinstance(rank, (int, float)) else None,
                pct_change_24h=_to_float(usd.get("percent_change_24h")),
                pct_change_7d=_to_float(usd.get("percent_change_7d")),
                last_updated=_parse_iso(usd.get("last_updated") or entry.get("last_updated")),
            )
        )
    return quotes
