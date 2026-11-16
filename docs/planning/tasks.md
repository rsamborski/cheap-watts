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
- [x] Implement **BMW CarData client** (`src/clients/cardata.py`) for reading BMW & MINI SoC, plug connection, and active charging status, including OAuth 2.0 Device Code + PKCE authorization (`pstryk-pilot cardata-auth`), telematics container auto-provisioning, and rotated refresh-token persistence (`data/cardata_tokens.json`).
- [x] Implement **Solar forecast client** (`src/clients/solar.py`) via Open-Meteo / Forecast.Solar.
- [x] Capture anonymized JSON test fixtures in `tests/fixtures/` and write unit tests for response parsing.
- [x] Add read-only live API smoke-test CLI command (`pstryk-pilot probe`) with per-service selection and unit tests.
- [x] Validate manual read-only live connectivity (`pstryk-pilot probe`) across Pstryk tariffs, Open-Meteo dual-string PV forecast, local LAN Solarman V5 Deye inverter telemetry/TOU slots, and BMW CarData telemetry.

## Phase 3: Decision Engine & Schedule Planner
- [x] Add static typical daily usage config (`TYPICAL_DAILY_USAGE_KWH`) in `src/config.py` and `.env.example` for v1 daily battery target sizing.
- [x] Implement EV evaluation logic: if an EV is actively charging, skip EV threshold evaluation and block active EV hours from Deye grid-charging; otherwise evaluate `BMW <= 20%` and `MINI <= 40%`.
- [x] Implement Summer / Sun optimization logic: when forecasted PV is sufficient to charge the home battery, set Deye TOU grid-charging `OFF` (PV charges battery automatically) and recommend daytime slow-charge (`6–10A`) sun windows for EVs needing charge.
- [x] Implement low-sun dynamic Deye grid-charge sizing: only when forecasted PV is insufficient, calculate deficit (`min(target_soc, daily_usage_needed) - current_soc - expected_pv`) and select cheapest Pstryk hours grouped into **1–2 grid-charging windows per day** scheduled **after** any active or reserved EV charging hours, mapped to Deye's 6 TOU slots.
- [x] Implement Deye TOU schedule diffing (`src/engine/diff.py`) to skip redundant API writes and produce clear audit logs.
- [x] Add comprehensive unit tests (`tests/test_planner.py`, `tests/test_diff.py`) covering sunny summer (grid-charge OFF), cloudy winter, active EV charging (no EV alert, email sent if Deye schedule changes to block overlap), post-EV grid-charge ordering, 1–2x/day window limit, daily usage cap, no-op steady state (no email), missing next-day prices, and dry-run invariants.

## Phase 4: Deye Write Execution & SMTP Notifications
- [x] Implement Deye Cloud / Solarman TOU write method in `src/clients/deye.py` gated strictly behind `not dry_run`.
- [x] Implement SMTP email notifier (`src/notify/email.py`) triggered **whenever and only when Deye TOU schedule changes (including blocking hours for an actively charging EV or disabling grid-charge for sun) or a new EV recommendation occurs**.
- [x] Enforce `[DRY-RUN]` subject/body prefix when running in `--dry-run` mode and cooldown deduplication via `state.py`.
- [x] Wire full end-to-end pipeline in `src/main.py` with top-level error logging and optional failure alert email.

## Phase 5: Dry-Run Validation & Deployment Packaging
- [x] Add `Dockerfile` and sample `crontab` configuration.
- [x] Document setup, environment variables, API credential generation, and cron installation in `README.md`.
- [x] Validate end-to-end `--dry-run` execution against live APIs and verify `[DRY-RUN]` email output.

## Phase 6: Automated Deployment (GitHub Actions + Mini PC)
- [ ] Add GitHub Actions workflow (`.github/workflows/docker-publish.yml`) to build and push `ghcr.io/rsamborski/pstryk-pilot:latest` (and `:sha-...`) on pushes to `main` after CI checks pass.
- [ ] Set up Mini PC host directory (`/opt/pstryk-pilot` or similar) with `.env`, persistent `data/` and `logs/` volumes, and initial `cardata-auth` token provisioning.
- [ ] Configure Mini PC `crontab` for 30-minute `docker run --rm --network host` execution, periodic `docker pull` update checks, and weekly `docker image prune`.
- [ ] Document Mini PC + GHCR deployment and update flow in `README.md` and `deploy/`.
- [ ] *(Manual)* Configure GitHub branch protection on `main` to require pull requests (no direct pushes) and passing CI checks.
- [ ] Update AGENTS.md to reflect the new workflow with branches for all new features/bug fixes instead of direct main pushes.

---

## Future Improvements (Post-v1)
*Tracked in separate feature planning documents under `docs/planning/features/`:*

- [F001: Vacation Mode](features/F001-vacation-mode.md)
- [F002: Historical Usage Analysis](features/F002-historical-usage-analysis.md)
- [F003: Missed EV Charging Window Re-Notification](features/F003-missed-ev-window-renotification.md)
