"""Pure, deterministic charging schedule planner for Deye battery and BMW/MINI EVs."""

from __future__ import annotations

import math
from collections.abc import Sequence
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

    sunny_slots = [s for s in available if solar_forecast.total_kwh_between(s.start, s.end) > 0.05]
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
            round(solar_forecast.total_kwh_between(sl[0].start, sl[-1].end), 4),
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


def _select_deye_windows(
    candidates: Sequence[PriceSlot],
    hours_needed: int,
) -> list[list[PriceSlot]]:
    """Select up to 2 contiguous windows of `hours_needed` total hours at minimum cost."""
    if hours_needed <= 0 or not candidates:
        return []

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
                    for cost2, sl2 in slices_by_len.get(len2, ()):
                        if sl2[0].start <= end1:
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
            local_start = slot.start.astimezone(tz)
            local_end = slot.end.astimezone(tz)
            s_min = _minutes_since_midnight(local_start)
            e_min = _minutes_since_midnight(local_end)
            if e_min == 0 and local_end > local_start:
                e_min = _MINUTES_PER_DAY

            if s_min < e_min:
                raw_active.append((s_min, e_min))
            elif s_min > e_min:
                raw_active.append((s_min, _MINUTES_PER_DAY))
                if e_min > 0:
                    raw_active.append((0, e_min))

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


def _compute_battery_deficit(
    settings: Settings,
    current_soc: int,
    expected_pv_kwh: float,
    solar_sufficient: bool,
) -> tuple[float, int]:
    """Compute `(grid_deficit_kwh, planned_target_soc)` under the daily-usage cap."""
    if solar_sufficient:
        return 0.0, settings.battery_min_soc

    capacity = settings.battery_capacity_kwh
    min_energy_kwh = (settings.battery_min_soc / 100.0) * capacity
    target_cap_kwh = (settings.battery_target_soc / 100.0) * capacity
    usage_cap_kwh = min(target_cap_kwh, min_energy_kwh + settings.typical_daily_usage_kwh)
    current_energy_kwh = (current_soc / 100.0) * capacity

    deficit_kwh = round(max(0.0, usage_cap_kwh - current_energy_kwh - expected_pv_kwh), 3)
    if deficit_kwh <= 0.0:
        return 0.0, settings.battery_min_soc

    planned_energy_kwh = min(usage_cap_kwh, current_energy_kwh + deficit_kwh)
    raw_soc = int(round((planned_energy_kwh / capacity) * 100.0))
    min_required = min(settings.battery_target_soc, max(settings.battery_min_soc, current_soc + 1))
    planned_soc = min(settings.battery_target_soc, max(min_required, raw_soc))
    return deficit_kwh, planned_soc


def plan_charging_schedule(
    settings: Settings,
    inverter: InverterStatus,
    price_slots: Sequence[PriceSlot],
    solar_forecast: SolarForecast,
    vehicles: Sequence[VehicleStatus] = (),
    now: datetime | None = None,
) -> ChargingPlan:
    """Generate a single-cycle optimization plan for EV alerts and Deye TOU slots."""
    tz = ZoneInfo(settings.timezone)
    current_time = (now or datetime.now(tz=UTC)).astimezone(tz)
    horizon_end = current_time + timedelta(hours=24)

    upcoming_slots = sorted(
        (
            _localize_slot(s, tz)
            for s in price_slots
            if s.end.astimezone(tz) > current_time and s.start.astimezone(tz) < horizon_end
        ),
        key=lambda s: s.start,
    )

    expected_pv_kwh = solar_forecast.total_kwh_between(current_time, horizon_end)
    solar_sufficient = expected_pv_kwh >= settings.solar_sufficient_kwh_threshold

    grid_deficit_kwh, planned_soc = _compute_battery_deficit(
        settings=settings,
        current_soc=inverter.battery_soc,
        expected_pv_kwh=expected_pv_kwh,
        solar_sufficient=solar_sufficient,
    )
    deye_hours_needed = (
        math.ceil(grid_deficit_kwh / settings.battery_max_charge_kw)
        if grid_deficit_kwh > 0.0
        else 0
    )

    ev_actively_charging: list[str] = []
    ev_recommendations: list[EvChargeRecommendation] = []
    blocked_ev_hours: list[datetime] = []
    post_ev_cutoff: datetime | None = None
    rationale_parts: list[str] = [
        f"PV24h={expected_pv_kwh:.2f}kWh (sufficient={solar_sufficient}, "
        f"threshold={settings.solar_sufficient_kwh_threshold:.1f}kWh)"
    ]

    active_vehicles = [v for v in vehicles if v.is_charging]
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

            available_for_ev = [s for s in upcoming_slots if s.start not in reserved_starts]
            if not available_for_ev:
                continue

            mode: Literal["solar_slow", "grid_cheap"]
            amps: int | None
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
                )
                extra_room = deye_hours_needed + (remaining_evs * _DEFAULT_EV_GRID_HOURS)
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
            rationale_parts.append(
                f"Recommend {vehicle.display_name} ({vehicle.soc_pct}%<={threshold}%) "
                f"{mode} {win_str}"
            )

        if reserved_slots_all:
            blocked_ev_hours = sorted({s.start for s in reserved_slots_all})
            post_ev_cutoff = max(s.end for s in reserved_slots_all)

    # Step 2: Select 0, 1, or 2 post-EV Deye grid-charging windows
    selected_windows: list[list[PriceSlot]] = []
    if solar_sufficient:
        rationale_parts.append("Sunny mode: Deye TOU grid-charging OFF")
    elif grid_deficit_kwh <= 0.0:
        rationale_parts.append(
            f"Battery SoC {inverter.battery_soc}% + PV covers daily target: "
            "Deye TOU grid-charging OFF"
        )
    elif not upcoming_slots:
        grid_deficit_kwh = 0.0
        rationale_parts.append("No upcoming tariff slots available: Deye TOU grid-charging OFF")
    else:
        blocked_set = set(blocked_ev_hours)
        deye_candidates = [
            s
            for s in upcoming_slots
            if s.start not in blocked_set and (post_ev_cutoff is None or s.start >= post_ev_cutoff)
        ]
        selected_windows = _select_deye_windows(deye_candidates, hours_needed=deye_hours_needed)
        if selected_windows:
            win_labels = [_format_window(w, tz) for w in selected_windows]
            rationale_parts.append(
                f"Low-sun deficit={grid_deficit_kwh:.2f}kWh ({deye_hours_needed}h -> "
                f"target {planned_soc}%): Deye windows {', '.join(win_labels)}"
            )
        else:
            rationale_parts.append(
                f"Deficit={grid_deficit_kwh:.2f}kWh but 0 post-EV slots remain in horizon: "
                "Deye TOU grid-charging OFF"
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
