"""Binance public market data adapter - crypto candles, no API key.

Endpoint: GET {host}/api/v3/klines?symbol=BTCUSDT&interval=4h|1d&limit<=1000
Hosts are tried in order. `data-api.binance.vision` is Binance's market-data
-only host; `api.binance.com` refuses requests from some regions (HTTP 451),
in which case the next host is tried and, failing all, the adapter reports
itself blocked so the collector can fall back to another provider.
"""
import logging
from datetime import datetime
from typing import List, Optional

from app.providers.base import BaseProvider
from app.providers.parsers import Candle, ProviderError, parse_binance_klines

log = logging.getLogger(__name__)

HOSTS = (
    "https://data-api.binance.vision",
    "https://data.binance.com",
    "https://api.binance.com",
)


class BinanceProvider(BaseProvider):
    name = "binance"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._hosts = list(HOSTS)
        self.set_rate_limit(240)  # far below Binance's per-IP weight budget

    def fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        start: Optional[datetime] = None,
        limit: int = 500,
    ) -> List[Candle]:
        params = {"symbol": symbol, "interval": timeframe, "limit": str(min(limit, 1000))}
        if start is not None:
            params["startTime"] = str(int(start.timestamp() * 1000))
        last_error: Optional[ProviderError] = None
        for host in list(self._hosts):
            try:
                candles = self.get_json(
                    "klines", f"{host}/api/v3/klines", params, parse_binance_klines,
                    symbol=symbol, timeframe=timeframe,
                )
            except ProviderError as exc:
                last_error = exc
                if exc.kind in ("blocked", "network", "timeout", "server_error", "parse_error"):
                    # This host is unusable from here: drop it and try the next.
                    if host in self._hosts and len(self._hosts) > 1:
                        self._hosts.remove(host)
                        log.warning("Binance host %s unusable (%s); trying next host", host, exc.kind)
                        self.disabled_reason = None  # one bad host must not disable the provider
                        self._consecutive_failures = 0
                        continue
                    self.disable(f"blocked: no reachable Binance market-data host ({exc.kind})")
                raise
            return candles
        raise last_error or ProviderError("no Binance host available", kind="blocked")
