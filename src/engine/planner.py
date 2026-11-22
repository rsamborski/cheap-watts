"""Pure, deterministic charging schedule planner for Deye battery and BMW/MINI EVs."""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from src.config import Settings
from src.models import (
    ChargingPlan,
    EvChargeRecommendation,
    InverterStatus,
    PriceSlot,
    SolarForecast,
    TouSlot,
    VehicleStatus,
)
from src.state import AppState

_DEFAULT_EV_GRID_HOURS = 3
_DEFAULT_EV_SOLAR_HOURS = 4
_DEFAULT_ACTIVE_EV_MINUTES = 180
_MINUTES_PER_DAY = 24 * 60


def _localize_slot(slot: PriceSlot, tz: ZoneInfo) -> PriceSlot:
    return PriceSlot(
        start=slot.start.astimezone(tz),
        end=slot.end.astimezone(tz),
        price_pln_kwh=slot.price_pln_kwh,
        price_net_pln_kwh=slot.price_net_pln_kwh,
        currency=slot.currency,
    )


def _format_window(slots: Sequence[PriceSlot], tz: ZoneInfo) -> str:
    start_str = slots[0].start.astimezone(tz).strftime("%H:%M")
    end_str = slots[-1].end.astimezone(tz).strftime("%H:%M")
    return f"{start_str}-{end_str}"


def _contiguous_runs(slots: Sequence[PriceSlot]) -> list[list[PriceSlot]]:
    """Group chronologically ordered PriceSlots into contiguous time runs."""
    if not slots:
        return []
    ordered = sorted(slots, key=lambda s: s.start)
    runs: list[list[PriceSlot]] = [[ordered[0]]]
    for slot in ordered[1:]:
        if slot.start == runs[-1][-1].end:
            runs[-1].append(slot)
        elif slot.start > runs[-1][-1].end:
            runs.append([slot])
    return runs


def _vehicle_threshold(vehicle: VehicleStatus, settings: Settings) -> int:
    if vehicle.vehicle_key == "bmw":
        return settings.bmw_soc_threshold
    return settings.mini_soc_threshold


def _select_solar_ev_window(
    available: Sequence[PriceSlot],
    solar_forecast: SolarForecast,
    desired_hours: int = _DEFAULT_EV_SOLAR_HOURS,
) -> list[PriceSlot]:
    """Select the contiguous daytime window maximizing forecasted PV kWh."""
    if not available:
        return []

    sunny_slots = [
        s for s in available if solar_forecast.effective_kwh_between(s.start, s.end) > 0.05
    ]
    pool = sunny_slots if sunny_slots else list(available)
    runs = _contiguous_runs(pool)
    max_run_len = max(len(r) for r in runs)
    win_len = min(desired_hours, max_run_len)

    candidates: list[list[PriceSlot]] = []
    for run in runs:
        if len(run) < win_len:
            continue
        for idx in range(len(run) - win_len + 1):
            candidates.append(run[idx : idx + win_len])

    return max(
        candidates,
        key=lambda sl: (
            round(solar_forecast.effective_kwh_between(sl[0].start, sl[-1].end), 4),
            -round(sum(s.price_pln_kwh for s in sl), 6),
            -sl[0].start.timestamp(),
        ),
    )


def _select_cheap_ev_window(
    available: Sequence[PriceSlot],
    deye_hours_needed: int,
    desired_hours: int = _DEFAULT_EV_GRID_HOURS,
) -> list[PriceSlot]:
    """Select the cheapest contiguous EV window while preserving post-EV room when possible."""
    if not available:
        return []

    runs = _contiguous_runs(available)
    max_run_len = max(len(r) for r in runs)
    win_len = min(desired_hours, max_run_len)

    all_slices: list[list[PriceSlot]] = []
    for run in runs:
        if len(run) < win_len:
            continue
        for idx in range(len(run) - win_len + 1):
            all_slices.append(run[idx : idx + win_len])

    if deye_hours_needed > 0:
        min_post_slots = min(deye_hours_needed, max(0, len(available) - win_len))
        leaving_room = [
            sl
            for sl in all_slices
            if sum(1 for s in available if s.start >= sl[-1].end) >= min_post_slots
        ]
        if leaving_room:
            all_slices = leaving_room

    return min(
        all_slices,
        key=lambda sl: (
            round(sum(s.price_pln_kwh for s in sl), 6),
            sl[0].start.timestamp(),
        ),
    )


