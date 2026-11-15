"""Offline unit tests for Deye TOU schedule diffing, steady-state suppression, and dry-run."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.config import Settings
from src.engine.diff import diff_tou_schedules, evaluate_cycle_actions
from src.engine.planner import build_idle_tou_slots, plan_charging_schedule
from src.models import InverterStatus, PriceSlot, SolarForecast, SolarHour, TouSlot, VehicleStatus
from src.state import AppState, StateStore

WARSAW = ZoneInfo("Europe/Warsaw")


def test_diff_detects_equivalent_24h_idle_schedules_as_no_change() -> None:
    # Inverter currently has unequal idle splits (00:00-05:00, 05:00-09:00, ...), all OFF at 20%
    current_custom_idle = [
        TouSlot(
            slot_index=1, start_time="00:00", end_time="05:00", target_soc=20, grid_charge=False
        ),
        TouSlot(
            slot_index=2, start_time="05:00", end_time="09:00", target_soc=20, grid_charge=False
        ),
        TouSlot(
            slot_index=3, start_time="09:00", end_time="13:00", target_soc=20, grid_charge=False
        ),
        TouSlot(
            slot_index=4, start_time="13:00", end_time="17:00", target_soc=20, grid_charge=False
        ),
        TouSlot(
            slot_index=5, start_time="17:00", end_time="21:00", target_soc=20, grid_charge=False
        ),
        TouSlot(
            slot_index=6, start_time="21:00", end_time="00:00", target_soc=20, grid_charge=False
        ),
    ]
    target_canonical_idle = build_idle_tou_slots(min_soc=20)

    diff = diff_tou_schedules(current_custom_idle, target_canonical_idle)
    assert diff.has_changes is False
    assert diff.slot_diffs == []
    assert "unchanged" in diff.format_audit_log()

    # If TOU mode is currently disabled on the inverter, enabling it is an actionable change
    diff_tou_off = diff_tou_schedules(
        current_custom_idle,
        target_canonical_idle,
        current_tou_enabled=False,
    )
    assert diff_tou_off.has_changes is True
    assert diff_tou_off.tou_enabled_changed is True
    assert "TOU mode: OFF -> ON" in diff_tou_off.format_audit_log()

    with pytest.raises(ValueError, match="Expected exactly 6"):
        diff_tou_schedules(current_custom_idle, target_canonical_idle[:3])


def test_active_ev_charging_emails_only_when_deye_schedule_changes_to_block_overlap() -> None:
    now = datetime(2026, 11, 16, 1, 0, tzinfo=WARSAW)
    day_start = datetime(2026, 11, 16, 0, 0, tzinfo=WARSAW)

    prices = [
        PriceSlot(
            start=day_start + timedelta(hours=h),
            end=day_start + timedelta(hours=h + 1),
            price_pln_kwh=0.25 if h in (1, 2, 4) else 0.90,
        )
        for h in range(24)
    ]
    forecast = SolarForecast(
        hours=[
            SolarHour(
                start=day_start + timedelta(hours=h),
                end=day_start + timedelta(hours=h + 1),
                irradiance_wm2=0.0,
                estimated_kwh=0.0,
            )
            for h in range(24)
        ],
        fetched_at=now,
    )
    ev_charging = VehicleStatus(
        vehicle_key="bmw",
        display_name="BMW",
        soc_pct=19,
        is_plugged_in=True,
        is_charging=True,
        time_remaining_minutes=120,  # 01:00-03:00 active
        fetched_at=now,
    )
    settings = Settings(_env_file=None, battery_max_charge_kw=5.0)

    # Case A: Deye previously had grid_charge=ON at 01:00-02:00 (overlapping active EV!)
    overlapping_slots = [
        TouSlot(
            slot_index=1, start_time="00:00", end_time="01:00", target_soc=20, grid_charge=False
        ),
        TouSlot(
            slot_index=2, start_time="01:00", end_time="02:00", target_soc=95, grid_charge=True
        ),
        TouSlot(
            slot_index=3, start_time="02:00", end_time="07:00", target_soc=20, grid_charge=False
        ),
        TouSlot(
            slot_index=4, start_time="07:00", end_time="12:00", target_soc=20, grid_charge=False
        ),
        TouSlot(
            slot_index=5, start_time="12:00", end_time="18:00", target_soc=20, grid_charge=False
        ),
        TouSlot(
            slot_index=6, start_time="18:00", end_time="00:00", target_soc=20, grid_charge=False
        ),
    ]
    inverter_overlap = InverterStatus(
        device_sn="2700012345",
        battery_soc=60,
        tou_enabled=True,
        tou_slots=overlapping_slots,
        fetched_at=now,
    )
    plan = plan_charging_schedule(
        settings=settings,
        inverter=inverter_overlap,
        price_slots=prices,
        solar_forecast=forecast,
        vehicles=[ev_charging],
        now=now,
    )
    diff_changed = diff_tou_schedules(inverter_overlap.tou_slots, plan.target_tou_slots)
    decision_changed = evaluate_cycle_actions(
        plan=plan,
        schedule_diff=diff_changed,
        state=AppState(),
        cooldown_hours=12.0,
        dry_run=False,
        now=now,
    )

    assert plan.ev_recommendations == []  # No EV alert while actively charging
    assert decision_changed.notifiable_ev_recommendations == []
    assert decision_changed.should_write_deye is True
    assert decision_changed.should_send_email is True  # Email sent because Deye schedule changed!

    # Case B: Next cron run 15m later -> Deye schedule ALREADY matches post-EV slot (04:00-05:00)
    diff_steady = diff_tou_schedules(plan.target_tou_slots, plan.target_tou_slots)
    decision_steady = evaluate_cycle_actions(
        plan=plan,
        schedule_diff=diff_steady,
        state=AppState(),
        cooldown_hours=12.0,
        dry_run=False,
        now=now + timedelta(minutes=15),
    )
    assert decision_steady.should_write_deye is False
    assert decision_steady.should_send_email is False  # Zero email spam on steady state!


def test_noop_steady_state_cooldown_and_dry_run_invariants() -> None:
    now = datetime(2026, 7, 15, 9, 0, tzinfo=WARSAW)
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
                irradiance_wm2=500.0 if 9 <= h <= 16 else 0.0,
                estimated_kwh=3.0 if 9 <= h <= 16 else 0.0,
            )
            for h in range(24)
        ],
        fetched_at=now,
    )
    bmw_low = VehicleStatus(
        vehicle_key="bmw",
        display_name="BMW",
        soc_pct=15,
        fetched_at=now,
    )
    settings = Settings(_env_file=None)

    # Start with inverter having a stale grid_charge=ON slot -> dry_run=True must NEVER write Deye
    stale_slots = build_idle_tou_slots(20)
    stale_slots[0] = TouSlot(
        slot_index=1,
        start_time="00:00",
        end_time="04:00",
        target_soc=90,
        grid_charge=True,
    )
    inverter = InverterStatus(
        device_sn="2700012345",
        battery_soc=40,
        tou_enabled=True,
        tou_slots=stale_slots,
        fetched_at=now,
    )

    plan = plan_charging_schedule(
        settings=settings,
        inverter=inverter,
        price_slots=prices,
        solar_forecast=forecast,
        vehicles=[bmw_low],
        now=now,
    )
    diff = diff_tou_schedules(inverter.tou_slots, plan.target_tou_slots)
    state = AppState()

    dry_decision = evaluate_cycle_actions(
        plan=plan,
        schedule_diff=diff,
        state=state,
        cooldown_hours=12.0,
        dry_run=True,
        now=now,
    )
    assert diff.has_changes is True
    assert dry_decision.should_write_deye is False  # Strict dry-run invariant!
    assert dry_decision.should_send_email is True
    assert dry_decision.email_subject_prefix == "[DRY-RUN] "
    assert len(dry_decision.notifiable_ev_recommendations) == 1
    assert "TOU #1:" in diff.format_audit_log()

    # Record EV notification and apply target schedule -> steady-state run sends NO email
    rec = dry_decision.notifiable_ev_recommendations[0]
    StateStore.record_vehicle_notification(
        state=state,
        vehicle_key=rec.vehicle_key,
        soc=rec.soc_pct,
        recommended_window=rec.recommended_window,
        now=now,
    )
    steady_diff = diff_tou_schedules(plan.target_tou_slots, plan.target_tou_slots)
    steady_dry = evaluate_cycle_actions(
        plan=plan,
        schedule_diff=steady_diff,
        state=state,
        cooldown_hours=12.0,
        dry_run=True,
        now=now + timedelta(minutes=30),
    )
    assert steady_dry.should_write_deye is False
    assert steady_dry.should_send_email is False  # No email in dry-run either when nothing changed!
    assert len(steady_dry.suppressed_ev_recommendations) == 1

    # Empty current schedule (e.g. initial state before first read) renders <unset> diffs cleanly
    empty_diff = diff_tou_schedules([], plan.target_tou_slots)
    assert empty_diff.has_changes is True
    assert "<unset>" in empty_diff.format_audit_log()

    # Non-00:00-anchored 6-slot schedule falls back to direct slot comparison
    shifted_slots = [
        TouSlot(
            slot_index=i + 1,
            start_time=f"{(i * 4 + 1):02d}:00",
            end_time=f"{((i + 1) * 4 + 1) % 24:02d}:00",
            target_soc=20,
            grid_charge=False,
        )
        for i in range(6)
    ]
    assert diff_tou_schedules(shifted_slots, shifted_slots).has_changes is False
    assert diff_tou_schedules(shifted_slots, plan.target_tou_slots).has_changes is True
