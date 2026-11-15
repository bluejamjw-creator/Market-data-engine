"""Shared HTTP plumbing for provider adapters.

Handles timeouts, retries with exponential backoff, per-minute throttling,
a per-run circuit breaker and an audit trail of every call. API keys are
never written to logs or to the audit trail.
"""
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, List, Optional

import httpx

from app.logging_setup import redact
from app.providers.parsers import ProviderError
from app.timeutil import utcnow

log = logging.getLogger(__name__)

# Errors that mean "stop calling this provider for the rest of this run".
_FATAL_KINDS = {"missing_credentials", "invalid_credentials", "quota_exhausted", "blocked"}
_RETRY_KINDS = {"timeout", "network", "server_error"}


@dataclass
class RequestRecord:
    provider: str
    endpoint: str
    symbol: Optional[str]
    timeframe: Optional[str]
    success: bool
    status_code: Optional[int]
    error_kind: Optional[str]
    error_message: Optional[str]
    attempts: int
    rows_returned: Optional[int]
    duration_ms: float
    requested_at: datetime = field(default_factory=utcnow)


class BaseProvider:
    name = "base"
    max_attempts = 3
    consecutive_failure_limit = 6

    def __init__(
        self,
        timeout: float = 20.0,
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=10.0),
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "market-data-engine/1.0"},
        )
        self._sleep = sleep
        self.records: List[RequestRecord] = []
        self.disabled_reason: Optional[str] = None
        self.successes = 0
        self.failures = 0
        self._consecutive_failures = 0
        self._min_interval = 0.0
        self._last_call = 0.0

    # ------------------------------------------------------------- lifecycle
    @property
    def available(self) -> bool:
        return self.disabled_reason is None

    def disable(self, reason: str) -> None:
        if self.disabled_reason is None:
            self.disabled_reason = redact(reason)[:300]
            log.warning("Provider %s disabled for this run: %s", self.name, self.disabled_reason)

    def close(self) -> None:
        self._client.close()

    def drain_records(self) -> List[RequestRecord]:
        records, self.records = self.records, []
        return records

    def set_rate_limit(self, calls_per_minute: float) -> None:
        self._min_interval = 60.0 / calls_per_minute if calls_per_minute > 0 else 0.0

    # --------------------------------------------------------------- request
    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        wait = self._min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            self._sleep(wait)
        self._last_call = time.monotonic()

    def _http_get(self, url: str, params: dict, headers: Optional[dict]) -> httpx.Response:
        try:
            return self._client.get(url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"request timed out ({type(exc).__name__})", kind="timeout") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"network error ({type(exc).__name__})", kind="network") from exc

    def get_json(
        self,
        endpoint: str,
        url: str,
        params: dict,
        parse: Callable[[Any], Any],
        headers: Optional[dict] = None,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
    ) -> Any:
        """GET `url`, parse the JSON body with `parse`, and audit the call.

        Transient failures (timeouts, network errors, HTTP 5xx) are retried
        with exponential backoff. Anything else fails once, so quota is not
        burned repeating a request that cannot succeed.
        """
        if not self.available:
            raise ProviderError(f"{self.name} unavailable: {self.disabled_reason}", kind="blocked")
        started = time.monotonic()
        attempts = 0
        status_code: Optional[int] = None
        error: Optional[ProviderError] = None
        result: Any = None
        while attempts < self.max_attempts:
            attempts += 1
            error = None
            try:
                self._throttle()
                response = self._http_get(url, params, headers)
                status_code = response.status_code
                if status_code >= 500:
                    raise ProviderError(f"HTTP {status_code} from {self.name}", "server_error", status_code)
                try:
                    payload = response.json()
                except ValueError as exc:
                    if status_code in (403, 451):
                        raise ProviderError(f"HTTP {status_code} from {self.name}", "blocked", status_code) from exc
                    if status_code == 429:
                        raise ProviderError("HTTP 429 rate limited", "rate_limited", 429) from exc
                    raise ProviderError(f"non-JSON response (HTTP {status_code})", "parse_error", status_code) from exc
                try:
                    result = parse(payload)
                except ProviderError as exc:
                    if exc.status_code is None:
                        exc.status_code = status_code
                    if status_code == 451:
                        exc.kind = "blocked"
                    raise
                if status_code >= 400:
                    kind = {401: "invalid_credentials", 403: "blocked", 404: "not_found",
                            429: "rate_limited", 451: "blocked"}.get(status_code, "http_error")
                    raise ProviderError(f"HTTP {status_code} from {self.name}", kind, status_code)
                break
            except ProviderError as exc:
                error = exc
                if exc.kind in _RETRY_KINDS and attempts < self.max_attempts:
                    delay = min(30.0, (2 ** (attempts - 1)) + random.uniform(0, 0.5))
                    log.warning("%s %s %s failed (%s); retry %d in %.1fs",
                                self.name, endpoint, symbol or "", exc.kind, attempts, delay)
                    self._sleep(delay)
                    continue
                break

        duration_ms = (time.monotonic() - started) * 1000.0
        rows = len(result) if error is None and hasattr(result, "__len__") else None
        self.records.append(
            RequestRecord(
                provider=self.name,
                endpoint=endpoint,
                symbol=symbol,
                timeframe=timeframe,
                success=error is None,
                status_code=status_code,
                error_kind=error.kind if error else None,
                error_message=redact(str(error))[:500] if error else None,
                attempts=attempts,
                rows_returned=rows,
                duration_ms=round(duration_ms, 1),
            )
        )
        if error is not None:
            self.failures += 1
            self._consecutive_failures += 1
            if error.kind in _FATAL_KINDS:
                self.disable(f"{error.kind}: {error}")
            elif self._consecutive_failures >= self.consecutive_failure_limit:
                self.disable(f"{self._consecutive_failures} consecutive failures (last: {error.kind})")
            raise error
        if self._consecutive_failures:
            log.info("Provider %s recovered after %d failure(s)", self.name, self._consecutive_failures)
        self._consecutive_failures = 0
        self.successes += 1
        return result
