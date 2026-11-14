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
- [ ] Define shared domain models (`src/models.py`: `PriceSlot`, `SolarHour`, `VehicleStatus`, `InverterStatus`, `ChargingPlan`).
- [ ] Implement **Pstryk API client** (`src/clients/pstryk.py`) for fetching today & next-day hourly tariffs.
- [ ] Implement **Deye Cloud / Solarman Open API read client** (`src/clients/deye.py`) behind an `InverterClient` protocol (battery SoC, PV power, current TOU slots).
- [ ] Implement **BMW CarData client** (`src/clients/cardata.py`) for reading BMW & MINI SoC, plug connection, and active charging status.
- [ ] Implement **Solar forecast client** (`src/clients/solar.py`) via Open-Meteo / Forecast.Solar.
- [ ] Capture anonymized JSON test fixtures in `tests/fixtures/` and write unit tests for response parsing.

## Phase 3: Decision Engine & Schedule Planner
- [ ] Implement EV evaluation logic: if an EV is actively charging, skip EV threshold evaluation and block active EV hours from Deye grid-charging; otherwise evaluate `BMW <= 20%` and `MINI <= 40%`.
- [ ] Implement Summer / Sun optimization logic: when forecasted PV is sufficient to charge the home battery, set Deye TOU grid-charging `OFF` (PV charges battery automatically) and recommend daytime slow-charge (`6–10A`) sun windows for EVs needing charge.
- [ ] Implement low-sun dynamic Deye grid-charge sizing: only when forecasted PV is insufficient, calculate deficit (`target_soc - current_soc - expected_pv`) and select cheapest Pstryk hours excluding blocked/reserved EV hours, mapped to Deye's 6 TOU slots.
- [ ] Implement Deye TOU schedule diffing (`src/engine/diff.py`) to skip redundant API writes and produce clear audit logs.
- [ ] Add comprehensive unit tests (`tests/test_planner.py`, `tests/test_diff.py`) covering sunny summer (grid-charge OFF), cloudy winter, active EV charging (no EV alert, email sent if Deye schedule changes to block overlap), no-op steady state (no email), missing next-day prices, and dry-run invariants.

## Phase 4: Deye Write Execution & SMTP Notifications
- [ ] Implement Deye Cloud / Solarman TOU write method in `src/clients/deye.py` gated strictly behind `not dry_run`.
- [ ] Implement SMTP email notifier (`src/notify/email.py`) triggered **whenever and only when Deye TOU schedule changes (including blocking hours for an actively charging EV or disabling grid-charge for sun) or a new EV recommendation occurs**.
- [ ] Enforce `[DRY-RUN]` subject/body prefix when running in `--dry-run` mode and cooldown deduplication via `state.py`.
- [ ] Wire full end-to-end pipeline in `src/main.py` with top-level error logging and optional failure alert email.

## Phase 5: Dry-Run Validation & Deployment Packaging
- [ ] Add `Dockerfile` and sample `crontab` configuration.
- [ ] Document setup, environment variables, API credential generation, and cron installation in `README.md`.
- [ ] Validate end-to-end `--dry-run` execution against live APIs and verify `[DRY-RUN]` email output.
