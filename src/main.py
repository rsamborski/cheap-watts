"""CLI entrypoint for pstryk-pilot."""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx

from src import __version__
from src.clients.cardata import (
    CarDataClient,
    CarDataTokenRecord,
    CarDataTokenStore,
    CarDataVehicleConfig,
)
from src.clients.deye import DeyeCloudClient, SolarmanV5Client
from src.clients.errors import ApiClientError
from src.clients.pstryk import PstrykClient
from src.clients.solar import SolarArray, SolarForecastClient
from src.config import Settings, load_settings
from src.engine.diff import diff_tou_schedules, evaluate_cycle_actions
from src.engine.planner import plan_charging_schedule
from src.logging_setup import setup_logging
from src.models import TouSlot, VehicleStatus
from src.notify.email import SmtpNotifier
from src.probe import (
    ProbeTarget,
    ServiceName,
    _mask_identifier,
    build_cardata_vehicles,
    format_probe_report,
    run_probes,
)
from src.state import StateStore

VehicleTarget = Literal["all", "bmw", "mini"]


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

    auth_parser = subparsers.add_parser(
        "cardata-auth",
        help="Authorize BMW / MINI CarData via OAuth 2.0 Device Code + PKCE flow.",
    )
    auth_parser.add_argument(
        "--vehicle",
        "-v",
        choices=("all", "bmw", "mini"),
        default="all",
        help="Authorize both vehicles or a single vehicle (default: all).",
    )

    return parser


def _has_core_pipeline_config(settings: Settings) -> bool:
    """Return True when Pstryk, Solar, and Deye inverter connectors are configured."""
    has_pstryk = settings.pstryk_api_key is not None
    has_solar = (
        settings.solar_latitude is not None
        and settings.solar_longitude is not None
        and settings.solar_kwp is not None
    )
    has_local_deye = bool(settings.deye_modbus_host and settings.deye_logger_serial)
    has_cloud_deye = bool(
        settings.deye_device_sn
        and (
            settings.deye_access_token is not None
            or (
                settings.deye_app_id
                and settings.deye_app_secret is not None
                and settings.deye_email
                and settings.deye_password is not None
            )
        )
    )
    return has_pstryk and has_solar and (has_local_deye or has_cloud_deye)


def _build_extra_solar_arrays(settings: Settings) -> list[SolarArray]:
    if settings.solar_kwp_2 is None:
        return []
    return [
        SolarArray(
            kwp=settings.solar_kwp_2,
            tilt_degrees=(
                settings.solar_tilt_degrees_2
                if settings.solar_tilt_degrees_2 is not None
                else settings.solar_tilt_degrees
            ),
            azimuth_degrees=(
                settings.solar_azimuth_degrees_2
                if settings.solar_azimuth_degrees_2 is not None
                else settings.solar_azimuth_degrees
            ),
        )
    ]


@contextlib.contextmanager
def _open_inverter_client(
    settings: Settings,
    transport: httpx.BaseTransport | None = None,
    modbus_factory: Callable[..., Any] | None = None,
) -> Any:
    if settings.deye_modbus_host and settings.deye_logger_serial:
        with SolarmanV5Client(
            host=settings.deye_modbus_host,
            logger_serial=settings.deye_logger_serial,
            port=settings.deye_modbus_port,
            device_sn=settings.deye_device_sn,
            modbus_factory=modbus_factory,
        ) as local_client:
            yield local_client
        return

    assert settings.deye_device_sn is not None
    with DeyeCloudClient(
        device_sn=settings.deye_device_sn,
        base_url=settings.deye_api_base_url,
        access_token=settings.deye_access_token,
        app_id=settings.deye_app_id,
        app_secret=settings.deye_app_secret,
        email=settings.deye_email,
        password=settings.deye_password,
        transport=transport,
    ) as cloud_client:
        yield cloud_client


