"""CLI entrypoint for pstryk-pilot."""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from src import __version__
from src.config import Settings, load_settings
from src.logging_setup import setup_logging
from src.probe import ProbeTarget, format_probe_report, run_probes
from src.state import StateStore


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="pstryk-pilot",
        description="Headless HEMS optimizing Deye battery and EV charging with Pstryk tariffs.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Path to environment file (default: .env).",
    )

    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser(
        "run",
        help="Execute a single optimization cycle (intended for cron).",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Simulate decisions and send [DRY-RUN] emails without modifying Deye settings.",
    )

    subparsers.add_parser(
        "status",
        help="Print configuration readiness and last persisted state.",
    )

    probe_parser = subparsers.add_parser(
        "probe",
        help="Run read-only live checks against configured external APIs.",
    )
    probe_parser.add_argument(
        "--service",
        "-s",
        choices=("all", "pstryk", "solar", "deye", "cardata"),
        default="all",
        help="Probe a single service or all services (default: all).",
    )

    return parser


def execute_run(settings: Settings, force_dry_run: bool = False) -> int:
    """Execute one optimization cycle."""
    effective_dry_run = force_dry_run or settings.dry_run
    run_id = uuid.uuid4().hex[:8]
    log_path = setup_logging(
        settings.log_dir,
        level=settings.log_level,
        tz_name=settings.timezone,
    )
    logger = logging.getLogger("pstryk_pilot.main")

    logger.info(
        "Starting pstryk-pilot run",
        extra={
            "event": "run_start",
            "run_id": run_id,
            "dry_run": effective_dry_run,
            "timezone": settings.timezone,
            "log_file": str(log_path),
        },
    )

    store = StateStore(settings.state_file)
    try:
        state = store.load()
        missing = settings.missing_live_settings()
        if missing:
            logger.warning(
                "Some live integration settings are not yet configured",
                extra={
                    "event": "config_incomplete",
                    "run_id": run_id,
                    "dry_run": effective_dry_run,
                    "missing": missing,
                },
            )

        # Phase 2-4 pipeline hook: API polling, decision planner, Deye diff/write, and SMTP alerts.
        logger.info(
            "Optimization cycle completed (Phase 1 scaffold active)",
            extra={
                "event": "run_complete",
                "run_id": run_id,
                "dry_run": effective_dry_run,
                "tracked_deye_slots": len(state.last_deye_schedule),
                "tracked_vehicle_alerts": sorted(state.vehicle_alerts.keys()),
            },
        )

        state.last_run_at = datetime.now(tz=UTC)
        state.last_run_status = "ok"
        state.last_run_dry_run = effective_dry_run
        store.save(state)
        return 0
    except Exception:
        logger.exception(
            "Unhandled error during pstryk-pilot run",
            extra={
                "event": "run_error",
                "run_id": run_id,
                "dry_run": effective_dry_run,
            },
        )
        return 1


def execute_status(settings: Settings) -> int:
    """Print current configuration readiness and persisted state summary."""
    store = StateStore(settings.state_file)
    state = store.load()
    missing = settings.missing_live_settings()

    print(f"pstryk-pilot v{__version__}")
    print(f"  Dry-run (env):   {settings.dry_run}")
    print(f"  Timezone:        {settings.timezone}")
    print(f"  Log directory:   {settings.log_dir}")
    print(f"  State file:      {settings.state_file} (exists={settings.state_file.exists()})")
    print(f"  Last run at:     {state.last_run_at.isoformat() if state.last_run_at else 'never'}")
    print(f"  Last run status: {state.last_run_status or 'n/a'}")
    print(f"  Missing config:  {', '.join(missing) if missing else 'none'}")
    return 0


def execute_probe(settings: Settings, target: ProbeTarget = "all") -> int:
    """Execute read-only API connectivity and parsing probes."""
    setup_logging(
        settings.log_dir,
        level=settings.log_level,
        tz_name=settings.timezone,
    )
    results = run_probes(settings, target=target)
    print(format_probe_report(results, timezone=settings.timezone))
    has_failures = any(res.status == "failed" for res in results)
    return 1 if has_failures else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments and dispatch the requested command."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    env_file: Path | None = args.env_file if args.env_file.exists() else None
    settings = load_settings(env_file=env_file)

    if args.command == "run":
        return execute_run(settings, force_dry_run=args.dry_run)
    if args.command == "status":
        return execute_status(settings)
    if args.command == "probe":
        return execute_probe(settings, target=args.service)

    parser.print_help()
    return 2


def app_main() -> None:
    """Console script entrypoint."""
    sys.exit(main())


if __name__ == "__main__":
    app_main()
