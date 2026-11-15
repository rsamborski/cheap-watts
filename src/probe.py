"""Read-only live API smoke-test ('probe') runner for Phase 2 connectors."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx

from src import __version__
from src.clients.cardata import CarDataClient, CarDataVehicleConfig
from src.clients.deye import DeyeCloudClient, SolarmanV5Client
from src.clients.errors import ApiClientError
from src.clients.pstryk import PstrykClient
from src.clients.solar import SolarArray, SolarForecastClient
from src.config import Settings

logger = logging.getLogger("pstryk_pilot.probe")

ServiceName = Literal["pstryk", "solar", "deye", "cardata"]
ProbeTarget = Literal["all", "pstryk", "solar", "deye", "cardata"]
ProbeStatus = Literal["ok", "skipped", "failed"]

ALL_SERVICES: tuple[ServiceName, ...] = ("pstryk", "solar", "deye", "cardata")


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of probing a single external read-only integration."""

    service: ServiceName
    status: ProbeStatus
    headline: str
    details: list[str] = field(default_factory=list)


def _mask_identifier(value: str | None) -> str:
    """Mask all but the last 4 characters of a device SN or VIN for display."""
    if not value:
        return "n/a"
    cleaned = value.strip()
    if len(cleaned) <= 4:
        return "****"
    return f"***{cleaned[-4:]}"


def build_cardata_vehicles(settings: Settings) -> list[CarDataVehicleConfig]:
    """Build BMW and MINI CarData configurations from Settings."""
    return [
        CarDataVehicleConfig(
            vehicle_key="bmw",
            display_name="BMW",
            client_id=settings.bmw_cardata_client_id,
            vin=settings.bmw_vin,
            access_token=settings.bmw_cardata_access_token,
            refresh_token=settings.bmw_cardata_refresh_token,
        ),
        CarDataVehicleConfig(
            vehicle_key="mini",
            display_name="MINI",
            client_id=settings.mini_cardata_client_id,
            vin=settings.mini_vin,
            access_token=settings.mini_cardata_access_token,
            refresh_token=settings.mini_cardata_refresh_token,
        ),
    ]


def probe_pstryk(
    settings: Settings,
    transport: httpx.BaseTransport | None = None,
    reference_time: datetime | None = None,
) -> ProbeResult:
    """Probe Pstryk dynamic hourly electricity tariffs (read-only)."""
    if settings.pstryk_api_key is None:
        return ProbeResult(
            service="pstryk",
            status="skipped",
            headline="Not configured (set PSTRYK_API_KEY in .env)",
        )

    tz = ZoneInfo(settings.timezone)
    now_local = (reference_time or datetime.now(tz=UTC)).astimezone(tz)
    today_date = now_local.date()
    tomorrow_date = today_date + timedelta(days=1)

    try:
        with PstrykClient(
            api_key=settings.pstryk_api_key,
            base_url=settings.pstryk_base_url,
            tz_name=settings.timezone,
            transport=transport,
        ) as client:
            slots = client.fetch_today_and_tomorrow(reference_time=reference_time)
    except (ApiClientError, Exception) as exc:
        logger.warning(
            "Pstryk probe failed",
            extra={"event": "probe_failed", "service": "pstryk", "error": str(exc)},
        )
        return ProbeResult(service="pstryk", status="failed", headline=str(exc))

    today_slots = [s for s in slots if s.start.date() == today_date]
    tomorrow_slots = [s for s in slots if s.start.date() == tomorrow_date]

    details: list[str] = []
    if today_slots:
        cheapest_today = min(today_slots, key=lambda s: s.price_pln_kwh)
        avg_today = sum(s.price_pln_kwh for s in today_slots) / len(today_slots)
        max_today = max(s.price_pln_kwh for s in today_slots)
        details.append(
            f"Today ({today_date}): {len(today_slots)} slots | "
            f"min={cheapest_today.price_pln_kwh:.4f} ({cheapest_today.start.strftime('%H:%M')}) | "
            f"avg={avg_today:.4f} | max={max_today:.4f} PLN/kWh"
        )
    else:
        details.append(f"Today ({today_date}): 0 slots returned")

    if tomorrow_slots:
        cheapest_tmr = min(tomorrow_slots, key=lambda s: s.price_pln_kwh)
        avg_tmr = sum(s.price_pln_kwh for s in tomorrow_slots) / len(tomorrow_slots)
        max_tmr = max(s.price_pln_kwh for s in tomorrow_slots)
        details.append(
            f"Tomorrow ({tomorrow_date}): {len(tomorrow_slots)} slots | "
            f"min={cheapest_tmr.price_pln_kwh:.4f} ({cheapest_tmr.start.strftime('%H:%M')}) | "
            f"avg={avg_tmr:.4f} | max={max_tmr:.4f} PLN/kWh"
        )
    else:
        details.append(f"Tomorrow ({tomorrow_date}): not yet published (expected after ~14:00 CET)")

    return ProbeResult(
        service="pstryk",
        status="ok",
        headline=f"Fetched {len(slots)} hourly price slots",
        details=details,
    )


