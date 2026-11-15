"""End-to-end offline tests for execute_run pipeline (dry-run, live write, and error alerts)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import httpx
from pydantic import SecretStr

from src.clients.cardata import CarDataTokenRecord, CarDataTokenStore
from src.config import Settings
from src.main import execute_run
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
