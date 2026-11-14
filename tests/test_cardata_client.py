"""Offline unit tests for the BMW CarData API client (BMW & MINI)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from src.clients.cardata import CarDataClient, CarDataVehicleConfig
from src.clients.errors import ApiClientError, AuthenticationError, PayloadParseError

FIXTURES = Path(__file__).parent / "fixtures"


def test_cardata_fetches_bmw_and_mini_with_oauth_and_vin_discovery() -> None:
    oauth_bytes = (FIXTURES / "cardata_oauth_token.json").read_bytes()
    bmw_bytes = (FIXTURES / "cardata_bmw_telematic.json").read_bytes()
    mini_bytes = (FIXTURES / "cardata_mini_telematic.json").read_bytes()

    mappings_payload = {
        "mappings": [
            {"vin": "WBA0000000EXAMPLE9", "mappingType": "SECONDARY"},
            {"vin": "WBA0000000EXAMPLE1", "mappingType": "PRIMARY"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gcdm/oauth/token":
            body = request.content.decode("utf-8")
            assert "grant_type=refresh_token" in body
            assert "refresh_token=test-api-key" in body
            return httpx.Response(200, content=oauth_bytes)
        if request.url.path == "/v1/customers/vehicles/mappings":
            assert request.headers["authorization"] == "Bearer test-api-key"
            assert request.headers["x-version"] == "v1"
            return httpx.Response(200, json=mappings_payload)
        if request.url.path == "/v1/customers/vehicles/WBA0000000EXAMPLE1/telematicData":
            return httpx.Response(200, content=bmw_bytes)
        if request.url.path == "/v1/customers/vehicles/WMW0000000EXAMPLE2/telematicData":
            return httpx.Response(200, content=mini_bytes)
        raise AssertionError(f"Unexpected CarData URL: {request.url}")

    bmw_cfg = CarDataVehicleConfig(
        vehicle_key="bmw",
        display_name="BMW",
        client_id="example-bmw-client",
        refresh_token=SecretStr("test-api-key"),
    )
    mini_cfg = CarDataVehicleConfig(
        vehicle_key="mini",
        display_name="MINI",
        vin="WMW0000000EXAMPLE2",
        access_token=SecretStr("test-api-key"),
    )
    unconfigured = CarDataVehicleConfig(vehicle_key="bmw", display_name="Unconfigured")

    with CarDataClient(transport=httpx.MockTransport(handler)) as client:
        statuses = client.fetch_all([bmw_cfg, mini_cfg, unconfigured])

    assert len(statuses) == 2

    bmw, mini = statuses
    assert bmw.vehicle_key == "bmw"
    assert bmw.vin == "WBA0000000EXAMPLE1"
    assert bmw.soc_pct == 18
    assert bmw.target_soc_pct == 100
    assert bmw.is_plugged_in is True
    assert bmw.is_charging is False
    assert bmw.charging_status_raw == "NOCHARGING"

    assert mini.vehicle_key == "mini"
    assert mini.vin == "WMW0000000EXAMPLE2"
    assert mini.soc_pct == 54
    assert mini.target_soc_pct == 80
    assert mini.is_plugged_in is True
    assert mini.is_charging is True
    assert mini.time_remaining_minutes == 35


def test_cardata_list_telematic_format_and_error_paths() -> None:
    list_payload = {
        "telematicData": [
            {"name": "soc", "value": "37"},
            {"name": "chargingStatus", "value": "CHARGING"},
        ]
    }
    cfg = CarDataVehicleConfig(
        vehicle_key="mini",
        display_name="MINI",
        vin="WMW0000000EXAMPLE2",
        access_token=SecretStr("test-api-key"),
    )

    with CarDataClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=list_payload))
    ) as client:
        status = client.fetch_vehicle_status(cfg)

    assert status.soc_pct == 37
    # Active charging implies plugged in even when port status descriptor is absent
    assert status.is_charging is True
    assert status.is_plugged_in is True

    # Missing credentials error
    with (
        CarDataClient() as no_auth_client,
        pytest.raises(AuthenticationError, match="Missing CarData credentials"),
    ):
        no_auth_client.fetch_vehicle_status(
            CarDataVehicleConfig(vehicle_key="bmw", display_name="BMW")
        )

    # Missing SoC descriptor
    with (
        CarDataClient(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(200, json={"telematicData": {}})
            )
        ) as bad_payload_client,
        pytest.raises(PayloadParseError, match="missing SoC"),
    ):
        bad_payload_client.fetch_vehicle_status(cfg)

    # Network error handling
    def fail_net(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with (
        CarDataClient(transport=httpx.MockTransport(fail_net)) as timeout_client,
        pytest.raises(ApiClientError, match="network error"),
    ):
        timeout_client.fetch_vehicle_status(cfg)


def test_cardata_additional_validation_and_discovery_errors() -> None:
    cfg_refresh = CarDataVehicleConfig(
        vehicle_key="bmw",
        display_name="BMW",
        client_id="example-client",
        refresh_token=SecretStr("test-api-key"),
    )

    # OAuth network failure
    def fail_net(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("oauth down", request=request)

    with (
        CarDataClient(transport=httpx.MockTransport(fail_net)) as client,
        pytest.raises(ApiClientError, match="OAuth network error"),
    ):
        client.fetch_vehicle_status(cfg_refresh)

    # OAuth missing access_token
    with (
        CarDataClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"scope": "read"}))
        ) as client,
        pytest.raises(PayloadParseError, match="access_token"),
    ):
        client.fetch_vehicle_status(cfg_refresh)

    # VIN discovery empty / invalid
    cfg_discover = CarDataVehicleConfig(
        vehicle_key="bmw",
        display_name="BMW",
        access_token=SecretStr("test-api-key"),
    )
    with (
        CarDataClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"mappings": []}))
        ) as client,
        pytest.raises(PayloadParseError, match="no vehicles"),
    ):
        client.fetch_vehicle_status(cfg_discover)

    with (
        CarDataClient(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(200, json={"mappings": [{"mappingType": "PRIMARY"}]})
            )
        ) as client,
        pytest.raises(PayloadParseError, match="valid 'vin'"),
    ):
        client.fetch_vehicle_status(cfg_discover)

    with (
        CarDataClient(transport=httpx.MockTransport(fail_net)) as client,
        pytest.raises(ApiClientError, match="VIN discovery network error"),
    ):
        client.fetch_vehicle_status(cfg_discover)

    # Cached token reuse + invalid optional ints + bad SoC values
    cfg_vin = CarDataVehicleConfig(
        vehicle_key="bmw",
        display_name="BMW",
        vin="WBA0000000EXAMPLE1",
        access_token=SecretStr("test-api-key"),
    )
    payload_weird_optionals = {
        "telematicData": {
            "soc": 55,
            "vehicle.powertrain.electric.battery.stateOfCharge.target": "invalid",
            "timeRemaining": -10,
        }
    }
    with CarDataClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=payload_weird_optionals))
    ) as client:
        s1 = client.fetch_vehicle_status(cfg_vin)
        s2 = client.fetch_vehicle_status(cfg_vin)
    assert s1.target_soc_pct is None
    assert s1.time_remaining_minutes is None
    assert s2.soc_pct == 55

    for bad_td in (
        {"telematicData": "not-a-container"},
        {"telematicData": {"soc": "bad"}},
        {"telematicData": {"soc": 150}},
    ):
        with (
            CarDataClient(
                transport=httpx.MockTransport(lambda _r, p=bad_td: httpx.Response(200, json=p))
            ) as client,
            pytest.raises(PayloadParseError),
        ):
            client.fetch_vehicle_status(cfg_vin)
