"""Tests for CLI entrypoint, --dry-run execution, and cardata-auth subcommand."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from src.clients.cardata import CarDataTokenStore
from src.config import Settings
from src.main import execute_cardata_auth, main
from src.probe import run_probes
from src.state import StateStore

FIXTURES = Path(__file__).parent / "fixtures"


def _clear_app_loggers() -> None:
    for handler in list(logging.getLogger("cheap_watts").handlers):
        handler.close()
    logging.getLogger("cheap_watts").handlers.clear()


def test_cli_run_dry_run_persists_state_and_logs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_dir = tmp_path / "logs"
    state_file = tmp_path / "data" / "state.json"
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"LOG_DIR={log_dir.as_posix()}\nSTATE_FILE={state_file.as_posix()}\nDRY_RUN=false\n",
        encoding="utf-8",
    )

    exit_code = main(["--env-file", str(env_file), "run", "--dry-run"])
    _clear_app_loggers()

    assert exit_code == 0
    state = StateStore(state_file).load()
    assert state.last_run_status == "ok"
    assert state.last_run_dry_run is True
    assert (log_dir / "cheap-watts.log").exists()

    status_code = main(["--env-file", str(env_file), "status"])
    captured = capsys.readouterr()
    assert status_code == 0
    assert "Last run status: ok" in captured.out


def test_cli_cardata_auth_shared_client_id_and_subsequent_probe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_dir = tmp_path / "logs"
    tokens_file = tmp_path / "data" / "cardata_tokens.json"

    # 1. Missing client_id returns exit code 1 with clear guidance
    empty_settings = Settings(
        _env_file=None,
        log_dir=log_dir,
        cardata_tokens_file=tokens_file,
    )
    rc_missing = execute_cardata_auth(empty_settings, vehicle_target="bmw")
    _clear_app_loggers()
    out_missing = capsys.readouterr().out
    assert rc_missing == 1
    assert "BMW_CARDATA_CLIENT_ID" in out_missing

    # 2. Both BMW & MINI share one client_id -> 1 device flow, 2 saved vehicles
    device_code_calls = 0
    bmw_bytes = (FIXTURES / "cardata_bmw_telematic.json").read_bytes()
    mini_bytes = (FIXTURES / "cardata_mini_telematic.json").read_bytes()

    def auth_and_probe_handler(request: httpx.Request) -> httpx.Response:
        nonlocal device_code_calls
        if request.url.path == "/gcdm/oauth/device/code":
            device_code_calls += 1
            return httpx.Response(
                200,
                json={
                    "device_code": "example-device-code",
                    "user_code": "USER-1234",
                    "verification_uri": "https://customer.bmwgroup.com/oneid/#/device",
                    "verification_uri_complete": (
                        "https://customer.bmwgroup.com/oneid/#/device?user_code=USER-1234"
                    ),
                    "expires_in": 900,
                    "interval": 5,
                },
            )
        if request.url.path == "/gcdm/oauth/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "test-api-key",
                    "refresh_token": "test-api-key-rotated",
                    "expires_in": 3600,
                },
            )
        if request.method == "GET" and request.url.path == "/customers/containers":
            return httpx.Response(200, json={"containers": []})
        if request.method == "POST" and request.url.path == "/customers/containers":
            return httpx.Response(201, json={"containerId": "CONT0000EXAMPLE1"})
        if request.url.path == "/customers/vehicles/mappings":
            return httpx.Response(
                200,
                json=[
                    {"vin": "WBA0000000EXAMPLE1", "mappingType": "PRIMARY"},
                    {"vin": "WMW0000000EXAMPLE2", "mappingType": "PRIMARY"},
                ],
            )
        if request.url.path == "/customers/vehicles/WBA0000000EXAMPLE1/telematicData":
            assert request.url.params.get("containerId") == "CONT0000EXAMPLE1"
            return httpx.Response(200, content=bmw_bytes)
        if request.url.path == "/customers/vehicles/WMW0000000EXAMPLE2/telematicData":
            assert request.url.params.get("containerId") == "CONT0000EXAMPLE1"
            return httpx.Response(200, content=mini_bytes)
        raise AssertionError(f"Unexpected URL: {request.url}")

    shared_settings = Settings(
        _env_file=None,
        log_dir=log_dir,
        cardata_tokens_file=tokens_file,
        bmw_cardata_client_id="example-shared-client",
        mini_cardata_client_id="example-shared-client",
    )
    mock_transport = httpx.MockTransport(auth_and_probe_handler)

    rc_ok = execute_cardata_auth(
        shared_settings,
        vehicle_target="all",
        transport=mock_transport,
        sleep_fn=lambda _s: None,
    )
    _clear_app_loggers()
    out_ok = capsys.readouterr().out

    assert rc_ok == 0
    assert device_code_calls == 1
    assert "USER-1234" in out_ok
    assert "[OK] BMW authorized" in out_ok
    assert "[OK] MINI authorized" in out_ok
    assert "test-api-key" not in out_ok

    store = CarDataTokenStore(tokens_file)
    assert store.get("bmw") is not None
    assert store.get("mini") is not None

    # 3. Probe immediately succeeds for both vehicles using persisted tokens + containerId
    probe_results = run_probes(
        shared_settings,
        target="cardata",
        transports={"cardata": mock_transport},
    )
    assert len(probe_results) == 1
    assert probe_results[0].status == "ok"
    assert "Fetched telemetry for 2 vehicle(s)" in probe_results[0].headline

    # 4. CLI main() dispatch for cardata-auth + error exit code 1 on upstream failure
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"LOG_DIR={log_dir.as_posix()}\n"
        f"CARDATA_TOKENS_FILE={tokens_file.as_posix()}\n"
        "BMW_CARDATA_CLIENT_ID=example-shared-client\n",
        encoding="utf-8",
    )
    fail_transport = httpx.MockTransport(
        lambda _r: httpx.Response(401, json={"error": "invalid_client"})
    )
    with patch(
        "src.main.execute_cardata_auth",
        wraps=lambda s, vehicle_target: execute_cardata_auth(
            s,
            vehicle_target=vehicle_target,
            transport=fail_transport,
            sleep_fn=lambda _s: None,
        ),
    ):
        rc_cli_fail = main(["--env-file", str(env_file), "cardata-auth"])
    _clear_app_loggers()
    out_fail = capsys.readouterr().out
    assert rc_cli_fail == 1
    assert "[SKIP] MINI:" in out_fail
    assert "[FAIL] CarData authorization failed" in out_fail