def probe_solar(
    settings: Settings,
    transport: httpx.BaseTransport | None = None,
    reference_time: datetime | None = None,
) -> ProbeResult:
    """Probe Open-Meteo solar irradiance and PV forecast (read-only)."""
    missing = [
        name
        for name, val in (
            ("SOLAR_LATITUDE", settings.solar_latitude),
            ("SOLAR_LONGITUDE", settings.solar_longitude),
            ("SOLAR_KWP", settings.solar_kwp),
        )
        if val is None
    ]
    if missing:
        return ProbeResult(
            service="solar",
            status="skipped",
            headline=f"Not configured (set {', '.join(missing)} in .env)",
        )

    assert settings.solar_latitude is not None
    assert settings.solar_longitude is not None
    assert settings.solar_kwp is not None

    tz = ZoneInfo(settings.timezone)
    now_local = (reference_time or datetime.now(tz=UTC)).astimezone(tz)
    today_date = now_local.date()
    tomorrow_date = today_date + timedelta(days=1)

    extra_arrays: list[SolarArray] = []
    if settings.solar_kwp_2 is not None:
        extra_arrays.append(
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
        )

    try:
        with SolarForecastClient(
            latitude=settings.solar_latitude,
            longitude=settings.solar_longitude,
            kwp=settings.solar_kwp,
            base_url=settings.solar_base_url,
            tilt_degrees=settings.solar_tilt_degrees,
            azimuth_degrees=settings.solar_azimuth_degrees,
            system_efficiency=settings.solar_system_efficiency,
            tz_name=settings.timezone,
            transport=transport,
            extra_arrays=extra_arrays,
        ) as client:
            forecast = client.fetch_forecast(forecast_days=2)
    except (ApiClientError, Exception) as exc:
        logger.warning(
            "Solar probe failed",
            extra={"event": "probe_failed", "service": "solar", "error": str(exc)},
        )
        return ProbeResult(service="solar", status="failed", headline=str(exc))

    today_kwh = forecast.total_kwh_for_date(today_date)
    tomorrow_kwh = forecast.total_kwh_for_date(tomorrow_date)
    threshold = settings.solar_sufficient_kwh_threshold
    total_kwp = settings.solar_kwp + sum(arr.kwp for arr in extra_arrays)
    array_label = f" ({1 + len(extra_arrays)} strings)" if extra_arrays else ""

    details = [
        f"Today ({today_date}): {today_kwh:.2f} kWh "
        f"(sufficient={today_kwh >= threshold}, threshold={threshold:.1f} kWh)",
        f"Tomorrow ({tomorrow_date}): {tomorrow_kwh:.2f} kWh "
        f"(sufficient={tomorrow_kwh >= threshold}, threshold={threshold:.1f} kWh)",
    ]
    if forecast.hours:
        peak = max(forecast.hours, key=lambda h: h.estimated_kwh)
        details.append(
            f"Peak hour: {peak.start.strftime('%Y-%m-%d %H:%M')} -> "
            f"{peak.estimated_kwh:.2f} kWh ({peak.irradiance_wm2:.0f} W/m²)"
        )

    return ProbeResult(
        service="solar",
        status="ok",
        headline=f"48h total {forecast.total_kwh:.2f} kWh ({total_kwp} kWp{array_label})",
        details=details,
    )


