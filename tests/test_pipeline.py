"""End-to-end offline tests for execute_run pipeline (dry-run, live write, and error alerts)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from pydantic import SecretStr

from src.clients.cardata import CarDataTokenRecord, CarDataTokenStore
from src.config import Settings
from src.main import execute_run, execute_status
from src.state import StateStore

FIXTURES = Path(__file__).parent / "fixtures"


def _clear_pilot_loggers() -> None:
    for handler in list(logging.getLogger("pstryk_pilot").handlers):
        handler.close()
    logging.getLogger("pstryk_pilot").handlers.clear()


def test_execute_run_end_to_end_dry_run_deduplication_then_live_write_and_error(
    tmp_path: Path,
) -> None:
    pstryk_bytes = (FIXTURES / "pstryk_unified_metrics.json").read_bytes()
    solar_bytes = (FIXTURES / "open_meteo_forecast.json").read_bytes()
    bmw_bytes = (FIXTURES / "cardata_bmw_telematic.json").read_bytes()

    tokens_file = tmp_path / "data" / "cardata_tokens.json"
    CarDataTokenStore(tokens_file).upsert(
        "bmw",
        CarDataTokenRecord(
            access_token=SecretStr("test-api-key"),
            refresh_token=SecretStr("test-api-key"),
            expires_at=datetime.now(tz=UTC) + timedelta(hours=2),
            client_id="example-bmw-client",
            vin="WBA0000000EXAMPLE1",
            container_id="CONT0000EXAMPLE1",
        ),
    )

    modbus_writes: list[tuple[int, list[int]]] = []
    telemetry = [0] * 86
    telemetry[0] = 25  # 25% battery SoC
    tou_regs = [0] * 32
    tou_regs[0] = 0xFF
    for idx, hhmm in enumerate((0, 400, 800, 1200, 1600, 2000)):
        tou_regs[2 + idx] = hhmm
        tou_regs[20 + idx] = 20

    class FakePipelineModbus:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def read_holding_registers(self, register_addr: int, quantity: int) -> list[int]:
            return list(telemetry) if register_addr == 588 else list(tou_regs)

        def write_multiple_holding_registers(self, register_addr: int, values: list[int]) -> None:
            modbus_writes.append((register_addr, list(values)))

        def disconnect(self) -> None:
            pass

    sent_emails: list[EmailMessage] = []

    class FakeSmtp:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def __enter__(self) -> FakeSmtp:
            return self

        def __exit__(self, *_a: object) -> None:
            pass

        def ehlo(self) -> None:
            pass

        def starttls(self, **_kw: object) -> None:
            pass

        def login(self, *_a: object) -> None:
            pass

        def send_message(self, msg: EmailMessage) -> None:
            sent_emails.append(msg)

    transports = {
        "pstryk": httpx.MockTransport(lambda _r: httpx.Response(200, content=pstryk_bytes)),
        "solar": httpx.MockTransport(lambda _r: httpx.Response(200, content=solar_bytes)),
        "cardata": httpx.MockTransport(lambda _r: httpx.Response(200, content=bmw_bytes)),
    }

    settings = Settings(
        _env_file=None,
        log_dir=tmp_path / "logs",
        state_file=tmp_path / "data" / "state.json",
        cardata_tokens_file=tokens_file,
        pstryk_api_key=SecretStr("test-api-key"),
        solar_latitude=52.23,
        solar_longitude=21.01,
        solar_kwp=5.0,
        solar_kwp_2=5.0,
        solar_azimuth_degrees=-90.0,
        solar_azimuth_degrees_2=90.0,
        solar_sufficient_kwh_threshold=50.0,  # Force low-sun grid-charge planning
        deye_modbus_host="192.168.1.50",
        deye_logger_serial="2700012345",
        bmw_cardata_client_id="example-bmw-client",
        smtp_host="smtp.example.com",
        smtp_from_email="pilot@example.com",
        smtp_to_email="owner@example.com",
    )

    ref_now = datetime(2026, 11, 15, 0, 5, tzinfo=UTC)

    # 1. First --dry-run cycle: skips Modbus write, sends 1 [DRY-RUN] email, saves state
    rc_dry_1 = execute_run(
        settings,
        force_dry_run=True,
        transports=transports,
        modbus_factory=FakePipelineModbus,
        smtp_factory=FakeSmtp,
        now=ref_now,
    )
    _clear_pilot_loggers()
    assert rc_dry_1 == 0
    assert modbus_writes == []  # Strict dry-run invariant!
    assert len(sent_emails) == 1
    assert str(sent_emails[0]["Subject"]).startswith("[DRY-RUN] pstryk-pilot:")

    # 2. Second --dry-run cycle 15m later with identical inputs -> 0 writes, 0 duplicate emails!
    rc_dry_2 = execute_run(
        settings,
        force_dry_run=True,
        transports=transports,
        modbus_factory=FakePipelineModbus,
        smtp_factory=FakeSmtp,
        now=ref_now + timedelta(minutes=15),
    )
    _clear_pilot_loggers()
    assert rc_dry_2 == 0
    assert modbus_writes == []
    assert len(sent_emails) == 1

    # 3. Live cycle (force_dry_run=False): compares against real hardware, writes Deye, emails!
    rc_live = execute_run(
        settings,
        force_dry_run=False,
        transports=transports,
        modbus_factory=FakePipelineModbus,
        smtp_factory=FakeSmtp,
        now=ref_now + timedelta(minutes=30),
    )
    _clear_pilot_loggers()
    assert rc_live == 0
    assert len(modbus_writes) == 2  # Registers 148..153 and 166..177 written!
    assert len(sent_emails) == 2
    assert not str(sent_emails[1]["Subject"]).startswith("[DRY-RUN]")

    # 4. Degraded CarData (e.g. 503) logs warning & still succeeds; Pstryk 500 triggers error alert
    degraded_transports = {
        **transports,
        "cardata": httpx.MockTransport(lambda _r: httpx.Response(503, text="bmw maintenance")),
    }
    rc_degraded = execute_run(
        settings,
        force_dry_run=True,
        transports=degraded_transports,
        modbus_factory=FakePipelineModbus,
        smtp_factory=FakeSmtp,
        now=ref_now + timedelta(minutes=45),
    )
    _clear_pilot_loggers()
    assert rc_degraded == 0
    assert len(sent_emails) == 3

    broken_transports = {
        **transports,
        "pstryk": httpx.MockTransport(lambda _r: httpx.Response(500, text="pstryk outage")),
    }
    rc_err = execute_run(
        settings,
        force_dry_run=True,
        transports=broken_transports,
        modbus_factory=FakePipelineModbus,
        smtp_factory=FakeSmtp,
        now=ref_now + timedelta(hours=1),
    )
    _clear_pilot_loggers()
    assert rc_err == 1
    assert len(sent_emails) == 4
    assert "Optimization cycle failed" in str(sent_emails[3]["Subject"])
    err_state = StateStore(settings.state_file).load()
    assert err_state.last_run_status == "error"
    assert err_state.last_error_notified_at is not None


def test_sticky_daily_plan_prevents_mid_charge_overwrite_until_new_tariffs_or_ev_conflict(
    tmp_path: Path,
) -> None:
    warsaw = ZoneInfo("Europe/Warsaw")
    mon_00 = datetime(2026, 11, 16, 0, 0, tzinfo=warsaw)

    def _build_pstryk_payload(rates: list[float]) -> bytes:
        frames = []
        for idx, price in enumerate(rates):
            slot_start = (mon_00 + timedelta(hours=idx)).astimezone(UTC)
            slot_end = (mon_00 + timedelta(hours=idx + 1)).astimezone(UTC)
            frames.append(
                {
                    "start": slot_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end": slot_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "metrics": {
                        "pricing": {
                            "full_price": price,
                            "price_net": round(price / 1.23, 4),
                            "price_gross": price,
                            "is_cheap": price <= 0.30,
                            "is_expensive": price >= 1.20,
                        }
                    },
                }
            )
        return json.dumps({"frames": frames, "summary": {}}).encode("utf-8")

    # Mon+Tue 48h: cheap Mon 22:00-01:00 and Tue 05:00-06:00 (plus Tue 22:00-00:00)
    mon_tue_rates = [0.95] * 48
    mon_tue_rates[22] = 0.20  # Mon 22:00-23:00
    mon_tue_rates[23] = 0.20  # Mon 23:00-00:00
    mon_tue_rates[24] = 0.20  # Tue 00:00-01:00
    mon_tue_rates[29] = 0.25  # Tue 05:00-06:00
    mon_tue_rates[46] = 0.18  # Tue 22:00-23:00
    mon_tue_rates[47] = 0.18  # Tue 23:00-00:00

    # Add Wed Day-Ahead prices at Tue 14:30 (with cheap Tue 22:00-01:00 and Wed 03:00-05:00)
    mon_wed_rates = list(mon_tue_rates) + [0.95] * 24
    mon_wed_rates[48] = 0.16  # Wed 00:00-01:00
    mon_wed_rates[51] = 0.15  # Wed 03:00-04:00
    mon_wed_rates[52] = 0.15  # Wed 04:00-05:00

    solar_payload = json.dumps(
        {
            "hourly": {
                "time": [
                    (mon_00 + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(72)
                ],
                "shortwave_radiation": [0.0] * 72,
            }
        }
    ).encode("utf-8")

    def _bmw_payload(soc: int, is_charging: bool) -> bytes:
        state_val = "CHARGINGACTIVE" if is_charging else "NOTCHARGING"
        return json.dumps(
            {
                "telematicData": {
                    "vehicle.drivetrain.batteryManagement.header": {
                        "value": soc,
                        "timestamp": "2026-11-16T18:50:00Z",
                    },
                    "vehicle.drivetrain.electricEngine.charging.status": {"value": state_val},
                    "vehicle.body.chargingPort.status": {"value": "CONNECTED"},
                    "vehicle.drivetrain.electricEngine.charging.timeRemaining": {
                        "value": 180 if is_charging else 0
                    },
                }
            }
        ).encode("utf-8")

    tokens_file = tmp_path / "data" / "cardata_tokens.json"
    CarDataTokenStore(tokens_file).upsert(
        "bmw",
        CarDataTokenRecord(
            access_token=SecretStr("test-api-key"),
            refresh_token=SecretStr("test-api-key"),
            expires_at=datetime.now(tz=UTC) + timedelta(hours=48),
            client_id="example-bmw-client",
            vin="WBA0000000EXAMPLE1",
            container_id="CONT0000EXAMPLE1",
        ),
    )

    hw_state = {"soc": 15}
    modbus_writes: list[tuple[int, list[int]]] = []
    tou_regs = [0] * 32
    tou_regs[0] = 0xFF
    for idx, hhmm in enumerate((0, 400, 800, 1200, 1600, 2000)):
        tou_regs[2 + idx] = hhmm
        tou_regs[20 + idx] = 15

    class FakeStatefulModbus:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def read_holding_registers(self, register_addr: int, quantity: int) -> list[int]:
            if register_addr == 588:
                tele = [0] * quantity
                tele[0] = hw_state["soc"]
                return tele
            return list(tou_regs)

        def write_multiple_holding_registers(self, register_addr: int, values: list[int]) -> None:
            modbus_writes.append((register_addr, list(values)))
            if register_addr == 146:
                tou_regs[0] = values[0]
            elif register_addr == 148:
                tou_regs[2:8] = list(values)
            elif register_addr == 166:
                tou_regs[20 : 20 + len(values)] = list(values)

        def disconnect(self) -> None:
            pass

    sent_emails: list[EmailMessage] = []

    class FakeSmtp:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def __enter__(self) -> FakeSmtp:
            return self

        def __exit__(self, *_a: object) -> None:
            pass

        def ehlo(self) -> None:
            pass

        def starttls(self, **_kw: object) -> None:
            pass

        def login(self, *_a: object) -> None:
            pass

        def send_message(self, msg: EmailMessage) -> None:
            sent_emails.append(msg)

    active_pstryk = {"bytes": _build_pstryk_payload(mon_tue_rates)}
    active_bmw = {"bytes": _bmw_payload(soc=65, is_charging=False)}
    transports = {
        "pstryk": httpx.MockTransport(
            lambda _r: httpx.Response(200, content=active_pstryk["bytes"])
        ),
        "solar": httpx.MockTransport(lambda _r: httpx.Response(200, content=solar_payload)),
        "cardata": httpx.MockTransport(lambda _r: httpx.Response(200, content=active_bmw["bytes"])),
    }

    settings = Settings(
        _env_file=None,
        log_dir=tmp_path / "logs",
        state_file=tmp_path / "data" / "state.json",
        cardata_tokens_file=tokens_file,
        pstryk_api_key=SecretStr("test-api-key"),
        solar_latitude=52.23,
        solar_longitude=21.01,
        solar_kwp=5.0,
        battery_capacity_kwh=10.0,
        battery_min_soc=15,
        battery_target_soc=95,
        battery_max_charge_kw=2.0,
        typical_daily_usage_kwh=12.0,
        usage_buffer_pct=10.0,
        solar_sufficient_kwh_threshold=12.0,
        deye_modbus_host="192.168.1.50",
        deye_logger_serial="2700012345",
        bmw_cardata_client_id="example-bmw-client",
        smtp_host="smtp.example.com",
        smtp_from_email="pilot@example.com",
        smtp_to_email="owner@example.com",
    )

    # 1. Mon 18:56 CET (SoC 15%): initial live write schedules 22:00-01:00 and 05:00-06:00
    assert (
        execute_run(
            settings,
            force_dry_run=False,
            transports=transports,
            modbus_factory=FakeStatefulModbus,
            smtp_factory=FakeSmtp,
            now=datetime(2026, 11, 16, 18, 56, tzinfo=warsaw),
        )
        == 0
    )
    _clear_pilot_loggers()
    assert len(modbus_writes) == 2
    assert len(sent_emails) == 1
    saved_boundaries = list(tou_regs[2:8])
    saved_flags = list(tou_regs[26:32])
    assert 500 in saved_boundaries and 2200 in saved_boundaries

    # 2. Mon 23:35 CET (SoC 44%, mid-charge): NO overwrite, NO duplicate email, 05:00-06:00 kept!
    hw_state["soc"] = 44
    assert (
        execute_run(
            settings,
            force_dry_run=False,
            transports=transports,
            modbus_factory=FakeStatefulModbus,
            smtp_factory=FakeSmtp,
            now=datetime(2026, 11, 16, 23, 35, tzinfo=warsaw),
        )
        == 0
    )
    _clear_pilot_loggers()
    assert len(modbus_writes) == 2
    assert len(sent_emails) == 1
    assert tou_regs[2:8] == saved_boundaries
    assert tou_regs[26:32] == saved_flags

    # 3. Tue 01:05 CET (SoC 75%, gap between windows): STILL locked -> 0 writes, 0 emails!
    hw_state["soc"] = 75
    assert (
        execute_run(
            settings,
            force_dry_run=False,
            transports=transports,
            modbus_factory=FakeStatefulModbus,
            smtp_factory=FakeSmtp,
            now=datetime(2026, 11, 17, 1, 5, tzinfo=warsaw),
        )
        == 0
    )
    _clear_pilot_loggers()
    assert len(modbus_writes) == 2
    assert len(sent_emails) == 1

    # 4. Tue 14:30 CET: Wed Day-Ahead prices arrive -> replan writes new horizon & sends email!
    hw_state["soc"] = 20
    active_pstryk["bytes"] = _build_pstryk_payload(mon_wed_rates)
    assert (
        execute_run(
            settings,
            force_dry_run=False,
            transports=transports,
            modbus_factory=FakeStatefulModbus,
            smtp_factory=FakeSmtp,
            now=datetime(2026, 11, 17, 14, 30, tzinfo=warsaw),
        )
        == 0
    )
    _clear_pilot_loggers()
    assert len(modbus_writes) == 4
    assert len(sent_emails) == 2

    # 5. Tue 22:30 CET: BMW actively charging in locked 22:00-01:00 -> immediate EV override!
    active_bmw["bytes"] = _bmw_payload(soc=35, is_charging=True)
    assert (
        execute_run(
            settings,
            force_dry_run=False,
            transports=transports,
            modbus_factory=FakeStatefulModbus,
            smtp_factory=FakeSmtp,
            now=datetime(2026, 11, 17, 22, 30, tzinfo=warsaw),
        )
        == 0
    )
    _clear_pilot_loggers()
    assert len(modbus_writes) == 6
    assert len(sent_emails) == 3

    # Verify status command reports the persisted Deye plan horizon cleanly
    assert execute_status(settings) == 0
