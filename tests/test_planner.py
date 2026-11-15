"""Comprehensive offline unit tests for the Phase 3 schedule planner."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.config import Settings
from src.engine.planner import (
    build_idle_tou_slots,
    map_windows_to_tou_slots,
    plan_charging_schedule,
)
from src.models import InverterStatus, PriceSlot, SolarForecast, SolarHour, TouSlot, VehicleStatus

WARSAW = ZoneInfo("Europe/Warsaw")


def _make_hourly_prices(
    base_start: datetime,
    prices: list[float],
) -> list[PriceSlot]:
    return [
        PriceSlot(
            start=base_start + timedelta(hours=idx),
            end=base_start + timedelta(hours=idx + 1),
            price_pln_kwh=price,
        )
        for idx, price in enumerate(prices)
    ]


def _make_solar_forecast(
    base_start: datetime,
    hourly_kwh: list[float],
) -> SolarForecast:
    hours = [
        SolarHour(
            start=base_start + timedelta(hours=idx),
            end=base_start + timedelta(hours=idx + 1),
            irradiance_wm2=kwh * 120.0,
            estimated_kwh=kwh,
        )
        for idx, kwh in enumerate(hourly_kwh)
    ]
    return SolarForecast(hours=hours, fetched_at=base_start)


def _make_inverter(soc: int = 30, tou_slots: list[TouSlot] | None = None) -> InverterStatus:
    return InverterStatus(
        device_sn="2700012345",
        battery_soc=soc,
        pv_power_w=0.0,
        tou_enabled=True,
        tou_slots=tou_slots if tou_slots is not None else build_idle_tou_slots(20),
        fetched_at=datetime(2026, 7, 15, 8, 0, tzinfo=WARSAW),
    )


def _assert_valid_deye_6_slot_ring(slots: list[TouSlot]) -> None:
    assert len(slots) == 6
    assert [s.slot_index for s in slots] == [1, 2, 3, 4, 5, 6]
    assert slots[0].start_time == "00:00"
    assert slots[-1].end_time == "00:00"
    for idx in range(6):
        assert slots[idx].end_time == slots[(idx + 1) % 6].start_time


def _count_grid_charge_windows(slots: list[TouSlot]) -> int:
    flags = [s.grid_charge for s in slots]
    if not any(flags):
        return 0
    if all(flags):
        return 1
    transitions = sum(1 for i in range(6) if not flags[i - 1] and flags[i])
    return transitions


def test_sunny_summer_disables_deye_grid_charge_and_recommends_solar_slow_ev() -> None:
    now = datetime(2026, 7, 15, 7, 0, tzinfo=WARSAW)
    day_start = datetime(2026, 7, 15, 0, 0, tzinfo=WARSAW)

    prices = _make_hourly_prices(day_start, [0.60] * 24)
    # Peak PV generation 10:00-14:00 (4.0 kWh/h -> 16 kWh in that 4h window, 26 kWh total)
    pv_profile = [
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.2,
        0.8,
        1.5,
        2.5,
        3.2,
        4.0,
        4.2,
        4.1,
        3.9,
        2.5,
        1.2,
        0.4,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
    ]
    forecast = _make_solar_forecast(day_start, pv_profile)

    bmw = VehicleStatus(
        vehicle_key="bmw",
        display_name="BMW",
        soc_pct=18,  # <= 20% threshold
        is_plugged_in=True,
        is_charging=False,
        fetched_at=now,
    )
    mini = VehicleStatus(
        vehicle_key="mini",
        display_name="MINI",
        soc_pct=65,  # > 40% threshold
        is_plugged_in=False,
        is_charging=False,
        fetched_at=now,
    )

    settings = Settings(
        _env_file=None,
        solar_sufficient_kwh_threshold=12.0,
        ev_slow_charge_amps=8,
    )
    plan = plan_charging_schedule(
        settings=settings,
        inverter=_make_inverter(soc=25),
        price_slots=prices,
        solar_forecast=forecast,
        vehicles=[bmw, mini],
        now=now,
    )

    assert plan.solar_sufficient is True
    assert plan.grid_deficit_kwh == 0.0
    _assert_valid_deye_6_slot_ring(plan.target_tou_slots)
    assert all(not slot.grid_charge for slot in plan.target_tou_slots)

    assert len(plan.ev_recommendations) == 1
    rec = plan.ev_recommendations[0]
    assert rec.vehicle_key == "bmw"
    assert rec.mode == "solar_slow"
    assert rec.recommended_amps == 8
    assert rec.recommended_window == "10:00-14:00"


def test_cloudy_winter_post_ev_ordering_and_both_evs_below_threshold() -> None:
    now = datetime(2026, 11, 15, 22, 0, tzinfo=WARSAW)
    base = datetime(2026, 11, 15, 22, 0, tzinfo=WARSAW)

    # 24h starting at 22:00:
    # 00:00-03:00 (idx 2..4) = 0.25 PLN/kWh (cheapest 3h -> BMW)
    # 03:00-06:00 (idx 5..7) = 0.30 PLN/kWh (next cheapest 3h -> MINI)
    # 06:00-08:00 (idx 8..9) = 0.38 PLN/kWh (cheapest post-EV 2h -> Deye battery!)
    hourly_rates = [0.95] * 24
    for i in (2, 3, 4):
        hourly_rates[i] = 0.25
    for i in (5, 6, 7):
        hourly_rates[i] = 0.30
    for i in (8, 9):
        hourly_rates[i] = 0.38

    prices = _make_hourly_prices(base, hourly_rates)
    forecast = _make_solar_forecast(base, [0.05] * 24)  # 1.2 kWh cloudy day

    bmw = VehicleStatus(
        vehicle_key="bmw",
        display_name="BMW",
        soc_pct=15,
        fetched_at=now,
    )
    mini = VehicleStatus(
        vehicle_key="mini",
        display_name="MINI",
        soc_pct=35,
        fetched_at=now,
    )
    settings = Settings(
        _env_file=None,
        battery_capacity_kwh=10.0,
        battery_min_soc=20,
        battery_target_soc=95,
        battery_max_charge_kw=5.0,
        typical_daily_usage_kwh=12.0,
        solar_sufficient_kwh_threshold=12.0,
    )

    plan = plan_charging_schedule(
        settings=settings,
        inverter=_make_inverter(soc=20),
        price_slots=prices,
        solar_forecast=forecast,
        vehicles=[bmw, mini],
        now=now,
    )

    assert plan.solar_sufficient is False
    assert len(plan.ev_recommendations) == 2
    assert plan.ev_recommendations[0].vehicle_key == "bmw"
    assert plan.ev_recommendations[0].mode == "grid_cheap"
    assert plan.ev_recommendations[0].recommended_window == "00:00-03:00"
    assert plan.ev_recommendations[1].vehicle_key == "mini"
    assert plan.ev_recommendations[1].recommended_window == "03:00-06:00"

    # Buffered demand = 12.0 * 1.10 = 13.2 kWh; usable battery at 20% floor = 0.0 kWh; PV = 1.2 kWh
    # Raw shortfall = 12.0 kWh -> clamped to headroom (9.5 - 2.0 = 7.5 kWh) -> 2h at 5 kW -> 95%
    assert plan.grid_deficit_kwh == 7.5
    _assert_valid_deye_6_slot_ring(plan.target_tou_slots)
    active_slots = [s for s in plan.target_tou_slots if s.grid_charge]
    assert len(active_slots) == 1
    assert active_slots[0].start_time == "06:00"
    assert active_slots[0].end_time == "08:00"
    assert active_slots[0].target_soc == 95


def test_active_ev_charging_skips_ev_alerts_and_schedules_deye_strictly_after() -> None:
    now = datetime(2026, 11, 16, 1, 0, tzinfo=WARSAW)
    day_start = datetime(2026, 11, 16, 0, 0, tzinfo=WARSAW)

    # 01:00-03:00 is cheapest (0.20 PLN), but MINI is actively charging for 120m (until 03:00)
    hourly_rates = [0.85] * 24
    hourly_rates[1] = 0.20  # 01:00-02:00 (blocked by active EV)
    hourly_rates[2] = 0.20  # 02:00-03:00 (blocked by active EV)
    hourly_rates[4] = 0.35  # 04:00-05:00 (post-EV cheap hour)
    prices = _make_hourly_prices(day_start, hourly_rates)
    forecast = _make_solar_forecast(day_start, [0.0] * 24)

    # BMW is <= 20% AND MINI is actively charging -> must skip ALL EV threshold alerts!
    bmw_low = VehicleStatus(
        vehicle_key="bmw",
        display_name="BMW",
        soc_pct=12,
        is_charging=False,
        fetched_at=now,
    )
    mini_active = VehicleStatus(
        vehicle_key="mini",
        display_name="MINI",
        soc_pct=30,
        is_plugged_in=True,
        is_charging=True,
        time_remaining_minutes=120,
        fetched_at=now,
    )

    settings = Settings(_env_file=None, battery_max_charge_kw=5.0)
    plan = plan_charging_schedule(
        settings=settings,
        inverter=_make_inverter(soc=60),  # 9.5 - 6.0 = 3.5 kWh -> 1 hour needed
        price_slots=prices,
        solar_forecast=forecast,
        vehicles=[bmw_low, mini_active],
        now=now,
    )

    assert plan.ev_actively_charging == ["mini"]
    assert plan.ev_recommendations == []
    assert len(plan.blocked_ev_hours) == 2

    active_tou = [s for s in plan.target_tou_slots if s.grid_charge]
    assert len(active_tou) == 1
    assert active_tou[0].start_time == "04:00"
    assert active_tou[0].end_time == "05:00"


def test_deye_consolidates_scattered_cheap_hours_into_at_most_two_windows() -> None:
    now = datetime(2026, 11, 16, 0, 0, tzinfo=WARSAW)
    # 4 isolated cheap single hours at 01:00, 05:00, 11:00, 16:00, while battery needs 3 hours!
    # Planner must NOT create 3 separate 1-hour windows; it must consolidate into <= 2 windows!
    hourly_rates = [0.90] * 24
    hourly_rates[1] = 0.20  # 01:00-02:00
    hourly_rates[2] = 0.45  # 02:00-03:00 (adjacent to 01:00 -> 2h window costs 0.65)
    hourly_rates[5] = 0.21  # 05:00-06:00
    hourly_rates[11] = 0.22  # 11:00-12:00
    hourly_rates[16] = 0.23  # 16:00-17:00

    prices = _make_hourly_prices(now, hourly_rates)
    forecast = _make_solar_forecast(now, [0.0] * 24)

    settings = Settings(
        _env_file=None,
        battery_capacity_kwh=15.0,
        battery_min_soc=20,
        battery_target_soc=95,
        battery_max_charge_kw=3.5,
        typical_daily_usage_kwh=15.0,
    )
    # Current 30% (4.5 kWh), target 95% (14.25 kWh) -> deficit 9.75 kWh -> ceil(9.75/3.5) = 3h
    plan = plan_charging_schedule(
        settings=settings,
        inverter=_make_inverter(soc=30),
        price_slots=prices,
        solar_forecast=forecast,
        now=now,
    )

    _assert_valid_deye_6_slot_ring(plan.target_tou_slots)
    window_count = _count_grid_charge_windows(plan.target_tou_slots)
    assert 1 <= window_count <= 2

    # Optimal 2-window 3h choice is [01:00-03:00] (0.65) + [05:00-06:00] (0.21) = 0.86 PLN total
    active_tou = [(s.start_time, s.end_time) for s in plan.target_tou_slots if s.grid_charge]
    assert active_tou == [("01:00", "03:00"), ("05:00", "06:00")]


def test_daily_usage_cap_limits_grid_charging_below_battery_target_soc() -> None:
    now = datetime(2026, 11, 16, 0, 0, tzinfo=WARSAW)
    hourly_rates = [0.70] * 24
    hourly_rates[2] = 0.30
    prices = _make_hourly_prices(now, hourly_rates)
    forecast = _make_solar_forecast(now, [0.0] * 24)
    # 0.5 kWh total PV across the day
    forecast = _make_solar_forecast(now, [0.5] + [0.0] * 23)

    # Battery is 10 kWh, min_soc=20% (2.0 kWh), target_soc=95% (9.5 kWh),
    # BUT typical_daily_usage_kwh is only 4.0 kWh -> usage cap = 2.0 + 4.0 = 6.0 kWh (60% SoC)!
    settings = Settings(
        _env_file=None,
        battery_capacity_kwh=10.0,
        battery_min_soc=20,
        battery_target_soc=95,
        battery_max_charge_kw=5.0,
        typical_daily_usage_kwh=4.0,
    )
    plan = plan_charging_schedule(
        settings=settings,
        inverter=_make_inverter(soc=25),  # 2.5 kWh current
        price_slots=prices,
        solar_forecast=forecast,
        now=now,
    )

    # Buffered demand = 4.0 * 1.10 = 4.4 kWh; usable battery above 20% = 0.5 kWh; PV = 0.5 kWh
    # Deficit = 4.4 - 0.5 - 0.5 = 3.4 kWh -> planned SoC = 25% + 34% = 59% (well below 95%!)
    assert plan.grid_deficit_kwh == 3.4
    active_slots = [s for s in plan.target_tou_slots if s.grid_charge]
    assert len(active_slots) == 1
    assert active_slots[0].start_time == "02:00"
    assert active_slots[0].end_time == "03:00"
    assert active_slots[0].target_soc == 59


def test_min_soc_15pct_reserve_excluded_and_10pct_usage_buffer() -> None:
    now = datetime(2026, 11, 16, 0, 0, tzinfo=WARSAW)
    hourly_rates = [0.80] * 24
    hourly_rates[2] = 0.22
    hourly_rates[3] = 0.24
    prices = _make_hourly_prices(now, hourly_rates)
    # 5.22 kWh expected PV across 24h
    forecast = _make_solar_forecast(now, [0.0] * 10 + [2.61, 2.61] + [0.0] * 12)

    settings = Settings(
        _env_file=None,
        battery_capacity_kwh=10.0,
        battery_min_soc=15,
        battery_target_soc=95,
        battery_max_charge_kw=5.0,
        typical_daily_usage_kwh=12.0,
        usage_buffer_pct=10.0,
        solar_sufficient_kwh_threshold=12.0,
    )
    # Battery is sitting at its 15% reserve floor -> 0.0 kWh usable energy!
    # Buffered demand = 12.0 * 1.10 = 13.2 kWh -> shortfall = 13.2 - 0.0 - 5.22 = 7.98 kWh!
    plan = plan_charging_schedule(
        settings=settings,
        inverter=_make_inverter(soc=15),
        price_slots=prices,
        solar_forecast=forecast,
        now=now,
    )

    assert plan.grid_deficit_kwh == 7.98
    active_slots = [s for s in plan.target_tou_slots if s.grid_charge]
    assert len(active_slots) == 1
    assert active_slots[0].start_time == "02:00"
    assert active_slots[0].end_time == "04:00"
    assert active_slots[0].target_soc == 95


def test_missing_tomorrow_prices_before_1400_and_midnight_wrap_mapping() -> None:
    # 11:00 CET before tomorrow's Day-Ahead prices are published (only today 00:00..24:00 exists)
    day_start = datetime(2026, 11, 15, 0, 0, tzinfo=WARSAW)
    now = datetime(2026, 11, 15, 11, 0, tzinfo=WARSAW)

    hourly_rates = [0.80] * 24
    hourly_rates[13] = 0.32  # 13:00-14:00 today
    hourly_rates[23] = 0.28  # 23:00-00:00 tonight
    today_only_prices = _make_hourly_prices(day_start, hourly_rates)
    forecast = _make_solar_forecast(day_start, [0.0] * 24)

    settings = Settings(_env_file=None, battery_max_charge_kw=4.0)
    plan = plan_charging_schedule(
        settings=settings,
        inverter=_make_inverter(soc=35),  # 9.5 - 3.5 = 6.0 kWh -> 2 hours needed
        price_slots=today_only_prices,
        solar_forecast=forecast,
        now=now,
    )

    _assert_valid_deye_6_slot_ring(plan.target_tou_slots)
    active = [(s.start_time, s.end_time) for s in plan.target_tou_slots if s.grid_charge]
    assert active == [("13:00", "14:00"), ("23:00", "00:00")]

    # Midnight-wrapping window (23:00 -> 01:00) maps cleanly onto 00:00-01:00 & 23:00-00:00
    wrap_slots = [
        PriceSlot(
            start=datetime(2026, 11, 15, 23, 0, tzinfo=WARSAW),
            end=datetime(2026, 11, 16, 0, 0, tzinfo=WARSAW),
            price_pln_kwh=0.20,
        ),
        PriceSlot(
            start=datetime(2026, 11, 16, 0, 0, tzinfo=WARSAW),
            end=datetime(2026, 11, 16, 1, 0, tzinfo=WARSAW),
            price_pln_kwh=0.20,
        ),
    ]
    mapped = map_windows_to_tou_slots([wrap_slots], planned_soc=85, min_soc=20, tz=WARSAW)
    _assert_valid_deye_6_slot_ring(mapped)
    assert (
        mapped[0].start_time == "00:00" and mapped[0].end_time == "01:00" and mapped[0].grid_charge
    )
    assert (
        mapped[-1].start_time == "23:00"
        and mapped[-1].end_time == "00:00"
        and mapped[-1].grid_charge
    )

    # Empty upcoming price horizon degrades safely to grid-charge OFF
    empty_plan = plan_charging_schedule(
        settings=settings,
        inverter=_make_inverter(soc=30),
        price_slots=[],
        solar_forecast=forecast,
        now=now,
    )
    assert empty_plan.grid_deficit_kwh == 0.0
    assert all(not s.grid_charge for s in empty_plan.target_tou_slots)

    # Low-sun day where battery SoC (95%) already covers daily target -> 0 deficit, grid-charge OFF
    already_full_plan = plan_charging_schedule(
        settings=settings,
        inverter=_make_inverter(soc=95),
        price_slots=today_only_prices,
        solar_forecast=forecast,
        now=now,
    )
    assert already_full_plan.solar_sufficient is False
    assert already_full_plan.grid_deficit_kwh == 0.0
    assert all(not s.grid_charge for s in already_full_plan.target_tou_slots)

    # Active EV session covering the entire remaining horizon -> 0 post-EV slots -> grid-charge OFF
    late_now = datetime(2026, 11, 15, 22, 0, tzinfo=WARSAW)
    ev_until_midnight = VehicleStatus(
        vehicle_key="bmw",
        display_name="BMW",
        soc_pct=30,
        is_plugged_in=True,
        is_charging=True,
        time_remaining_minutes=120,
        fetched_at=late_now,
    )
    blocked_horizon_plan = plan_charging_schedule(
        settings=settings,
        inverter=_make_inverter(soc=30),
        price_slots=today_only_prices,
        solar_forecast=forecast,
        vehicles=[ev_until_midnight],
        now=late_now,
    )
    assert all(not s.grid_charge for s in blocked_horizon_plan.target_tou_slots)