def _poll_vehicles_gracefully(
    settings: Settings,
    logger: logging.Logger,
    transport: httpx.BaseTransport | None = None,
) -> list[VehicleStatus]:
    token_store = CarDataTokenStore(settings.cardata_tokens_file)
    vehicles = [v for v in build_cardata_vehicles(settings) if v.has_credentials(token_store)]
    if not vehicles:
        return []

    statuses: list[VehicleStatus] = []
    with CarDataClient(
        base_url=settings.cardata_base_url,
        token_url=settings.cardata_token_url,
        device_code_url=settings.cardata_device_code_url,
        transport=transport,
        token_store=token_store,
    ) as client:
        for veh in vehicles:
            try:
                statuses.append(client.fetch_vehicle_status(veh))
            except Exception as exc:
                logger.warning(
                    "CarData poll failed for vehicle; continuing cycle without this EV",
                    extra={
                        "event": "cardata_poll_degraded",
                        "vehicle": veh.vehicle_key,
                        "error": str(exc),
                    },
                )
    return statuses


def execute_run(
    settings: Settings,
    force_dry_run: bool = False,
    transports: Mapping[ServiceName, httpx.BaseTransport] | None = None,
    modbus_factory: Callable[..., Any] | None = None,
    smtp_factory: Callable[..., Any] | None = None,
    now: datetime | None = None,
) -> int:
    """Execute one full end-to-end optimization cycle."""
    effective_dry_run = force_dry_run or settings.dry_run
    current_time = now or datetime.now(tz=UTC)
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
    notifier = SmtpNotifier(settings, smtp_factory=smtp_factory)
    transport_map = transports or {}

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

        if not _has_core_pipeline_config(settings):
            logger.info(
                "Skipping live optimization pipeline (core Pstryk/Solar/Deye config incomplete)",
                extra={
                    "event": "run_skipped_unconfigured",
                    "run_id": run_id,
                    "dry_run": effective_dry_run,
                },
            )
            state.last_run_at = current_time
            state.last_run_status = "ok"
            state.last_run_dry_run = effective_dry_run
            store.save(state)
            return 0

        assert settings.pstryk_api_key is not None
        assert settings.solar_latitude is not None
        assert settings.solar_longitude is not None
        assert settings.solar_kwp is not None

        with PstrykClient(
            api_key=settings.pstryk_api_key,
            base_url=settings.pstryk_base_url,
            tz_name=settings.timezone,
            transport=transport_map.get("pstryk"),
        ) as pstryk_client:
            price_slots = pstryk_client.fetch_today_and_tomorrow(reference_time=current_time)

        with SolarForecastClient(
            latitude=settings.solar_latitude,
            longitude=settings.solar_longitude,
            kwp=settings.solar_kwp,
            base_url=settings.solar_base_url,
            tilt_degrees=settings.solar_tilt_degrees,
            azimuth_degrees=settings.solar_azimuth_degrees,
            system_efficiency=settings.solar_system_efficiency,
            tz_name=settings.timezone,
            transport=transport_map.get("solar"),
            extra_arrays=_build_extra_solar_arrays(settings),
        ) as solar_client:
            solar_forecast = solar_client.fetch_forecast(forecast_days=2)

        vehicles = _poll_vehicles_gracefully(
            settings=settings,
            logger=logger,
            transport=transport_map.get("cardata"),
        )

        # Clear alert cooldown state for vehicles that recharged above threshold
        for veh in vehicles:
            threshold = (
                settings.bmw_soc_threshold
                if veh.vehicle_key == "bmw"
                else settings.mini_soc_threshold
            )
            if not veh.is_charging and veh.soc_pct > threshold:
                StateStore.clear_vehicle_alert(state, veh.vehicle_key)

        with _open_inverter_client(
            settings=settings,
            transport=transport_map.get("deye"),
            modbus_factory=modbus_factory,
        ) as deye_client:
            inverter_status = deye_client.get_status()
            plan = plan_charging_schedule(
                settings=settings,
                inverter=inverter_status,
                price_slots=price_slots,
                solar_forecast=solar_forecast,
                vehicles=vehicles,
                now=current_time,
            )

            hardware_diff = diff_tou_schedules(
                current_slots=inverter_status.tou_slots,
                target_slots=plan.target_tou_slots,
                current_tou_enabled=inverter_status.tou_enabled,
            )
            if effective_dry_run and state.last_run_dry_run and len(state.last_deye_schedule) == 6:
                baseline_slots = [TouSlot.from_record(r) for r in state.last_deye_schedule]
                effective_diff = diff_tou_schedules(
                    current_slots=baseline_slots,
                    target_slots=plan.target_tou_slots,
                    current_tou_enabled=True,
                )
            else:
                effective_diff = hardware_diff

            decision = evaluate_cycle_actions(
                plan=plan,
                schedule_diff=effective_diff,
                state=state,
                cooldown_hours=settings.alert_cooldown_hours,
                dry_run=effective_dry_run,
                now=current_time,
            )

            logger.info(
                f"Plan rationale: {plan.rationale} | {hardware_diff.format_audit_log()}",
                extra={
                    "event": "plan_computed",
                    "run_id": run_id,
                    "dry_run": effective_dry_run,
                    "solar_sufficient": plan.solar_sufficient,
                    "grid_deficit_kwh": plan.grid_deficit_kwh,
                    "deye_changed": effective_diff.has_changes,
                    "ev_alerts": len(decision.notifiable_ev_recommendations),
                },
            )

            if decision.should_write_deye and not effective_dry_run:
                deye_client.write_tou_schedule(
                    plan.target_tou_slots,
                    dry_run=effective_dry_run,
                    enable_tou=True,
                )
                state.last_deye_schedule = [s.to_record() for s in plan.target_tou_slots]
                state.last_deye_updated_at = current_time
            elif effective_dry_run and effective_diff.has_changes:
                logger.info(
                    "Dry-run mode active: skipped Deye TOU write",
                    extra={"event": "deye_write_skipped_dry_run", "run_id": run_id},
                )
                state.last_deye_schedule = [s.to_record() for s in plan.target_tou_slots]

        email_sent = False
        if decision.should_send_email:
            email_sent = notifier.send_cycle_notification(
                plan=plan,
                decision=decision,
                inverter=inverter_status,
            )
            if email_sent:
                for rec in decision.notifiable_ev_recommendations:
                    StateStore.record_vehicle_notification(
                        state=state,
                        vehicle_key=rec.vehicle_key,
                        soc=rec.soc_pct,
                        recommended_window=rec.recommended_window,
                        now=current_time,
                    )

        logger.info(
            "Optimization cycle completed",
            extra={
                "event": "run_complete",
                "run_id": run_id,
                "dry_run": effective_dry_run,
                "deye_written": decision.should_write_deye and not effective_dry_run,
                "email_sent": email_sent,
                "tracked_deye_slots": len(state.last_deye_schedule),
                "tracked_vehicle_alerts": sorted(state.vehicle_alerts.keys()),
            },
        )

        state.last_run_at = current_time
        state.last_run_status = "ok"
        state.last_run_dry_run = effective_dry_run
        store.save(state)
        return 0
    except Exception as exc:
        logger.exception(
            "Unhandled error during pstryk-pilot run",
            extra={
                "event": "run_error",
                "run_id": run_id,
                "dry_run": effective_dry_run,
            },
        )
        with contextlib.suppress(Exception):
            err_state = store.load()
            err_state.last_run_at = current_time
            err_state.last_run_status = "error"
            err_state.last_run_dry_run = effective_dry_run
            if StateStore.should_notify_error(
                err_state,
                cooldown_hours=settings.alert_cooldown_hours,
                now=current_time,
            ) and notifier.send_failure_alert(
                run_id=run_id,
                error_summary=f"{exc.__class__.__name__}: {exc}",
                dry_run=effective_dry_run,
                now=current_time,
            ):
                err_state.last_error_notified_at = current_time
            store.save(err_state)
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


