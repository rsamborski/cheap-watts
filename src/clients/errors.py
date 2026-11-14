"""Shared exceptions and HTTP response validation helpers for external API clients."""

from __future__ import annotations

from typing import Any

import httpx


class ApiClientError(RuntimeError):
    """Base error raised when an upstream API request or response fails."""


class AuthenticationError(ApiClientError):
    """Raised on HTTP 401/403 or upstream authentication rejection."""


class RateLimitError(ApiClientError):
    """Raised on HTTP 429 Too Many Requests."""


class PayloadParseError(ApiClientError):
    """Raised when an upstream response body cannot be parsed into expected models."""


def parse_json_response(response: httpx.Response, service: str) -> dict[str, Any]:
    """Validate HTTP status code and decode a top-level JSON object."""
    status = response.status_code
    if status in (401, 403):
        raise AuthenticationError(f"{service} authentication failed (HTTP {status})")
    if status == 429:
        raise RateLimitError(f"{service} rate limit exceeded (HTTP 429)")
    if status >= 400:
        raise ApiClientError(f"{service} request failed with HTTP {status}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise PayloadParseError(f"{service} returned invalid JSON") from exc

    if not isinstance(payload, dict):
        raise PayloadParseError(f"{service} expected JSON object, got {type(payload).__name__}")
    return payload
