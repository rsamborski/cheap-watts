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
    """Tracks the last charging recommendation sent for a specific vehicle."""

    last_notified_at: datetime | None = None
    last_notified_soc: int | None = None
    recommended_window: str | None = None


class AppState(BaseModel):
    """Root persisted state written atomically to disk."""

    last_run_at: datetime | None = None
    last_run_status: Literal["ok", "error"] | None = None
    last_run_dry_run: bool | None = None
    last_deye_schedule: list[TouSlotRecord] = Field(default_factory=list)
    last_deye_updated_at: datetime | None = None
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
    def should_notify_vehicle(
        state: AppState,
        vehicle_key: str,
        recommended_window: str,
        cooldown_hours: float,
        now: datetime | None = None,
    ) -> bool:
        """Determine whether a vehicle charging recommendation email should be sent.

        Returns True when:
        - No prior alert is recorded for this vehicle,
        - The recommended charging window changed since the last alert, or
        - The alert cooldown window has elapsed.
        """
        current_time = now or datetime.now(tz=UTC)
        record = state.vehicle_alerts.get(vehicle_key)
        if record is None or record.last_notified_at is None:
            return True
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
    ) -> None:
        """Record that an alert was sent for the given vehicle."""
        state.vehicle_alerts[vehicle_key] = VehicleAlertRecord(
            last_notified_at=now or datetime.now(tz=UTC),
            last_notified_soc=soc,
            recommended_window=recommended_window,
        )

    @staticmethod
    def clear_vehicle_alert(state: AppState, vehicle_key: str) -> None:
        """Clear stored alert state once a vehicle recovers above its SoC threshold."""
        state.vehicle_alerts.pop(vehicle_key, None)

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
