"""Offline unit tests for the Pstryk API client."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from src.clients.errors import (
    ApiClientError,
    AuthenticationError,
    PayloadParseError,
    RateLimitError,
)
from src.clients.pstryk import PstrykClient

FIXTURES = Path(__file__).parent / "fixtures"


def test_pstryk_fetch_today_and_tomorrow_parses_fixture() -> None:
    fixture_bytes = (FIXTURES / "pstryk_unified_metrics.json").read_bytes()
    captured_headers: dict[str, str] = {}
    captured_params: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        captured_params.update(dict(request.url.params))
        assert request.url.path == "/integrations/meter-data/unified-metrics/"
        return httpx.Response(200, content=fixture_bytes)

    ref_time = datetime(2026, 11, 15, 15, 30, tzinfo=UTC)
    with PstrykClient(
        api_key=SecretStr("Bearer test-api-key"),
        tz_name="Europe/Warsaw",
        transport=httpx.MockTransport(handler),
    ) as client:
        slots = client.fetch_today_and_tomorrow(reference_time=ref_time)

    assert captured_headers["authorization"] == "test-api-key"
    assert captured_params["metrics"] == "pricing"
    assert captured_params["resolution"] == "hour"

    assert len(slots) == 5
    # 2026-11-14T23:00:00Z == 2026-11-15T00:00:00+01:00 in Europe/Warsaw
    assert slots[0].start.hour == 0
    assert slots[0].start.date() == date(2026, 11, 15)
    assert slots[0].price_pln_kwh == pytest.approx(0.3444)
    # Negative solar midday slot preserved
    assert any(s.price_pln_kwh == pytest.approx(-0.08) for s in slots)
    # Next-day (2026-11-16 local) slot present
    assert PstrykClient.has_next_day_prices(slots, date(2026, 11, 15)) is True


def test_pstryk_handles_unpublished_tomorrow_prices_and_mwh_units() -> None:
    today_only_bytes = (FIXTURES / "pstryk_today_only.json").read_bytes()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=today_only_bytes)

    ref_time = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
    with PstrykClient(
        api_key=SecretStr("test-api-key"),
        transport=httpx.MockTransport(handler),
    ) as client:
        slots = client.fetch_today_and_tomorrow(reference_time=ref_time)

    assert len(slots) == 1
    assert PstrykClient.has_next_day_prices(slots, date(2026, 11, 14)) is False

    # Verify PLN/MWh unit conversion and timestamp fallback
    mwh_payload = {
        "frames": [
            {
                "timestamp": "2026-11-14T10:00:00Z",
                "price_gross": 450.0,
                "price_net": 365.85,
                "unit": "PLN/MWh",
            }
        ]
    }
    with PstrykClient(
        api_key=SecretStr("test-api-key"),
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=mwh_payload)),
    ) as client:
        mwh_slots = client.fetch_today_and_tomorrow(reference_time=ref_time)

    assert len(mwh_slots) == 1
    assert mwh_slots[0].price_pln_kwh == pytest.approx(0.45)
    assert mwh_slots[0].price_net_pln_kwh == pytest.approx(0.36585)


@pytest.mark.parametrize(
    ("status_code", "expected_exc"),
    [
        (401, AuthenticationError),
        (403, AuthenticationError),
        (429, RateLimitError),
        (502, ApiClientError),
    ],
)
def test_pstryk_http_error_mapping(status_code: int, expected_exc: type[Exception]) -> None:
    t0 = datetime(2026, 11, 14, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)
    with (
        PstrykClient(
            api_key=SecretStr("test-api-key"),
            transport=httpx.MockTransport(lambda _r: httpx.Response(status_code, json={})),
        ) as client,
        pytest.raises(expected_exc),
    ):
        client.fetch_prices(t0, t1)


def test_pstryk_validation_and_malformed_payload_errors() -> None:
    t0 = datetime(2026, 11, 14, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)

    with PstrykClient(
        api_key=SecretStr("test-api-key"),
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"not-json")),
    ) as client:
        with pytest.raises(ValueError, match="window_end"):
            client.fetch_prices(t0, t0)
        with pytest.raises(PayloadParseError):
            client.fetch_prices(t0, t1)

    with (
        PstrykClient(
            api_key=SecretStr("test-api-key"),
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"unexpected": 1})),
        ) as client,
        pytest.raises(PayloadParseError, match="frames"),
    ):
        client.fetch_prices(t0, t1)

    def raise_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with (
        PstrykClient(
            api_key=SecretStr("test-api-key"),
            transport=httpx.MockTransport(raise_connect_error),
        ) as client,
        pytest.raises(ApiClientError, match="network error"),
    ):
        client.fetch_prices(t0, t1)


def test_pstryk_edge_case_payloads() -> None:
    t0 = datetime(2026, 11, 14, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)

    # JSON array root rejected by parse_json_response
    with (
        PstrykClient(
            api_key=SecretStr("test-api-key"),
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=[1, 2])),
        ) as client,
        pytest.raises(PayloadParseError, match="expected JSON object"),
    ):
        client.fetch_prices(t0, t1)

    # Non-dict frame item
    with (
        PstrykClient(
            api_key=SecretStr("test-api-key"),
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"frames": ["bad"]})),
        ) as client,
        pytest.raises(PayloadParseError, match="non-object"),
    ):
        client.fetch_prices(t0, t1)

    # data.frames + naive timestamp + fallback `price` key
    alt_payload = {
        "data": {
            "frames": [
                {
                    "start": "2026-11-14T12:00:00",
                    "price": 0.62,
                }
            ]
        }
    }
    with PstrykClient(
        api_key=SecretStr("test-api-key"),
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=alt_payload)),
    ) as client:
        slots = client.fetch_prices(t0, t1)
    assert len(slots) == 1
    assert slots[0].price_pln_kwh == pytest.approx(0.62)

    # Invalid price_gross / price_net / timestamp
    for bad_frame in (
        {"window_start": "2026-11-14T12:00:00Z", "price_gross": "bad"},
        {"window_start": "2026-11-14T12:00:00Z", "price_gross": 0.5, "price_net": "bad"},
        {"window_start": "not-a-timestamp", "price_gross": 0.5},
        {"window_start": "", "price_gross": 0.5},
    ):
        with (
            PstrykClient(
                api_key=SecretStr("test-api-key"),
                transport=httpx.MockTransport(
                    lambda _r, f=bad_frame: httpx.Response(200, json={"frames": [f]})
                ),
            ) as client,
            pytest.raises(PayloadParseError),
        ):
            client.fetch_prices(t0, t1)
