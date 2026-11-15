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
    SolarmanV5Client,
    _decode_modbus_hhmm,
    _normalize_hhmm,
    _parse_bool_flag,
    _to_int16,
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


def _sample_modbus_blocks() -> tuple[list[int], list[int]]:
    telemetry = [0] * 86
    telemetry[588 - 588] = 74  # Battery SOC 74%
    telemetry[590 - 588] = (-850) & 0xFFFF  # Charging at -850W (signed int16)
    telemetry[625 - 588] = (-320) & 0xFFFF  # Exporting -320W to grid
    telemetry[653 - 588] = 1410  # Home load 1410W
    telemetry[672 - 588] = 1100  # PV1 1100W
    telemetry[673 - 588] = 950  # PV2 950W -> total 2050W

    tou = [0] * 32
    tou[146 - 146] = 0xFF  # TOU enabled + all 7 days
    for idx, hhmm in enumerate((0, 400, 800, 1200, 1600, 2030)):
        tou[(148 - 146) + idx] = hhmm
    for idx, soc in enumerate((95, 30, 30, 50, 30, 80)):
        tou[(166 - 146) + idx] = soc
    for idx, charge in enumerate((1, 0, 0, 1, 0, 0)):
        tou[(172 - 146) + idx] = charge
    return telemetry, tou


def test_solarman_v5_client_reads_telemetry_and_tou_registers() -> None:
    telemetry, tou = _sample_modbus_blocks()
    disconnected = False

    class FakeModbus:
        def __init__(self, address: str, serial: int, **kwargs: object) -> None:
            assert address == "192.168.1.50"
            assert serial == 2700012345
            assert kwargs["port"] == 8899

        def read_holding_registers(self, register_addr: int, quantity: int) -> list[int]:
            if register_addr == 588 and quantity == 86:
                return list(telemetry)
            if register_addr == 146 and quantity == 32:
                return list(tou)
            raise AssertionError(f"Unexpected read: {register_addr} x {quantity}")

        def disconnect(self) -> None:
            nonlocal disconnected
            disconnected = True

    with SolarmanV5Client(
        host="192.168.1.50",
        logger_serial="2700012345",
        device_sn="DEYE0000EXAMPLE1",
        modbus_factory=FakeModbus,
    ) as client:
        assert isinstance(client, InverterClient)
        status = client.get_status()

    assert disconnected is True
    assert status.device_sn == "DEYE0000EXAMPLE1"
    assert status.battery_soc == 74
    assert status.battery_power_w == pytest.approx(-850.0)
    assert status.grid_power_w == pytest.approx(-320.0)
    assert status.load_power_w == pytest.approx(1410.0)
    assert status.pv_power_w == pytest.approx(2050.0)
    assert status.tou_enabled is True
    assert len(status.tou_slots) == 6
    assert status.tou_slots[0].start_time == "00:00"
    assert status.tou_slots[0].end_time == "04:00"
    assert status.tou_slots[0].target_soc == 95
    assert status.tou_slots[0].grid_charge is True
    # Slot 6 wraps end_time back to Slot 1 start_time ("00:00")
    assert status.tou_slots[5].start_time == "20:30"
    assert status.tou_slots[5].end_time == "00:00"
    assert status.tou_slots[5].grid_charge is False


def test_solarman_v5_client_validation_and_error_handling() -> None:
    assert _to_int16(0xFFFF) == -1
    assert _to_int16(500) == 500
    assert _decode_modbus_hhmm(2400, 1) == "00:00"
    with pytest.raises(PayloadParseError, match="HHMM"):
        _decode_modbus_hhmm(2599, 1)

    with pytest.raises(ValueError, match="host"):
        SolarmanV5Client(host="  ", logger_serial=123)
    with pytest.raises(ValueError, match="port"):
        SolarmanV5Client(host="192.168.1.50", logger_serial=123, port=70000)
    with pytest.raises(ValueError, match="numeric"):
        SolarmanV5Client(host="192.168.1.50", logger_serial="not-a-number")
    with pytest.raises(ValueError, match="positive"):
        SolarmanV5Client(host="192.168.1.50", logger_serial=0)

    class BrokenSocketModbus:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def read_holding_registers(self, **_kw: object) -> list[int]:
            raise OSError("timed out")

        def disconnect(self) -> None:
            raise RuntimeError("already closed")

    with (
        SolarmanV5Client(
            host="192.168.1.50",
            logger_serial=2700012345,
            modbus_factory=BrokenSocketModbus,
        ) as broken_client,
        pytest.raises(ApiClientError, match="Solarman V5 error"),
    ):
        broken_client.get_status()

    # Short register lists and out-of-range SoC values raise PayloadParseError
    telemetry, tou = _sample_modbus_blocks()

    for bad_tel, bad_tou in (
        (telemetry[:10], tou),
        (telemetry, tou[:5]),
        ([150, *telemetry[1:]], tou),
        (telemetry, [*tou[:20], 150, *tou[21:]]),
    ):

        class BadPayloadModbus:
            def __init__(
                self,
                *_a: object,
                _t: list[int] = bad_tel,
                _u: list[int] = bad_tou,
                **_kw: object,
            ) -> None:
                self._t = _t
                self._u = _u

            def read_holding_registers(self, register_addr: int, quantity: int) -> list[int]:
                return self._t if register_addr == 588 else self._u

            def disconnect(self) -> None:
                pass

        with (
            SolarmanV5Client(
                host="192.168.1.50",
                logger_serial=2700012345,
                modbus_factory=BadPayloadModbus,
            ) as bad_client,
            pytest.raises(PayloadParseError),
        ):
            bad_client.get_status()