def execute_cardata_auth(
    settings: Settings,
    vehicle_target: VehicleTarget = "all",
    transport: httpx.BaseTransport | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int:
    """Run interactive OAuth 2.0 Device Code + PKCE flow for BMW / MINI CarData."""
    setup_logging(
        settings.log_dir,
        level=settings.log_level,
        tz_name=settings.timezone,
    )
    logger = logging.getLogger("pstryk_pilot.main")

    all_vehicles = build_cardata_vehicles(settings)
    selected = [
        v for v in all_vehicles if vehicle_target == "all" or v.vehicle_key == vehicle_target
    ]
    with_client_id = [v for v in selected if v.client_id and v.client_id.strip()]

    if not with_client_id:
        if vehicle_target == "bmw":
            missing_hint = "BMW_CARDATA_CLIENT_ID"
        elif vehicle_target == "mini":
            missing_hint = "MINI_CARDATA_CLIENT_ID"
        else:
            missing_hint = "BMW_CARDATA_CLIENT_ID and/or MINI_CARDATA_CLIENT_ID"
        print(
            f"[FAIL] No CarData Client ID configured ({missing_hint}).\n"
            "  1. Sign in to My BMW / MINI Portal -> BMW CarData -> Create CarData Client.\n"
            "  2. Copy the Client ID into your .env file and re-run this command."
        )
        return 1

    for skipped in selected:
        if not (skipped.client_id and skipped.client_id.strip()):
            print(
                f"  [SKIP] {skipped.display_name}: "
                f"{skipped.vehicle_key.upper()}_CARDATA_CLIENT_ID not set in .env"
            )

    grouped: dict[str, list[CarDataVehicleConfig]] = {}
    for veh in with_client_id:
        assert veh.client_id is not None
        cid = veh.client_id.strip()
        grouped.setdefault(cid, []).append(veh)

    token_store = CarDataTokenStore(settings.cardata_tokens_file)

    try:
        with CarDataClient(
            base_url=settings.cardata_base_url,
            token_url=settings.cardata_token_url,
            device_code_url=settings.cardata_device_code_url,
            transport=transport,
            token_store=token_store,
        ) as client:
            for client_id, group_vehicles in grouped.items():
                label = ", ".join(v.display_name for v in group_vehicles)
                challenge = client.request_device_code(client_id)
                target_url = challenge.verification_uri_complete or challenge.verification_uri
                minutes = max(1, round(challenge.expires_in / 60))

                print(f"\n=== BMW Group CarData Authorization ({label}) ===", flush=True)
                print(f"  1. Open in your browser:\n     {target_url}", flush=True)
                if (
                    challenge.verification_uri_complete
                    and challenge.verification_uri != challenge.verification_uri_complete
                ):
                    print(f"     (Base URL: {challenge.verification_uri})", flush=True)
                print(f"  2. Sign in and confirm user code: {challenge.user_code}", flush=True)
                print(f"Waiting for browser approval (expires in ~{minutes}m)...", flush=True)

                bundle = client.poll_device_token(
                    client_id=client_id,
                    challenge=challenge,
                    sleep_fn=sleep_fn,
                )
                access_raw = bundle.access_token.get_secret_value()
                container_id = client.ensure_container(access_raw)

                now = datetime.now(tz=UTC)
                for veh in group_vehicles:
                    vin = (
                        veh.vin.strip()
                        if veh.vin and veh.vin.strip()
                        else client.discover_vin(access_raw, vehicle_key=veh.vehicle_key)
                    )
                    token_store.upsert(
                        veh.vehicle_key,
                        CarDataTokenRecord(
                            access_token=bundle.access_token,
                            refresh_token=bundle.refresh_token,
                            expires_at=now + timedelta(seconds=bundle.expires_in),
                            client_id=client_id,
                            vin=vin,
                            container_id=container_id,
                            updated_at=now,
                        ),
                    )
                    logger.info(
                        "Authorized CarData vehicle",
                        extra={
                            "event": "cardata_auth_ok",
                            "vehicle": veh.vehicle_key,
                        },
                    )
                    print(
                        f"  [OK] {veh.display_name} authorized | "
                        f"VIN: {_mask_identifier(vin)} | "
                        f"Container: {_mask_identifier(container_id)}",
                        flush=True,
                    )
    except ApiClientError as exc:
        logger.error(
            "CarData authorization failed",
            extra={"event": "cardata_auth_failed", "error": str(exc)},
        )
        print(f"[FAIL] CarData authorization failed: {exc}")
        return 1

    print(
        f"\nSaved rotated tokens to {settings.cardata_tokens_file}.\n"
        "Next step: uv run pstryk-pilot probe -s cardata"
    )
    return 0


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
    if args.command == "cardata-auth":
        return execute_cardata_auth(settings, vehicle_target=args.vehicle)

    parser.print_help()
    return 2


def app_main() -> None:
    """Console script entrypoint."""
    sys.exit(main())


if __name__ == "__main__":
    app_main()
