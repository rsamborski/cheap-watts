# F003: Missed EV Charging Window Re-Notification

- **Feature ID:** `F003`
- **Status:** Planned (Post-v1)
- **Target:** `src/state.py`, `src/engine/planner.py`, `src/main.py`, `tests/test_planner.py`

---

## Goal

If the user misses a recommended EV charging window (e.g., due to urgent travel or forgetting to plug in) and the vehicle remains below its SoC threshold, automatically send a follow-up notification suggesting the next best charging window instead of staying silent due to alert cooldown.

---

## Requirements

1. **Track Recommended EV Windows in State:**
   - Persist the recommended EV charging window (`start`, `end`, `vehicle_id`, `recommended_at`) in `data/state.json` via `src/state.py`.
2. **Missed Window Detection:**
   - On subsequent cron runs, check if a previously recommended window has elapsed (`now > recommended_window.end`), the vehicle never started charging, and its SoC is still `<= threshold`.
3. **Re-Plan & Re-Notify:**
   - Bypass/reset the normal cooldown for that vehicle so the planner selects the next best available window (sun slow-charge or cheapest upcoming Pstryk hours) and sends a follow-up email.
   - Avoid duplicate alerts while the currently recommended window is still upcoming or active.

---

## Implementation Checklist

- [ ] Extend `src/state.py` to store and load the last recommended EV window (`start`, `end`, `vehicle_id`).
- [ ] Update deduplication / cooldown logic in `src/main.py` / `src/engine/planner.py` to detect missed windows when `now > window.end` and EV is still below threshold.
- [ ] Add unit tests covering missed window detection, next-best window selection, and suppressing duplicate alerts while a recommended window is still active/upcoming.