def test_solarman_and_cloud_write_tou_schedule_and_dry_run_invariant() -> None:
    from src.models import TouSlot

    valid_slots = [
        TouSlot(
            slot_index=1, start_time="00:00", end_time="02:00", target_soc=20, grid_charge=False
        ),
        TouSlot(
            slot_index=2, start_time="02:00", end_time="04:30", target_soc=85, grid_charge=True
        ),
        TouSlot(
            slot_index=3, start_time="04:30", end_time="09:00", target_soc=20, grid_charge=False
        ),
        TouSlot(
            slot_index=4, start_time="09:00", end_time="14:00", target_soc=20, grid_charge=False
        ),
        TouSlot(
            slot_index=5, start_time="14:00", end_time="19:00", target_soc=20, grid_charge=False
        ),
        TouSlot(
            slot_index=6, start_time="19:00", end_time="00:00", target_soc=20, grid_charge=False
        ),
    ]

    # 1. Local Solarman V5: dry_run=True MUST NOT connect or write any registers
    writes: list[tuple[int, list[int]]] = []
    tou_regs = [0] * 32
    tou_regs[0] = 0x0000  # TOU disabled initially
    tou_regs[(172 - 146) + 1] = 0x0002  # Generator bit 1 set on slot #2 -> must be preserved!

    class RecordingModbus:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def read_holding_registers(self, register_addr: int, quantity: int) -> list[int]:
            assert register_addr == 146 and quantity == 32
            return list(tou_regs)

        def write_multiple_holding_registers(self, register_addr: int, values: list[int]) -> None:
            writes.append((register_addr, list(values)))

        def disconnect(self) -> None:
            pass

    with SolarmanV5Client(
        host="192.168.1.50",
        logger_serial=2700012345,
        modbus_factory=RecordingModbus,
    ) as local_client:
        assert local_client.write_tou_schedule(valid_slots, dry_run=True) is False
        assert writes == []

        assert local_client.write_tou_schedule(valid_slots, dry_run=False, enable_tou=True) is True

    assert writes == [
        (146, [0x00FF]),
        (148, [0, 200, 430, 900, 1400, 1900]),
        (166, [20, 85, 20, 20, 20, 20, 0, 0x0003, 0, 0, 0, 0]),
    ]

    # 2. Deye Cloud: dry_run=True skips HTTP POST; dry_run=False posts /v1.0/config/tou/update
    cloud_posts: list[str] = []

    def cloud_handler(request: httpx.Request) -> httpx.Response:
        cloud_posts.append(request.url.path)
        return httpx.Response(200, json={"success": True, "code": "1000000"})

    with DeyeCloudClient(
        device_sn="DEYE0000EXAMPLE1",
        access_token=SecretStr("test-api-key"),
        transport=httpx.MockTransport(cloud_handler),
    ) as cloud_client:
        assert cloud_client.write_tou_schedule(valid_slots, dry_run=True) is False
        assert cloud_posts == []

        assert cloud_client.write_tou_schedule(valid_slots, dry_run=False) is True
        assert cloud_posts == ["/v1.0/config/tou/update"]

    # 3. Validation errors (wrong count, duplicate index, non-contiguous, non-ascending)
    with SolarmanV5Client(
        host="192.168.1.50",
        logger_serial=2700012345,
        modbus_factory=RecordingModbus,
    ) as val_client:
        with pytest.raises(ValueError, match="exactly 6"):
            val_client.write_tou_schedule(valid_slots[:4], dry_run=True)
        bad_boundary = list(valid_slots)
        bad_boundary[0] = TouSlot(
            slot_index=1, start_time="00:00", end_time="03:00", target_soc=20, grid_charge=False
        )
        with pytest.raises(ValueError, match="Non-contiguous"):
            val_client.write_tou_schedule(bad_boundary, dry_run=True)
