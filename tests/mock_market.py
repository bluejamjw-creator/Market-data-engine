"""A fake market-data API for tests, served through httpx.MockTransport.

Answers the same URLs the real adapters call, with synthetic candles, so the
whole pipeline can be exercised with no network access and no API quota.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
import pandas as pd

from tests.helpers import base_under_pivot_closes, uptrend_closes

UTC = timezone.utc
FAKE_KEYS = ("td-test-key-0001", "av-test-key-0002", "cmc-test-key-0003")


def _seed(symbol: str) -> int:
    return sum(ord(ch) for ch in symbol) % 97 + 1


def _closes(symbol: str, bars: int) -> np.ndarray:
    base = symbol.replace("USDT", "").replace("/USD", "")
    if base in ("NVDA", "ETH"):
        return base_under_pivot_closes(bars - 14, seed=_seed(base))
    return uptrend_closes(bars, start=50.0 + _seed(base), seed=_seed(base))


class MockMarket:
    def __init__(self, now: datetime, binance_blocked_hosts=()):
        self.now = now
        self.calls = Counter()
        self.requests = []
        self.binance_blocked_hosts = set(binance_blocked_hosts)
        self.transport = httpx.MockTransport(self._handle)

    # ------------------------------------------------------------ series
    def equity_index(self, interval: str) -> pd.DatetimeIndex:
        days = pd.bdate_range(end=self.now.date(), periods=260, tz="UTC")
        if interval == "1day":
            return days
        stamps = []
        for day in days[-70:]:
            for hour, minute in ((14, 30), (18, 30)):
                stamp = day + timedelta(hours=hour, minutes=minute)
                if stamp <= self.now:
                    stamps.append(stamp)
        return pd.DatetimeIndex(stamps)

    def crypto_index(self, interval: str) -> pd.DatetimeIndex:
        if interval == "1d":
            end = self.now.replace(hour=0, minute=0, second=0, microsecond=0)
            return pd.date_range(end=end, periods=300, freq="D", tz="UTC")
        end = self.now.replace(hour=self.now.hour - self.now.hour % 4, minute=0, second=0, microsecond=0)
        return pd.date_range(end=end, periods=300, freq="4h", tz="UTC")

    def series(self, symbol: str, index: pd.DatetimeIndex) -> pd.DataFrame:
        closes = _closes(symbol, len(index))
        opens = np.r_[closes[0], closes[:-1]]
        change = np.r_[0.0, np.diff(closes)]
        return pd.DataFrame({
            "open": opens,
            "high": np.maximum(opens, closes) * 1.004,
            "low": np.minimum(opens, closes) * 0.996,
            "close": closes,
            "volume": np.where(change >= 0, 6_000_000.0, 3_500_000.0),
        }, index=index)

    # ----------------------------------------------------------- handler
    def _handle(self, request: httpx.Request) -> httpx.Response:
        host, path, params = request.url.host, request.url.path, dict(request.url.params)
        self.requests.append(request)
        if host == "api.twelvedata.com":
            if request.headers.get("Authorization") != "apikey td-test-key-0001":
                return httpx.Response(401, json={"code": 401, "message": "**apikey** parameter is incorrect", "status": "error"})
            if path == "/api_usage":
                self.calls["td_usage"] += 1
                return httpx.Response(200, json={"current_usage": 1, "plan_limit": 8, "daily_usage": 12,
                                                 "plan_daily_limit": 800, "plan_category": "basic"})
            symbol, interval, mic = params["symbol"], params["interval"], params.get("mic_code")
            self.calls[f"td:{symbol}:{interval}"] += 1
            if mic:
                self.calls[f"td_mic:{mic}"] += 1
                return httpx.Response(200, json={
                    "code": 403, "status": "error",
                    "message": f"**symbol** {symbol} is available starting with Grow plan. Consider upgrading."})
            frame = self.series(symbol, self.equity_index(interval))
            if "start_date" in params:
                start = datetime.strptime(params["start_date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                frame = frame[frame.index >= start]
            fmt = "%Y-%m-%d" if interval == "1day" else "%Y-%m-%d %H:%M:%S"
            values = [
                {"datetime": ts.strftime(fmt), "open": f"{r.open:.5f}", "high": f"{r.high:.5f}",
                 "low": f"{r.low:.5f}", "close": f"{r.close:.5f}", "volume": str(int(r.volume))}
                for ts, r in frame.iterrows()
            ]
            return httpx.Response(200, json={"meta": {"symbol": symbol}, "values": values, "status": "ok"})
        if path == "/api/v3/klines":
            self.calls[f"binance_host:{host}"] += 1
            if host in self.binance_blocked_hosts:
                return httpx.Response(451, json={
                    "code": 0, "msg": "Service unavailable from a restricted location according to 'b. Eligibility'"})
            symbol, interval = params["symbol"], params["interval"]
            self.calls[f"binance:{symbol}:{interval}"] += 1
            frame = self.series(symbol, self.crypto_index(interval))
            if "startTime" in params:
                start = datetime.fromtimestamp(int(params["startTime"]) / 1000, tz=UTC)
                frame = frame[frame.index >= start]
            step = 86_400_000 if interval == "1d" else 14_400_000
            rows = [
                [int(ts.timestamp() * 1000), f"{r.open:.6f}", f"{r.high:.6f}", f"{r.low:.6f}", f"{r.close:.6f}",
                 f"{r.volume:.2f}", int(ts.timestamp() * 1000) + step - 1, "0", 10, "0", "0", "0"]
                for ts, r in frame.iterrows()
            ]
            return httpx.Response(200, json=rows)
        if host == "pro-api.coinmarketcap.com":
            self.calls["cmc"] += 1
            if request.headers.get("X-CMC_PRO_API_KEY") != "cmc-test-key-0003":
                return httpx.Response(401, json={"status": {"error_code": 1001, "error_message": "This API Key is invalid."}})
            data = {}
            for rank, symbol in enumerate(params["symbol"].split(","), start=1):
                data[symbol] = {"symbol": symbol, "cmc_rank": rank, "quote": {"USD": {
                    "price": 100.0 + rank, "volume_24h": 1e9, "market_cap": 1e11 / rank,
                    "percent_change_24h": -1.0, "percent_change_7d": -5.0,
                    "last_updated": self.now.isoformat().replace("+00:00", "Z")}}}
            return httpx.Response(200, json={"status": {"error_code": 0}, "data": data})
        if host == "www.alphavantage.co":
            self.calls["av"] += 1
            assert params.get("apikey") == "av-test-key-0002"
            frame = self.series(params["symbol"], self.equity_index("1day")).tail(100)
            series = {
                ts.strftime("%Y-%m-%d"): {"1. open": f"{r.open:.4f}", "2. high": f"{r.high:.4f}",
                                         "3. low": f"{r.low:.4f}", "4. close": f"{r.close:.4f}",
                                         "5. volume": str(int(r.volume))}
                for ts, r in frame.iterrows()
            }
            return httpx.Response(200, json={"Meta Data": {}, "Time Series (Daily)": series})
        return httpx.Response(404, json={"message": "unexpected url in test"})

    def candle_calls(self) -> int:
        return sum(count for key, count in self.calls.items() if key.startswith(("td:", "binance:")))
