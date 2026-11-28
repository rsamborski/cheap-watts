"""Unit tests for API retry policy and exponential backoff across HTTP API clients."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr

from src.clients.cardata import CarDataClient, CarDataVehicleConfig
from src.clients.errors import (
    ApiClientError,
    AuthenticationError,
    PayloadParseError,
    RateLimitError,
)
from src.clients.pstryk import PstrykClient
from src.clients.retry import (
    RetryPolicy,
    _default_sleep,
    _parse_retry_after,
    is_retryable_http_exception,
    is_retryable_status,
)
from src.clients.solar import SolarArray, SolarForecastClient
from src.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"


def test_default_sleep_and_retry_policy_math(monkeypatch: pytest.MonkeyPatch) -> None:
    slept_real: list[float] = []
    monkeypatch.setattr("src.clients.retry.time.sleep", slept_real.append)
    _default_sleep(1.5)
    assert slept_real == [1.5]

    with pytest.raises(ValueError, match="max_retries"):
        RetryPolicy(max_retries=-1)
    with pytest.raises(ValueError, match="backoff_seconds"):
        RetryPolicy(backoff_seconds=-0.1)
    with pytest.raises(ValueError, match="max_backoff_seconds"):
        RetryPolicy(max_backoff_seconds=-1.0)

    settings = Settings(_env_file=None, api_max_retries=4, api_retry_backoff_seconds=1.5)
    from_cfg = RetryPolicy.from_settings(settings)
    assert from_cfg.max_retries == 4
    assert from_cfg.backoff_seconds == pytest.approx(1.5)

    policy = RetryPolicy(max_retries=3, backoff_seconds=2.0, max_backoff_seconds=30.0)
    assert [policy.compute_delay(i) for i in range(4)] == [2.0, 4.0, 8.0, 16.0]
    assert policy.compute_delay(5) == 30.0
    assert policy.compute_delay(0, retry_after=12.0) == 12.0
    assert policy.compute_delay(0, retry_after=99.0) == 30.0


def test_retry_predicates_and_retry_after_parsing() -> None:
    assert is_retryable_status(408) is True
    assert is_retryable_status(429) is True
    assert is_retryable_status(500) is True
    assert is_retryable_status(502) is True
    assert is_retryable_status(503) is True
    assert is_retryable_status(504) is True

    for non_retry_status in (200, 400, 401, 403, 404, 422, 501, 505):
        assert is_retryable_status(non_retry_status) is False

    dummy_req = httpx.Request("GET", "https://example.com")
    assert is_retryable_http_exception(httpx.ReadTimeout("t", request=dummy_req)) is True
    assert is_retryable_http_exception(httpx.ConnectError("c", request=dummy_req)) is True
    assert is_retryable_http_exception(httpx.RemoteProtocolError("r", request=dummy_req)) is True
    assert is_retryable_http_exception(httpx.UnsupportedProtocol("u")) is False

    assert _parse_retry_after(None) is None
    assert _parse_retry_after("   ") is None
    assert _parse_retry_after("-3") is None
    assert _parse_retry_after("nan") is None
    assert _parse_retry_after("not-a-date") is None
    assert _parse_retry_after("7.5") == pytest.approx(7.5)

    future_http_date = format_datetime(datetime.now(tz=UTC) + timedelta(seconds=10), usegmt=True)
    parsed_date = _parse_retry_after(future_http_date)
    assert parsed_date is not None
    assert 5.0 <= parsed_date <= 11.0

    # Naive HTTP-date format without explicit timezone still evaluates safely in UTC
    naive_past = "Wed, 21 Oct 2015 07:28:00"
    assert _parse_retry_after(naive_past) == 0.0


def test_pstryk_retries_read_timeout_three_times_and_recovers() -> None:
    fixture_bytes = (FIXTURES / "pstryk_unified_metrics.json").read_bytes()
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise httpx.ReadTimeout("read timed out", request=request)
        return httpx.Response(200, content=fixture_bytes)

    policy = RetryPolicy(max_retries=3, backoff_seconds=2.0, sleep_fn=sleeps.append)
    with PstrykClient(
        api_key=SecretStr("test-api-key"),
        transport=httpx.MockTransport(handler),
        retry_policy=policy,
    ) as client:
        slots = client.fetch_today_and_tomorrow(
            reference_time=datetime(2026, 11, 15, 15, 30, tzinfo=UTC)
        )

    assert attempts == 4
    assert sleeps == [2.0, 4.0, 8.0]
    assert len(slots) == 5


def test_pstryk_exhausts_retries_on_persistent_read_timeout() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("read timed out", request=request)

    policy = RetryPolicy(max_retries=3, backoff_seconds=2.0, sleep_fn=sleeps.append)
    t0 = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 11, 16, 0, 0, tzinfo=UTC)

    with (
        PstrykClient(
            api_key=SecretStr("test-api-key"),
            transport=httpx.MockTransport(handler),
            retry_policy=policy,
        ) as client,
        pytest.raises(ApiClientError, match="Pstryk network error: ReadTimeout"),
    ):
        client.fetch_prices(t0, t1)

    assert attempts == 4
    assert sleeps == [2.0, 4.0, 8.0]


def test_open_meteo_retries_http_503_three_times_and_recovers() -> None:
    fixture_bytes = (FIXTURES / "open_meteo_forecast.json").read_bytes()
    attempts = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            return httpx.Response(503, text="service unavailable")
        return httpx.Response(200, content=fixture_bytes)

    policy = RetryPolicy(max_retries=3, backoff_seconds=2.0, sleep_fn=sleeps.append)
    with SolarForecastClient(
        latitude=52.23,
        longitude=21.01,
        kwp=10.0,
        transport=httpx.MockTransport(handler),
        retry_policy=policy,
    ) as client:
        forecast = client.fetch_forecast(forecast_days=2)

    assert attempts == 4
    assert sleeps == [2.0, 4.0, 8.0]
    assert len(forecast.hours) == 6


def test_open_meteo_exhausts_retries_on_persistent_503_and_handles_dual_array_recovery() -> None:
    attempts = 0
    sleeps: list[float] = []

    def always_503(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, text="unavailable")

    policy = RetryPolicy(max_retries=3, backoff_seconds=2.0, sleep_fn=sleeps.append)
    with (
        SolarForecastClient(
            latitude=52.23,
            longitude=21.01,
            kwp=10.0,
            transport=httpx.MockTransport(always_503),
            retry_policy=policy,
        ) as client,
        pytest.raises(ApiClientError, match="Open-Meteo request failed with HTTP 503"),
    ):
        client.fetch_forecast(forecast_days=2)

    assert attempts == 4
    assert sleeps == [2.0, 4.0, 8.0]

    # Dual-string transient 503 on second array recovers cleanly
    calls: list[str] = []
    sleeps_dual: list[float] = []

    def dual_handler(request: httpx.Request) -> httpx.Response:
        az = request.url.params["azimuth"]
        calls.append(az)
        if az == "90.0" and calls.count("90.0") == 1:
            return httpx.Response(503, text="blip")
        return httpx.Response(
            200,
            json={
                "timezone": "UTC",
                "hourly": {
                    "time": ["2026-06-15T12:00"],
                    "global_tilted_irradiance": [500.0],
                },
            },
        )

    dual_policy = RetryPolicy(max_retries=3, backoff_seconds=1.0, sleep_fn=sleeps_dual.append)
    with SolarForecastClient(
        latitude=52.23,
        longitude=21.01,
        kwp=4.0,
        azimuth_degrees=-90.0,
        extra_arrays=[SolarArray(kwp=6.0, azimuth_degrees=90.0)],
        transport=httpx.MockTransport(dual_handler),
        retry_policy=dual_policy,
    ) as dual_client:
        forecast = dual_client.fetch_forecast(forecast_days=1)

    assert calls == ["-90.0", "90.0", "90.0"]
    assert sleeps_dual == [1.0]
    assert len(forecast.hours) == 1


def test_non_retryable_errors_and_rate_limit_retry_after() -> None:
    # Non-retryable statuses & exceptions must fail on first attempt with 0 sleeps
    for status_code, exc_cls in (
        (400, ApiClientError),
        (401, AuthenticationError),
        (403, AuthenticationError),
        (404, ApiClientError),
        (501, ApiClientError),
        (505, ApiClientError),
    ):
        attempts = 0
        sleeps: list[float] = []

        def handler(_r: httpx.Request, sc: int = status_code) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(sc, json={})

        policy = RetryPolicy(max_retries=3, backoff_seconds=2.0, sleep_fn=sleeps.append)
        with (
            PstrykClient(
                api_key=SecretStr("test-api-key"),
                transport=httpx.MockTransport(handler),
                retry_policy=policy,
            ) as client,
            pytest.raises(exc_cls),
        ):
            client.fetch_prices(
                datetime(2026, 11, 15, 0, 0, tzinfo=UTC),
                datetime(2026, 11, 16, 0, 0, tzinfo=UTC),
            )
        assert attempts == 1
        assert sleeps == []

    # Non-retryable httpx.UnsupportedProtocol fails immediately
    proto_attempts = 0
    proto_sleeps: list[float] = []

    def raise_proto(_r: httpx.Request) -> httpx.Response:
        nonlocal proto_attempts
        proto_attempts += 1
        raise httpx.UnsupportedProtocol("bad scheme")

    with (
        PstrykClient(
            api_key=SecretStr("test-api-key"),
            transport=httpx.MockTransport(raise_proto),
            retry_policy=RetryPolicy(max_retries=3, sleep_fn=proto_sleeps.append),
        ) as proto_client,
        pytest.raises(ApiClientError, match="UnsupportedProtocol"),
    ):
        proto_client.fetch_prices(
            datetime(2026, 11, 15, 0, 0, tzinfo=UTC),
            datetime(2026, 11, 16, 0, 0, tzinfo=UTC),
        )
    assert proto_attempts == 1
    assert proto_sleeps == []

    # PayloadParseError on HTTP 200 fails immediately without retrying
    parse_attempts = 0
    parse_sleeps: list[float] = []

    def bad_json(_r: httpx.Request) -> httpx.Response:
        nonlocal parse_attempts
        parse_attempts += 1
        return httpx.Response(200, content=b"not-json")

    with (
        PstrykClient(
            api_key=SecretStr("test-api-key"),
            transport=httpx.MockTransport(bad_json),
            retry_policy=RetryPolicy(max_retries=3, sleep_fn=parse_sleeps.append),
        ) as parse_client,
        pytest.raises(PayloadParseError),
    ):
        parse_client.fetch_prices(
            datetime(2026, 11, 15, 0, 0, tzinfo=UTC),
            datetime(2026, 11, 16, 0, 0, tzinfo=UTC),
        )
    assert parse_attempts == 1
    assert parse_sleeps == []

    # HTTP 429 respects Retry-After header and clamps to max_backoff_seconds
    rl_attempts = 0
    rl_sleeps: list[float] = []

    def rate_limit_handler(_r: httpx.Request) -> httpx.Response:
        nonlocal rl_attempts
        rl_attempts += 1
        return httpx.Response(429, headers={"Retry-After": "99"}, json={})

    rl_policy = RetryPolicy(
        max_retries=2,
        backoff_seconds=2.0,
        max_backoff_seconds=10.0,
        sleep_fn=rl_sleeps.append,
    )
    with (
        PstrykClient(
            api_key=SecretStr("test-api-key"),
            transport=httpx.MockTransport(rate_limit_handler),
            retry_policy=rl_policy,
        ) as rl_client,
        pytest.raises(RateLimitError),
    ):
        rl_client.fetch_prices(
            datetime(2026, 11, 15, 0, 0, tzinfo=UTC),
            datetime(2026, 11, 16, 0, 0, tzinfo=UTC),
        )
    assert rl_attempts == 3
    assert rl_sleeps == [10.0, 10.0]


def test_cardata_retries_transient_errors_while_preserving_http_400_flow() -> None:
    bmw_bytes = (FIXTURES / "cardata_bmw_telematic.json").read_bytes()
    telematic_calls = 0
    container_calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal telematic_calls, container_calls
        if request.url.path == "/customers/vehicles/WBA0000000EXAMPLE1/telematicData":
            telematic_calls += 1
            cid = request.url.params.get("containerId", "")
            if not cid:
                # HTTP 400 triggers immediate container auto-provisioning without retry
                return httpx.Response(400, json={"error": "missing containerId"})
            if telematic_calls == 2:
                raise httpx.ReadTimeout("temporary read timeout", request=request)
            if telematic_calls == 3:
                return httpx.Response(503, text="temporary gateway error")
            return httpx.Response(200, content=bmw_bytes)

        if request.url.path == "/customers/containers":
            if request.method == "GET":
                return httpx.Response(200, json={"containers": []})
            if request.method == "POST":
                container_calls += 1
                if container_calls == 1:
                    # HTTP 400 triggers immediate core descriptor fallback without retry
                    return httpx.Response(400, json={"error": "invalid optional descriptor"})
                return httpx.Response(201, json={"containerId": "CONT0000EXAMPLE1"})

        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    cfg = CarDataVehicleConfig(
        vehicle_key="bmw",
        display_name="BMW",
        vin="WBA0000000EXAMPLE1",
        access_token=SecretStr("test-api-key"),
    )
    policy = RetryPolicy(max_retries=3, backoff_seconds=2.0, sleep_fn=sleeps.append)

    with CarDataClient(transport=httpx.MockTransport(handler), retry_policy=policy) as client:
        status = client.fetch_vehicle_status(cfg)

    assert status.soc_pct == 18
    assert container_calls == 2
    assert telematic_calls == 4
    # Only the ReadTimeout and 503 slept; the two HTTP 400s did not sleep/retry
    assert sleeps == [2.0, 4.0]

    # Device token polling still handles HTTP 400 authorization_pending cleanly
    poll_calls = 0
    poll_sleeps: list[float] = []
    retry_sleeps: list[float] = []

    def device_handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_calls
        if request.url.path == "/gcdm/oauth/device/code":
            return httpx.Response(
                200,
                json={
                    "device_code": "dc-1",
                    "user_code": "CODE-1",
                    "verification_uri": "https://example.com",
                    "expires_in": 60,
                    "interval": 1,
                },
            )
        poll_calls += 1
        if poll_calls == 1:
            return httpx.Response(400, json={"error": "authorization_pending"})
        if poll_calls == 2:
            return httpx.Response(503, text="blip")
        return httpx.Response(
            200,
            json={"access_token": "test-api-key", "refresh_token": "r", "expires_in": 3600},
        )

    with (
        patch("src.clients.retry._default_sleep", retry_sleeps.append),
        CarDataClient(
            transport=httpx.MockTransport(device_handler),
            retry_policy=RetryPolicy(max_retries=3, backoff_seconds=1.0),
        ) as dc_client,
    ):
        dc = dc_client.request_device_code("client-1")
        bundle = dc_client.poll_device_token("client-1", dc, sleep_fn=poll_sleeps.append)

    assert bundle.access_token.get_secret_value() == "test-api-key"
    assert poll_calls == 3
    assert poll_sleeps == [1.0, 1.0]
    assert retry_sleeps == [1.0]
