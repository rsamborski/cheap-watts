# 📋 `pstryk-pilot` Implementation Backlog

Track progress across implementation phases. Update checkboxes as tasks are completed.

---

## Phase 0: Planning & Repository Setup
- [x] Initialize `pstryk-pilot` repository and `README.md`.
- [x] Finalize architecture and operational requirements (`adr-001-initial-architecture.md`).
- [x] Establish phased execution plan (`execution-plan.md`).

## Phase 1: Project Scaffolding & Core Infrastructure (`uv`)
- [x] Initialize Python 3.12+ project with `uv` (`pyproject.toml`, `uv.lock`, flat `src/` layout).
- [x] Configure `pydantic-settings` in `src/config.py` and create `.env.example` (Pstryk key, Deye Cloud credentials, BMW CarData client ID, coordinates/kWp, battery specs, EV thresholds, SMTP config, `DRY_RUN`).
- [x] Implement Linux-style plain-text rotating file logging (`src/logging_setup.py` -> `logs/pstryk-pilot.log`) for easy post-mortem investigation.
- [x] Implement local state persistence (`src/state.py`) for alert deduplication and last-applied Deye schedule tracking, ensuring runtime state/logs are git-ignored.
- [x] Create CLI entrypoint (`src/main.py`) supporting `run` and `--dry-run`.

## Phase 2: Read-Only API Connectors & Test Fixtures
- [x] Define shared domain models (`src/models.py`: `PriceSlot`, `SolarHour`, `VehicleStatus`, `InverterStatus`, `ChargingPlan`).
- [x] Implement **Pstryk API client** (`src/clients/pstryk.py`) for fetching today & next-day hourly tariffs.
- [x] Implement **Deye inverter read clients** (`src/clients/deye.py`) behind an `InverterClient` protocol: **Local LAN Solarman V5 TCP (`:8899`)** (`SolarmanV5Client`) + Deye Cloud Open API (`DeyeCloudClient`) for battery SoC, PV/grid/load power, and 6 TOU slots.
- [x] Implement **BMW CarData client** (`src/clients/cardata.py`) for reading BMW & MINI SoC, plug connection, and active charging status.
- [x] Implement **Solar forecast client** (`src/clients/solar.py`) via Open-Meteo / Forecast.Solar.
- [x] Capture anonymized JSON test fixtures in `tests/fixtures/` and write unit tests for response parsing.
- [x] Add read-only live API smoke-test CLI command (`pstryk-pilot probe`) with per-service selection and unit tests.

## Phase 3: Decision Engine & Schedule Planner
- [ ] Add static typical daily usage config (`TYPICAL_DAILY_USAGE_KWH`) in `src/config.py` and `.env.example` for v1 daily battery target sizing.
- [ ] Implement EV evaluation logic: if an EV is actively charging, skip EV threshold evaluation and block active EV hours from Deye grid-charging; otherwise evaluate `BMW <= 20%` and `MINI <= 40%`.
- [ ] Implement Summer / Sun optimization logic: when forecasted PV is sufficient to charge the home battery, set Deye TOU grid-charging `OFF` (PV charges battery automatically) and recommend daytime slow-charge (`6–10A`) sun windows for EVs needing charge.
- [ ] Implement low-sun dynamic Deye grid-charge sizing: only when forecasted PV is insufficient, calculate deficit (`min(target_soc, daily_usage_needed) - current_soc - expected_pv`) and select cheapest Pstryk hours grouped into **1–2 grid-charging windows per day** scheduled **after** any active or reserved EV charging hours, mapped to Deye's 6 TOU slots.
- [ ] Implement Deye TOU schedule diffing (`src/engine/diff.py`) to skip redundant API writes and produce clear audit logs.
- [ ] Add comprehensive unit tests (`tests/test_planner.py`, `tests/test_diff.py`) covering sunny summer (grid-charge OFF), cloudy winter, active EV charging (no EV alert, email sent if Deye schedule changes to block overlap), post-EV grid-charge ordering, 1–2x/day window limit, daily usage cap, no-op steady state (no email), missing next-day prices, and dry-run invariants.

## Phase 4: Deye Write Execution & SMTP Notifications
- [ ] Implement Deye Cloud / Solarman TOU write method in `src/clients/deye.py` gated strictly behind `not dry_run`.
- [ ] Implement SMTP email notifier (`src/notify/email.py`) triggered **whenever and only when Deye TOU schedule changes (including blocking hours for an actively charging EV or disabling grid-charge for sun) or a new EV recommendation occurs**.
- [ ] Enforce `[DRY-RUN]` subject/body prefix when running in `--dry-run` mode and cooldown deduplication via `state.py`.
- [ ] Wire full end-to-end pipeline in `src/main.py` with top-level error logging and optional failure alert email.

## Phase 5: Dry-Run Validation & Deployment Packaging
- [ ] Add `Dockerfile` and sample `crontab` configuration.
- [ ] Document setup, environment variables, API credential generation, and cron installation in `README.md`.
- [ ] Validate end-to-end `--dry-run` execution against live APIs and verify `[DRY-RUN]` email output.

---

## Future Improvements (Post-v1)
*Implement after core v1 functionality is complete and validated.*

### 1. Vacation Mode
- [ ] Add vacation configuration in `src/config.py` and `.env.example`: multiple `from`/`to` date ranges (e.g., `VACATION_RANGES`) and expected minimum battery SoC level (`VACATION_TARGET_SOC_PCT`).
- [ ] Update planner & notifier to detect active vacation dates, suppress EV charging notifications, and cap Deye grid-charging to the configured minimum vacation battery SoC.
- [ ] Add unit tests covering active/inactive vacation ranges, multiple date ranges, EV notification suppression, and minimum battery target.

### 2. Historical Usage Analysis
- [ ] Investigate Deye Cloud / Solarman Open API endpoints for fetching historical daily/hourly electrical consumption.
- [ ] Add a daily CLI subcommand/mode (e.g., `pstryk-pilot analyze-usage`) and sample daily `cron` entry to fetch past days of real usage and compute usage patterns.
- [ ] Persist learned usage profile in `state.py` and feed it into the grid-charging planner with a configurable 10–20% buffer (replacing/refining static `TYPICAL_DAILY_USAGE_KWH`).
- [ ] Add unit tests with historical usage fixtures, buffer calculation, and fallback to static `TYPICAL_DAILY_USAGE_KWH`.
