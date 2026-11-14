# ⚡ pstryk-pilot

[![Lint — Ruff](https://github.com/rsamborski/pstryk-pilot/actions/workflows/lint.yml/badge.svg?branch=main)](https://github.com/rsamborski/pstryk-pilot/actions/workflows/lint.yml)
[![Format — Black](https://github.com/rsamborski/pstryk-pilot/actions/workflows/format.yml/badge.svg?branch=main)](https://github.com/rsamborski/pstryk-pilot/actions/workflows/format.yml)
[![Tests & Coverage — Pytest](https://github.com/rsamborski/pstryk-pilot/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/rsamborski/pstryk-pilot/actions/workflows/tests.yml)
[![Security Scan — Gitleaks](https://github.com/rsamborski/pstryk-pilot/actions/workflows/security-scan.yml/badge.svg?branch=main)](https://github.com/rsamborski/pstryk-pilot/actions/workflows/security-scan.yml)

A headless Home Energy Management System (HEMS) written in Python (`uv`), optimizing home battery storage and electric/hybrid vehicle charging based on **Pstryk** dynamic tariffs and solar (**PV**) forecasts.

---

## 🎯 Project Goal
Run periodically via `cron` to automatically schedule home battery grid-charging during the cheapest hours when solar production is insufficient, avoid simultaneous EV and home battery charging, and send actionable EV charging recommendations — with full local logging and a safe `--dry-run` mode.

---

## 🧩 Components & Interfaces

### 1. Dynamic Energy Tariffs: **Pstryk**
* **Interface:** [Pstryk Backend API](https://pstryk.pl)
* **Authentication:** Personal API key generated in the Pstryk mobile app (*Account > Devices and integrations*).
* **Role:** Provides current and next-day hourly electricity prices used to pick optimal charging windows (strictly for personal, non-commercial use).

### 2. Inverter & Battery Storage: **Deye SUN-10K-SG04LP3-EU**
* **Primary Interface:** **Deye Cloud / Solarman Open API** (`App ID` + `App Secret`), wrapped behind a clean adapter interface so local polling (Solarman V5 TCP / Modbus RS-485) can be added in the future.
* **Role:** Reads battery State of Charge (SoC), live PV output, and current Time-of-Use (TOU) settings; updates TOU grid-charging schedules when needed.

### 3. Vehicles: **BMW & MINI**
* **Interface:** **BMW CarData API** (official read-only telemetry across BMW and MINI).
* **Role:** Monitors battery SoC and active charging status:
  * **Charging Recommendations:** Sends an email alert when a vehicle needs charging (`BMW <= 20%`, `MINI <= 40%`), recommending either daytime slow-charging on solar surplus or the cheapest tariff hours.
  * **Conflict Prevention:** Reserves EV charging windows and blocks Deye grid-charging whenever a vehicle is actively charging so the home battery and EV never charge from the grid at the same time.

### 4. Solar Forecast & Notifications
* **PV Forecast:** Uses **Open-Meteo** solar forecasts alongside Deye telemetry — when enough sun is expected, Deye grid-charging is turned off so the battery charges naturally from PV.
* **Email Alerts (SMTP):** Sends notifications only when changes occur (updated Deye schedule or a new EV charging recommendation), prefixed with `[DRY-RUN]` when running in dry-run mode.

---

## 🏗️ High-Level Flow

1. **Data Collection:** Fetches Pstryk hourly prices, Deye battery/PV state, BMW & MINI telemetry, and solar forecast.
2. **Decision Engine:** Evaluates EV charging status/thresholds, checks whether expected PV is sufficient to fill the home battery, and computes non-overlapping cheapest grid-charge slots when needed.
3. **Execution & Audit:** Applies schedule changes to Deye (or logs the planned diff without modifying the inverter in `--dry-run` mode), records every step to rotating local logs, and sends change-driven email summaries.
