"""Unit tests for shared domain models and validation rules."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from src.models import (
    ChargingPlan,
    EvChargeRecommendation,
    InverterStatus,
    PriceSlot,
    SolarForecast,
    SolarHour,
    TouSlot,
    VehicleStatus,
)


def test_price_slot_requires_timezone_and_positive_duration() -> None:
    t0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    slot = PriceSlot(start=t0, end=t0 + timedelta(hours=1), price_pln_kwh=-0.05)
    assert slot.price_pln_kwh == -0.05

    with pytest.raises(ValidationError):
        PriceSlot(
            start=datetime(2026, 11, 14, 12, 0),
            end=t0 + timedelta(hours=1),
            price_pln_kwh=0.5,
        )

    with pytest.raises(ValidationError):
        PriceSlot(start=t0, end=t0, price_pln_kwh=0.5)


def test_solar_forecast_aggregation_helpers() -> None:
    warsaw = ZoneInfo("Europe/Warsaw")
    d1_morning = datetime(2026, 6, 15, 9, 0, tzinfo=warsaw)
    d1_noon = datetime(2026, 6, 15, 12, 0, tzinfo=warsaw)
    d2_noon = datetime(2026, 6, 16, 12, 0, tzinfo=warsaw)

    hours = [
        SolarHour(
            start=d1_morning,
            end=d1_morning + timedelta(hours=1),
            irradiance_wm2=500.0,
            estimated_kwh=3.4,
        ),
        SolarHour(
            start=d1_noon,
            end=d1_noon + timedelta(hours=1),
            irradiance_wm2=900.0,
            estimated_kwh=6.1,
        ),
        SolarHour(
            start=d2_noon,
            end=d2_noon + timedelta(hours=1),
            irradiance_wm2=700.0,
            estimated_kwh=4.8,
        ),
    ]
    forecast = SolarForecast(hours=hours, fetched_at=datetime(2026, 6, 15, 6, 0, tzinfo=UTC))

    assert forecast.total_kwh == 14.3
    assert forecast.total_kwh_for_date(date(2026, 6, 15)) == 9.5
    assert forecast.total_kwh_for_date(date(2026, 6, 16)) == 4.8
    assert (
        forecast.total_kwh_between(
            datetime(2026, 6, 15, 10, 0, tzinfo=warsaw),
            datetime(2026, 6, 15, 14, 0, tzinfo=warsaw),
        )
        == 6.1
    )

    with pytest.raises(ValidationError):
        SolarHour(start=d1_noon, end=d1_morning, irradiance_wm2=100.0, estimated_kwh=0.5)


def test_tou_slot_validation_and_state_record_roundtrip() -> None:
    slot = TouSlot(
        slot_index=1,
        start_time="01:00",
        end_time="05:00",
        target_soc=95,
        grid_charge=True,
    )
    record = slot.to_record()
    reconstructed = TouSlot.from_record(record)
    assert reconstructed == slot

    with pytest.raises(ValidationError):
        TouSlot(
            slot_index=1,
            start_time="25:00",
            end_time="05:00",
            target_soc=95,
            grid_charge=True,
        )


def test_inverter_vehicle_and_charging_plan_models() -> None:
    now = datetime(2026, 11, 14, 21, 0, tzinfo=UTC)
    inv = InverterStatus(
        device_sn="DEYE0000EXAMPLE1",
        battery_soc=48,
        pv_power_w=1200.0,
        fetched_at=now,
    )
    assert inv.battery_soc == 48

    veh = VehicleStatus(
        vehicle_key="bmw",
        display_name="BMW",
        soc_pct=18,
        is_plugged_in=True,
        is_charging=False,
        fetched_at=now,
    )
    assert veh.vehicle_key == "bmw"

    rec = EvChargeRecommendation(
        vehicle_key="bmw",
        display_name="BMW",
        soc_pct=18,
        threshold_pct=20,
        mode="solar_slow",
        recommended_window="10:00-14:00",
        recommended_amps=8,
    )
    plan = ChargingPlan(
        generated_at=now,
        solar_sufficient=True,
        expected_pv_kwh=18.5,
        ev_recommendations=[rec],
        rationale="Strong solar forecast; grid charging disabled.",
    )
    assert plan.solar_sufficient is True
    assert len(plan.ev_recommendations) == 1
