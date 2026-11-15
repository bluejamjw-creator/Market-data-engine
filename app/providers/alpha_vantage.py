"""Alpha Vantage adapter - daily equity cross-check only.

Endpoint: GET https://www.alphavantage.co/query
  function=TIME_SERIES_DAILY, symbol, outputsize=compact (latest 100 days).
The free key allows roughly 25 requests per day, so this is used for a small
rotating sample of US symbols to verify the primary provider's closes, not
as a bulk data source. The key can only be sent as a query parameter; it is
redacted from every log line and stored error.
"""
from typing import List, Optional

from app.providers.base import BaseProvider
from app.providers.parsers import Candle, parse_alpha_vantage_daily

BASE_URL = "https://www.alphavantage.co/query"


class AlphaVantageProvider(BaseProvider):
    name = "alpha_vantage"
    max_attempts = 2

    def __init__(self, api_key: Optional[str], **kwargs):
        super().__init__(**kwargs)
        self._api_key = api_key
        if not api_key:
            self.disable("missing_credentials: ALPHA_VANTAGE_API_KEY is not set")
        self.set_rate_limit(4)  # free tier is also limited per minute

    def fetch_daily(self, symbol: str) -> List[Candle]:
        params = {
            "function": "TIME_SERIES_DAILY",
            "symbol": symbol,
            "outputsize": "compact",
            "apikey": self._api_key or "",
        }
        return self.get_json(
            "TIME_SERIES_DAILY", BASE_URL, params, parse_alpha_vantage_daily,
            symbol=symbol, timeframe="1d",
        )
