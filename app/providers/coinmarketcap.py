"""CoinMarketCap adapter - crypto market information (rank, cap, 24h volume).

Endpoint: GET https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest
  symbol=BTC,ETH,...  convert=USD   header X-CMC_PRO_API_KEY
One call covers the whole crypto list (1 credit per 100 symbols), so a scan
costs one credit. It supplies snapshots, not candles.
"""
from typing import List, Optional, Sequence

from app.providers.base import BaseProvider
from app.providers.parsers import Quote, parse_coinmarketcap_quotes

BASE_URL = "https://pro-api.coinmarketcap.com"


class CoinMarketCapProvider(BaseProvider):
    name = "coinmarketcap"
    max_attempts = 2

    def __init__(self, api_key: Optional[str], **kwargs):
        super().__init__(**kwargs)
        self._api_key = api_key
        if not api_key:
            self.disable("missing_credentials: COINMARKETCAP_API_KEY is not set")

    def fetch_quotes(self, symbols: Sequence[str]) -> List[Quote]:
        if not symbols:
            return []
        return self.get_json(
            "quotes_latest",
            f"{BASE_URL}/v1/cryptocurrency/quotes/latest",
            {"symbol": ",".join(symbols), "convert": "USD", "skip_invalid": "true"},
            parse_coinmarketcap_quotes,
            headers={"X-CMC_PRO_API_KEY": self._api_key or ""},
        )
