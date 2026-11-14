"""Solar PV generation forecast client backed by Open-Meteo irradiance data."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, tzinfo
from types import TracebackType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from src.clients.errors import ApiClientError, PayloadParseError, parse_json_response
from src.models import SolarForecast, SolarHour

logger = logging.getLogger("pstryk_pilot.clients.solar")

_FORECAST_PATH = "/v1/forecast"


def _resolve_response_tz(raw_tz: Any) -> tzinfo:
    if not isinstance(raw_tz, str) or raw_tz.strip().upper() in {"", "UTC", "GMT", "GMT+0"}:
        return UTC
    try:
        return ZoneInfo(raw_tz.strip())
    except ZoneInfoNotFoundError:
        return UTC


def _parse_open_meteo_time(raw: Any, response_tz: tzinfo, target_tz: ZoneInfo) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise PayloadParseError(f"Open-Meteo hourly entry missing timestamp: {raw!r}")
    normalized = raw.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise PayloadParseError(f"Invalid Open-Meteo timestamp: {raw!r}") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        dt = dt.replace(tzinfo=response_tz)
    return dt.astimezone(target_tz)


class SolarForecastClient:
    """Fetches hourly solar irradiance from Open-Meteo and estimates PV kWh output."""

    def __init__(
        self,
        latitude: float,
        longitude: float,
        kwp: float,
        base_url: str = "https://api.open-meteo.com",
        tilt_degrees: float = 35.0,
        azimuth_degrees: float = 0.0,
        system_efficiency: float = 0.85,
        tz_name: str = "Europe/Warsaw",
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not -90.0 <= latitude <= 90.0:
            raise ValueError("latitude must be between -90 and 90")
        if not -180.0 <= longitude <= 180.0:
            raise ValueError("longitude must be between -180 and 180")
        if kwp <= 0.0:
            raise ValueError("kwp must be strictly positive")
        if not 0.0 < system_efficiency <= 1.0:
            raise ValueError("system_efficiency must be in (0, 1]")

        self._latitude = latitude
        self._longitude = longitude
        self._kwp = kwp
        self._tilt = tilt_degrees
        self._azimuth = azimuth_degrees
        self._efficiency = system_efficiency
        self._tz = ZoneInfo(tz_name)
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        """Close underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self) -> SolarForecastClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def fetch_forecast(self, forecast_days: int = 2) -> SolarForecast:
        """Fetch hourly irradiance for `forecast_days` and compute PV generation kWh."""
        if not 1 <= forecast_days <= 7:
            raise ValueError("forecast_days must be between 1 and 7")

        try:
            response = self._client.get(
                _FORECAST_PATH,
                params={
                    "latitude": self._latitude,
                    "longitude": self._longitude,
                    "hourly": "shortwave_radiation,global_tilted_irradiance",
                    "tilt": self._tilt,
                    "azimuth": self._azimuth,
                    "timezone": "UTC",
                    "forecast_days": forecast_days,
                },
            )
        except httpx.HTTPError as exc:
            raise ApiClientError(
                f"Open-Meteo solar forecast network error: {exc.__class__.__name__}"
            ) from exc

        payload = parse_json_response(response, service="Open-Meteo")
        hourly = payload.get("hourly")
        if not isinstance(hourly, dict):
            raise PayloadParseError("Open-Meteo response missing 'hourly' object")

        times = hourly.get("time")
        if not isinstance(times, list) or not times:
            raise PayloadParseError("Open-Meteo 'hourly.time' must be a non-empty list")

        ghi_list = hourly.get("shortwave_radiation")
        gti_list = hourly.get("global_tilted_irradiance")
        if not isinstance(ghi_list, list) and not isinstance(gti_list, list):
            raise PayloadParseError(
                "Open-Meteo response missing both 'shortwave_radiation' and "
                "'global_tilted_irradiance' arrays"
            )

        expected_len = len(times)
        if isinstance(ghi_list, list) and len(ghi_list) != expected_len:
            raise PayloadParseError("Open-Meteo 'shortwave_radiation' length mismatch")
        if isinstance(gti_list, list) and len(gti_list) != expected_len:
            raise PayloadParseError("Open-Meteo 'global_tilted_irradiance' length mismatch")

        response_tz = _resolve_response_tz(payload.get("timezone"))
        hours: list[SolarHour] = []
        for idx, raw_ts in enumerate(times):
            start_dt = _parse_open_meteo_time(raw_ts, response_tz, self._tz)
            raw_irradiance = None
            if isinstance(gti_list, list) and gti_list[idx] is not None:
                raw_irradiance = gti_list[idx]
            elif isinstance(ghi_list, list) and ghi_list[idx] is not None:
                raw_irradiance = ghi_list[idx]

            irradiance = self._parse_irradiance(raw_irradiance)
            raw_kwh = (irradiance / 1000.0) * self._kwp * self._efficiency
            estimated_kwh = round(min(self._kwp, max(0.0, raw_kwh)), 4)

            hours.append(
                SolarHour(
                    start=start_dt,
                    end=start_dt + timedelta(hours=1),
                    irradiance_wm2=irradiance,
                    estimated_kwh=estimated_kwh,
                )
            )

        hours.sort(key=lambda item: item.start)
        forecast = SolarForecast(hours=hours, fetched_at=datetime.now(tz=UTC))
        logger.info(
            "Fetched Open-Meteo solar forecast",
            extra={
                "event": "solar_forecast_fetched",
                "hour_count": len(hours),
                "total_kwh": forecast.total_kwh,
                "kwp": self._kwp,
            },
        )
        return forecast

    @staticmethod
    def _parse_irradiance(raw: Any) -> float:
        if raw is None:
            return 0.0
        try:
            val = float(raw)
        except (TypeError, ValueError) as exc:
            raise PayloadParseError(f"Invalid Open-Meteo irradiance value: {raw!r}") from exc
        return max(0.0, round(val, 2))
