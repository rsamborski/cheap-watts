"""Offline unit tests for the Open-Meteo solar forecast client."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from src.clients.errors import ApiClientError, PayloadParseError
from src.clients.solar import SolarForecastClient

FIXTURES = Path(__file__).parent / "fixtures"


def test_solar_forecast_parses_open_meteo_fixture_and_converts_timezone() -> None:
    fixture_bytes = (FIXTURES / "open_meteo_forecast.json").read_bytes()
    captured_params: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params.update(dict(request.url.params))
        assert request.url.path == "/v1/forecast"
        return httpx.Response(200, content=fixture_bytes)

    with SolarForecastClient(
        latitude=52.23,
        longitude=21.01,
        kwp=10.0,
        tilt_degrees=35.0,
        azimuth_degrees=0.0,
        system_efficiency=0.85,
        tz_name="Europe/Warsaw",
        transport=httpx.MockTransport(handler),
    ) as client:
        forecast = client.fetch_forecast(forecast_days=2)

    assert captured_params["latitude"] == "52.23"
    assert captured_params["longitude"] == "21.01"
    assert captured_params["timezone"] == "UTC"
    assert len(forecast.hours) == 6

    # 2026-06-15T03:00 UTC -> 05:00 CEST (+02:00), night 0 W/m2
    assert forecast.hours[0].start.hour == 5
    assert forecast.hours[0].estimated_kwh == 0.0

    # 2026-06-15T10:00 UTC -> 12:00 CEST, GTI = 950 W/m2 -> 0.95 * 10 * 0.85 = 8.075 kWh
    peak_hour = forecast.hours[3]
    assert peak_hour.start.hour == 12
    assert peak_hour.irradiance_wm2 == pytest.approx(950.0)
    assert peak_hour.estimated_kwh == pytest.approx(8.075)

    assert forecast.total_kwh_for_date(date(2026, 6, 15)) == pytest.approx(25.67, abs=0.01)


def test_solar_forecast_ghi_fallback_clamping_and_validation() -> None:
    with pytest.raises(ValueError, match="latitude"):
        SolarForecastClient(latitude=95.0, longitude=21.0, kwp=5.0)
    with pytest.raises(ValueError, match="longitude"):
        SolarForecastClient(latitude=52.0, longitude=190.0, kwp=5.0)
    with pytest.raises(ValueError, match="kwp"):
        SolarForecastClient(latitude=52.0, longitude=21.0, kwp=0.0)
    with pytest.raises(ValueError, match="system_efficiency"):
        SolarForecastClient(latitude=52.0, longitude=21.0, kwp=5.0, system_efficiency=0.0)

    payload = {
        "timezone": "Europe/Warsaw",
        "hourly": {
            "time": ["2026-06-15T12:00", "2026-06-15T13:00"],
            "shortwave_radiation": [1250.0, None],
            "global_tilted_irradiance": [None, -2.0],
        },
    }
    with SolarForecastClient(
        latitude=52.23,
        longitude=21.01,
        kwp=6.0,
        system_efficiency=1.0,
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=payload)),
    ) as client:
        with pytest.raises(ValueError, match="forecast_days"):
            client.fetch_forecast(forecast_days=0)
        forecast = client.fetch_forecast(forecast_days=1)

    # 1250 W/m2 * 6 kWp * 1.0 = 7.5 kWh -> clamped at 6.0 kWp rating
    assert forecast.hours[0].estimated_kwh == pytest.approx(6.0)
    # Negative float noise clamped to 0.0
    assert forecast.hours[1].irradiance_wm2 == 0.0
    assert forecast.hours[1].estimated_kwh == 0.0


def test_solar_forecast_error_handling() -> None:
    bad_length_payload = {
        "hourly": {
            "time": ["2026-06-15T12:00", "2026-06-15T13:00"],
            "shortwave_radiation": [500.0],
        }
    }
    with (
        SolarForecastClient(
            latitude=52.23,
            longitude=21.01,
            kwp=6.0,
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=bad_length_payload)),
        ) as client,
        pytest.raises(PayloadParseError, match="length mismatch"),
    ):
        client.fetch_forecast()

    def raise_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    with (
        SolarForecastClient(
            latitude=52.23,
            longitude=21.01,
            kwp=6.0,
            transport=httpx.MockTransport(raise_timeout),
        ) as net_client,
        pytest.raises(ApiClientError, match="network error"),
    ):
        net_client.fetch_forecast()


def test_solar_forecast_additional_malformed_payloads() -> None:
    # Unknown timezone string falls back to UTC + Z-suffixed timestamp
    z_payload = {
        "timezone": "Invalid/NoSuchZone",
        "hourly": {
            "time": ["2026-06-15T10:00Z"],
            "shortwave_radiation": [500.0],
        },
    }
    with SolarForecastClient(
        latitude=52.23,
        longitude=21.01,
        kwp=5.0,
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=z_payload)),
    ) as client:
        forecast = client.fetch_forecast(forecast_days=1)
    assert len(forecast.hours) == 1

    bad_payloads = [
        {"no_hourly": True},
        {"hourly": {"time": []}},
        {"hourly": {"time": ["2026-06-15T10:00"]}},
        {
            "hourly": {
                "time": ["2026-06-15T10:00"],
                "shortwave_radiation": [500.0],
                "global_tilted_irradiance": [500.0, 600.0],
            }
        },
        {"hourly": {"time": [""], "shortwave_radiation": [100.0]}},
        {"hourly": {"time": ["not-iso"], "shortwave_radiation": [100.0]}},
        {"hourly": {"time": ["2026-06-15T10:00"], "shortwave_radiation": ["bad"]}},
    ]
    for bad in bad_payloads:
        with (
            SolarForecastClient(
                latitude=52.23,
                longitude=21.01,
                kwp=5.0,
                transport=httpx.MockTransport(lambda _r, p=bad: httpx.Response(200, json=p)),
            ) as client,
            pytest.raises(PayloadParseError),
        ):
            client.fetch_forecast(forecast_days=1)
