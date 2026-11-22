"""Deye 6-slot TOU schedule diffing and change-driven notification gating."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from src.models import ChargingPlan, EvChargeRecommendation, TouSlot
from src.state import AppState, StateStore

_MINUTES_PER_DAY = 24 * 60


def _hhmm_to_minutes(hhmm: str) -> int:
    hh_str, mm_str = hhmm.strip().split(":", maxsplit=1)
    return int(hh_str) * 60 + int(mm_str)


def _format_slot_brief(slot: TouSlot | None) -> str:
    if slot is None:
        return "<unset>"
    gc_str = "ON" if slot.grid_charge else "OFF"
    sell_suffix = ", sell=ON" if slot.sell else ""
    return (
        f"{slot.start_time}-{slot.end_time} (target={slot.target_soc}%, grid={gc_str}{sell_suffix})"
    )


def _effective_24h_timeline(
    slots: Sequence[TouSlot],
) -> list[tuple[int, int, bool, int, bool]] | None:
    """Normalize a 6-slot `00:00`-anchored 24h ring by merging identical adjacent segments."""
    if len(slots) != 6:
        return None
    ordered = sorted(slots, key=lambda s: s.slot_index)
    if [s.slot_index for s in ordered] != [1, 2, 3, 4, 5, 6]:
        return None
    if ordered[0].start_time != "00:00":
        return None

    starts = [_hhmm_to_minutes(s.start_time) for s in ordered]
    for idx in range(6):
        expected_next = ordered[(idx + 1) % 6].start_time
        if ordered[idx].end_time != expected_next:
            return None
        if idx > 0 and starts[idx] <= starts[idx - 1]:
            return None

    merged: list[tuple[int, int, bool, int, bool]] = []
    for idx, slot in enumerate(ordered):
        seg_start = starts[idx]
        seg_end = starts[idx + 1] if idx < 5 else _MINUTES_PER_DAY
        key = (slot.grid_charge, slot.target_soc, slot.sell)
        if merged and merged[-1][2:] == key:
            prev = merged[-1]
            merged[-1] = (prev[0], seg_end, key[0], key[1], key[2])
        else:
            merged.append((seg_start, seg_end, key[0], key[1], key[2]))
    return merged


@dataclass(frozen=True)
class SlotDiffEntry:
    """Before/after diff for a single Deye TOU slot."""

    slot_index: int
    before: TouSlot | None
    after: TouSlot

    def describe(self) -> str:
        """Render a human-readable audit line for this slot change."""
        return (
            f"TOU #{self.slot_index}: {_format_slot_brief(self.before)} -> "
            f"{_format_slot_brief(self.after)}"
        )


@dataclass(frozen=True)
class ScheduleDiff:
    """Result of comparing current vs. planned 6-slot Deye TOU schedules."""

    has_changes: bool
    reason: str
    tou_enabled_changed: bool = False
    slot_diffs: list[SlotDiffEntry] = field(default_factory=list)

    def format_audit_log(self) -> str:
        """Render a multi-line human-readable audit summary."""
        if not self.has_changes:
            return f"Deye TOU schedule unchanged ({self.reason})"
        lines = [f"Deye TOU schedule update required ({self.reason}):"]
        if self.tou_enabled_changed:
            lines.append("  TOU mode: OFF -> ON")
        for entry in self.slot_diffs:
            lines.append(f"  {entry.describe()}")
        return "\n".join(lines)


@dataclass(frozen=True)
class CycleDecision:
    """Actionable write and notification flags for a single optimization cycle."""

    dry_run: bool
    should_write_deye: bool
    should_send_email: bool
    email_subject_prefix: str
    schedule_diff: ScheduleDiff
    notifiable_ev_recommendations: list[EvChargeRecommendation] = field(default_factory=list)
    suppressed_ev_recommendations: list[EvChargeRecommendation] = field(default_factory=list)
    missed_followup_counts: dict[str, int] = field(default_factory=dict)


def diff_tou_schedules(
    current_slots: Sequence[TouSlot],
    target_slots: Sequence[TouSlot],
    current_tou_enabled: bool = True,
    target_tou_enabled: bool = True,
) -> ScheduleDiff:
    """Compare current vs. target Deye TOU schedules and detect effective mutations."""
    if len(target_slots) != 6:
        raise ValueError(f"Expected exactly 6 target TOU slots, got {len(target_slots)}")

    ordered_target = sorted(target_slots, key=lambda s: s.slot_index)
    current_by_index = {s.slot_index: s for s in current_slots}
    tou_enabled_changed = current_tou_enabled != target_tou_enabled

    slot_diffs = [
        SlotDiffEntry(
            slot_index=target.slot_index,
            before=current_by_index.get(target.slot_index),
            after=target,
        )
        for target in ordered_target
        if current_by_index.get(target.slot_index) != target
    ]

    if not tou_enabled_changed and len(current_slots) == 6:
        cur_timeline = _effective_24h_timeline(current_slots)
        tgt_timeline = _effective_24h_timeline(ordered_target)
        if cur_timeline is not None and tgt_timeline is not None and cur_timeline == tgt_timeline:
            return ScheduleDiff(
                has_changes=False,
                reason="effective 24h TOU timeline identical",
                tou_enabled_changed=False,
                slot_diffs=[],
            )

    if not tou_enabled_changed and not slot_diffs:
        return ScheduleDiff(
            has_changes=False,
            reason="all 6 TOU slots identical",
            tou_enabled_changed=False,
            slot_diffs=[],
        )

    reasons: list[str] = []
    if tou_enabled_changed:
        reasons.append("TOU enable flag changed")
    if len(current_slots) != 6:
        reasons.append(f"current schedule had {len(current_slots)} slot(s)")
    elif slot_diffs:
        reasons.append(f"{len(slot_diffs)} slot(s) modified")

    return ScheduleDiff(
        has_changes=True,
        reason=", ".join(reasons),
        tou_enabled_changed=tou_enabled_changed,
        slot_diffs=slot_diffs,
    )


def evaluate_cycle_actions(
    plan: ChargingPlan,
    schedule_diff: ScheduleDiff,
    state: AppState,
    cooldown_hours: float,
    dry_run: bool = False,
    now: datetime | None = None,
    max_missed_renotifications: int = 3,
) -> CycleDecision:
    """Determine Deye write and SMTP notification triggers enforcing dry-run and cooldowns."""
    notifiable: list[EvChargeRecommendation] = []
    suppressed: list[EvChargeRecommendation] = []
    missed_followup_counts: dict[str, int] = {}

    for rec in plan.ev_recommendations:
        is_missed = StateStore.is_missed_window_renotification(
            state=state,
            vehicle_key=rec.vehicle_key,
            max_missed_renotifications=max_missed_renotifications,
            now=now,
        )
        if StateStore.should_notify_vehicle(
            state=state,
            vehicle_key=rec.vehicle_key,
            recommended_window=rec.recommended_window,
            cooldown_hours=cooldown_hours,
            max_missed_renotifications=max_missed_renotifications,
            now=now,
        ):
            notifiable.append(rec)
            if is_missed and rec.vehicle_key in state.vehicle_alerts:
                missed_followup_counts[rec.vehicle_key] = (
                    state.vehicle_alerts[rec.vehicle_key].missed_window_count + 1
                )
        else:
            suppressed.append(rec)

    should_write_deye = schedule_diff.has_changes and not dry_run
    should_send_email = schedule_diff.has_changes or bool(notifiable)
    subject_prefix = "[DRY-RUN] " if dry_run else ""

    return CycleDecision(
        dry_run=dry_run,
        should_write_deye=should_write_deye,
        should_send_email=should_send_email,
        email_subject_prefix=subject_prefix,
        schedule_diff=schedule_diff,
        notifiable_ev_recommendations=notifiable,
        suppressed_ev_recommendations=suppressed,
        missed_followup_counts=missed_followup_counts,
    )
