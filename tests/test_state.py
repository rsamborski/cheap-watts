"""Tests for atomic local state persistence and alert deduplication."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.state import AppState, StateStore, TouSlotRecord


def test_state_store_roundtrip_and_missing_file(tmp_path: Path) -> None:
    state_path = tmp_path / "data" / "state.json"
    store = StateStore(state_path)

    initial = store.load()
    assert initial == AppState()

    now = datetime(2026, 11, 14, 20, 0, tzinfo=UTC)
    initial.last_run_at = now
    initial.last_run_status = "ok"
    initial.last_run_dry_run = True
    initial.last_deye_schedule = [
        TouSlotRecord(
            slot_index=1,
            start_time="01:00",
            end_time="04:00",
            target_soc=95,
            grid_charge=True,
        )
    ]
    store.save(initial)

    reloaded = store.load()
    assert reloaded.last_run_at == now
    assert reloaded.last_run_status == "ok"
    assert reloaded.last_run_dry_run is True
    assert len(reloaded.last_deye_schedule) == 1
    assert reloaded.last_deye_schedule[0].grid_charge is True
    assert not state_path.with_suffix(".json.tmp").exists()


def test_vehicle_alert_cooldown_and_window_change() -> None:
    state = AppState()
    t0 = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)

    # First time below threshold -> must notify
    assert StateStore.should_notify_vehicle(
        state,
        vehicle_key="bmw",
        recommended_window="01:00-04:00",
        cooldown_hours=12.0,
        now=t0,
    )

    StateStore.record_vehicle_notification(
        state,
        vehicle_key="bmw",
        soc=18,
        recommended_window="01:00-04:00",
        now=t0,
    )

    # Same window 30 minutes later -> suppressed by cooldown
    assert not StateStore.should_notify_vehicle(
        state,
        vehicle_key="bmw",
        recommended_window="01:00-04:00",
        cooldown_hours=12.0,
        now=t0 + timedelta(minutes=30),
    )

    # Recommended window changed -> notify immediately despite cooldown
    assert StateStore.should_notify_vehicle(
        state,
        vehicle_key="bmw",
        recommended_window="02:00-05:00",
        cooldown_hours=12.0,
        now=t0 + timedelta(minutes=30),
    )

    # Cooldown elapsed -> notify again
    assert StateStore.should_notify_vehicle(
        state,
        vehicle_key="bmw",
        recommended_window="01:00-04:00",
        cooldown_hours=12.0,
        now=t0 + timedelta(hours=12, minutes=1),
    )

    # Vehicle recharged above threshold -> clear alert state
    StateStore.clear_vehicle_alert(state, "bmw")
    assert "bmw" not in state.vehicle_alerts
