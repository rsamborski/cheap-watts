"""Solar PV generation forecast client backed by Open-Meteo irradiance data."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from types import TracebackType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from src.clients.errors import ApiClientError, PayloadParseError, parse_json_response
from src.clients.retry import RetryPolicy, request_with_retry
from src.models import DaylightWindow, SolarForecast, SolarHour

logger = logging.getLogger("cheap_watts.clients.solar")

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


@dataclass(frozen=True)
class SolarArray:
    """Single PV string/plane orientation and rated capacity."""

    kwp: float
    tilt_degrees: float = 35.0
    azimuth_degrees: float = 0.0

    def __post_init__(self) -> None:
        if self.kwp <= 0.0:
            raise ValueError("kwp must be strictly positive")
        if not 0.0 <= self.tilt_degrees <= 90.0:
            raise ValueError("tilt_degrees must be between 0 and 90")
        if not -180.0 <= self.azimuth_degrees <= 180.0:
            raise ValueError("azimuth_degrees must be between -180 and 180")


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
        extra_arrays: Sequence[SolarArray] = (),
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        if not -90.0 <= latitude <= 90.0:
            raise ValueError("latitude must be between -90 and 90")
        if not -180.0 <= longitude <= 180.0:
            raise ValueError("longitude must be between -180 and 180")
        if not 0.0 < system_efficiency <= 1.0:
            raise ValueError("system_efficiency must be in (0, 1]")

        primary_array = SolarArray(
            kwp=kwp,
            tilt_degrees=tilt_degrees,
            azimuth_degrees=azimuth_degrees,
        )
        self._latitude = latitude
        self._longitude = longitude
        self._arrays: tuple[SolarArray, ...] = (primary_array, *extra_arrays)
        self._kwp = sum(arr.kwp for arr in self._arrays)
        self._efficiency = system_efficiency
        self._tz = ZoneInfo(tz_name)
        self._retry_policy = retry_policy or RetryPolicy()
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

        per_array_results = [
            (array, self._fetch_single_array(array, forecast_days, include_daily=(idx == 0)))
            for idx, array in enumerate(self._arrays)
        ]
        per_array_hours = [(array, hours) for array, (hours, _) in per_array_results]
        daylight_windows = per_array_results[0][1][1]
        if len(per_array_hours) == 1:
            hours = per_array_hours[0][1]
        else:
            hours = self._merge_array_hours(per_array_hours)

        hours.sort(key=lambda item: item.start)
        forecast = SolarForecast(
            hours=hours,
            daylight_windows=daylight_windows,
            fetched_at=datetime.now(tz=UTC),
        )
        logger.info(
            "Fetched Open-Meteo solar forecast",
            extra={
                "event": "solar_forecast_fetched",
                "hour_count": len(hours),
                "daylight_days": len(daylight_windows),
                "total_kwh": forecast.total_kwh,
                "kwp": self._kwp,
                "arrays": len(self._arrays),
            },
        )
        return forecast

    def _fetch_single_array(
        self,
        array: SolarArray,
        forecast_days: int,
        include_daily: bool = True,
    ) -> tuple[list[SolarHour], list[DaylightWindow]]:
        """Fetch and convert hourly irradiance (and optional daily sun bounds) for a PV plane."""
        params: dict[str, Any] = {
            "latitude": self._latitude,
            "longitude": self._longitude,
            "hourly": "shortwave_radiation,global_tilted_irradiance",
            "tilt": array.tilt_degrees,
            "azimuth": array.azimuth_degrees,
            "timezone": "UTC",
            "forecast_days": forecast_days,
        }
        if include_daily:
            params["daily"] = "sunrise,sunset"

        try:
            response = request_with_retry(
                self._client,
                "GET",
                _FORECAST_PATH,
                service="Open-Meteo",
                policy=self._retry_policy,
                params=params,
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
            raw_kwh = (irradiance / 1000.0) * array.kwp * self._efficiency
            estimated_kwh = round(min(array.kwp, max(0.0, raw_kwh)), 4)

            hours.append(
                SolarHour(
                    start=start_dt,
                    end=start_dt + timedelta(hours=1),
                    irradiance_wm2=irradiance,
                    estimated_kwh=estimated_kwh,
                )
            )
        daylight_windows = (
            self._parse_daylight_windows(payload.get("daily"), response_tz, self._tz)
            if include_daily
            else []
        )
        return hours, daylight_windows

    @staticmethod
    def _parse_daylight_windows(
        daily: Any,
        response_tz: tzinfo,
        target_tz: ZoneInfo,
    ) -> list[DaylightWindow]:
        if daily is None:
            return []
        if not isinstance(daily, dict):
            raise PayloadParseError("Open-Meteo 'daily' must be an object")

        sunrises = daily.get("sunrise")
        sunsets = daily.get("sunset")
        if sunrises is None and sunsets is None:
            return []
        if not isinstance(sunrises, list) or not isinstance(sunsets, list):
            raise PayloadParseError("Open-Meteo 'daily.sunrise' and 'daily.sunset' length mismatch")
        if len(sunrises) != len(sunsets):
            raise PayloadParseError("Open-Meteo 'daily.sunrise' and 'daily.sunset' length mismatch")

        windows: list[DaylightWindow] = []
        for raw_sr, raw_ss in zip(sunrises, sunsets, strict=True):
            if raw_sr is None or raw_ss is None:
                continue
            sunrise_dt = _parse_open_meteo_time(raw_sr, response_tz, target_tz)
            sunset_dt = _parse_open_meteo_time(raw_ss, response_tz, target_tz)
            if sunset_dt < sunrise_dt:
                raise PayloadParseError("Open-Meteo sunset precedes sunrise")
            windows.append(DaylightWindow(sunrise=sunrise_dt, sunset=sunset_dt))

        windows.sort(key=lambda item: item.sunrise)
        return windows

    @staticmethod
    def _merge_array_hours(
        per_array_hours: Sequence[tuple[SolarArray, list[SolarHour]]],
    ) -> list[SolarHour]:
        """Combine hourly forecasts across multiple PV array planes by timestamp."""
        buckets: dict[datetime, list[tuple[SolarArray, SolarHour]]] = {}
        for array, hours in per_array_hours:
            for hour in hours:
                buckets.setdefault(hour.start, []).append((array, hour))

        merged: list[SolarHour] = []
        for start_dt, entries in buckets.items():
            total_kwp = sum(arr.kwp for arr, _ in entries)
            weighted_irradiance = (
                sum(arr.kwp * hr.irradiance_wm2 for arr, hr in entries) / total_kwp
                if total_kwp > 0.0
                else 0.0
            )
            total_kwh = sum(hr.estimated_kwh for _, hr in entries)
            merged.append(
                SolarHour(
                    start=start_dt,
                    end=entries[0][1].end,
                    irradiance_wm2=round(weighted_irradiance, 2),
                    estimated_kwh=round(total_kwh, 4),
                )
            )
        return merged

    @staticmethod
    def _parse_irradiance(raw: Any) -> float:
        if raw is None:
            return 0.0
        try:
            val = float(raw)
        except (TypeError, ValueError) as exc:
            raise PayloadParseError(f"Invalid Open-Meteo irradiance value: {raw!r}") from exc
        return max(0.0, round(val, 2))
