"""Tests for CLI entrypoint and --dry-run execution."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.main import main
from src.state import StateStore


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
    for handler in list(logging.getLogger("pstryk_pilot").handlers):
        handler.close()
    logging.getLogger("pstryk_pilot").handlers.clear()

    assert exit_code == 0
    state = StateStore(state_file).load()
    assert state.last_run_status == "ok"
    assert state.last_run_dry_run is True
    assert (log_dir / "pstryk-pilot.log").exists()

    status_code = main(["--env-file", str(env_file), "status"])
    captured = capsys.readouterr()
    assert status_code == 0
    assert "Last run status: ok" in captured.out
