# F003: EV Home Presence, Postponement & Missed-Window Re-Notification

- **Feature ID:** `F003`
- **Status:** Implemented
- **Target:** `src/clients/cardata.py`, `src/models.py`, `src/config.py`, `src/state.py`, `src/engine/planner.py`, `src/engine/diff.py`, `src/notify/email.py`, `src/probe.py`, `src/main.py`, `tests/`

---

## Goal

Prevent premature EV charging recommendations and unnecessary Deye TOU blocking when an EV is physically away from home, provide an explicit CLI command to postpone EV charging (`cheap-watts postpone`), and automatically re-notify (up to a bounded cap) when a recommended charging window at home is missed.

---

## Requirements

### 1. CarData GPS Home Geofence (`is_at_home`)
- **Telematics Location Descriptors & Container Auto-Upgrade:**
  - Include `vehicle.cabin.infotainment.navigation.currentLocation.latitude` and `vehicle.cabin.infotainment.navigation.currentLocation.longitude` in CarData telematics container descriptors (`src/clients/cardata.py`).
  - Version the auto-provisioned container name to `cheap-watts-v2` so `CarDataClient` automatically upgrades from older `cheap-watts` v1 containers using the existing `cardata:api:read` OAuth token without requiring interactive browser re-authorization (falling back to `_CORE_CONTAINER_DESCRIPTORS` on HTTP `400` if rejected).
- **Home Geofence Configuration & Distance Calculation:**
  - Add `HOME_RADIUS_METERS` (default `200.0`) to `Settings` (`src/config.py`) and `.env.example`.
  - Compute the Haversine distance between the vehicle's reported GPS coordinates and `(SOLAR_LATITUDE, SOLAR_LONGITUDE)`.
  - Extend `VehicleStatus` (`src/models.py`) with `distance_from_home_m: float | None = None` and `is_at_home: bool | None = None`.
  - If GPS coordinates are absent/disabled in iDrive or home coordinates are unset, fall back gracefully with `is_at_home = None` (`None` treated as unknown / eligible rather than failing the poll).
- **Planner Gating While Away (`is_at_home is False`):**
  - When `is_at_home is False`, defer EV charging recommendations completely **and** skip home Deye TOU blocking (both low-SoC EV reservations and away-from-home public charging sessions must not block home battery charging).
  - On the first cycle after the vehicle returns home (`is_at_home` becomes `True` while `soc_pct <= threshold`), plan EV windows from `now` onward, trigger `ev_conflict_override` if the locked Deye TOU schedule needs adjustment, and send the EV notification email.

### 2. Manual Postpone CLI (`cheap-watts postpone`)
- **CLI Subcommand (`src/main.py`):**
  - Add `cheap-watts postpone -v <bmw|mini> --until <HH:MM|YYYY-MM-DDTHH:MM>` (interpreting `HH:MM` in configured `TIMEZONE` as today if strictly in the future, else tomorrow).
  - Support `--clear` (remove an active postponement), `--dry-run`, and `--no-run` (update state without immediately running an optimization cycle).
- **Persistence & Status Visibility (`src/state.py`):**
  - Persist `postponed_until: datetime | None` per vehicle in `data/state.json` (`VehicleAlertRecord`).
  - Display any active per-vehicle `postponed_until` timestamps in `cheap-watts status`.
- **Immediate Re-Evaluation & Planner Filtering (`src/engine/planner.py`):**
  - Unless `--no-run` is specified, immediately execute one optimization cycle after updating `postponed_until` so Deye TOU unblocks any pre-`postponed_until` hours right away.
  - While `now < postponed_until`, restrict EV candidate slots for that vehicle to `slot.start >= postponed_until`. If no tariff slots `>= postponed_until` exist yet in the current 24h horizon, defer EV recommendation until those slots enter the horizon.
  - Auto-clear `postponed_until` once `now >= postponed_until`, `is_charging` becomes `True`, or `soc_pct > threshold`.

