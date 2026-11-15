"""Offline unit tests for the BMW CarData API client, PKCE Device Flow, and token store."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from src.clients.cardata import (
    CarDataClient,
    CarDataTokenRecord,
    CarDataTokenStore,
    CarDataVehicleConfig,
    generate_pkce_pair,
)
from src.clients.errors import (
    ApiClientError,
    AuthenticationError,
    PayloadParseError,
    RateLimitError,
)

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
        if request.url.path == "/customers/vehicles/mappings":
            assert request.headers["authorization"] == "Bearer test-api-key"
            assert request.headers["x-version"] == "v1"
            return httpx.Response(200, json=mappings_payload)
        if request.url.path == "/customers/vehicles/WBA0000000EXAMPLE1/telematicData":
            return httpx.Response(200, content=bmw_bytes)
        if request.url.path == "/customers/vehicles/WMW0000000EXAMPLE2/telematicData":
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


def test_pkce_and_device_code_polling_flow() -> None:
    verifier, challenge = generate_pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert challenge == expected_challenge

    poll_attempts = 0
    slept_intervals: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_attempts
        if request.url.path == "/gcdm/oauth/device/code":
            form = request.content.decode("utf-8")
            assert "client_id=example-client-id" in form
            assert "response_type=device_code" in form
            assert "code_challenge_method=S256" in form
            assert "cardata%3Aapi%3Aread" in form
            return httpx.Response(
                200,
                json={
                    "device_code": "example-device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://customer.bmwgroup.com/oneid/#/device",
                    "verification_uri_complete": (
                        "https://customer.bmwgroup.com/oneid/#/device?user_code=ABCD-EFGH"
                    ),
                    "expires_in": 300,
                    "interval": 5,
                },
            )
        if request.url.path == "/gcdm/oauth/token":
            poll_attempts += 1
            if poll_attempts == 1:
                return httpx.Response(400, json={"error": "authorization_pending"})
            if poll_attempts == 2:
                return httpx.Response(400, json={"error": "slow_down"})
            return httpx.Response(
                200,
                json={
                    "access_token": "test-api-key",
                    "refresh_token": "test-api-key-rotated",
                    "expires_in": 3600,
                },
            )
        raise AssertionError(f"Unexpected URL: {request.url}")

    with CarDataClient(transport=httpx.MockTransport(handler)) as client:
        dc = client.request_device_code("example-client-id")
        assert dc.user_code == "ABCD-EFGH"
        assert dc.interval == 5
        bundle = client.poll_device_token(
            client_id="example-client-id",
            challenge=dc,
            sleep_fn=slept_intervals.append,
        )

    assert bundle.access_token.get_secret_value() == "test-api-key"
    assert bundle.refresh_token is not None
    assert bundle.refresh_token.get_secret_value() == "test-api-key-rotated"
    assert slept_intervals == [5.0, 5.0, 10.0]


def test_device_code_error_branches() -> None:
    # Missing verification fields
    with (
        CarDataClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"user_code": "X"}))
        ) as bad_dc_client,
        pytest.raises(PayloadParseError, match="verification fields"),
    ):
        bad_dc_client.request_device_code("example-client")

    def fail_net(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with (
        CarDataClient(transport=httpx.MockTransport(fail_net)) as net_client,
        pytest.raises(ApiClientError, match="device code network error"),
    ):
        net_client.request_device_code("example-client")

    base_dc = CarDataClient(
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(
                200,
                json={
                    "device_code": "example-dc",
                    "user_code": "CODE-1",
                    "verification_uri": "https://customer.bmwgroup.com/device",
                    "expires_in": 60,
                    "interval": 2,
                },
            )
        )
    ).request_device_code("example-client")

    for err_code, match_text in (
        ("expired_token", "expired"),
        ("access_denied", "denied"),
        ("invalid_client", "invalid_client"),
    ):
        with (
            CarDataClient(
                transport=httpx.MockTransport(
                    lambda _r, e=err_code: httpx.Response(400, json={"error": e})
                )
            ) as poll_err_client,
            pytest.raises(AuthenticationError, match=match_text),
        ):
            poll_err_client.poll_device_token("example-client", base_dc, sleep_fn=lambda _s: None)

    # Timeout branch when monotonic clock advances past expires_in
    ticks = iter([0.0, 10.0, 100.0])
    with (
        CarDataClient(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(400, json={"error": "authorization_pending"})
            )
        ) as timeout_poll_client,
        pytest.raises(AuthenticationError, match="Timed out"),
    ):
        timeout_poll_client.poll_device_token(
            "example-client",
            base_dc,
            sleep_fn=lambda _s: None,
            monotonic_fn=lambda: next(ticks),
        )


def test_container_provisioning_reuse_fallback_and_brand_vin_discovery() -> None:
    # 1. Reuse existing ACTIVE pstryk-pilot container from root JSON array
    with CarDataClient(
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(
                200,
                json=[
                    {"containerId": "OTHER1", "name": "other", "state": "ACTIVE"},
                    {"containerId": "CONT0000EXAMPLE1", "name": "pstryk-pilot", "state": "ACTIVE"},
                ],
            )
        )
    ) as reuse_client:
        assert reuse_client.ensure_container("test-api-key") == "CONT0000EXAMPLE1"

    # 2. Fallback to core descriptors when POST /customers/containers returns HTTP 400
    post_bodies: list[dict[str, object]] = []

    def fallback_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/customers/containers":
            return httpx.Response(200, json={"containers": []})
        if request.method == "POST" and request.url.path == "/customers/containers":
            body = json.loads(request.content.decode("utf-8"))
            post_bodies.append(body)
            if len(post_bodies) == 1:
                return httpx.Response(400, json={"error": "CU-400 invalid descriptor"})
            return httpx.Response(201, json={"containerId": "CONT0000EXAMPLE2"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    with CarDataClient(transport=httpx.MockTransport(fallback_handler)) as create_client:
        cid = create_client.ensure_container("test-api-key")
    assert cid == "CONT0000EXAMPLE2"
    assert len(post_bodies) == 2

    # 3. Multi-vehicle root array VIN discovery picks WBA for BMW and WMW for MINI
    multi_mappings = [
        {"vin": "WMW0000000EXAMPLE2", "mappingType": "PRIMARY"},
        {"vin": "WBA0000000EXAMPLE1", "mappingType": "PRIMARY"},
    ]
    with CarDataClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=multi_mappings))
    ) as vin_client:
        assert vin_client.discover_vin("test-api-key", vehicle_key="bmw") == "WBA0000000EXAMPLE1"
        assert vin_client.discover_vin("test-api-key", vehicle_key="mini") == "WMW0000000EXAMPLE2"


def test_token_store_rotation_persistence_and_auto_container_recovery(tmp_path: Path) -> None:
    store_path = tmp_path / "data" / "cardata_tokens.json"
    store = CarDataTokenStore(store_path)
    assert store.load_all() == {}

    now = datetime.now(tz=UTC)
    # Save an expired access token so client refreshes & rotates it
    store.upsert(
        "bmw",
        CarDataTokenRecord(
            access_token=SecretStr("test-api-key-expired"),
            refresh_token=SecretStr("test-api-key-old-refresh"),
            expires_at=now - timedelta(minutes=10),
            client_id="example-bmw-client",
            vin="WBA0000000EXAMPLE1",
            container_id=None,
            updated_at=now - timedelta(hours=1),
        ),
    )

    bmw_bytes = (FIXTURES / "cardata_bmw_telematic.json").read_bytes()
    telematic_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gcdm/oauth/token":
            assert "refresh_token=test-api-key-old-refresh" in request.content.decode("utf-8")
            return httpx.Response(
                200,
                json={
                    "access_token": "test-api-key-fresh",
                    "refresh_token": "test-api-key-rotated",
                    "expires_in": 3600,
                },
            )
        if request.url.path == "/customers/vehicles/WBA0000000EXAMPLE1/telematicData":
            cid = request.url.params.get("containerId", "")
            telematic_calls.append(cid)
            if not cid:
                return httpx.Response(400, json={"error": "missing containerId"})
            assert cid == "CONT0000EXAMPLE9"
            return httpx.Response(200, content=bmw_bytes)
        if request.url.path == "/customers/containers":
            return httpx.Response(
                200,
                json={
                    "containers": [
                        {
                            "containerId": "CONT0000EXAMPLE9",
                            "name": "pstryk-pilot",
                            "state": "ACTIVE",
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected URL: {request.url}")

    cfg = CarDataVehicleConfig(
        vehicle_key="bmw",
        display_name="BMW",
        client_id="example-bmw-client",
    )
    assert cfg.has_credentials(store) is True
    assert (
        CarDataVehicleConfig(
            vehicle_key="bmw",
            display_name="BMW",
            client_id="different-client",
        ).has_credentials(store)
        is False
    )

    with CarDataClient(transport=httpx.MockTransport(handler), token_store=store) as client:
        status = client.fetch_vehicle_status(cfg)

    assert status.soc_pct == 18
    assert telematic_calls == ["", "CONT0000EXAMPLE9"]

    persisted = store.get("bmw")
    assert persisted is not None
    assert persisted.has_valid_access_token() is True
    assert persisted.access_token.get_secret_value() == "test-api-key-fresh"
    assert persisted.refresh_token is not None
    assert persisted.refresh_token.get_secret_value() == "test-api-key-rotated"
    assert persisted.container_id == "CONT0000EXAMPLE9"

    # Second client session reuses cached valid access_token + container_id in 1 request
    single_call_count = 0

    def cached_handler(request: httpx.Request) -> httpx.Response:
        nonlocal single_call_count
        single_call_count += 1
        assert request.url.params.get("containerId") == "CONT0000EXAMPLE9"
        assert request.headers["authorization"] == "Bearer test-api-key-fresh"
        return httpx.Response(200, content=bmw_bytes)

    with CarDataClient(
        transport=httpx.MockTransport(cached_handler),
        token_store=store,
    ) as second_client:
        status2 = second_client.fetch_vehicle_status(cfg)

    assert status2.soc_pct == 18
    assert single_call_count == 1


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

    def fail_net(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("oauth down", request=request)

    with (
        CarDataClient(transport=httpx.MockTransport(fail_net)) as client,
        pytest.raises(ApiClientError, match="OAuth network error"),
    ):
        client.fetch_vehicle_status(cfg_refresh)

    with (
        CarDataClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"scope": "read"}))
        ) as client,
        pytest.raises(PayloadParseError, match="access_token"),
    ):
        client.fetch_vehicle_status(cfg_refresh)

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

    # _parse_json_any error branches (401, 429, 500, invalid JSON, scalar JSON)
    for status_code, exc_cls in (
        (401, AuthenticationError),
        (429, RateLimitError),
        (502, ApiClientError),
    ):
        with (
            CarDataClient(
                transport=httpx.MockTransport(
                    lambda _r, sc=status_code: httpx.Response(sc, text="err")
                )
            ) as err_client,
            pytest.raises(exc_cls),
        ):
            err_client.discover_vin("test-api-key")

    for raw_body in (b"not-json", b'"just-a-string"'):
        with (
            CarDataClient(
                transport=httpx.MockTransport(lambda _r, b=raw_body: httpx.Response(200, content=b))
            ) as parse_err_client,
            pytest.raises(PayloadParseError),
        ):
            parse_err_client.discover_vin("test-api-key")

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