def _clock_intervals_for_slot(
    slot_start: datetime,
    slot_end: datetime,
    tz: ZoneInfo,
) -> list[tuple[int, int]]:
    local_start = slot_start.astimezone(tz)
    local_end = slot_end.astimezone(tz)
    s_min = _minutes_since_midnight(local_start)
    e_min = _minutes_since_midnight(local_end)
    if e_min == 0 and local_end > local_start:
        e_min = _MINUTES_PER_DAY

    if s_min < e_min:
        return [(s_min, e_min)]
    if s_min > e_min:
        intervals = [(s_min, _MINUTES_PER_DAY)]
        if e_min > 0:
            intervals.append((0, e_min))
        return intervals
    return []


def _window_clock_intervals(slots: Sequence[PriceSlot], tz: ZoneInfo) -> list[tuple[int, int]]:
    intervals: list[tuple[int, int]] = []
    for slot in slots:
        intervals.extend(_clock_intervals_for_slot(slot.start, slot.end, tz))
    return intervals


def _intervals_overlap(
    left: Sequence[tuple[int, int]],
    right: Sequence[tuple[int, int]],
) -> bool:
    return any(max(a0, b0) < min(a1, b1) for a0, a1 in left for b0, b1 in right)


def _select_deye_windows(
    candidates: Sequence[PriceSlot],
    hours_needed: int,
    tz: ZoneInfo | None = None,
) -> list[list[PriceSlot]]:
    """Select up to 2 contiguous windows of `hours_needed` total hours at minimum cost."""
    if hours_needed <= 0 or not candidates:
        return []

    zone = tz or ZoneInfo("Europe/Warsaw")
    runs = _contiguous_runs(candidates)
    slices_by_len: dict[int, list[tuple[float, list[PriceSlot]]]] = {}
    for run in runs:
        run_len = len(run)
        for length in range(1, min(hours_needed, run_len) + 1):
            bucket = slices_by_len.setdefault(length, [])
            for idx in range(run_len - length + 1):
                sl = run[idx : idx + length]
                cost = sum(s.price_pln_kwh for s in sl)
                bucket.append((cost, sl))

    max_possible = min(hours_needed, len(candidates))
    for target_h in range(max_possible, 0, -1):
        best_choice: tuple[float, int, float, float, list[list[PriceSlot]]] | None = None

        # 1-window candidates of exact length `target_h`
        for cost, sl in slices_by_len.get(target_h, ()):
            key_1 = (round(cost, 6), 1, sl[0].start.timestamp(), 0.0, [sl])
            if best_choice is None or key_1[:4] < best_choice[:4]:
                best_choice = key_1

        # 2-window non-adjacent candidates summing to `target_h`
        if target_h >= 2:
            for len1 in range(1, target_h):
                len2 = target_h - len1
                for cost1, sl1 in slices_by_len.get(len1, ()):
                    end1 = sl1[-1].end
                    t1 = sl1[0].start.timestamp()
                    iv1 = _window_clock_intervals(sl1, zone)
                    for cost2, sl2 in slices_by_len.get(len2, ()):
                        if sl2[0].start <= end1:
                            continue
                        iv2 = _window_clock_intervals(sl2, zone)
                        if _intervals_overlap(iv1, iv2):
                            continue
                        key_2 = (
                            round(cost1 + cost2, 6),
                            2,
                            t1,
                            sl2[0].start.timestamp(),
                            [sl1, sl2],
                        )
                        if best_choice is None or key_2[:4] < best_choice[:4]:
                            best_choice = key_2

        if best_choice is not None:
            return best_choice[4]

    return []


