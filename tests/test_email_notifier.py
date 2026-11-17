"""Offline unit tests for SMTP change-driven notifications, [DRY-RUN] prefixing, and TLS modes."""

from __future__ import annotations

from datetime import datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from pydantic import SecretStr

from src.config import Settings
from src.engine.diff import diff_tou_schedules, evaluate_cycle_actions
from src.engine.planner import build_idle_tou_slots, plan_charging_schedule
from src.models import InverterStatus, PriceSlot, SolarForecast, SolarHour, TouSlot, VehicleStatus
from src.notify.email import SmtpNotifier, build_cycle_email
from src.state import AppState

WARSAW = ZoneInfo("Europe/Warsaw")


class FakeSmtpServer:
    """In-memory mock SMTP server capturing TLS, auth, and sent EmailMessages."""

    def __init__(self, host: str, port: int, **_kwargs: object) -> None:
        self.host = host
        self.port = port
        self.started_tls = False
        self.logged_in_user: str | None = None
        self.sent: list[EmailMessage] = []

    def __enter__(self) -> FakeSmtpServer:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def ehlo(self) -> None:
        return None

    def starttls(self, **_kwargs: object) -> None:
        self.started_tls = True

    def login(self, user: str, _password: str) -> None:
        self.logged_in_user = user

    def send_message(self, msg: EmailMessage) -> None:
        self.sent.append(msg)


def test_smtp_notifier_dry_run_prefix_live_starttls_ssl465_and_noop_suppression() -> None:
    now = datetime(2026, 11, 16, 0, 0, tzinfo=WARSAW)
    prices = [
        PriceSlot(
            start=now + timedelta(hours=h),
            end=now + timedelta(hours=h + 1),
            price_pln_kwh=0.25 if h in (1, 2, 3, 4) else 0.85,
        )
        for h in range(24)
    ]
    forecast = SolarForecast(
        hours=[
            SolarHour(
                start=now + timedelta(hours=h),
                end=now + timedelta(hours=h + 1),
                irradiance_wm2=0.0,
                estimated_kwh=0.0,
            )
            for h in range(24)
        ],
        fetched_at=now,
    )
    bmw = VehicleStatus(
        vehicle_key="bmw",
        display_name="BMW",
        soc_pct=15,
        fetched_at=now,
    )
    inverter = InverterStatus(
        device_sn="2700012345",
        battery_soc=40,
        pv_power_w=120.0,
        tou_enabled=True,
        tou_slots=build_idle_tou_slots(20),
        fetched_at=now,
    )

    servers: list[FakeSmtpServer] = []

    def make_server(host: str, port: int, **kw: object) -> FakeSmtpServer:
        srv = FakeSmtpServer(host, port, **kw)
        servers.append(srv)
        return srv

    settings = Settings(
        _env_file=None,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_username="alerts@example.com",
        smtp_password=SecretStr("test-api-key"),
        smtp_use_tls=True,
        smtp_from_email="alerts@example.com",
        smtp_to_email="owner@example.com, driver2@example.com",
    )

    plan = plan_charging_schedule(
        settings=settings,
        inverter=inverter,
        price_slots=prices,
        solar_forecast=forecast,
        vehicles=[bmw],
        now=now,
    )
    diff = diff_tou_schedules(inverter.tou_slots, plan.target_tou_slots)

    # 1. Dry-run email MUST carry [DRY-RUN] in subject AND body, sent via STARTTLS on 587
    dry_decision = evaluate_cycle_actions(
        plan=plan,
        schedule_diff=diff,
        state=AppState(),
        cooldown_hours=12.0,
        dry_run=True,
        now=now,
    )
    notifier = SmtpNotifier(settings, smtp_factory=make_server, smtp_ssl_factory=make_server)
    assert notifier.send_cycle_notification(plan, dry_decision, inverter) is True
    assert len(servers) == 1
    assert servers[0].started_tls is True
    assert servers[0].logged_in_user == "alerts@example.com"

    sent_msg = servers[0].sent[0]
    assert str(sent_msg["Subject"]).startswith("[DRY-RUN] cheap-watts:")
    assert str(sent_msg["To"]) == "owner@example.com, driver2@example.com"
    body_text = sent_msg.get_content()
    assert "[DRY-RUN] Simulation mode active" in body_text
    assert "BMW" in body_text
    assert "test-api-key" not in body_text

    # 2. Steady-state no-op decision MUST return None / False and send 0 emails
    noop_diff = diff_tou_schedules(plan.target_tou_slots, plan.target_tou_slots)
    noop_plan = plan.model_copy(update={"ev_recommendations": []})
    noop_decision = evaluate_cycle_actions(
        plan=noop_plan,
        schedule_diff=noop_diff,
        state=AppState(),
        cooldown_hours=12.0,
        dry_run=False,
        now=now,
    )
    assert (
        build_cycle_email(noop_plan, noop_decision, inverter, "a@example.com", "b@example.com")
        is None
    )
    assert notifier.send_cycle_notification(noop_plan, noop_decision, inverter) is False
    assert len(servers) == 1

    # 3. Implicit SSL on port 465 + failure alert email (and graceful failure handling)
    ssl_settings = settings.model_copy(update={"smtp_port": 465})
    ssl_notifier = SmtpNotifier(
        ssl_settings,
        smtp_factory=make_server,
        smtp_ssl_factory=make_server,
    )
    assert ssl_notifier.send_failure_alert("abc12345", "RuntimeError: boom", dry_run=True) is True
    assert len(servers) == 2
    assert servers[1].port == 465
    assert str(servers[1].sent[0]["Subject"]).startswith("[DRY-RUN] cheap-watts:")

    # Unconfigured notifier skips cleanly
    unconfigured = SmtpNotifier(Settings(_env_file=None))
    assert unconfigured.send_cycle_notification(plan, dry_decision, inverter) is False
    assert unconfigured.send_failure_alert("abc12345", "boom") is False

    # Secondary SMTP exception during failure alert is suppressed safely
    def broken_smtp(*_a: object, **_kw: object) -> FakeSmtpServer:
        raise OSError("smtp unreachable")

    broken_notifier = SmtpNotifier(settings, smtp_factory=broken_smtp)
    assert broken_notifier.send_failure_alert("abc12345", "boom") is False


