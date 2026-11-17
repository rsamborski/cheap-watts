"""Read-only Pstryk API client for fetching dynamic hourly electricity tariffs."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from types import TracebackType
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import SecretStr

from src.clients.errors import ApiClientError, PayloadParseError, parse_json_response
from src.models import PriceSlot

logger = logging.getLogger("cheap_watts.clients.pstryk")

_UNIFIED_METRICS_PATH = "/integrations/meter-data/unified-metrics/"


def _parse_iso_datetime(raw: Any, field_name: str, target_tz: ZoneInfo) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise PayloadParseError(f"Pstryk frame missing valid {field_name}: {raw!r}")
    normalized = raw.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise PayloadParseError(f"Invalid ISO timestamp in Pstryk {field_name}: {raw!r}") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(target_tz)


def _extract_frames(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Locate the list of hourly pricing frames in Pstryk unified-metrics payloads."""
    candidates: list[Any] = [payload.get("frames")]
    metrics = payload.get("metrics")
    if isinstance(metrics, dict):
        pricing = metrics.get("pricing")
        if isinstance(pricing, dict):
            candidates.append(pricing.get("frames"))
        elif isinstance(pricing, list):
            candidates.append(pricing)
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.append(data.get("frames"))

    for candidate in candidates:
        if isinstance(candidate, list):
            if not all(isinstance(item, dict) for item in candidate):
                raise PayloadParseError("Pstryk frames array contains non-object items")
            return candidate

    raise PayloadParseError("Pstryk response did not contain a 'frames' list")


def _normalize_api_token(secret: SecretStr) -> str:
    """Normalize Pstryk API key (raw `sk-...` token expected in `Authorization` header)."""
    token = secret.get_secret_value().strip().strip('"').strip("'")
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


class PstrykClient:
    """HTTP client for Pstryk unified-metrics hourly tariff data."""

    def __init__(
        self,
        api_key: SecretStr,
        base_url: str = "https://api.pstryk.pl",
        tz_name: str = "Europe/Warsaw",
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._auth_token = _normalize_api_token(api_key)
        self._tz = ZoneInfo(tz_name)
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        """Close underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self) -> PstrykClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def fetch_prices(self, window_start: datetime, window_end: datetime) -> list[PriceSlot]:
        """Fetch hourly dynamic tariff slots within `[window_start, window_end)`."""
        if window_end <= window_start:
            raise ValueError("window_end must be strictly after window_start")

        start_utc = window_start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_utc = window_end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            response = self._client.get(
                _UNIFIED_METRICS_PATH,
                params={
                    "metrics": "pricing",
                    "resolution": "hour",
                    "window_start": start_utc,
                    "window_end": end_utc,
                },
                headers={
                    "Authorization": self._auth_token,
                },
            )
        except httpx.HTTPError as exc:
            raise ApiClientError(f"Pstryk network error: {exc.__class__.__name__}") from exc

        payload = parse_json_response(response, service="Pstryk")
        frames = _extract_frames(payload)

        slots: list[PriceSlot] = []
        for frame in frames:
            slot = self._parse_frame(frame)
            if slot is not None:
                slots.append(slot)

        slots.sort(key=lambda item: item.start)
        logger.info(
            "Fetched Pstryk hourly tariffs",
            extra={
                "event": "pstryk_prices_fetched",
                "slot_count": len(slots),
                "window_start": start_utc,
                "window_end": end_utc,
            },
        )
        return slots

    def fetch_today_and_tomorrow(
        self,
        reference_time: datetime | None = None,
    ) -> list[PriceSlot]:
        """Fetch all available hourly tariffs for today and tomorrow in local timezone."""
        now_local = (reference_time or datetime.now(tz=UTC)).astimezone(self._tz)
        today_date = now_local.date()
        after_tomorrow_date = today_date + timedelta(days=2)

        start_local = datetime.combine(today_date, time.min, tzinfo=self._tz)
        end_local = datetime.combine(after_tomorrow_date, time.min, tzinfo=self._tz)

        slots = self.fetch_prices(start_local, end_local)
        has_tomorrow = self.has_next_day_prices(slots, today_date)
        logger.debug(
            "Evaluated Pstryk day-ahead availability",
            extra={
                "event": "pstryk_day_ahead_check",
                "today": today_date.isoformat(),
                "has_tomorrow_prices": has_tomorrow,
            },
        )
        return slots

    @staticmethod
    def has_next_day_prices(slots: Sequence[PriceSlot], reference_date: date) -> bool:
        """Return True if at least one hourly slot exists for `reference_date + 1 day`."""
        tomorrow = reference_date + timedelta(days=1)
        return any(slot.start.date() == tomorrow for slot in slots)

    def _parse_frame(self, frame: dict[str, Any]) -> PriceSlot | None:
        metrics_obj = frame.get("metrics")
        pricing_obj = metrics_obj.get("pricing") if isinstance(metrics_obj, dict) else None
        source: dict[str, Any] = pricing_obj if isinstance(pricing_obj, dict) else frame

        raw_gross = source.get("price_gross")
        if raw_gross is None:
            raw_gross = source.get("full_price")
        if raw_gross is None:
            raw_gross = source.get("price_prosumer_gross")
        if raw_gross is None:
            raw_gross = source.get("price", frame.get("price"))

        # Future hours prior to ~14:00 CET Day-Ahead publication may carry null prices.
        if raw_gross is None:
            return None

        try:
            gross_val = float(raw_gross)
        except (TypeError, ValueError) as exc:
            raise PayloadParseError(f"Invalid Pstryk gross price value: {raw_gross!r}") from exc

        raw_net = source.get("price_net", frame.get("price_net"))
        net_val: float | None = None
        if raw_net is not None:
            try:
                net_val = float(raw_net)
            except (TypeError, ValueError) as exc:
                raise PayloadParseError(f"Invalid Pstryk net price value: {raw_net!r}") from exc

        unit = str(source.get("unit", frame.get("unit", "PLN/kWh"))).strip().upper()
        if unit == "PLN/MWH":
            gross_val = round(gross_val / 1000.0, 6)
            if net_val is not None:
                net_val = round(net_val / 1000.0, 6)

        raw_start = frame.get("start") or frame.get("window_start") or frame.get("timestamp")
        start_dt = _parse_iso_datetime(raw_start, "start", self._tz)

        raw_end = frame.get("end") or frame.get("window_end")
        if raw_end is not None:
            end_dt = _parse_iso_datetime(raw_end, "end", self._tz)
        else:
            end_dt = start_dt + timedelta(hours=1)

        currency = str(source.get("currency") or frame.get("currency") or "PLN").strip().upper()
        return PriceSlot(
            start=start_dt,
            end=end_dt,
            price_pln_kwh=gross_val,
            price_net_pln_kwh=net_val,
            currency=currency,
        )
