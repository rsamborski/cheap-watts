# ADR-001: Initial `pstryk-pilot` Architecture & Operational Decisions

- **Status:** Accepted
- **Date:** 2026-11-14

## Context

`pstryk-pilot` automates home battery charging (`Deye SUN-10K-SG04LP3-EU`) and coordinates electric/hybrid vehicle charging (`BMW` & `MINI`) against dynamic hourly electricity tariffs (`Pstryk`) and rooftop solar (`PV`) generation.

During initial design discovery, several hardware and operational trade-offs had to be resolved before writing code.

## Decisions

### 1. Python + `uv` Headless Cron Application
- **Decision:** Build as a headless CLI application in Python 3.12+ managed with `uv`, invoked every 15–30 minutes via `cron` on a Linux/macOS/Docker host.
- **Rationale:** Zero web UI overhead; `cron` naturally handles both periodic EV/PV conflict checks (every 15–30 min) and daily Day-Ahead schedule planning once Pstryk publishes next-day tariffs.

### 2. Deye Communication via Cloud Open API (Behind Protocol Interface)
- **Decision:** Use **Deye Cloud / Solarman Open API** for reading inverter telemetry (battery SoC, PV generation, current TOU slots) and writing Time-of-Use (TOU) charging schedules, wrapped behind an abstract `InverterClient` protocol.
- **Rationale:** Requires zero additional RS-485/Ethernet cabling or LAN logger configuration to launch v1, while preserving a clean seam to add local Solarman V5 TCP (`:8899`) or Modbus RTU later.

### 3. Read-Only EV Telemetry + Email Recommendations + Strict Non-Overlap
- **Decision:** Read SoC and charging state from **BMW CarData API**. Do not integrate smart Wallbox hardware in v1; instead:
  1. If CarData reports an EV is **already actively charging**, skip EV threshold evaluation (no "time to charge" alert), and block the active EV charging window from Deye grid-charging. If blocking those hours changes Deye's TOU schedule, apply the update and notify via email; if Deye's schedule is already non-overlapping/OFF, do nothing and send no email.
  2. Only when no EV is actively charging, evaluate thresholds (**BMW <= 20%** or **MINI <= 40%**): recommend the best window for the EV (daytime slow-charge on PV when sunny, or cheapest Pstryk hours when solar is insufficient) and keep Deye grid-charging out of those reserved hours.
- **Rationale:** Prevents simultaneous high-load draw from grid-charging both EV and home battery, avoids redundant EV alerts while the vehicle is already charging, and still notifies the user whenever Deye's schedule is modified to protect against overlap.

### 4. PV-First Battery Strategy (Grid-Charge Only When Solar Is Insufficient)
- **Decision:** Use a free solar forecast API (**Open-Meteo / Forecast.Solar** configured with site `lat`/`lon` and `kWp`) combined with live Deye PV output:
  1. **Enough sun (Summer / sunny forecast):** Turn Deye TOU **grid-charging OFF** completely — the Deye inverter charges the battery automatically from excess solar without touching the grid.
  2. **Insufficient sun (Winter / cloudy forecast):** Dynamically calculate the grid energy deficit based on a configured **typical daily usage** (`TYPICAL_DAILY_USAGE_KWH` in v1) minus current battery energy and expected PV, and enable Deye TOU grid-charging **only 1–2 times per day** during the cheapest Pstryk hours **scheduled after any active or recommended EV charging window** (never before, so EV charging does not immediately drain the newly grid-charged battery).
- **Rationale:** Never pays for grid electricity when solar can fill the battery naturally, avoids over-charging beyond daily household needs, limits grid-charge cycles to 1–2 consolidated windows per day, and prevents EV charging from discharging freshly grid-charged battery capacity.

### 5. Change-Driven SMTP Notifications with Deduplication
- **Decision:** Send notifications over standard SMTP (e.g., Gmail App Password or custom SMTP) **whenever and only when actionable changes occur** — either the Deye TOU schedule changes (new cheap slots, turning grid-charge OFF for sunny weather, or blocking hours because an EV started charging) OR a new EV charge recommendation triggers — backed by local state (`state.json` / SQLite) to deduplicate alerts. If neither changes, log the summary and send no email.
- **Rationale:** Keeps the inbox strictly signal-only: every inverter schedule mutation or new EV recommendation is reported, with zero noise on steady-state cron runs.

### 6. Safe `--dry-run` Mode with `[DRY-RUN]` Emails on Planned Changes
- **Decision:** In `--dry-run` mode, execute all read APIs, run the full planner, log the exact Deye TOU register/slot diffs without mutating the inverter, and **whenever a Deye schedule change or new EV alert would occur, send the email prefixed with `[DRY-RUN]`** (sending nothing when no changes are expected).
- **Rationale:** Allows multi-day real-world validation of pricing, solar forecasts, and EV recommendations directly from the user's inbox with zero risk to inverter settings and zero steady-state spam.
