"""Offline unit tests for the read-only API probe runner and CLI subcommand."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr

from src.clients.cardata import CarDataTokenRecord, CarDataTokenStore
from src.config import Settings
from src.main import main
from src.probe import _mask_identifier, format_probe_report, run_probes

FIXTURES = Path(__file__).parent / "fixtures"


def test_mask_identifier_variants() -> None:
    assert _mask_identifier(None) == "n/a"
    assert _mask_identifier("") == "n/a"
    assert _mask_identifier("ABCD") == "****"
    assert _mask_identifier("DEYE0000EXAMPLE1") == "***PLE1"


def test_run_probes_skips_all_when_unconfigured() -> None:
    settings = Settings(_env_file=None)
    results = run_probes(settings, target="all")

    assert [r.service for r in results] == ["pstryk", "solar", "deye", "cardata"]
    assert all(r.status == "skipped" for r in results)

    report = format_probe_report(results, timezone="Europe/Warsaw")
    assert "Summary: 0 ok, 4 skipped, 0 failed" in report
    assert "[SKIP] pstryk" in report
    assert "[SKIP] solar" in report
    assert "[SKIP] deye" in report
    assert "[SKIP] cardata" in report


def test_run_probes_all_services_succeed_with_fixtures(tmp_path: Path) -> None:
    pstryk_bytes = (FIXTURES / "pstryk_unified_metrics.json").read_bytes()
    solar_bytes = (FIXTURES / "open_meteo_forecast.json").read_bytes()
    latest_bytes = (FIXTURES / "deye_device_latest.json").read_bytes()
    tou_bytes = (FIXTURES / "deye_config_tou.json").read_bytes()
    bmw_bytes = (FIXTURES / "cardata_bmw_telematic.json").read_bytes()

    def deye_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1.0/device/latest":
            return httpx.Response(200, content=latest_bytes)
        if request.url.path == "/v1.0/config/tou":
            return httpx.Response(200, content=tou_bytes)
        raise AssertionError(f"Unexpected Deye path: {request.url.path}")

    def cardata_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/customers/vehicles/WBA0000000EXAMPLE1/telematicData":
            return httpx.Response(200, content=bmw_bytes)
        raise AssertionError(f"Unexpected CarData path: {request.url.path}")

    tokens_file = tmp_path / "cardata_tokens.json"
    CarDataTokenStore(tokens_file).upsert(
        "bmw",
        CarDataTokenRecord(
            access_token=SecretStr("test-api-key"),
            refresh_token=SecretStr("test-api-key"),
            expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
            client_id="example-bmw-client",
            vin="WBA0000000EXAMPLE1",
            container_id="CONT0000EXAMPLE1",
        ),
    )

    settings = Settings(
        _env_file=None,
        pstryk_api_key=SecretStr("test-api-key"),
        solar_latitude=52.23,
        solar_longitude=21.01,
        solar_kwp=10.0,
        deye_device_sn="DEYE0000EXAMPLE1",
        deye_access_token=SecretStr("test-api-key"),
        bmw_cardata_client_id="example-bmw-client",
        bmw_vin="WBA0000000EXAMPLE1",
        cardata_tokens_file=tokens_file,
    )

    transports = {
        "pstryk": httpx.MockTransport(lambda _r: httpx.Response(200, content=pstryk_bytes)),
        "solar": httpx.MockTransport(lambda _r: httpx.Response(200, content=solar_bytes)),
        "deye": httpx.MockTransport(deye_handler),
        "cardata": httpx.MockTransport(cardata_handler),
    }

    ref_time = datetime(2026, 11, 15, 15, 30, tzinfo=UTC)
    results = run_probes(
        settings,
        target="all",
        transports=transports,
        reference_time=ref_time,
    )

    assert all(r.status == "ok" for r in results)
    report = format_probe_report(results, timezone=settings.timezone)
    assert "Summary: 4 ok, 0 skipped, 0 failed" in report
    assert "Fetched 5 hourly price slots" in report
    assert "Battery SoC 46%" in report
    assert "TOU #1: 00:00-04:00" in report
    assert "BMW (***PLE1)" in report
    assert "MINI: skipped (not configured)" in report
    assert "test-api-key" not in report


def test_probe_cardata_displays_at_home_distance_without_raw_coords(tmp_path: Path) -> None:
    tokens_file = tmp_path / "cardata_tokens.json"
    CarDataTokenStore(tokens_file).upsert(
        "mini",
        CarDataTokenRecord(
            access_token=SecretStr("test-api-key"),
            refresh_token=SecretStr("test-api-key"),
            expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
            client_id="example-mini-client",
            vin="WMW0000000EXAMPLE2",
            container_id="CONT0000EXAMPLE2",
        ),
    )
    settings = Settings(
        _env_file=None,
        solar_latitude=52.2300,
        solar_longitude=21.0100,
        home_radius_meters=200.0,
        mini_cardata_client_id="example-mini-client",
        cardata_tokens_file=tokens_file,
    )
    payload = {
        "telematicData": {
            "vehicle.drivetrain.batteryManagement.header": {"value": 32},
            "vehicle.cabin.infotainment.navigation.currentLocation.latitude": {"value": "52.29"},
            "vehicle.cabin.infotainment.navigation.currentLocation.longitude": {"value": "21.10"},
        }
    }
    results = run_probes(
        settings,
        target="cardata",
        transports={"cardata": httpx.MockTransport(lambda _r: httpx.Response(200, json=payload))},
    )
    assert len(results) == 1
    assert results[0].status == "ok"
    mini_line = next(d for d in results[0].details if d.startswith("MINI"))
    assert "at_home=False" in mini_line
    assert "km)" in mini_line
    assert "52.29" not in mini_line
    assert "21.10" not in mini_line


def test_run_probes_handles_failures_and_partial_data(tmp_path: Path) -> None:
    today_only_bytes = (FIXTURES / "pstryk_today_only.json").read_bytes()
    mini_bytes = (FIXTURES / "cardata_mini_telematic.json").read_bytes()

    def cardata_mixed_handler(request: httpx.Request) -> httpx.Response:
        if "WBA0000000EXAMPLE1" in request.url.path:
            return httpx.Response(401, json={"error": "invalid_token"})
        if "WMW0000000EXAMPLE2" in request.url.path:
            return httpx.Response(200, content=mini_bytes)
        raise AssertionError(f"Unexpected path: {request.url.path}")

    tokens_file = tmp_path / "cardata_tokens.json"
    token_store = CarDataTokenStore(tokens_file)
    for key, cid, vin in (
        ("bmw", "example-bmw-client", "WBA0000000EXAMPLE1"),
        ("mini", "example-mini-client", "WMW0000000EXAMPLE2"),
    ):
        token_store.upsert(
            key,
            CarDataTokenRecord(
                access_token=SecretStr("test-api-key"),
                refresh_token=SecretStr("test-api-key"),
                expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
                client_id=cid,
                vin=vin,
                container_id="CONT0000EXAMPLE1",
            ),
        )

    settings = Settings(
        _env_file=None,
        pstryk_api_key=SecretStr("test-api-key"),
        solar_latitude=52.23,
        solar_longitude=21.01,
        solar_kwp=10.0,
        deye_device_sn="DEYE0000EXAMPLE1",
        deye_access_token=SecretStr("test-api-key"),
        bmw_cardata_client_id="example-bmw-client",
        bmw_vin="WBA0000000EXAMPLE1",
        mini_cardata_client_id="example-mini-client",
        mini_vin="WMW0000000EXAMPLE2",
        cardata_tokens_file=tokens_file,
    )

    transports = {
        "pstryk": httpx.MockTransport(lambda _r: httpx.Response(200, content=today_only_bytes)),
        "solar": httpx.MockTransport(lambda _r: httpx.Response(503, text="unavailable")),
        "deye": httpx.MockTransport(lambda _r: httpx.Response(401, json={"msg": "bad token"})),
        "cardata": httpx.MockTransport(cardata_mixed_handler),
    }

    # Pick a reference date that leaves Today with 0 slots and Tomorrow not published
    ref_time = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)
    results = run_probes(
        settings,
        target="all",
        transports=transports,
        reference_time=ref_time,
    )

    by_svc = {r.service: r for r in results}
    assert by_svc["pstryk"].status == "ok"
    assert any("0 slots returned" in d for d in by_svc["pstryk"].details)
    assert any("not yet published" in d for d in by_svc["pstryk"].details)
    assert by_svc["solar"].status == "failed"
    assert by_svc["deye"].status == "failed"
    assert by_svc["cardata"].status == "failed"
    assert any("BMW: FAILED" in d for d in by_svc["cardata"].details)
    assert any("MINI (***PLE2)" in d for d in by_svc["cardata"].details)


def test_cli_probe_command_dispatches_and_sets_exit_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_dir = tmp_path / "logs"
    env_file = tmp_path / ".env"
    env_file.write_text(f"LOG_DIR={log_dir.as_posix()}\n", encoding="utf-8")

    # Unconfigured single-service probe -> skipped -> exit code 0
    exit_ok = main(["--env-file", str(env_file), "probe", "--service", "solar"])
    for handler in list(logging.getLogger("cheap_watts").handlers):
        handler.close()
    logging.getLogger("cheap_watts").handlers.clear()

    captured = capsys.readouterr()
    assert exit_ok == 0
    assert "[SKIP] solar" in captured.out
    assert "Summary: 0 ok, 1 skipped, 0 failed" in captured.out

    # Simulate a failing probe -> exit code 1
    env_file.write_text(
        f"LOG_DIR={log_dir.as_posix()}\nPSTRYK_API_KEY=test-api-key\n",
        encoding="utf-8",
    )
    failing_transport = {
        "pstryk": httpx.MockTransport(lambda _r: httpx.Response(401, json={"detail": "denied"}))
    }
    with patch(
        "src.main.run_probes",
        wraps=lambda s, target, **kw: run_probes(s, target, failing_transport, **kw),
    ):
        exit_fail = main(["--env-file", str(env_file), "probe", "-s", "pstryk"])

    for handler in list(logging.getLogger("cheap_watts").handlers):
        handler.close()
    logging.getLogger("cheap_watts").handlers.clear()

    captured_fail = capsys.readouterr()
    assert exit_fail == 1
    assert "[FAIL] pstryk" in captured_fail.out
    assert "Summary: 0 ok, 0 skipped, 1 failed" in captured_fail.out


def test_probe_solar_dual_string_uses_fallback_tilt_and_reports_total_kwp() -> None:
    captured_params: list[tuple[str, str]] = []

    def solar_handler(request: httpx.Request) -> httpx.Response:
        captured_params.append((request.url.params["tilt"], request.url.params["azimuth"]))
        return httpx.Response(
            200,
            json={
                "timezone": "UTC",
                "hourly": {
                    "time": ["2026-11-15T10:00"],
                    "global_tilted_irradiance": [400.0],
                },
            },
        )

    settings = Settings(
        _env_file=None,
        solar_latitude=52.23,
        solar_longitude=21.01,
        solar_kwp=4.5,
        solar_tilt_degrees=30.0,
        solar_azimuth_degrees=-90.0,
        solar_kwp_2=4.5,
        solar_azimuth_degrees_2=90.0,
        solar_system_efficiency=0.85,
    )

    results = run_probes(
        settings,
        target="solar",
        transports={"solar": httpx.MockTransport(solar_handler)},
        reference_time=datetime(2026, 11, 15, 10, 0, tzinfo=UTC),
    )

    assert len(results) == 1
    assert results[0].status == "ok"
    assert "9.0 kWp (2 strings)" in results[0].headline
    assert captured_params == [("30.0", "-90.0"), ("30.0", "90.0")]


def test_probe_deye_prefers_local_solarman_v5_when_configured() -> None:
    telemetry = [0] * 86
    telemetry[0] = 68  # reg 588 SoC
    telemetry[84] = 1500  # reg 672 PV1
    tou = [0] * 32
    tou[0] = 1  # TOU enabled
    for idx, hhmm in enumerate((0, 400, 800, 1200, 1600, 2000)):
        tou[2 + idx] = hhmm
        tou[20 + idx] = 90 if idx == 0 else 25
        tou[26 + idx] = 1 if idx == 0 else 0

    class FakeLocalModbus:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def read_holding_registers(self, register_addr: int, quantity: int) -> list[int]:
            return telemetry if register_addr == 588 else tou

        def disconnect(self) -> None:
            pass

    settings = Settings(
        _env_file=None,
        deye_modbus_host="192.168.1.50",
        deye_modbus_port=8899,
        deye_logger_serial="2700012345",
    )
    assert "DEYE_MODBUS_HOST" not in settings.missing_live_settings()
    assert "DEYE_LOGGER_SERIAL" not in settings.missing_live_settings()

    results = run_probes(settings, target="deye", modbus_factory=FakeLocalModbus)
    assert len(results) == 1
    assert results[0].status == "ok"
    assert "Battery SoC 68%" in results[0].headline
    assert any("Mode: local-lan (192.168.1.50:8899)" in d for d in results[0].details)


def test_probe_pstryk_verbose_lists_all_price_slots(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pstryk_bytes = (FIXTURES / "pstryk_unified_metrics.json").read_bytes()
    ref_time = datetime(2026, 11, 15, 11, 30, tzinfo=UTC)
    settings = Settings(
        _env_file=None,
        log_dir=tmp_path / "logs",
        pstryk_api_key=SecretStr("test-api-key"),
    )
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, content=pstryk_bytes))

    # Non-verbose omits individual hourly slots
    quiet_results = run_probes(
        settings,
        target="pstryk",
        transports={"pstryk": transport},
        reference_time=ref_time,
        verbose=False,
    )
    assert len(quiet_results) == 1
    assert not any("2026-11-15 12:00-13:00" in d for d in quiet_results[0].details)

    # Verbose prints all hourly slots with net prices and `<-- now` marker
    verbose_results = run_probes(
        settings,
        target="pstryk",
        transports={"pstryk": transport},
        reference_time=ref_time,
        verbose=True,
    )
    report = format_probe_report(verbose_results, timezone=settings.timezone)
    assert "2026-11-15 00:00-01:00  0.3444 PLN/kWh (net 0.2800)" in report
    assert "2026-11-15 12:00-13:00  -0.0800 PLN/kWh (net -0.0650) <-- now" in report
    assert "2026-11-16 00:00-01:00  0.2337 PLN/kWh (net 0.1900)" in report

    # CLI `-s pstryk -v` forwards verbose=True through main()
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"LOG_DIR={(tmp_path / 'logs').as_posix()}\nPSTRYK_API_KEY=test-api-key\n",
        encoding="utf-8",
    )
    with patch(
        "src.main.run_probes",
        wraps=lambda s, target, **kw: run_probes(
            s,
            target,
            transports={"pstryk": transport},
            reference_time=ref_time,
            **kw,
        ),
    ):
        rc = main(["--env-file", str(env_file), "probe", "-s", "pstryk", "-v"])

    for handler in list(logging.getLogger("cheap_watts").handlers):
        handler.close()
    logging.getLogger("cheap_watts").handlers.clear()

    out = capsys.readouterr().out
    assert rc == 0
    assert "2026-11-15 12:00-13:00  -0.0800 PLN/kWh (net -0.0650) <-- now" in out
