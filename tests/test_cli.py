"""Tests for CLI entrypoint, --dry-run execution, and cardata-auth subcommand."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
import pytest

from src.clients.cardata import CarDataTokenStore
from src.config import Settings
from src.main import app_main, execute_cardata_auth, execute_postpone, main, parse_postpone_until
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


def test_cli_no_args_prints_help_without_running_cycle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with patch("src.main.execute_run") as mock_run:
        exit_empty = main([])
        out_empty = capsys.readouterr().out
        assert exit_empty == 0
        mock_run.assert_not_called()
        assert "usage: cheap-watts" in out_empty
        for subcommand in ("run", "status", "probe", "cardata-auth"):
            assert subcommand in out_empty

        exit_flag_only = main(["--env-file", str(tmp_path / ".env")])
        out_flag_only = capsys.readouterr().out
        assert exit_flag_only == 0
        mock_run.assert_not_called()
        assert "usage: cheap-watts" in out_flag_only

        monkeypatch.setattr("sys.argv", ["cheap-watts"])
        with pytest.raises(SystemExit) as exc_info:
            app_main()
        out_app_main = capsys.readouterr().out
        assert exc_info.value.code == 0
        mock_run.assert_not_called()
        assert "usage: cheap-watts" in out_app_main


def test_dockerfile_does_not_default_to_run_command() -> None:
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    instructions = [
        line.strip()
        for line in dockerfile.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert 'ENTRYPOINT ["cheap-watts"]' in instructions
    assert not any(line.startswith("CMD") for line in instructions)


def test_cli_postpone_until_clear_no_run_and_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    warsaw = ZoneInfo("Europe/Warsaw")
    now = datetime(2026, 11, 22, 11, 0, tzinfo=warsaw)

    assert parse_postpone_until("16:00", now, warsaw) == datetime(
        2026, 11, 22, 16, 0, tzinfo=warsaw
    )
    assert parse_postpone_until("10:00", now, warsaw) == datetime(
        2026, 11, 23, 10, 0, tzinfo=warsaw
    )
    assert parse_postpone_until("2026-11-23T08:30", now, warsaw) == datetime(
        2026, 11, 23, 8, 30, tzinfo=warsaw
    )
    for invalid in ("", "25:99", "not-a-time", "2026-11-22T09:00"):
        with pytest.raises(ValueError):
            parse_postpone_until(invalid, now, warsaw)

    log_dir = tmp_path / "logs"
    state_file = tmp_path / "data" / "state.json"
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"LOG_DIR={log_dir.as_posix()}\nSTATE_FILE={state_file.as_posix()}\nTIMEZONE=Europe/Warsaw\n",
        encoding="utf-8",
    )

    rc_cli = main(
        [
            "--env-file",
            str(env_file),
            "postpone",
            "-v",
            "mini",
            "--until",
            "2099-01-01T16:00",
            "--no-run",
        ]
    )
    out_cli = capsys.readouterr().out
    assert rc_cli == 0
    assert "Postponed MINI EV charging until 2099-01-01T16:00:00+01:00" in out_cli

    rc_status = main(["--env-file", str(env_file), "status"])
    out_status = capsys.readouterr().out
    assert rc_status == 0
    assert "EV [mini]:" in out_status
    assert "postponed_until=2099-01-01T16:00:00+01:00" in out_status

    settings = Settings(
        _env_file=None,
        log_dir=log_dir,
        state_file=state_file,
        timezone="Europe/Warsaw",
    )
    rc_bad = execute_postpone(settings, vehicle_key="mini", until_raw="99:99", no_run=True, now=now)
    assert rc_bad == 2
    assert "Invalid --until value" in capsys.readouterr().out

    # Calling postpone with immediate run triggers execute_run (cleanly skips unconfigured)
    rc_run = execute_postpone(
        settings, vehicle_key="mini", until_raw="16:00", no_run=False, now=now
    )
    _clear_app_loggers()
    assert rc_run == 0
    saved = StateStore(state_file).load()
    assert saved.vehicle_alerts["mini"].postponed_until == datetime(
        2026, 11, 22, 16, 0, tzinfo=warsaw
    )

    rc_clear = main(["--env-file", str(env_file), "postpone", "-v", "mini", "--clear", "--no-run"])
    assert rc_clear == 0
    assert "Cleared EV charging postponement for MINI." in capsys.readouterr().out
    assert "mini" not in StateStore(state_file).load().vehicle_alerts
