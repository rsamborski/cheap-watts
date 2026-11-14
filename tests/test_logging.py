"""Tests for Linux-style plain-text rotating file logging in local timezone."""

from __future__ import annotations

import logging
from pathlib import Path

from src.logging_setup import setup_logging


def test_setup_logging_writes_linux_style_text_log_in_configured_tz(tmp_path: Path) -> None:
    log_path = setup_logging(tmp_path, level="DEBUG", tz_name="Europe/Warsaw")
    logger = logging.getLogger("pstryk_pilot.test")

    logger.info(
        "Decision evaluated",
        extra={"event": "plan_ready", "dry_run": True, "bmw_soc": 18},
    )

    try:
        raise RuntimeError("simulated api timeout")
    except RuntimeError:
        logger.exception("Failed polling telemetry")

    for handler in list(logging.getLogger("pstryk_pilot").handlers):
        handler.flush()
        handler.close()
    logging.getLogger("pstryk_pilot").handlers.clear()

    content = log_path.read_text(encoding="utf-8")
    lines = content.strip().splitlines()

    assert log_path.name == "pstryk-pilot.log"
    assert ("CET " in lines[0]) or ("CEST " in lines[0])
    assert "UTC" not in lines[0]
    assert "INFO     pstryk_pilot.test[" in lines[0]
    assert "Decision evaluated | event=plan_ready dry_run=True bmw_soc=18" in lines[0]
    assert "ERROR    pstryk_pilot.test[" in content
    assert "RuntimeError: simulated api timeout" in content


def test_rotating_file_handler_uses_os_local_tz_by_default_and_rotates(tmp_path: Path) -> None:
    log_path = setup_logging(tmp_path, max_bytes=300, backup_count=2)
    logger = logging.getLogger("pstryk_pilot.rotate")

    for idx in range(10):
        logger.info("Log rotation entry", extra={"iteration": idx})

    for handler in list(logging.getLogger("pstryk_pilot").handlers):
        handler.flush()
        handler.close()
    logging.getLogger("pstryk_pilot").handlers.clear()

    assert log_path.exists()
    assert (tmp_path / "pstryk-pilot.log.1").exists()
