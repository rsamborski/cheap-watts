# F001: Vacation Mode

- **Feature ID:** `F001`
- **Status:** Planned (Post-v1)
- **Target:** `src/config.py`, `src/engine/planner.py`, `src/notify/email.py`, `tests/test_planner.py`

---

## Goal

Allow configuring one or more vacation date ranges so `pstryk-pilot` automatically enters a low-maintenance mode while the household is away: suppressing EV charging alerts and keeping the home battery at a minimal configured SoC.

---

## Requirements

1. **Environment Configuration:**
   - `VACATION_RANGES`: Support multiple `from`/`to` date ranges (e.g., `2026-07-10:2026-07-24,2026-12-23:2027-01-02`).
   - `VACATION_TARGET_SOC_PCT`: Expected minimum battery SoC (%) to maintain during vacation periods (e.g., `30`).
2. **Active Vacation Detection:**
   - Determine whether the current local date falls within any configured `[from, to]` range (inclusive).
3. **Planner & Notifier Behavior in Vacation Mode:**
   - **Suppress EV notifications:** Do not send EV charging recommendation emails while in vacation mode.
   - **Minimal battery grid-charging:** Override the normal daily-usage / target SoC sizing so Deye grid-charging only maintains `VACATION_TARGET_SOC_PCT` (still respecting PV-first logic so solar charges for free if available).
   - **Auditability:** Clearly log `[VACATION MODE]` in `logs/pstryk-pilot.log` and in any Deye schedule-change email.

---

## Implementation Checklist

- [ ] Add `VACATION_RANGES` parser and `VACATION_TARGET_SOC_PCT` setting to `src/config.py` and `.env.example`.
- [ ] Implement date-range matching helper and integrate vacation mode into `src/engine/planner.py`.
- [ ] Suppress EV notification dispatch in `src/main.py` / `src/notify/email.py` when vacation mode is active.
- [ ] Add unit tests covering single/multiple date ranges, boundary dates, EV notification suppression, and minimum SoC grid-charge capping.
