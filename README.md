# ⚡ pstryk-pilot

An intelligent Home Energy Management System (HEMS) optimizing battery storage and electric/hybrid vehicle charging based on **Pstryk** dynamic tariffs.

---

## 🎯 Project Goal
Automatically control home energy flows (PV, battery storage, EV chargers) to maximize energy consumption during the cheapest hours of the day and avoid price peaks.

---

## 🧩 Components & Available Interfaces (APIs)

### 1. Energy Prices & Balance: **Pstryk**
* **Interface:** [Pstryk Backend API (Swagger)](https://pstryk.pl)
* **Authentication:** Free API key for personal use (generated in the mobile app: *Account > Devices and integrations* / *Konto > Urządzenia i integracje*).
* **Key Features:** Fetching current and forecasted dynamic prices, consumption data (meter), and balance (including the `/unified-metrics/` endpoint).
* **Notes:** Strictly for non-commercial use in accordance with the Pstryk terms of service.

### 2. Inverter & Battery Storage: **Deye SUN-10K-SG04LP3-EU**
* **Option A (Recommended — Local):** **Modbus RTU (RS-485)** (wired via the RJ45 *Modbus* port + RS485-to-Ethernet/USB/ESP32 converter) or **Solarman V5 TCP** (local polling of the Wi-Fi logger on port `8899`). Enables real-time reading and writing of registers (battery charge/discharge schedules, power limits) without cloud dependency.
* **Option B (Cloud):** **Deye Cloud Open API** (`App ID` + `App Secret` from the Deye developer portal) or **Solarman Open API**.

### 3. Vehicles: **BMW & MINI**
* **Telemetry (Data Reading):** **BMW CarData API (REST + MQTT)** — the official, free BMW Group API (shared across BMW and MINI brands), available after generating a `Client ID` in the customer portal. Allows monitoring battery State of Charge (SoC), plug connection status, and charging power.
* **Vehicle API Limitations:** BMW CarData operates strictly in **read-only** mode (no ability to remotely send start/stop commands or modify charging schedules directly on the vehicles).
* **Charging Control (Workaround):**
  * **Target (Most Reliable):** Control via the wall charger (**Wallbox** via OCPP, Modbus, or local API) — vehicles are set to charge immediately, while `pstryk-pilot` toggles power on the Wallbox during cheap time windows.
  * **Alternative:** Integration with a certified third-party aggregator that has access to the commercial BMW Energy API (e.g., Gridio).

---

## 🏗️ Proposed Logical Architecture

1. **Pricing Module (Pstryk):** Fetches hourly rates for the next day and determines the cheapest time windows (schedule).
2. **State Module (BMW CarData + Deye):** Checks the current battery State of Charge (SoC) of the Deye storage and connected vehicles (BMW / MINI), calculating energy demand (kWh).
3. **Decision Engine (Orchestrator):** Matches energy demand to the cheapest hours, factoring in PV forecasts and priorities (e.g., EV > battery storage).
4. **Execution Module:** Writes charging schedules to the Deye inverter (Modbus) and starts/pauses charging sessions on the Wallbox.

---

## 🚀 Next Steps
- [X] Initialize the `pstryk-pilot` repository on GitHub.
- [ ] Generate an API key in the Pstryk app and a `Client ID` in the BMW CarData portal.
- [ ] Choose the communication method for Deye (local Modbus RS-485 vs. Wi-Fi logger) and verify the Wallbox model.
- [ ] Implement initial API connectors (fetching Pstryk prices + reading vehicle SoC).