def probe_deye(
    settings: Settings,
    transport: httpx.BaseTransport | None = None,
    modbus_factory: Callable[..., Any] | None = None,
) -> ProbeResult:
    """Probe Deye inverter telemetry and current 6-slot TOU schedule (Local LAN or Cloud)."""
    has_local_lan = bool(settings.deye_modbus_host and settings.deye_logger_serial)
    has_direct_token = settings.deye_access_token is not None
    has_account_creds = all(
        (
            settings.deye_app_id,
            settings.deye_app_secret is not None,
            settings.deye_email,
            settings.deye_password is not None,
        )
    )
    has_cloud = bool(settings.deye_device_sn and (has_direct_token or has_account_creds))

    if not has_local_lan and not has_cloud:
        return ProbeResult(
            service="deye",
            status="skipped",
            headline=(
                "Not configured (set DEYE_MODBUS_HOST + DEYE_LOGGER_SERIAL for local LAN, "
                "or DEYE_DEVICE_SN + Deye Cloud credentials in .env)"
            ),
        )

    try:
        if has_local_lan:
            assert settings.deye_modbus_host is not None
            assert settings.deye_logger_serial is not None
            mode_label = f"local-lan ({settings.deye_modbus_host}:{settings.deye_modbus_port})"
            with SolarmanV5Client(
                host=settings.deye_modbus_host,
                logger_serial=settings.deye_logger_serial,
                port=settings.deye_modbus_port,
                device_sn=settings.deye_device_sn,
                modbus_factory=modbus_factory,
            ) as local_client:
                status = local_client.get_status()
        else:
            assert settings.deye_device_sn is not None
            mode_label = "deye-cloud"
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
                status = cloud_client.get_status()
    except (ApiClientError, Exception) as exc:
        logger.warning(
            "Deye probe failed",
            extra={"event": "probe_failed", "service": "deye", "error": str(exc)},
        )
        return ProbeResult(service="deye", status="failed", headline=str(exc))

    details = [
        f"Mode: {mode_label} | Device: {_mask_identifier(status.device_sn)} | "
        f"Battery power: {status.battery_power_w:.0f}W | TOU enabled: {status.tou_enabled}"
    ]
    for slot in status.tou_slots:
        grid_flag = "ON" if slot.grid_charge else "OFF"
        sell_flag = "ON" if slot.sell else "OFF"
        details.append(
            f"TOU #{slot.slot_index}: {slot.start_time}-{slot.end_time} | "
            f"target={slot.target_soc}% | grid_charge={grid_flag} | sell={sell_flag}"
        )

    return ProbeResult(
        service="deye",
        status="ok",
        headline=(
            f"Battery SoC {status.battery_soc}% | PV {status.pv_power_w:.0f}W | "
            f"Load {status.load_power_w:.0f}W | Grid {status.grid_power_w:.0f}W"
        ),
        details=details,
    )


