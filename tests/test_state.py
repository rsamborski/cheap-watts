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


def test_missed_window_renotification_cap_active_window_and_postpone() -> None:
    state = AppState()
    t0 = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
    w_start = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    w_end = datetime(2026, 11, 22, 15, 0, tzinfo=UTC)

    StateStore.record_vehicle_notification(
        state,
        vehicle_key="mini",
        soc=30,
        recommended_window="12:00-15:00",
        now=t0,
        window_start=w_start,
        window_end=w_end,
    )

    # Mid-window suppression even if candidate window string shifts
    assert not StateStore.should_notify_vehicle(
        state,
        vehicle_key="mini",
        recommended_window="13:00-16:00",
        cooldown_hours=12.0,
        max_missed_renotifications=3,
        now=datetime(2026, 11, 22, 13, 30, tzinfo=UTC),
    )

    # Window missed at 15:00 without charging -> 3 follow-ups allowed (max=3)
    for idx in range(3):
        check_time = w_end + timedelta(hours=idx * 4)
        next_start = check_time + timedelta(hours=1)
        next_end = next_start + timedelta(hours=3)
        assert StateStore.is_missed_window_renotification(
            state, "mini", max_missed_renotifications=3, now=check_time
        )
        assert StateStore.should_notify_vehicle(
            state,
            vehicle_key="mini",
            recommended_window="12:00-15:00",
            cooldown_hours=12.0,
            max_missed_renotifications=3,
            now=check_time,
        )
        StateStore.record_vehicle_notification(
            state,
            vehicle_key="mini",
            soc=30,
            recommended_window="12:00-15:00",
            now=check_time,
            window_start=next_start,
            window_end=next_end,
            is_missed_followup=True,
        )

    assert state.vehicle_alerts["mini"].missed_window_count == 3

    # 4th missed window falls back to normal 12h cooldown and pauses EV slot reservation
    fourth_miss = state.vehicle_alerts["mini"].window_end
    assert fourth_miss is not None
    assert not StateStore.is_missed_window_renotification(
        state, "mini", max_missed_renotifications=3, now=fourth_miss
    )
    assert StateStore.paused_missed_vehicles(
        state,
        cooldown_hours=12.0,
        max_missed_renotifications=3,
        now=fourth_miss,
    ) == {"mini"}
    assert not StateStore.should_notify_vehicle(
        state,
        vehicle_key="mini",
        recommended_window="12:00-15:00",
        cooldown_hours=12.0,
        max_missed_renotifications=3,
        now=fourth_miss,
    )
    last_sent = state.vehicle_alerts["mini"].last_notified_at
    assert last_sent is not None
    assert (
        StateStore.paused_missed_vehicles(
            state,
            cooldown_hours=12.0,
            max_missed_renotifications=3,
            now=last_sent + timedelta(hours=12, minutes=1),
        )
        == set()
    )
    assert StateStore.should_notify_vehicle(
        state,
        vehicle_key="mini",
        recommended_window="12:00-15:00",
        cooldown_hours=12.0,
        max_missed_renotifications=3,
        now=last_sent + timedelta(hours=12, minutes=1),
    )

    # If charged_in_window is True, missed-window bypass does not fire after window_end
    state.vehicle_alerts["mini"].missed_window_count = 0
    state.vehicle_alerts["mini"].charged_in_window = True
    state.vehicle_alerts["mini"].last_notified_at = fourth_miss - timedelta(hours=1)
    assert not StateStore.is_missed_window_renotification(
        state, "mini", max_missed_renotifications=3, now=fourth_miss
    )

    # Set and clear postponement
    postpone_target = t0 + timedelta(days=2)
    StateStore.set_vehicle_postponement(state, "mini", postpone_target)
    assert StateStore.active_postponements(state, t0) == {"mini": postpone_target}
    StateStore.clear_vehicle_postponement(state, "mini")
    assert StateStore.active_postponements(state, t0) == {}
    StateStore.clear_vehicle_postponement(state, "bmw")
    StateStore.set_vehicle_postponement(state, "bmw", postpone_target)
    state.vehicle_alerts["bmw"].missed_window_count = 0
    StateStore.clear_vehicle_postponement(state, "bmw")
    assert "bmw" not in state.vehicle_alerts