### 3. Missed EV Charging Window Detection & Bounded Re-Notification
- **State Tracking (`VehicleAlertRecord` in `src/state.py`):**
  - Extend `VehicleAlertRecord` with `window_start: datetime | None = None`, `window_end: datetime | None = None`, `charged_in_window: bool = False`, `missed_window_count: int = 0`, and `postponed_until: datetime | None = None`.
- **Episode Tracking & Reset:**
  - Mark `charged_in_window = True` if `is_charging` is observed while an active/previous window is tracked (`now < window_end`).
  - Reset the vehicle alert record (`clear_vehicle_alert`, including `missed_window_count = 0` and `postponed_until = None`) once `not is_charging and soc_pct > threshold`.
- **Bounded Missed-Window Re-Notification (`MAX_MISSED_EV_RENOTIFICATIONS`):**
  - Add `MAX_MISSED_EV_RENOTIFICATIONS` (default `3`) to `Settings` (`src/config.py`) and `.env.example`.
  - When `now >= window_end`, `not charged_in_window`, `soc_pct <= threshold`, and `is_at_home is not False`:
    - If `missed_window_count < MAX_MISSED_EV_RENOTIFICATIONS`, bypass `ALERT_COOLDOWN_HOURS` (even when the next best window shares the same `HH:MM-HH:MM` clock label on the following day), increment `missed_window_count`, replan the next best available window, and send a follow-up email.
    - Once `missed_window_count >= MAX_MISSED_EV_RENOTIFICATIONS`, fall back to standard `ALERT_COOLDOWN_HOURS` deduplication.
  - Suppress duplicate EV alerts while `now < window_end` and the recommended window is unchanged.

### 4. Observability & Privacy
- **Coordinate Privacy:** Never log or print raw GPS latitude/longitude values in `logs/cheap-watts.log`, `cheap-watts probe`, or SMTP emails — expose only `is_at_home` and rounded distance (`m` / `km`).
- **Clear Rationale & Messaging:** Record explicit notes in `ChargingPlan.rationale` and cycle emails when an EV is deferred (away from home or manually postponed) or re-notified after a missed window.

---

## Implementation Checklist

- [x] Add `HOME_RADIUS_METERS` (default `200.0`) and `MAX_MISSED_EV_RENOTIFICATIONS` (default `3`) to `src/config.py` and `.env.example`.
- [x] Extend `VehicleStatus` (`src/models.py`) with `distance_from_home_m` and `is_at_home`, and update `CarDataClient` (`src/clients/cardata.py`) to auto-upgrade to `cheap-watts-v2` with GPS descriptors and Haversine geofence calculation (preserving core fallback on HTTP `400` and `None` when GPS is unavailable).
- [x] Extend `VehicleAlertRecord` and `StateStore` (`src/state.py`) with `window_start`, `window_end`, `charged_in_window`, `missed_window_count`, and `postponed_until`.
- [x] Update `src/engine/planner.py` (`plan_charging_schedule`) and `src/engine/diff.py` (`evaluate_cycle_actions`) to skip away vehicles (`is_at_home is False`), constrain slots to `>= postponed_until`, and implement bounded missed-window cooldown bypasses.
- [x] Add `cheap-watts postpone` CLI subcommand and wire `status` / `probe` / `run` updates in `src/main.py`, `src/probe.py`, and `src/notify/email.py` (with zero raw GPS coordinates logged/printed).
- [x] Update `README.md` with CarData v2 container notes, `HOME_RADIUS_METERS` / `MAX_MISSED_EV_RENOTIFICATIONS` settings, and `cheap-watts postpone` CLI usage.
- [x] Add offline unit tests covering GPS home/away geofencing, `cheap-watts-v2` container creation fallback, `cheap-watts postpone` parsing/clearing/replanning, bounded missed-window re-notifications (`MAX_MISSED_EV_RENOTIFICATIONS = 3`), and privacy masking of raw coordinates.