def probe_cardata(
    settings: Settings,
    transport: httpx.BaseTransport | None = None,
) -> ProbeResult:
    """Probe BMW and MINI EV telemetry via BMW CarData (read-only)."""
    vehicles = build_cardata_vehicles(settings)
    configured = [v for v in vehicles if v.is_configured]
    if not configured:
        return ProbeResult(
            service="cardata",
            status="skipped",
            headline=(
                "Not configured (set *_CARDATA_ACCESS_TOKEN or "
                "*_CARDATA_CLIENT_ID + *_CARDATA_REFRESH_TOKEN in .env)"
            ),
        )

    details: list[str] = []
    errors: list[str] = []
    ok_vehicles = 0

    with CarDataClient(
        base_url=settings.cardata_base_url,
        token_url=settings.cardata_token_url,
        transport=transport,
    ) as client:
        for vehicle in vehicles:
            if not vehicle.is_configured:
                details.append(f"{vehicle.display_name}: skipped (not configured)")
                continue

            threshold = (
                settings.bmw_soc_threshold
                if vehicle.vehicle_key == "bmw"
                else settings.mini_soc_threshold
            )
            try:
                st = client.fetch_vehicle_status(vehicle)
            except (ApiClientError, Exception) as exc:
                logger.warning(
                    "CarData vehicle probe failed",
                    extra={
                        "event": "probe_failed",
                        "service": "cardata",
                        "vehicle": vehicle.vehicle_key,
                        "error": str(exc),
                    },
                )
                errors.append(f"{vehicle.display_name}: {exc}")
                details.append(f"{vehicle.display_name}: FAILED ({exc})")
                continue

            ok_vehicles += 1
            extras: list[str] = []
            if st.target_soc_pct is not None:
                extras.append(f"target={st.target_soc_pct}%")
            if st.time_remaining_minutes is not None:
                extras.append(f"remaining={st.time_remaining_minutes}m")
            extra_suffix = f" | {' | '.join(extras)}" if extras else ""
            details.append(
                f"{st.display_name} ({_mask_identifier(st.vin)}): "
                f"SoC={st.soc_pct}% (threshold={threshold}%) | "
                f"plugged_in={st.is_plugged_in} | charging={st.is_charging}{extra_suffix}"
            )

    if errors:
        return ProbeResult(
            service="cardata",
            status="failed",
            headline="; ".join(errors),
            details=details,
        )

    return ProbeResult(
        service="cardata",
        status="ok",
        headline=f"Fetched telemetry for {ok_vehicles} vehicle(s)",
        details=details,
    )


def run_probes(
    settings: Settings,
    target: ProbeTarget = "all",
    transports: Mapping[ServiceName, httpx.BaseTransport] | None = None,
    reference_time: datetime | None = None,
    modbus_factory: Callable[..., Any] | None = None,
) -> list[ProbeResult]:
    """Execute read-only probes for the requested service(s)."""
    selected: Sequence[ServiceName] = ALL_SERVICES if target == "all" else (target,)
    transport_map = transports or {}
    results: list[ProbeResult] = []

    for service in selected:
        svc_transport = transport_map.get(service)
        if service == "pstryk":
            results.append(
                probe_pstryk(settings, transport=svc_transport, reference_time=reference_time)
            )
        elif service == "solar":
            results.append(
                probe_solar(settings, transport=svc_transport, reference_time=reference_time)
            )
        elif service == "deye":
            results.append(
                probe_deye(settings, transport=svc_transport, modbus_factory=modbus_factory)
            )
        elif service == "cardata":
            results.append(probe_cardata(settings, transport=svc_transport))

    return results


def format_probe_report(results: Sequence[ProbeResult], timezone: str) -> str:
    """Format probe results into a human-readable CLI report."""
    badge_map: dict[ProbeStatus, str] = {
        "ok": "[OK]  ",
        "skipped": "[SKIP]",
        "failed": "[FAIL]",
    }
    lines = [f"pstryk-pilot v{__version__} — Read-Only API Probe (timezone: {timezone})"]
    for res in results:
        lines.append(f"  {badge_map[res.status]} {res.service:<8} {res.headline}")
        for detail in res.details:
            lines.append(f"                 {detail}")

    ok_count = sum(1 for r in results if r.status == "ok")
    skip_count = sum(1 for r in results if r.status == "skipped")
    fail_count = sum(1 for r in results if r.status == "failed")
    lines.append(f"Summary: {ok_count} ok, {skip_count} skipped, {fail_count} failed")
    return "\n".join(lines)
