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


def _clear_app_loggers() -> None:
    for handler in list(logging.getLogger("cheap_watts").handlers):
        handler.close()
    logging.getLogger("cheap_watts").handlers.clear()


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
        smtp_from_email="alerts@example.com",
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
    _clear_app_loggers()
    assert rc_dry_1 == 0
    assert modbus_writes == []  # Strict dry-run invariant!
    assert len(sent_emails) == 1
    assert str(sent_emails[0]["Subject"]).startswith("[DRY-RUN] cheap-watts:")

    # 2. Second --dry-run cycle 15m later with identical inputs -> 0 writes, 0 duplicate emails!
    rc_dry_2 = execute_run(
        settings,
        force_dry_run=True,
        transports=transports,
        modbus_factory=FakePipelineModbus,
        smtp_factory=FakeSmtp,
        now=ref_now + timedelta(minutes=15),
    )
    _clear_app_loggers()
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
    _clear_app_loggers()
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
    _clear_app_loggers()
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
    _clear_app_loggers()
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
        smtp_from_email="alerts@example.com",
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
    _clear_app_loggers()
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
    _clear_app_loggers()
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
    _clear_app_loggers()
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
    _clear_app_loggers()
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
    _clear_app_loggers()
    assert len(modbus_writes) == 6
    assert len(sent_emails) == 3

    # Verify status command reports the persisted Deye plan horizon cleanly
    assert execute_status(settings) == 0


