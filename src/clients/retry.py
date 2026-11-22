"""Retry policy and exponential backoff helpers for transient API/network failures."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from src.config import Settings

logger = logging.getLogger("cheap_watts.clients.retry")

DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 2.0
DEFAULT_MAX_BACKOFF_SECONDS = 30.0

_RETRYABLE_HTTPX_EXCEPTIONS: tuple[type[httpx.HTTPError], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
)


def _default_sleep(seconds: float) -> None:
    """Sleep helper isolated for deterministic monkeypatching in unit tests."""
    time.sleep(seconds)


def is_retryable_status(status_code: int) -> bool:
    """Return True for transient HTTP status codes (408, 429, and 5xx except 501/505)."""
    if status_code in (408, 429):
        return True
    return 500 <= status_code <= 599 and status_code not in (501, 505)


def is_retryable_http_exception(exc: BaseException) -> bool:
    """Return True for transient httpx transport/timeout errors."""
    return isinstance(exc, _RETRYABLE_HTTPX_EXCEPTIONS)


def _parse_retry_after(raw_value: str | None) -> float | None:
    """Parse an HTTP `Retry-After` header (numeric seconds or HTTP-date) into delay seconds."""
    if not raw_value:
        return None
    cleaned = raw_value.strip()
    if not cleaned:
        return None
    try:
        seconds = float(cleaned)
    except ValueError:
        pass
    else:
        if math.isfinite(seconds) and seconds >= 0.0:
            return seconds
        return None

    try:
        dt = parsedate_to_datetime(cleaned)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        dt = dt.replace(tzinfo=UTC)
    return max(0.0, (dt - datetime.now(tz=UTC)).total_seconds())


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff configuration for external HTTP API requests."""

    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS
    max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS
    sleep_fn: Callable[[float], None] | None = None

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.backoff_seconds < 0.0:
            raise ValueError("backoff_seconds must be >= 0.0")
        if self.max_backoff_seconds < 0.0:
            raise ValueError("max_backoff_seconds must be >= 0.0")

    @classmethod
    def from_settings(cls, settings: Settings) -> RetryPolicy:
        """Build a RetryPolicy from application configuration settings."""
        return cls(
            max_retries=settings.api_max_retries,
            backoff_seconds=settings.api_retry_backoff_seconds,
        )

    def compute_delay(self, retry_index: int, retry_after: float | None = None) -> float:
        """Compute backoff delay in seconds for the 0-based `retry_index`."""
        delay = self.backoff_seconds * (2**retry_index)
        if retry_after is not None:
            delay = max(delay, retry_after)
        return min(self.max_backoff_seconds, max(0.0, delay))

    def sleep(self, seconds: float) -> None:
        """Pause execution for `seconds` using the configured sleep callback."""
        sleeper = self.sleep_fn or _default_sleep
        sleeper(seconds)


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    service: str,
    policy: RetryPolicy | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Execute an HTTP request with exponential backoff retries on transient failures."""
    active_policy = policy or RetryPolicy()
    max_retries = active_policy.max_retries
    attempt = 0

    while True:
        try:
            response = client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            if attempt >= max_retries or not is_retryable_http_exception(exc):
                raise
            delay = active_policy.compute_delay(attempt)
            logger.warning(
                f"{service} request failed ({exc.__class__.__name__}); "
                f"retrying in {delay:.1f}s ({attempt + 1}/{max_retries})",
                extra={
                    "event": "api_retry",
                    "service": service,
                    "retry": attempt + 1,
                    "max_retries": max_retries,
                    "delay_seconds": round(delay, 3),
                    "reason": exc.__class__.__name__,
                },
            )
            active_policy.sleep(delay)
            attempt += 1
            continue

        if is_retryable_status(response.status_code) and attempt < max_retries:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            status_code = response.status_code
            response.close()
            delay = active_policy.compute_delay(attempt, retry_after=retry_after)
            logger.warning(
                f"{service} returned HTTP {status_code}; "
                f"retrying in {delay:.1f}s ({attempt + 1}/{max_retries})",
                extra={
                    "event": "api_retry",
                    "service": service,
                    "retry": attempt + 1,
                    "max_retries": max_retries,
                    "delay_seconds": round(delay, 3),
                    "reason": f"HTTP {status_code}",
                },
            )
            active_policy.sleep(delay)
            attempt += 1
            continue

        if attempt > 0 and response.status_code < 400:
            logger.info(
                f"{service} request recovered on retry {attempt}/{max_retries}",
                extra={
                    "event": "api_retry_recovered",
                    "service": service,
                    "retry": attempt,
                    "max_retries": max_retries,
                },
            )
        return response
