"""Local state persistence for alert deduplication and schedule tracking."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class TouSlotRecord(BaseModel):
    """Persisted snapshot of a single Deye Time-of-Use (TOU) slot."""

    slot_index: int = Field(ge=1, le=6)
    start_time: str
    end_time: str
    target_soc: int = Field(ge=0, le=100)
    grid_charge: bool


class VehicleAlertRecord(BaseModel):
    """Tracks the last charging recommendation, missed-window count, and postponement."""

    last_notified_at: datetime | None = None
    last_notified_soc: int | None = None
    recommended_window: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    charged_in_window: bool = False
    missed_window_count: int = 0
    postponed_until: datetime | None = None


class AppState(BaseModel):
    """Root persisted state written atomically to disk."""

    last_run_at: datetime | None = None
    last_run_status: Literal["ok", "error"] | None = None
    last_run_dry_run: bool | None = None
    last_deye_schedule: list[TouSlotRecord] = Field(default_factory=list)
    last_deye_updated_at: datetime | None = None
    deye_plan_horizon_end: datetime | None = None
    deye_plan_solar_sufficient: bool | None = None
    last_error_notified_at: datetime | None = None
    vehicle_alerts: dict[str, VehicleAlertRecord] = Field(default_factory=dict)


class StateStore:
    """Atomic JSON file store for AppState."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> AppState:
        """Load state from disk, returning a fresh default AppState if absent."""
        if not self.path.exists():
            return AppState()
        raw = self.path.read_text(encoding="utf-8")
        return AppState.model_validate_json(raw)

    def save(self, state: AppState) -> None:
        """Atomically persist state to disk via temporary file replacement."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        serialized = state.model_dump_json(indent=2)
        tmp_path.write_text(f"{serialized}\n", encoding="utf-8")
        os.replace(tmp_path, self.path)

    @staticmethod
    def is_window_missed(record: VehicleAlertRecord | None, now: datetime) -> bool:
        """Return True if a previously recommended window elapsed without charging."""
        return (
            record is not None
            and record.window_end is not None
            and now >= record.window_end
            and not record.charged_in_window
        )

    @classmethod
    def is_missed_window_renotification(
        cls,
        state: AppState,
        vehicle_key: str,
        max_missed_renotifications: int,
        now: datetime | None = None,
    ) -> bool:
        """Return True when a missed EV window qualifies for a bounded cooldown bypass."""
        current_time = now or datetime.now(tz=UTC)
        record = state.vehicle_alerts.get(vehicle_key)
        if record is None or record.last_notified_at is None:
            return False
        return (
            cls.is_window_missed(record, current_time)
            and record.missed_window_count < max_missed_renotifications
        )

    @classmethod
    def should_notify_vehicle(
        cls,
        state: AppState,
        vehicle_key: str,
        recommended_window: str,
        cooldown_hours: float,
        max_missed_renotifications: int = 3,
        now: datetime | None = None,
    ) -> bool:
        """Determine whether a vehicle charging recommendation email should be sent.

        Returns True when:
        - No prior alert is recorded for this vehicle,
        - A previously recommended window ended without charging within the missed-window cap,
        - The recommended charging window changed outside an active window, or
        - The alert cooldown window has elapsed.
        """
        current_time = now or datetime.now(tz=UTC)
        record = state.vehicle_alerts.get(vehicle_key)
        if record is None or record.last_notified_at is None:
            return True
        if (
            record.window_start is not None
            and record.window_end is not None
            and record.window_start <= current_time < record.window_end
        ):
            return False
        if cls.is_window_missed(record, current_time):
            if record.missed_window_count < max_missed_renotifications:
                return True
            return (current_time - record.last_notified_at) >= timedelta(hours=cooldown_hours)
        if record.recommended_window != recommended_window:
            return True
        elapsed = current_time - record.last_notified_at
        return elapsed >= timedelta(hours=cooldown_hours)

    @staticmethod
    def record_vehicle_notification(
        state: AppState,
        vehicle_key: str,
        soc: int,
        recommended_window: str,
        now: datetime | None = None,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        is_missed_followup: bool = False,
    ) -> None:
        """Record that an alert was sent for the given vehicle."""
        current_time = now or datetime.now(tz=UTC)
        prior = state.vehicle_alerts.get(vehicle_key)
        prior_count = prior.missed_window_count if prior is not None else 0
        next_count = prior_count + 1 if is_missed_followup else prior_count
        preserved_postpone = (
            prior.postponed_until
            if prior is not None
            and prior.postponed_until is not None
            and prior.postponed_until > current_time
            else None
        )
        state.vehicle_alerts[vehicle_key] = VehicleAlertRecord(
            last_notified_at=current_time,
            last_notified_soc=soc,
            recommended_window=recommended_window,
            window_start=window_start,
            window_end=window_end,
            charged_in_window=False,
            missed_window_count=next_count,
            postponed_until=preserved_postpone,
        )

    @staticmethod
    def clear_vehicle_alert(state: AppState, vehicle_key: str) -> None:
        """Clear stored alert state once a vehicle recovers above its SoC threshold."""
        state.vehicle_alerts.pop(vehicle_key, None)

    @staticmethod
    def set_vehicle_postponement(
        state: AppState,
        vehicle_key: str,
        postponed_until: datetime,
    ) -> None:
        """Set or update an EV charging postponement cutoff for `vehicle_key`."""
        prior = state.vehicle_alerts.get(vehicle_key)
        prior_count = prior.missed_window_count if prior is not None else 0
        state.vehicle_alerts[vehicle_key] = VehicleAlertRecord(
            missed_window_count=prior_count,
            postponed_until=postponed_until,
        )

    @staticmethod
    def clear_vehicle_postponement(state: AppState, vehicle_key: str) -> None:
        """Clear an active EV charging postponement without resetting alert history."""
        record = state.vehicle_alerts.get(vehicle_key)
        if record is None:
            return
        record.postponed_until = None
        if (
            record.last_notified_at is None
            and record.recommended_window is None
            and record.missed_window_count == 0
        ):
            state.vehicle_alerts.pop(vehicle_key, None)

    @staticmethod
    def active_postponements(state: AppState, now: datetime) -> dict[str, datetime]:
        """Return `{vehicle_key: postponed_until}` entries strictly in the future."""
        return {
            key: rec.postponed_until
            for key, rec in state.vehicle_alerts.items()
            if rec.postponed_until is not None and rec.postponed_until > now
        }

    @staticmethod
    def active_ev_windows(
        state: AppState,
        now: datetime,
    ) -> dict[str, tuple[datetime, datetime]]:
        """Return `{vehicle_key: (window_start, window_end)}` for active EV windows."""
        return {
            key: (rec.window_start, rec.window_end)
            for key, rec in state.vehicle_alerts.items()
            if rec.window_start is not None
            and rec.window_end is not None
            and rec.window_start <= now < rec.window_end
            and (rec.postponed_until is None or rec.postponed_until <= now)
        }

    @classmethod
    def paused_missed_vehicles(
        cls,
        state: AppState,
        cooldown_hours: float,
        max_missed_renotifications: int,
        now: datetime,
    ) -> set[str]:
        """Return vehicles paused in cooldown after reaching the missed-window cap."""
        return {
            key
            for key, rec in state.vehicle_alerts.items()
            if rec.last_notified_at is not None
            and cls.is_window_missed(rec, now)
            and rec.missed_window_count >= max_missed_renotifications
            and (now - rec.last_notified_at) < timedelta(hours=cooldown_hours)
        }

    @staticmethod
    def should_notify_error(
        state: AppState,
        cooldown_hours: float,
        now: datetime | None = None,
    ) -> bool:
        """Return True if a failure alert email may be sent outside cooldown."""
        if state.last_error_notified_at is None:
            return True
        current_time = now or datetime.now(tz=UTC)
        return (current_time - state.last_error_notified_at) >= timedelta(hours=cooldown_hours)