def test_pipeline_away_deferral_then_home_arrival_and_bounded_missed_renotifications(
    tmp_path: Path,
) -> None:
    warsaw = ZoneInfo("Europe/Warsaw")
    day_00 = datetime(2026, 11, 22, 0, 0, tzinfo=warsaw)

    # 72h prices: days 0-1 at 01:00-04:00 & 12:00-15:00; day 2 shifts to 15:00-18:00
    rates: list[float] = []
    for day_idx in range(3):
        day_rates = [0.90] * 24
        for h in (1, 2, 3):
            day_rates[h] = 0.20
        if day_idx < 2:
            for h in (12, 13, 14):
                day_rates[h] = 0.20
        else:
            for h in (4, 5):
                day_rates[h] = 0.25
            for h in (15, 16, 17):
                day_rates[h] = 0.20
        rates.extend(day_rates)

    frames = []
    for idx, price in enumerate(rates):
        s_utc = (day_00 + timedelta(hours=idx)).astimezone(UTC)
        e_utc = (day_00 + timedelta(hours=idx + 1)).astimezone(UTC)
        frames.append(
            {
                "start": s_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": e_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "metrics": {"pricing": {"full_price": price}},
            }
        )
    pstryk_bytes = json.dumps({"frames": frames, "summary": {}}).encode("utf-8")
    solar_bytes = json.dumps(
        {
            "hourly": {
                "time": [
                    (day_00 + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(72)
                ],
                "shortwave_radiation": [0.0] * 72,
            }
        }
    ).encode("utf-8")

    mini_location = {"lat": 52.2900, "lon": 21.1000}  # Start away from home (~9km)

    def mini_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "telematicData": {
                    "vehicle.drivetrain.batteryManagement.header": {"value": 28},
                    "vehicle.drivetrain.electricEngine.charging.status": {"value": "NOCHARGING"},
                    "vehicle.body.chargingPort.status": {"value": "DISCONNECTED"},
                    "vehicle.cabin.infotainment.navigation.currentLocation.latitude": {
                        "value": str(mini_location["lat"])
                    },
                    "vehicle.cabin.infotainment.navigation.currentLocation.longitude": {
                        "value": str(mini_location["lon"])
                    },
                }
            },
        )

    tokens_file = tmp_path / "data" / "cardata_tokens.json"
    CarDataTokenStore(tokens_file).upsert(
        "mini",
        CarDataTokenRecord(
            access_token=SecretStr("test-api-key"),
            refresh_token=SecretStr("test-api-key"),
            expires_at=datetime.now(tz=UTC) + timedelta(hours=72),
            client_id="example-mini-client",
            vin="WMW0000000EXAMPLE2",
            container_id="CONT0000EXAMPLE2",
        ),
    )

    telemetry = [0] * 86
    telemetry[0] = 20
    tou_regs = [0] * 32
    tou_regs[0] = 0xFF
    for idx, hhmm in enumerate((0, 400, 800, 1200, 1600, 2000)):
        tou_regs[2 + idx] = hhmm
        tou_regs[20 + idx] = 20

    class FakeModbus:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def read_holding_registers(self, register_addr: int, quantity: int) -> list[int]:
            return [20] + [0] * (quantity - 1) if register_addr == 588 else list(tou_regs)

        def write_multiple_holding_registers(self, register_addr: int, values: list[int]) -> None:
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

    transports = {
        "pstryk": httpx.MockTransport(lambda _r: httpx.Response(200, content=pstryk_bytes)),
        "solar": httpx.MockTransport(lambda _r: httpx.Response(200, content=solar_bytes)),
        "cardata": httpx.MockTransport(mini_handler),
    }

    settings = Settings(
        _env_file=None,
        log_dir=tmp_path / "logs",
        state_file=tmp_path / "data" / "state.json",
        cardata_tokens_file=tokens_file,
        pstryk_api_key=SecretStr("test-api-key"),
        solar_latitude=52.2300,
        solar_longitude=21.0100,
        solar_kwp=5.0,
        home_radius_meters=200.0,
        solar_sufficient_kwh_threshold=50.0,
        deye_modbus_host="192.168.1.50",
        deye_logger_serial="2700012345",
        mini_cardata_client_id="example-mini-client",
        smtp_host="smtp.example.com",
        smtp_from_email="alerts@example.com",
        smtp_to_email="owner@example.com",
        alert_cooldown_hours=12.0,
        max_missed_ev_renotifications=3,
    )

    # 1. 11:00: MINI away -> Deye schedules initial grid-charge, NO MINI EV recommendation!
    assert (
        execute_run(
            settings,
            force_dry_run=False,
            transports=transports,
            modbus_factory=FakeModbus,
            smtp_factory=FakeSmtp,
            now=datetime(2026, 11, 22, 11, 0, tzinfo=warsaw),
        )
        == 0
    )
    _clear_app_loggers()
    assert len(sent_emails) == 1
    assert "EV Charging Recommendations" not in sent_emails[0].get_content()

    # 2. 11:30: MINI arrives home -> recommends 12:00-15:00 immediately!
    mini_location["lat"] = 52.2301
    mini_location["lon"] = 21.0101
    assert (
        execute_run(
            settings,
            force_dry_run=False,
            transports=transports,
            modbus_factory=FakeModbus,
            smtp_factory=FakeSmtp,
            now=datetime(2026, 11, 22, 11, 30, tzinfo=warsaw),
        )
        == 0
    )
    _clear_app_loggers()
    assert len(sent_emails) == 2
    assert "Window 12:00-15:00" in sent_emails[1].get_content()

    # 3. 13:00 (mid-window): no duplicate alert!
    assert (
        execute_run(
            settings,
            force_dry_run=False,
            transports=transports,
            modbus_factory=FakeModbus,
            smtp_factory=FakeSmtp,
            now=datetime(2026, 11, 22, 13, 0, tzinfo=warsaw),
        )
        == 0
    )
    _clear_app_loggers()
    assert len(sent_emails) == 2

    # 4. Missed windows #1, #2, #3 (at end of each window, well inside 12h cooldown) -> 3 follow-ups
    for expected_num, ts in enumerate(
        (
            datetime(2026, 11, 22, 15, 5, tzinfo=warsaw),  # Missed 12:00-15:00 -> next 01:00-04:00
            datetime(2026, 11, 23, 4, 5, tzinfo=warsaw),  # Missed 01:00-04:00 -> next 12:00-15:00
            datetime(2026, 11, 23, 15, 5, tzinfo=warsaw),  # Missed 12:00-15:00 -> next 01:00-04:00
        ),
        start=1,
    ):
        assert (
            execute_run(
                settings,
                force_dry_run=False,
                transports=transports,
                modbus_factory=FakeModbus,
                smtp_factory=FakeSmtp,
                now=ts,
            )
            == 0
        )
        _clear_app_loggers()
        assert len(sent_emails) == 2 + expected_num
        assert (
            f"[missed previous window — follow-up #{expected_num}]" in sent_emails[-1].get_content()
        )

    # 5. 4th miss with 24h cooldown (<13h elapsed) pauses EV reservation and sends 0 emails
    saved = StateStore(settings.state_file).load()
    assert saved.vehicle_alerts["mini"].missed_window_count == 3

    capped_settings = settings.model_copy(update={"alert_cooldown_hours": 24.0})
    assert (
        execute_run(
            capped_settings,
            force_dry_run=False,
            transports=transports,
            modbus_factory=FakeModbus,
            smtp_factory=FakeSmtp,
            now=datetime(2026, 11, 24, 4, 5, tzinfo=warsaw),
        )
        == 0
    )
    _clear_app_loggers()
    assert len(sent_emails) == 5
    assert StateStore(settings.state_file).load().vehicle_alerts["mini"].missed_window_count == 3

    # Once 24h cooldown elapses, standard EV notification resumes without incrementing missed count
    assert (
        execute_run(
            capped_settings,
            force_dry_run=False,
            transports=transports,
            modbus_factory=FakeModbus,
            smtp_factory=FakeSmtp,
            now=datetime(2026, 11, 24, 15, 10, tzinfo=warsaw),
        )
        == 0
    )
    _clear_app_loggers()
    assert len(sent_emails) == 6
    assert "EV Charging Recommendations" in sent_emails[-1].get_content()
    assert "[missed previous window" not in sent_emails[-1].get_content()
    assert StateStore(settings.state_file).load().vehicle_alerts["mini"].missed_window_count == 3


