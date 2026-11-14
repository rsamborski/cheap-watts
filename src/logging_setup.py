"""Linux-style plain-text rotating file and console logging setup."""

from __future__ import annotations

import logging
import sys
from datetime import datetime, tzinfo
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

_STANDARD_LOG_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class PlainTextLogFormatter(logging.Formatter):
    """Formats log records as standard Linux-style plain-text log lines in local timezone."""

    def __init__(self, tz_name: str | None = None) -> None:
        super().__init__()
        self._tz: tzinfo | None = ZoneInfo(tz_name) if tz_name else None

    def format(self, record: logging.LogRecord) -> str:
        if self._tz is not None:
            local_dt = datetime.fromtimestamp(record.created, tz=self._tz)
        else:
            local_dt = datetime.fromtimestamp(record.created).astimezone()
        ts = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
        extras = [
            f"{key}={value}"
            for key, value in record.__dict__.items()
            if key not in _STANDARD_LOG_RECORD_ATTRS and not key.startswith("_")
        ]
        suffix = f" | {' '.join(extras)}" if extras else ""
        base = (
            f"{ts} {record.levelname:<8} "
            f"{record.name}[{record.process}]: {record.getMessage()}{suffix}"
        )
        if record.exc_info:
            return f"{base}\n{self.formatException(record.exc_info)}"
        return base


def setup_logging(
    log_dir: Path,
    level: str = "INFO",
    log_filename: str = "pstryk-pilot.log",
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
    tz_name: str | None = None,
) -> Path:
    """Configure console + rotating plain-text file logging for pstryk_pilot.

    When ``tz_name`` is provided (e.g. ``Europe/Warsaw``), timestamps use that timezone;
    otherwise the host OS local timezone is used.
    Returns the path to the active .log file.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / log_filename

    logger = logging.getLogger("pstryk_pilot")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    # Cleanly close & detach existing handlers to avoid duplicate entries or leaked FDs.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = PlainTextLogFormatter(tz_name=tz_name)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return log_path
