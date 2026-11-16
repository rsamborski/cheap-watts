"""Core scheduling, optimization, and TOU diffing engine."""

from src.engine.diff import (
    CycleDecision,
    ScheduleDiff,
    SlotDiffEntry,
    diff_tou_schedules,
    evaluate_cycle_actions,
)
from src.engine.planner import (
    build_idle_tou_slots,
    ev_conflicts_with_locked_tou,
    is_tou_grid_charging_at,
    map_windows_to_tou_slots,
    plan_charging_schedule,
    should_replan_deye_schedule,
)

__all__ = [
    "CycleDecision",
    "ScheduleDiff",
    "SlotDiffEntry",
    "build_idle_tou_slots",
    "diff_tou_schedules",
    "ev_conflicts_with_locked_tou",
    "evaluate_cycle_actions",
    "is_tou_grid_charging_at",
    "map_windows_to_tou_slots",
    "plan_charging_schedule",
    "should_replan_deye_schedule",
]