def test_execute_run_retries_transient_api_failures_before_failing_or_recovering(
    tmp_path: Path,
) -> None:
    pstryk_bytes = (FIXTURES / "pstryk_unified_metrics.json").read_bytes()
    solar_bytes = (FIXTURES / "open_meteo_forecast.json").read_bytes()

    telemetry = [0] * 86
    telemetry[0] = 60
    tou_regs = [0] * 32
    tou_regs[0] = 0xFF
    for idx, hhmm in enumerate((0, 400, 800, 1200, 1600, 2000)):
        tou_regs[2 + idx] = hhmm
        tou_regs[20 + idx] = 20

    class FakeModbus:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def read_holding_registers(self, register_addr: int, quantity: int) -> list[int]:
            return list(telemetry) if register_addr == 588 else list(tou_regs)

        def write_multiple_holding_registers(self, *_a: object, **_kw: object) -> None:
            pass

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

    pstryk_calls = 0
    solar_calls = 0

    def flaky_pstryk(request: httpx.Request) -> httpx.Response:
        nonlocal pstryk_calls
        pstryk_calls += 1
        if pstryk_calls <= 3:
            raise httpx.ReadTimeout("temporary read timeout", request=request)
        return httpx.Response(200, content=pstryk_bytes)

    def flaky_solar(_request: httpx.Request) -> httpx.Response:
        nonlocal solar_calls
        solar_calls += 1
        if solar_calls <= 3:
            return httpx.Response(503, text="upstream busy")
        return httpx.Response(200, content=solar_bytes)

    settings = Settings(
        _env_file=None,
        log_dir=tmp_path / "logs",
        state_file=tmp_path / "data" / "state.json",
        pstryk_api_key=SecretStr("test-api-key"),
        solar_latitude=52.23,
        solar_longitude=21.01,
        solar_kwp=5.0,
        deye_modbus_host="192.168.1.50",
        deye_logger_serial="2700012345",
        smtp_host="smtp.example.com",
        smtp_from_email="alerts@example.com",
        smtp_to_email="owner@example.com",
        api_max_retries=3,
        api_retry_backoff_seconds=2.0,
    )

    ref_now = datetime(2026, 11, 15, 0, 5, tzinfo=UTC)

    # 1. Both Pstryk ReadTimeout (3x) and Open-Meteo 503 (3x) recover on 4th attempt -> no alert
    rc_ok = execute_run(
        settings,
        force_dry_run=True,
        transports={
            "pstryk": httpx.MockTransport(flaky_pstryk),
            "solar": httpx.MockTransport(flaky_solar),
        },
        modbus_factory=FakeModbus,
        smtp_factory=FakeSmtp,
        now=ref_now,
    )
    _clear_app_loggers()
    assert rc_ok == 0
    assert pstryk_calls == 4
    assert solar_calls == 4
    assert not any("Optimization cycle failed" in str(m["Subject"]) for m in sent_emails)

    # 2. Persistent Open-Meteo 503 exhausts 4 attempts (1 + 3 retries) before sending failure alert
    persistent_solar_calls = 0

    def always_503_solar(_request: httpx.Request) -> httpx.Response:
        nonlocal persistent_solar_calls
        persistent_solar_calls += 1
        return httpx.Response(503, text="still down")

    rc_fail = execute_run(
        settings,
        force_dry_run=True,
        transports={
            "pstryk": httpx.MockTransport(lambda _r: httpx.Response(200, content=pstryk_bytes)),
            "solar": httpx.MockTransport(always_503_solar),
        },
        modbus_factory=FakeModbus,
        smtp_factory=FakeSmtp,
        now=ref_now + timedelta(minutes=30),
    )
    _clear_app_loggers()
    assert rc_fail == 1
    assert persistent_solar_calls == 4
    failure_emails = [m for m in sent_emails if "Optimization cycle failed" in str(m["Subject"])]
    assert len(failure_emails) == 1
    assert "Open-Meteo request failed with HTTP 503" in failure_emails[0].get_content()
