"""Shared domain models for tariff prices, solar forecasts, telemetry, and charging plans."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.state import TouSlotRecord

_HHMM_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _require_tz_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class PriceSlot(BaseModel):
    """Hourly dynamic electricity tariff slot from Pstryk."""

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime
    price_pln_kwh: float
    price_net_pln_kwh: float | None = None
    currency: str = "PLN"

    @field_validator("start")
    @classmethod
    def _validate_start_tz(cls, value: datetime) -> datetime:
        return _require_tz_aware(value, "start")

    @field_validator("end")
    @classmethod
    def _validate_end_tz(cls, value: datetime) -> datetime:
        return _require_tz_aware(value, "end")

    @model_validator(mode="after")
    def _validate_interval(self) -> PriceSlot:
        if self.end <= self.start:
            raise ValueError("PriceSlot.end must be strictly after start")
        return self


class SolarHour(BaseModel):
    """Hourly solar irradiance and PV energy generation forecast."""

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime
    irradiance_wm2: float = Field(ge=0.0)
    estimated_kwh: float = Field(ge=0.0)

    @field_validator("start")
    @classmethod
    def _validate_start_tz(cls, value: datetime) -> datetime:
        return _require_tz_aware(value, "start")

    @field_validator("end")
    @classmethod
    def _validate_end_tz(cls, value: datetime) -> datetime:
        return _require_tz_aware(value, "end")

    @model_validator(mode="after")
    def _validate_interval(self) -> SolarHour:
        if self.end <= self.start:
            raise ValueError("SolarHour.end must be strictly after start")
        return self


class SolarForecast(BaseModel):
    """Collection of hourly PV generation forecasts with aggregation helpers."""

    model_config = ConfigDict(frozen=True)

    hours: list[SolarHour] = Field(default_factory=list)
    fetched_at: datetime

    @field_validator("fetched_at")
    @classmethod
    def _validate_fetched_at_tz(cls, value: datetime) -> datetime:
        return _require_tz_aware(value, "fetched_at")

    @property
    def total_kwh(self) -> float:
        """Total forecasted PV generation across all returned hours."""
        return round(sum(hour.estimated_kwh for hour in self.hours), 3)

    def total_kwh_for_date(self, target_date: date) -> float:
        """Sum forecasted PV kWh for slots starting on the given local calendar date."""
        total = sum(hour.estimated_kwh for hour in self.hours if hour.start.date() == target_date)
        return round(total, 3)

    def total_kwh_between(self, window_start: datetime, window_end: datetime) -> float:
        """Sum forecasted PV kWh for slots overlapping `[window_start, window_end)`."""
        total = sum(
            hour.estimated_kwh
            for hour in self.hours
            if hour.end > window_start and hour.start < window_end
        )
        return round(total, 3)


class TouSlot(BaseModel):
    """Single Time-of-Use (TOU) schedule slot on the Deye hybrid inverter."""

    model_config = ConfigDict(frozen=True)

    slot_index: int = Field(ge=1, le=6)
    start_time: str
    end_time: str
    target_soc: int = Field(ge=0, le=100)
    grid_charge: bool
    sell: bool = False

    @field_validator("start_time", "end_time")
    @classmethod
    def _validate_hhmm(cls, value: str) -> str:
        normalized = value.strip()
        if not _HHMM_PATTERN.match(normalized):
            raise ValueError(f"Invalid HH:MM time string: {value!r}")
        return normalized

    def to_record(self) -> TouSlotRecord:
        """Convert to a state-persisted TouSlotRecord."""
        return TouSlotRecord(
            slot_index=self.slot_index,
            start_time=self.start_time,
            end_time=self.end_time,
            target_soc=self.target_soc,
            grid_charge=self.grid_charge,
        )

    @classmethod
    def from_record(cls, record: TouSlotRecord) -> TouSlot:
        """Reconstruct a TouSlot from a persisted TouSlotRecord."""
        return cls(
            slot_index=record.slot_index,
            start_time=record.start_time,
            end_time=record.end_time,
            target_soc=record.target_soc,
            grid_charge=record.grid_charge,
        )


class InverterStatus(BaseModel):
    """Point-in-time telemetry and TOU schedule snapshot from the Deye inverter."""

    model_config = ConfigDict(frozen=True)

    device_sn: str
    battery_soc: int = Field(ge=0, le=100)
    pv_power_w: float = Field(default=0.0, ge=0.0)
    battery_power_w: float = 0.0
    grid_power_w: float = 0.0
    load_power_w: float = Field(default=0.0, ge=0.0)
    tou_enabled: bool = True
    tou_slots: list[TouSlot] = Field(default_factory=list)
    fetched_at: datetime

    @field_validator("fetched_at")
    @classmethod
    def _validate_fetched_at_tz(cls, value: datetime) -> datetime:
        return _require_tz_aware(value, "fetched_at")


class VehicleStatus(BaseModel):
    """Point-in-time EV/PHEV telemetry read from BMW CarData."""

    model_config = ConfigDict(frozen=True)

    vehicle_key: Literal["bmw", "mini"]
    display_name: str
    vin: str | None = None
    soc_pct: int = Field(ge=0, le=100)
    is_plugged_in: bool = False
    is_charging: bool = False
    charging_status_raw: str | None = None
    target_soc_pct: int | None = Field(default=None, ge=0, le=100)
    time_remaining_minutes: int | None = Field(default=None, ge=0)
    fetched_at: datetime

    @field_validator("fetched_at")
    @classmethod
    def _validate_fetched_at_tz(cls, value: datetime) -> datetime:
        return _require_tz_aware(value, "fetched_at")


class EvChargeRecommendation(BaseModel):
    """Recommended charging window for a vehicle below its SoC threshold."""

    model_config = ConfigDict(frozen=True)

    vehicle_key: Literal["bmw", "mini"]
    display_name: str
    soc_pct: int = Field(ge=0, le=100)
    threshold_pct: int = Field(ge=0, le=100)
    mode: Literal["solar_slow", "grid_cheap"]
    recommended_window: str
    slots: list[PriceSlot] = Field(default_factory=list)
    recommended_amps: int | None = Field(default=None, ge=6, le=32)


class ChargingPlan(BaseModel):
    """Decision output produced by the schedule planner for a single optimization cycle."""

    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    solar_sufficient: bool
    expected_pv_kwh: float = Field(ge=0.0)
    grid_deficit_kwh: float = Field(default=0.0, ge=0.0)
    ev_actively_charging: list[str] = Field(default_factory=list)
    blocked_ev_hours: list[datetime] = Field(default_factory=list)
    ev_recommendations: list[EvChargeRecommendation] = Field(default_factory=list)
    target_tou_slots: list[TouSlot] = Field(default_factory=list)
    rationale: str = ""

    @field_validator("generated_at")
    @classmethod
    def _validate_generated_at_tz(cls, value: datetime) -> datetime:
        return _require_tz_aware(value, "generated_at")
