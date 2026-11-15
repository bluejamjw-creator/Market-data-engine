"""Twelve Data adapter - primary cross-market candle source.

Endpoint: GET https://api.twelvedata.com/time_series
  symbol, interval (4h | 1day), outputsize (1-5000), mic_code, start_date,
  timezone=UTC, order=ASC. One API credit per symbol per call.
The free Basic plan allows 8 credits/minute and 800/day and covers US
equities/ETFs, forex and crypto only; other exchanges answer with a
plan error, which is recorded as `unsupported` instead of being retried.
"""
import logging
from datetime import datetime
from typing import List, Optional

from app.providers.base import BaseProvider
from app.providers.parsers import Candle, ProviderError, parse_twelve_data_time_series

log = logging.getLogger(__name__)

BASE_URL = "https://api.twelvedata.com"
_INTERVALS = {"1d": "1day", "4h": "4h"}
# Used until /api_usage reports the real plan limit (Basic plan = 8/minute).
DEFAULT_CALLS_PER_MINUTE = 8


class TwelveDataProvider(BaseProvider):
    name = "twelve_data"

    def __init__(self, api_key: Optional[str], **kwargs):
        super().__init__(**kwargs)
        self._api_key = api_key
        self.plan_limit_per_minute: Optional[int] = None
        self.daily_usage: Optional[int] = None
        self.plan_daily_limit: Optional[int] = None
        self.plan_category: Optional[str] = None
        if not api_key:
            self.disable("missing_credentials: TWELVE_DATA_API_KEY is not set")
        # Stay a little under the limit so a slow clock never trips it.
        self.set_rate_limit(DEFAULT_CALLS_PER_MINUTE * 0.9)

    def _headers(self) -> dict:
        # Header auth keeps the key out of URLs (and therefore out of any log).
        return {"Authorization": f"apikey {self._api_key}"}

    def refresh_usage(self) -> Optional[dict]:
        """Read plan limits from /api_usage (costs no data credits)."""
        if not self.available:
            return None

        def parse(payload):
            if not isinstance(payload, dict):
                raise ProviderError("unexpected /api_usage response", kind="parse_error")
            if payload.get("status") == "error":
                from app.providers.parsers import classify_twelve_data_error

                raise classify_twelve_data_error(payload)
            return payload

        try:
            usage = self.get_json("api_usage", f"{BASE_URL}/api_usage", {}, parse, headers=self._headers())
        except ProviderError as exc:
            log.warning("Twelve Data usage check failed (%s)", exc.kind)
            return None
        limit = usage.get("plan_limit")
        if isinstance(limit, (int, float)) and limit > 0:
            self.plan_limit_per_minute = int(limit)
            self.set_rate_limit(max(1.0, limit * 0.9))
        self.daily_usage = usage.get("daily_usage")
        self.plan_daily_limit = usage.get("plan_daily_limit")
        self.plan_category = usage.get("plan_category")
        log.info(
            "Twelve Data plan: %s, %s credits/minute, daily usage %s of %s",
            self.plan_category or "unknown",
            self.plan_limit_per_minute or "unknown",
            self.daily_usage if self.daily_usage is not None else "unknown",
            self.plan_daily_limit if self.plan_daily_limit is not None else "no daily cap reported",
        )
        return usage

    def remaining_daily_credits(self) -> Optional[int]:
        if self.plan_daily_limit is None or self.daily_usage is None:
            return None
        used_this_run = self.successes + self.failures
        return int(self.plan_daily_limit) - int(self.daily_usage) - used_this_run

    def fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        mic_code: Optional[str] = None,
        start: Optional[datetime] = None,
        outputsize: int = 400,
    ) -> List[Candle]:
        remaining = self.remaining_daily_credits()
        if remaining is not None and remaining <= 5:
            self.disable("quota_exhausted: Twelve Data daily credit limit nearly reached")
        params = {
            "symbol": symbol,
            "interval": _INTERVALS[timeframe],
            "timezone": "UTC",
            "order": "ASC",
            "outputsize": str(outputsize),
        }
        if mic_code:
            params["mic_code"] = mic_code
        if start is not None:
            params["start_date"] = start.strftime("%Y-%m-%d %H:%M:%S")
        try:
            return self.get_json(
                "time_series", f"{BASE_URL}/time_series", params, parse_twelve_data_time_series,
                headers=self._headers(), symbol=symbol, timeframe=timeframe,
            )
        except ProviderError as exc:
            if exc.kind != "rate_limited":
                raise
        # The per-minute window was exhausted: wait for it to reset, try once more.
        log.warning("Twelve Data per-minute limit hit; waiting 61s before one retry")
        self._sleep(61)
        return self.get_json(
            "time_series", f"{BASE_URL}/time_series", params, parse_twelve_data_time_series,
            headers=self._headers(), symbol=symbol, timeframe=timeframe,
        )