def test_solar_ev_recommendation_formatting_in_email() -> None:
    now = datetime(2026, 7, 15, 8, 0, tzinfo=WARSAW)
    day_start = datetime(2026, 7, 15, 0, 0, tzinfo=WARSAW)
    prices = [
        PriceSlot(
            start=day_start + timedelta(hours=h),
            end=day_start + timedelta(hours=h + 1),
            price_pln_kwh=0.50,
        )
        for h in range(24)
    ]
    forecast = SolarForecast(
        hours=[
            SolarHour(
                start=day_start + timedelta(hours=h),
                end=day_start + timedelta(hours=h + 1),
                irradiance_wm2=600.0 if 9 <= h <= 15 else 0.0,
                estimated_kwh=3.5 if 9 <= h <= 15 else 0.0,
            )
            for h in range(24)
        ],
        fetched_at=now,
    )
    mini = VehicleStatus(
        vehicle_key="mini",
        display_name="MINI",
        soc_pct=32,
        fetched_at=now,
    )
    # Inverter had grid_charge ON -> sunny weather disables it AND recommends MINI solar_slow
    stale_slots = build_idle_tou_slots(20)
    stale_slots[0] = TouSlot(
        slot_index=1, start_time="00:00", end_time="04:00", target_soc=90, grid_charge=True
    )
    inverter = InverterStatus(
        device_sn="2700012345",
        battery_soc=50,
        tou_enabled=True,
        tou_slots=stale_slots,
        fetched_at=now,
    )
    settings = Settings(_env_file=None, ev_slow_charge_amps=8)
    plan = plan_charging_schedule(
        settings=settings,
        inverter=inverter,
        price_slots=prices,
        solar_forecast=forecast,
        vehicles=[mini],
        now=now,
    )
    diff = diff_tou_schedules(inverter.tou_slots, plan.target_tou_slots)
    decision = evaluate_cycle_actions(
        plan=plan,
        schedule_diff=diff,
        state=AppState(),
        cooldown_hours=12.0,
        dry_run=False,
        now=now,
    )
    msg = build_cycle_email(plan, decision, inverter, "a@example.com", "b@example.com")
    assert msg is not None
    assert not str(msg["Subject"]).startswith("[DRY-RUN]")
    assert "8A slow-charge on PV surplus" in msg.get_content()
