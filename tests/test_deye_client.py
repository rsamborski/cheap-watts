"""Offline unit tests for the Deye Cloud read client and InverterClient protocol."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from src.clients.deye import (
    DeyeCloudClient,
    InverterClient,
    _normalize_hhmm,
    _parse_bool_flag,
)
from src.clients.errors import ApiClientError, AuthenticationError, PayloadParseError

FIXTURES = Path(__file__).parent / "fixtures"


def test_deye_client_authenticates_hashes_password_and_reads_status() -> None:
    token_bytes = (FIXTURES / "deye_token.json").read_bytes()
    latest_bytes = (FIXTURES / "deye_device_latest.json").read_bytes()
    tou_bytes = (FIXTURES / "deye_config_tou.json").read_bytes()

    token_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.url.path == "/v1.0/account/token":
            token_calls += 1
            assert request.url.params["appId"] == "example-app-id"
            body = json.loads(request.content.decode("utf-8"))
            expected_hash = hashlib.sha256(b"test-api-key").hexdigest()
            assert body["password"] == expected_hash
            assert body["appSecret"] == "test-api-key"
            return httpx.Response(200, content=token_bytes)

        assert request.headers["authorization"] == "Bearer test-api-key"
        if request.url.path == "/v1.0/device/latest":
            return httpx.Response(200, content=latest_bytes)
        if request.url.path == "/v1.0/config/tou":
            return httpx.Response(200, content=tou_bytes)
        raise AssertionError(f"Unexpected path: {request.url.path}")

    with DeyeCloudClient(
        device_sn="DEYE0000EXAMPLE1",
        app_id="example-app-id",
        app_secret=SecretStr("test-api-key"),
        email="owner@example.com",
        password=SecretStr("test-api-key"),
        transport=httpx.MockTransport(handler),
    ) as client:
        assert isinstance(client, InverterClient)
        status = client.get_status()
        # Second call reuses cached token
        status_again = client.get_status()

    assert token_calls == 1
    assert status.device_sn == "DEYE0000EXAMPLE1"
    assert status.battery_soc == 46
    assert status.pv_power_w == pytest.approx(2840.5)
    assert status.battery_power_w == pytest.approx(-1250.0)
    assert status.grid_power_w == pytest.approx(85.0)
    assert status.load_power_w == pytest.approx(1675.5)
    assert status.tou_enabled is True
    assert len(status.tou_slots) == 6
    assert status.tou_slots[0].start_time == "00:00"
    assert status.tou_slots[0].end_time == "04:00"
    assert status.tou_slots[0].target_soc == 95
    assert status.tou_slots[0].grid_charge is True
    # Slot 6 normalized "20:00-24:00" -> end_time "00:00"
    assert status.tou_slots[5].start_time == "20:00"
    assert status.tou_slots[5].end_time == "00:00"
    assert status.tou_slots[5].grid_charge is False
    assert status_again.battery_soc == 46


def test_deye_client_pv_string_fallback_and_direct_access_token() -> None:
    latest_payload = {
        "success": True,
        "dataList": [
            {"key": "BMS_SOC", "value": 82},
            {"key": "pv1Power", "value": 650.0},
            {"key": "pv2Power", "value": 550.0},
        ],
    }
    tou_payload = {
        "success": True,
        "touAction": "OFF",
        "touSlots": [
            {
                "startTime": "1:00",
                "endTime": "5:00",
                "soc": 90,
                "enableGridCharge": "0",
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1.0/device/latest":
            return httpx.Response(200, json=latest_payload)
        return httpx.Response(200, json=tou_payload)

    with DeyeCloudClient(
        device_sn="DEYE0000EXAMPLE1",
        access_token=SecretStr("test-api-key"),
        transport=httpx.MockTransport(handler),
    ) as client:
        status = client.get_status()

    assert status.battery_soc == 82
    assert status.pv_power_w == pytest.approx(1200.0)
    assert status.tou_enabled is False
    assert status.tou_slots[0].start_time == "01:00"
    assert status.tou_slots[0].end_time == "05:00"
    assert status.tou_slots[0].grid_charge is False


def test_deye_client_error_handling() -> None:
    with pytest.raises(ValueError, match="device_sn"):
        DeyeCloudClient(device_sn="   ")

    with (
        DeyeCloudClient(device_sn="DEYE0000EXAMPLE1") as missing_creds_client,
        pytest.raises(AuthenticationError),
    ):
        missing_creds_client.authenticate()

    auth_err_payload = {"code": "2101019", "success": False, "msg": "invalid access token"}
    with (
        DeyeCloudClient(
            device_sn="DEYE0000EXAMPLE1",
            access_token=SecretStr("test-api-key"),
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=auth_err_payload)),
        ) as client,
        pytest.raises(AuthenticationError, match="2101019"),
    ):
        client.get_status()

    api_err_payload = {"code": "5001", "success": False, "msg": "device offline"}
    with (
        DeyeCloudClient(
            device_sn="DEYE0000EXAMPLE1",
            access_token=SecretStr("test-api-key"),
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=api_err_payload)),
        ) as client,
        pytest.raises(ApiClientError, match="device offline"),
    ):
        client.get_status()

    no_soc_payload = {"success": True, "dataList": [{"key": "pvPower", "value": 100}]}
    with (
        DeyeCloudClient(
            device_sn="DEYE0000EXAMPLE1",
            access_token=SecretStr("test-api-key"),
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json=(
                        no_soc_payload
                        if r.url.path == "/v1.0/device/latest"
                        else {"success": True, "timeUseSettingItems": []}
                    ),
                )
            ),
        ) as client,
        pytest.raises(PayloadParseError, match="SoC"),
    ):
        client.get_status()


def test_deye_helpers_and_malformed_payload_branches() -> None:
    assert _parse_bool_flag("true") is True
    assert _parse_bool_flag(None, default=True) is True
    with pytest.raises(PayloadParseError, match="boolean flag"):
        _parse_bool_flag("maybe")

    assert _normalize_hhmm("invalid") == "invalid"

    # Pre-hashed password + missing accessToken error
    with (
        DeyeCloudClient(
            device_sn="DEYE0000EXAMPLE1",
            app_id="app",
            app_secret=SecretStr("test-api-key"),
            email="u@example.com",
            password=SecretStr("a" * 64),
            password_is_sha256=True,
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(200, json={"success": True, "data": {}})
            ),
        ) as client,
        pytest.raises(PayloadParseError, match="accessToken"),
    ):
        client.authenticate()

    # Token network error & post_authenticated network error
    def net_fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with (
        DeyeCloudClient(
            device_sn="DEYE0000EXAMPLE1",
            app_id="app",
            app_secret=SecretStr("test-api-key"),
            email="u@example.com",
            password=SecretStr("test-api-key"),
            transport=httpx.MockTransport(net_fail),
        ) as client,
        pytest.raises(ApiClientError, match="token network error"),
    ):
        client.authenticate()

    with (
        DeyeCloudClient(
            device_sn="DEYE0000EXAMPLE1",
            access_token=SecretStr("test-api-key"),
            transport=httpx.MockTransport(net_fail),
        ) as client,
        pytest.raises(ApiClientError, match="network error"),
    ):
        client.get_status()

    # Nested data.deviceDataList, invalid pv string ignored, out-of-range / non-numeric SoC
    nested_metrics = DeyeCloudClient(device_sn="DEYE0000EXAMPLE1")._extract_device_metrics(
        {
            "data": {
                "deviceDataList": [
                    {
                        "deviceSn": "DEYE0000EXAMPLE1",
                        "dataList": [
                            {"key": "SOC", "value": "55"},
                            {"key": "pv1Power", "value": "not-a-float"},
                            {"key": "pv2Power", "value": "400"},
                        ],
                    }
                ]
            }
        }
    )
    assert DeyeCloudClient._sum_pv_strings(nested_metrics) == pytest.approx(400.0)

    with pytest.raises(PayloadParseError, match="dataList"):
        DeyeCloudClient(device_sn="DEYE0000EXAMPLE1")._extract_device_metrics({"data": {}})

    with pytest.raises(PayloadParseError, match="Invalid Deye battery SoC"):
        DeyeCloudClient._parse_required_soc({"soc": "nan-val"})
    with pytest.raises(PayloadParseError, match="out of \\[0, 100\\]"):
        DeyeCloudClient._parse_required_soc({"soc": 125})
    with pytest.raises(PayloadParseError, match="Invalid numeric value"):
        DeyeCloudClient._lookup_float({"pvpower": "bad"}, ("pvpower",), default=0.0)

    # TOU edge cases
    with pytest.raises(PayloadParseError, match="not a list"):
        DeyeCloudClient._parse_tou_config({"touSwitch": 1, "timeUseSettingItems": "bad"})
    with pytest.raises(PayloadParseError, match="not an object"):
        DeyeCloudClient._parse_tou_config({"timeUseSettingItems": ["bad"]})
    with pytest.raises(PayloadParseError, match="missing start/end"):
        DeyeCloudClient._parse_tou_config({"timeUseSettingItems": [{"val": 50}]})
    with pytest.raises(PayloadParseError, match="missing target SoC"):
        DeyeCloudClient._parse_tou_config({"timeUseSettingItems": [{"time": "00:00-04:00"}]})
    with pytest.raises(PayloadParseError, match="Invalid Deye TOU slot"):
        DeyeCloudClient._parse_tou_config(
            {"timeUseSettingItems": [{"time": "00:00-04:00", "val": "bad"}]}
        )