def _minutes_since_midnight(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _min_to_hhmm(minute_of_day: int) -> str:
    wrapped = minute_of_day % _MINUTES_PER_DAY
    hh, mm = divmod(wrapped, 60)
    return f"{hh:02d}:{mm:02d}"


def build_idle_tou_slots(min_soc: int) -> list[TouSlot]:
    """Return the canonical 6-slot all-OFF Deye TOU schedule covering 00:00..24:00."""
    boundaries = (0, 240, 480, 720, 960, 1200, 1440)
    return [
        TouSlot(
            slot_index=idx + 1,
            start_time=_min_to_hhmm(boundaries[idx]),
            end_time=_min_to_hhmm(boundaries[idx + 1]),
            target_soc=min_soc,
            grid_charge=False,
            sell=False,
        )
        for idx in range(6)
    ]


def map_windows_to_tou_slots(
    windows: Sequence[Sequence[PriceSlot]],
    planned_soc: int,
    min_soc: int,
    tz: ZoneInfo,
) -> list[TouSlot]:
    """Map 0, 1, or 2 grid-charging windows onto Deye's 6 contiguous 24-hour TOU slots."""
    if not windows:
        return build_idle_tou_slots(min_soc=min_soc)

    raw_active: list[tuple[int, int]] = []
    for window in windows:
        for slot in window:
            raw_active.extend(_clock_intervals_for_slot(slot.start, slot.end, tz))

    if not raw_active:
        return build_idle_tou_slots(min_soc=min_soc)

    raw_active.sort(key=lambda seg: (seg[0], seg[1]))
    merged_active: list[tuple[int, int]] = [raw_active[0]]
    for s_min, e_min in raw_active[1:]:
        prev_s, prev_e = merged_active[-1]
        if s_min <= prev_e:
            merged_active[-1] = (prev_s, max(prev_e, e_min))
        else:
            merged_active.append((s_min, e_min))

    # Build full [0, 1440) contiguous partition: (start_min, end_min, target_soc, grid_charge)
    segments: list[tuple[int, int, int, bool]] = []
    cursor = 0
    for s_min, e_min in merged_active:
        if cursor < s_min:
            segments.append((cursor, s_min, min_soc, False))
        segments.append((s_min, e_min, planned_soc, True))
        cursor = e_min
    if cursor < _MINUTES_PER_DAY:
        segments.append((cursor, _MINUTES_PER_DAY, min_soc, False))

    # Split longest idle (or active) segments cleanly until we have exactly 6 slots
    while len(segments) < 6:
        splittable_idle = [
            (idx, seg[1] - seg[0])
            for idx, seg in enumerate(segments)
            if not seg[3] and (seg[1] - seg[0]) >= 2
        ]
        if splittable_idle:
            split_idx = max(splittable_idle, key=lambda item: item[1])[0]
        else:
            splittable_any = [
                (idx, seg[1] - seg[0]) for idx, seg in enumerate(segments) if (seg[1] - seg[0]) >= 2
            ]
            split_idx = max(splittable_any, key=lambda item: item[1])[0]

        seg_start, seg_end, seg_soc, seg_gc = segments[split_idx]
        hour_aligned_mid = (((seg_start + seg_end) // 2) // 60) * 60
        if seg_start < hour_aligned_mid < seg_end:
            mid = hour_aligned_mid
        else:
            mid = (seg_start + seg_end) // 2

        segments[split_idx : split_idx + 1] = [
            (seg_start, mid, seg_soc, seg_gc),
            (mid, seg_end, seg_soc, seg_gc),
        ]

    return [
        TouSlot(
            slot_index=idx + 1,
            start_time=_min_to_hhmm(s_min),
            end_time=_min_to_hhmm(e_min),
            target_soc=soc,
            grid_charge=gc,
            sell=False,
        )
        for idx, (s_min, e_min, soc, gc) in enumerate(segments[:6])
    ]


def _project_battery_energy_kwh(
    settings: Settings,
    current_soc: int,
    start_time: datetime,
    target_time: datetime,
    solar_forecast: SolarForecast,
) -> float:
    """Project battery energy at `target_time` using constant `TYPICAL_DAILY_USAGE_KWH / 24`."""
    capacity = settings.battery_capacity_kwh
    min_reserve_kwh = (settings.battery_min_soc / 100.0) * capacity
    energy_kwh = (current_soc / 100.0) * capacity
    if target_time <= start_time:
        return energy_kwh

    floor_kwh = min(energy_kwh, min_reserve_kwh)
    hourly_load_kw = settings.typical_daily_usage_kwh / 24.0
    cursor = start_time.astimezone(UTC)
    end_utc = target_time.astimezone(UTC)
    while cursor < end_utc:
        next_hour = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        step_end = min(next_hour, end_utc)
        dt_hours = (step_end - cursor).total_seconds() / 3600.0
        pv_kwh = solar_forecast.effective_kwh_between(cursor, step_end)
        net_kwh = pv_kwh - (hourly_load_kw * dt_hours)
        if net_kwh >= 0.0:
            energy_kwh = min(capacity, energy_kwh + net_kwh)
        else:
            energy_kwh = max(floor_kwh, energy_kwh + net_kwh)
        cursor = step_end
    return energy_kwh


def _compute_battery_deficit(
    settings: Settings,
    current_soc: int,
    expected_pv_kwh: float,
    solar_sufficient: bool,
    projected_energy_kwh: float | None = None,
) -> tuple[float, int]:
    """Compute `(grid_deficit_kwh, planned_target_soc)` excluding `BATTERY_MIN_SOC` reserve.

    Energy at or below `BATTERY_MIN_SOC` is an untouchable outage/lifespan reserve and is
    never counted as usable energy for daily consumption. Expected daily usage is scaled by
    `1 + usage_buffer_pct / 100` (default +10%), and the resulting shortfall is only clamped
    at the end to physical battery headroom up to `BATTERY_TARGET_SOC`.
    """
    if solar_sufficient:
        return 0.0, settings.battery_min_soc

    capacity = settings.battery_capacity_kwh
    min_reserve_kwh = (settings.battery_min_soc / 100.0) * capacity
    target_cap_kwh = (settings.battery_target_soc / 100.0) * capacity
    effective_energy_kwh = (
        projected_energy_kwh
        if projected_energy_kwh is not None
        else (current_soc / 100.0) * capacity
    )

    usable_current_kwh = effective_energy_kwh - min_reserve_kwh
    headroom_to_target_kwh = max(0.0, target_cap_kwh - effective_energy_kwh)

    buffered_demand_kwh = settings.typical_daily_usage_kwh * (
        1.0 + settings.usage_buffer_pct / 100.0
    )
    raw_shortfall_kwh = max(0.0, buffered_demand_kwh - usable_current_kwh - expected_pv_kwh)
    deficit_kwh = round(min(headroom_to_target_kwh, raw_shortfall_kwh), 3)
    if deficit_kwh <= 0.0:
        return 0.0, settings.battery_min_soc

    planned_energy_kwh = min(target_cap_kwh, effective_energy_kwh + deficit_kwh)
    raw_soc = int(round((planned_energy_kwh / capacity) * 100.0))
    effective_soc = int(round((effective_energy_kwh / capacity) * 100.0))
    min_required = min(
        settings.battery_target_soc, max(settings.battery_min_soc, effective_soc + 1)
    )
    planned_soc = min(settings.battery_target_soc, max(min_required, raw_soc))
    return deficit_kwh, planned_soc


def _plan_deye_windows(
    settings: Settings,
    current_soc: int,
    candidates: Sequence[PriceSlot],
    solar_forecast: SolarForecast,
    current_time: datetime,
    tz: ZoneInfo,
) -> tuple[list[list[PriceSlot]], float, int, int, int]:
    """Select Deye grid-charging windows accounting for projected discharge before start."""
    capacity = settings.battery_capacity_kwh
    target_cap_kwh = (settings.battery_target_soc / 100.0) * capacity

    usable_candidates = [
        slot
        for slot in candidates
        if _project_battery_energy_kwh(
            settings,
            current_soc,
            current_time,
            max(current_time, slot.start),
            solar_forecast,
        )
        < target_cap_kwh - 1e-6
    ]
    if not usable_candidates:
        return [], 0.0, settings.battery_min_soc, 0, current_soc

    cheapest = min(
        usable_candidates,
        key=lambda s: (round(s.price_pln_kwh, 6), s.start.timestamp()),
    )
    anchor_start = max(current_time, cheapest.start)
    proj_kwh = _project_battery_energy_kwh(
        settings, current_soc, current_time, anchor_start, solar_forecast
    )
    proj_soc = int(round((proj_kwh / capacity) * 100.0))
    post_pv = solar_forecast.effective_kwh_between(anchor_start, anchor_start + timedelta(hours=24))
    deficit_kwh, planned_soc = _compute_battery_deficit(
        settings=settings,
        current_soc=current_soc,
        expected_pv_kwh=post_pv,
        solar_sufficient=False,
        projected_energy_kwh=proj_kwh,
    )
    if deficit_kwh <= 0.0:
        return [], 0.0, settings.battery_min_soc, 0, proj_soc

    hours_needed = math.ceil(deficit_kwh / settings.battery_max_charge_kw)
    selected: list[list[PriceSlot]] = []
    for _ in range(4):
        selected = _select_deye_windows(usable_candidates, hours_needed=hours_needed, tz=tz)
        if not selected:
            return [], deficit_kwh, settings.battery_min_soc, hours_needed, proj_soc
        win_start = max(current_time, selected[0][0].start)
        proj_kwh = _project_battery_energy_kwh(
            settings, current_soc, current_time, win_start, solar_forecast
        )
        proj_soc = int(round((proj_kwh / capacity) * 100.0))
        post_pv = solar_forecast.effective_kwh_between(win_start, win_start + timedelta(hours=24))
        new_deficit, new_planned_soc = _compute_battery_deficit(
            settings=settings,
            current_soc=current_soc,
            expected_pv_kwh=post_pv,
            solar_sufficient=False,
            projected_energy_kwh=proj_kwh,
        )
        if new_deficit <= 0.0:
            return [], 0.0, settings.battery_min_soc, 0, proj_soc
        deficit_kwh, planned_soc = new_deficit, new_planned_soc
        new_hours = math.ceil(new_deficit / settings.battery_max_charge_kw)
        if new_hours > hours_needed:
            hours_needed = new_hours
            continue
        break

    return selected, deficit_kwh, planned_soc, hours_needed, proj_soc


def _format_distance(distance_m: float | None) -> str:
    if distance_m is None:
        return "away"
    if distance_m < 1000.0:
        return f"{distance_m:.0f}m"
    return f"{distance_m / 1000.0:.1f}km"


def plan_charging_schedule(
    settings: Settings,
    inverter: InverterStatus,
    price_slots: Sequence[PriceSlot],
    solar_forecast: SolarForecast,
    vehicles: Sequence[VehicleStatus] = (),
    now: datetime | None = None,
    postponed_vehicles: Mapping[str, datetime] | None = None,
    active_ev_windows: Mapping[str, tuple[datetime, datetime]] | None = None,
    paused_vehicles: Collection[str] = (),
) -> ChargingPlan:
    """Generate a single-cycle optimization plan for EV alerts and Deye TOU slots."""
    tz = ZoneInfo(settings.timezone)
    current_time = (now or datetime.now(tz=UTC)).astimezone(tz)
    current_hour_start = current_time.replace(minute=0, second=0, microsecond=0)
    horizon_end = current_hour_start + timedelta(hours=24)
    postponed_map = postponed_vehicles or {}
    active_windows_map = active_ev_windows or {}
    paused_set = set(paused_vehicles)

    upcoming_slots = sorted(
        (
            _localize_slot(s, tz)
            for s in price_slots
            if s.end.astimezone(tz) > current_time and s.start.astimezone(tz) < horizon_end
        ),
        key=lambda s: s.start,
    )

    expected_pv_kwh = solar_forecast.effective_kwh_between(current_time, horizon_end)
    solar_sufficient = expected_pv_kwh >= settings.solar_sufficient_kwh_threshold

    prelim_deye_hours = 0
    if not solar_sufficient and upcoming_slots:
        _, _, _, prelim_deye_hours, _ = _plan_deye_windows(
            settings=settings,
            current_soc=inverter.battery_soc,
            candidates=upcoming_slots,
            solar_forecast=solar_forecast,
            current_time=current_time,
            tz=tz,
        )

    ev_actively_charging: list[str] = []
    ev_recommendations: list[EvChargeRecommendation] = []
    blocked_ev_hours: list[datetime] = []
    post_ev_cutoff: datetime | None = None
    rationale_parts: list[str] = [
        f"PV24h={expected_pv_kwh:.2f}kWh (sufficient={solar_sufficient}, "
        f"threshold={settings.solar_sufficient_kwh_threshold:.1f}kWh)"
    ]

    charging_vehicles = [v for v in vehicles if v.is_charging]
    active_vehicles = [v for v in charging_vehicles if v.is_at_home is not False]
    away_charging = [v for v in charging_vehicles if v.is_at_home is False]
    for away_v in away_charging:
        rationale_parts.append(
            f"{away_v.display_name} charging away from home "
            f"({_format_distance(away_v.distance_from_home_m)}); not blocking home Deye TOU"
        )

    if active_vehicles:
        ev_actively_charging = [v.vehicle_key for v in active_vehicles]
        active_ends = [
            current_time
            + timedelta(
                minutes=(
                    v.time_remaining_minutes
                    if v.time_remaining_minutes and v.time_remaining_minutes > 0
                    else _DEFAULT_ACTIVE_EV_MINUTES
                )
            )
            for v in active_vehicles
        ]
        max_active_end = max(active_ends)
        blocked_slots = [s for s in upcoming_slots if s.start < max_active_end]
        blocked_ev_hours = [s.start for s in blocked_slots]
        post_ev_cutoff = max((s.end for s in blocked_slots), default=max_active_end)
        names = ", ".join(v.display_name for v in active_vehicles)
        rationale_parts.append(
            f"Active EV charging ({names}); skipped EV alerts and blocked "
            f"{len(blocked_ev_hours)}h until {post_ev_cutoff.strftime('%H:%M')}"
        )
    else:
        reserved_starts: set[datetime] = set()
        reserved_slots_all: list[PriceSlot] = []
        for idx, vehicle in enumerate(vehicles):
            threshold = _vehicle_threshold(vehicle, settings)
            if vehicle.soc_pct > threshold:
                continue
            if vehicle.is_at_home is False:
                rationale_parts.append(
                    f"{vehicle.display_name} ({vehicle.soc_pct}%<={threshold}%) "
                    f"away from home ({_format_distance(vehicle.distance_from_home_m)}); "
                    "deferred EV charging"
                )
                continue
            if vehicle.vehicle_key in paused_set:
                rationale_parts.append(
                    f"{vehicle.display_name} ({vehicle.soc_pct}%<={threshold}%) "
                    "paused after max missed windows; deferred EV charging"
                )
                continue

            raw_cutoff = postponed_map.get(vehicle.vehicle_key)
            cutoff_local = (
                raw_cutoff.astimezone(tz)
                if raw_cutoff is not None and raw_cutoff.astimezone(tz) > current_time
                else None
            )
            available_for_ev = [
                s
                for s in upcoming_slots
                if s.start not in reserved_starts
                and (cutoff_local is None or s.start >= cutoff_local)
            ]
            if not available_for_ev:
                if cutoff_local is not None:
                    rationale_parts.append(
                        f"{vehicle.display_name} postponed until "
                        f"{cutoff_local.strftime('%Y-%m-%d %H:%M')}; "
                        "no eligible slots in horizon yet"
                    )
                continue

            active_bounds = (
                active_windows_map.get(vehicle.vehicle_key) if cutoff_local is None else None
            )
            chosen: list[PriceSlot] = []
            mode: Literal["solar_slow", "grid_cheap"]
            amps: int | None
            win_str: str
            if active_bounds is not None:
                w_start = active_bounds[0].astimezone(tz)
                w_end = active_bounds[1].astimezone(tz)
                chosen = [s for s in available_for_ev if s.start < w_end and s.end > w_start]
                if chosen:
                    mode = "solar_slow" if solar_sufficient else "grid_cheap"
                    amps = settings.ev_slow_charge_amps if solar_sufficient else None
                    win_str = f"{w_start.strftime('%H:%M')}-{w_end.strftime('%H:%M')}"

            if not chosen:
                if solar_sufficient:
                    chosen = _select_solar_ev_window(available_for_ev, solar_forecast)
                    mode = "solar_slow"
                    amps = settings.ev_slow_charge_amps
                else:
                    # Reserve post-EV room on the last/only EV recommendation if battery needs grid
                    remaining_evs = sum(
                        1
                        for later in vehicles[idx + 1 :]
                        if later.soc_pct <= _vehicle_threshold(later, settings)
                        and later.is_at_home is not False
                        and later.vehicle_key not in paused_set
                    )
                    extra_room = prelim_deye_hours + (remaining_evs * _DEFAULT_EV_GRID_HOURS)
                    chosen = _select_cheap_ev_window(available_for_ev, deye_hours_needed=extra_room)
                    mode = "grid_cheap"
                    amps = None

                if not chosen:
                    continue
                win_str = _format_window(chosen, tz)
            ev_recommendations.append(
                EvChargeRecommendation(
                    vehicle_key=vehicle.vehicle_key,
                    display_name=vehicle.display_name,
                    soc_pct=vehicle.soc_pct,
                    threshold_pct=threshold,
                    mode=mode,
                    recommended_window=win_str,
                    slots=chosen,
                    recommended_amps=amps,
                )
            )
            for s in chosen:
                reserved_starts.add(s.start)
                reserved_slots_all.append(s)
            postpone_note = (
                f" (postponed >= {cutoff_local.strftime('%H:%M')})"
                if cutoff_local is not None
                else ""
            )
            rationale_parts.append(
                f"Recommend {vehicle.display_name} ({vehicle.soc_pct}%<={threshold}%) "
                f"{mode} {win_str}{postpone_note}"
            )

        if reserved_slots_all:
            blocked_ev_hours = sorted({s.start for s in reserved_slots_all})
            post_ev_cutoff = max(s.end for s in reserved_slots_all)

    # Step 2: Select 0, 1, or 2 post-EV Deye grid-charging windows
    selected_windows: list[list[PriceSlot]] = []
    grid_deficit_kwh = 0.0
    planned_soc = settings.battery_min_soc

    if solar_sufficient:
        rationale_parts.append("Sunny mode: Deye TOU grid-charging OFF")
    elif not upcoming_slots:
        rationale_parts.append("No upcoming tariff slots available: Deye TOU grid-charging OFF")
    else:
        blocked_set = set(blocked_ev_hours)
        deye_candidates = [
            s
            for s in upcoming_slots
            if s.start not in blocked_set and (post_ev_cutoff is None or s.start >= post_ev_cutoff)
        ]
        if not deye_candidates:
            _, blocked_deficit, _, _, _ = _plan_deye_windows(
                settings=settings,
                current_soc=inverter.battery_soc,
                candidates=upcoming_slots,
                solar_forecast=solar_forecast,
                current_time=current_time,
                tz=tz,
            )
            grid_deficit_kwh = blocked_deficit
            if grid_deficit_kwh > 0.0:
                rationale_parts.append(
                    f"Deficit={grid_deficit_kwh:.2f}kWh but 0 post-EV slots remain in horizon: "
                    "Deye TOU grid-charging OFF"
                )
            else:
                rationale_parts.append(
                    f"Battery SoC {inverter.battery_soc}% + PV covers daily target: "
                    "Deye TOU grid-charging OFF"
                )
        else:
            (
                selected_windows,
                grid_deficit_kwh,
                planned_soc,
                deye_hours_needed,
                projected_soc,
            ) = _plan_deye_windows(
                settings=settings,
                current_soc=inverter.battery_soc,
                candidates=deye_candidates,
                solar_forecast=solar_forecast,
                current_time=current_time,
                tz=tz,
            )
            if grid_deficit_kwh <= 0.0:
                rationale_parts.append(
                    f"Battery SoC {inverter.battery_soc}% (projected ~{projected_soc}%) + "
                    "PV covers daily target: Deye TOU grid-charging OFF"
                )
            else:
                win_labels = [_format_window(w, tz) for w in selected_windows]
                rationale_parts.append(
                    f"Low-sun deficit={grid_deficit_kwh:.2f}kWh "
                    f"(SoC {inverter.battery_soc}% -> ~{projected_soc}%, "
                    f"{deye_hours_needed}h -> target {planned_soc}%): "
                    f"Deye windows {', '.join(win_labels)}"
                )

    target_tou_slots = map_windows_to_tou_slots(
        windows=selected_windows,
        planned_soc=planned_soc,
        min_soc=settings.battery_min_soc,
        tz=tz,
    )

    return ChargingPlan(
        generated_at=current_time,
        solar_sufficient=solar_sufficient,
        expected_pv_kwh=expected_pv_kwh,
        grid_deficit_kwh=grid_deficit_kwh,
        ev_actively_charging=ev_actively_charging,
        blocked_ev_hours=blocked_ev_hours,
        ev_recommendations=ev_recommendations,
        target_tou_slots=target_tou_slots,
        rationale="; ".join(rationale_parts),
    )


def _hhmm_to_minutes(hhmm: str) -> int:
    hh_str, mm_str = hhmm.strip().split(":", maxsplit=1)
    return int(hh_str) * 60 + int(mm_str)


def _tou_active_intervals(slots: Sequence[TouSlot]) -> list[tuple[int, int]]:
    """Return sorted, merged `[0, 1440)` minute intervals where `grid_charge` is ON."""
    raw: list[tuple[int, int]] = []
    for slot in sorted(slots, key=lambda s: s.slot_index):
        if not slot.grid_charge:
            continue
        s_min = _hhmm_to_minutes(slot.start_time)
        e_min = _hhmm_to_minutes(slot.end_time)
        if e_min == 0 and s_min > 0:
            e_min = _MINUTES_PER_DAY
        if s_min < e_min:
            raw.append((s_min, e_min))
        elif s_min > e_min:
            raw.append((s_min, _MINUTES_PER_DAY))
            if e_min > 0:
                raw.append((0, e_min))

    if not raw:
        return []
    raw.sort()
    merged: list[tuple[int, int]] = [raw[0]]
    for s_min, e_min in raw[1:]:
        prev_s, prev_e = merged[-1]
        if s_min <= prev_e:
            merged[-1] = (prev_s, max(prev_e, e_min))
        else:
            merged.append((s_min, e_min))
    return merged


def is_tou_grid_charging_at(slots: Sequence[TouSlot], local_dt: datetime) -> bool:
    """Return True if `local_dt` falls inside an active `grid_charge=ON` TOU window."""
    minute = _minutes_since_midnight(local_dt)
    return any(s_min <= minute < e_min for s_min, e_min in _tou_active_intervals(slots))


def ev_conflicts_with_locked_tou(
    fresh_plan: ChargingPlan,
    locked_slots: Sequence[TouSlot],
    price_slots: Sequence[PriceSlot],
    now: datetime,
    tz: ZoneInfo,
) -> bool:
    """Return True if active EV charging or EV reservations conflict with `locked_slots`."""
    if not fresh_plan.ev_actively_charging and not fresh_plan.ev_recommendations:
        return False

    active_tou = _tou_active_intervals(locked_slots)
    if not active_tou:
        return False

    ev_intervals: list[tuple[int, int]] = []
    for hour_start in fresh_plan.blocked_ev_hours:
        ev_intervals.extend(
            _clock_intervals_for_slot(hour_start, hour_start + timedelta(hours=1), tz)
        )
    if not ev_intervals and fresh_plan.ev_actively_charging:
        local_now = now.astimezone(tz)
        ev_intervals.extend(
            _clock_intervals_for_slot(local_now, local_now + timedelta(hours=1), tz)
        )

    if _intervals_overlap(ev_intervals, active_tou):
        return True

    if not fresh_plan.solar_sufficient and fresh_plan.blocked_ev_hours:
        post_ev_cutoff = max(
            dt.astimezone(tz) + timedelta(hours=1) for dt in fresh_plan.blocked_ev_hours
        )
        local_now = now.astimezone(tz)
        horizon_end = local_now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=24)
        for slot in price_slots:
            s_local = _localize_slot(slot, tz)
            if (
                s_local.end > local_now
                and s_local.start < post_ev_cutoff
                and s_local.start < horizon_end
                and _intervals_overlap(
                    _clock_intervals_for_slot(s_local.start, s_local.end, tz), active_tou
                )
            ):
                return True

    return False


def should_replan_deye_schedule(
    fresh_plan: ChargingPlan,
    inverter: InverterStatus,
    price_slots: Sequence[PriceSlot],
    state: AppState,
    effective_dry_run: bool,
    now: datetime,
    tz: ZoneInfo,
    force_replan: bool = False,
) -> tuple[bool, str]:
    """Decide whether Deye TOU slots may be updated or remain locked for the current horizon."""
    local_now = now.astimezone(tz)
    current_price_horizon = max((s.end.astimezone(tz) for s in price_slots), default=None)
    has_persisted = len(state.last_deye_schedule) == 6
    locked_slots: list[TouSlot] | None
    if has_persisted:
        locked_slots = [TouSlot.from_record(r) for r in state.last_deye_schedule]
    elif len(inverter.tou_slots) == 6 and inverter.tou_enabled:
        locked_slots = list(inverter.tou_slots)
    else:
        locked_slots = None

    if locked_slots is None:
        return True, "initial_plan"

    if force_replan:
        return True, "manual_postpone_replan"

    if state.last_run_dry_run is True and not effective_dry_run:
        return True, "dry_run_to_live_transition"

    if ev_conflicts_with_locked_tou(fresh_plan, locked_slots, price_slots, now, tz):
        return True, "ev_conflict_override"

    if is_tou_grid_charging_at(locked_slots, local_now):
        return False, "active_charging_window_locked"

    if not has_persisted or state.deye_plan_horizon_end is None:
        return True, "initial_plan"

    prev_horizon = state.deye_plan_horizon_end.astimezone(tz)
    if current_price_horizon is not None and current_price_horizon > prev_horizon:
        return True, "new_tariff_horizon"
    if local_now >= prev_horizon:
        return True, "tariff_horizon_expired"

    if (
        state.deye_plan_solar_sufficient is not None
        and state.deye_plan_solar_sufficient != fresh_plan.solar_sufficient
    ):
        return True, "solar_regime_changed"

    return False, "sticky_daily_plan_locked"
