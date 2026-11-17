# ⚡ cheap-watts

[![Lint — Ruff](https://github.com/rsamborski/cheap-watts/actions/workflows/lint.yml/badge.svg?branch=main)](https://github.com/rsamborski/cheap-watts/actions/workflows/lint.yml)
[![Format — Black](https://github.com/rsamborski/cheap-watts/actions/workflows/format.yml/badge.svg?branch=main)](https://github.com/rsamborski/cheap-watts/actions/workflows/format.yml)
[![Tests & Coverage — Pytest](https://github.com/rsamborski/cheap-watts/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/rsamborski/cheap-watts/actions/workflows/tests.yml)
[![Security Scan — Gitleaks](https://github.com/rsamborski/cheap-watts/actions/workflows/security-scan.yml/badge.svg?branch=main)](https://github.com/rsamborski/cheap-watts/actions/workflows/security-scan.yml)
[![Docker — GHCR](https://github.com/rsamborski/cheap-watts/actions/workflows/docker-publish.yml/badge.svg?branch=main)](https://github.com/rsamborski/cheap-watts/actions/workflows/docker-publish.yml)

A headless Home Energy Management System (HEMS) written in Python 3.12+ (`uv`), invoked periodically via `cron` to optimize **Deye SUN-10K-SG04LP3-EU** home battery charging and **BMW / MINI** EV charging recommendations against **Pstryk** dynamic hourly tariffs and **Open-Meteo** solar forecasts.

---

## ✨ Key Capabilities

1. **PV-First Battery Strategy:**
   - **Sufficient Sun (`24h PV >= SOLAR_SUFFICIENT_KWH_THRESHOLD`):** Turns Deye Time-of-Use (TOU) **grid-charging OFF** across all 6 slots so rooftop PV charges the home battery for free.
   - **Low Sun / Cloudy Weather:** Excludes the untouchable `BATTERY_MIN_SOC` reserve from usable battery energy, applies `USAGE_BUFFER_PCT` (`+10%` default) to `TYPICAL_DAILY_USAGE_KWH`, and selects the globally cheapest **1–2 contiguous Pstryk windows** mapped cleanly onto Deye's 6-slot `00:00..24:00` TOU ring.
   - **Sticky Daily TOU Plan:** Locks Deye TOU slots per Pstryk Day-Ahead tariff horizon (preventing mid-charge overwrites and night-to-night churn across 30-min cron runs), re-planning early only if an EV plugs in / conflicts or the solar sufficiency regime flips.
2. **EV Priority & Strict Non-Overlap (BMW & MINI):**
   - **Active EV Session Detection:** If BMW CarData reports an EV is actively charging, skips redundant EV alerts and blocks those hours from Deye grid-charging.
   - **Post-EV Battery Ordering:** Whenever an EV is actively charging or recommended a charging window (`BMW <= 20%`, `MINI <= 40%`), Deye home battery grid-charging is scheduled **strictly after** the EV window so EV charging never drains freshly grid-charged battery capacity.
   - **Sun Slow-Charge vs. Cheap Grid:** Recommends daytime slow-charge (`EV_SLOW_CHARGE_AMPS`, e.g. `8A`) on sunny days and cheapest contiguous Pstryk hours on cloudy days.
3. **Local LAN Inverter Control (Zero Cloud Rate Limits):**
   - Talks directly to the Deye / **SOLARMAN Smart** Wi-Fi stick logger over local TCP (`:8899` via `pysolarmanv5`), preserving weekday/generator bitmasks on TOU registers (`146, 148..153, 166..177`), with Deye Cloud Open API kept as an optional fallback.
4. **Signal-Only SMTP Alerts & Safe `--dry-run` Mode:**
   - Sends emails **only when actionable changes occur** (a Deye TOU schedule update or a new EV recommendation outside cooldown).
   - `--dry-run` executes all live read APIs and planning, **strictly skips all Deye writes**, and prefixes change/alert emails with `[DRY-RUN]`.

---

## 🚀 Quick Start

### Prerequisites
- Linux / WSL2 / macOS with Python `>= 3.12` and [`uv`](https://docs.astral.sh/uv/).

```bash
# 1. Install dependencies
uv sync

# 2. Create your local .env (never committed to git)
cp .env.example .env
```

---

## 🔑 Credential & Hardware Setup

### 1. Pstryk Dynamic Tariffs (`PSTRYK_API_KEY`)
1. Open the **Pstryk** mobile app -> **Account -> Devices and integrations**.
2. Generate a personal API token (`sk-...`) and set `PSTRYK_API_KEY` in `.env`.

### 2. Deye `SUN-10K-SG04LP3-EU` via Local LAN (`SOLARMAN Smart`)
1. Find the **10-digit logger serial number** (`27...`) in the **SOLARMAN Smart** app (*Device -> Logger*) or on the Wi-Fi stick label.
2. Reserve a **static DHCP lease** for the logger on your home router so its LAN IP never changes.
3. Set in `.env`:
   ```dotenv
   DEYE_MODBUS_HOST=192.168.1.50
   DEYE_MODBUS_PORT=8899
   DEYE_LOGGER_SERIAL=2700012345
   ```

### 3. Solar / PV Forecast (`Open-Meteo` — No API Key Required)
Set your home coordinates and array kWp. Single-plane and dual-string (e.g., East `-90.0` / West `90.0`) setups are supported:
```dotenv
SOLAR_LATITUDE=52.23
SOLAR_LONGITUDE=21.01
SOLAR_KWP=5.0
SOLAR_TILT_DEGREES=35.0
SOLAR_AZIMUTH_DEGREES=-90.0
# Optional 2nd string:
SOLAR_KWP_2=5.0
SOLAR_TILT_DEGREES_2=35.0
SOLAR_AZIMUTH_DEGREES_2=90.0
```

### 4. BMW & MINI (`BMW CarData` OAuth 2.0 PKCE)
1. Log in to the **My BMW** / **MINI** web portal -> **BMW CarData** -> **Create CarData Client**.
2. Enable the `cardata:api:read` scope and copy the generated **Client ID** (UUID) into `.env`:
   ```dotenv
   BMW_CARDATA_CLIENT_ID=<bmw-client-uuid>
   MINI_CARDATA_CLIENT_ID=<mini-client-uuid>
   ```
3. Run the interactive browser authorization once per account:
   ```bash
   uv run cheap-watts cardata-auth
   # Or authorize a single vehicle when accounts belong to different drivers:
   uv run cheap-watts cardata-auth -v bmw
   uv run cheap-watts cardata-auth -v mini
   ```
   `cheap-watts` automatically provisions/reuses the `cheap-watts` telematics container, discovers VINs, and stores `0600`-protected rotated refresh tokens in `data/cardata_tokens.json`.

### 5. SMTP Email Notifications
Configure any standard SMTP provider (for Gmail, create a 16-character **App Password** under Google Account Security):
```dotenv
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=your.account@example.com
SMTP_PASSWORD=your-app-password
SMTP_USE_TLS=true
SMTP_FROM_EMAIL=your.account@example.com
SMTP_TO_EMAIL=driver1@example.com, driver2@example.com
```

---

## 🛠️ CLI Commands

```bash
# Verify read-only connectivity across all configured integrations (or a single service):
uv run cheap-watts probe
uv run cheap-watts probe -s deye

# Check configuration readiness and last persisted state:
uv run cheap-watts status

# Run one full optimization cycle in safe simulation mode (no inverter writes, [DRY-RUN] emails):
uv run cheap-watts run --dry-run

# Run one live optimization cycle (applies Deye TOU updates when schedule changes):
uv run cheap-watts run
```

---

## ⚙️ Key Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PSTRYK_API_KEY` | *(required)* | Personal Pstryk API token (`Authorization: <token>`). |
| `DEYE_MODBUS_HOST` / `DEYE_LOGGER_SERIAL` | *(required)* | Local LAN IP and 10-digit SN of Deye / Solarman V5 Wi-Fi stick (`:8899`). |
| `BATTERY_CAPACITY_KWH` | `10.0` | Total usable home battery capacity in kWh. |
| `BATTERY_MIN_SOC` / `BATTERY_TARGET_SOC` | `20` / `95` | Discharge floor SoC (%) and max grid-charge ceiling SoC (%). |
| `BATTERY_MAX_CHARGE_KW` | `5.0` | Expected grid charging power (kW) used to size window duration. |
| `TYPICAL_DAILY_USAGE_KWH` | `12.0` | Typical 24h household consumption in kWh. |
| `USAGE_BUFFER_PCT` | `10.0` | Safety buffer (%) added to `TYPICAL_DAILY_USAGE_KWH` for unexpected loads. |
| `SOLAR_SUFFICIENT_KWH_THRESHOLD` | `12.0` | 24h PV kWh threshold at/above which Deye grid-charging is disabled. |
| `BMW_SOC_THRESHOLD` / `MINI_SOC_THRESHOLD` | `20` / `40` | EV SoC (%) thresholds triggering charging recommendations. |
| `EV_SLOW_CHARGE_AMPS` | `8` | Recommended slow-charge current (`6–16A`) during sunny PV windows. |
| `ALERT_COOLDOWN_HOURS` | `12` | Minimum hours before repeating an unchanged EV/error alert. |
| `DRY_RUN` | `false` | Set `true` (or pass `--dry-run`) to skip all Deye writes globally. |

---

## 🕒 Deployment (`cron` & `Docker`)

### Option A (Recommended): Mini PC + Docker + GHCR Auto-Update
Every merge to `main` runs all CI checks and publishes `ghcr.io/rsamborski/cheap-watts:latest` (and `:sha-<short_sha>`). The Mini PC needs no `git` checkout or Python toolchain:

1. **Bootstrap `/opt/cheap-watts` (`UID/GID 1000` matches the container `watts` user):**
   ```bash
   ./deploy/setup-minipc.sh
   # Creates /opt/cheap-watts/{data,logs} (1000:1000) and /opt/cheap-watts/.env (0600)
   ```
   Copy `.env.example` into `/opt/cheap-watts/.env` and fill in your credentials/settings.

2. **Pull or authenticate Docker to GHCR:**
   Public GHCR images can be pulled directly without authentication (`docker pull ghcr.io/rsamborski/cheap-watts:latest`). If you publish to a private package/fork, log in first with a `read:packages` Personal Access Token:
   ```bash
   docker login ghcr.io -u <github-username>
   docker pull ghcr.io/rsamborski/cheap-watts:latest
   ```

3. **Provision BMW CarData tokens:**
   Either copy an existing `data/cardata_tokens.json` (`chmod 600` and `chown 1000:1000`), or run interactive OAuth inside the container:
   ```bash
   docker run -it --rm \
     --env-file /opt/cheap-watts/.env \
     -v /opt/cheap-watts/data:/app/data \
     ghcr.io/rsamborski/cheap-watts:latest cardata-auth
   ```

4. **Verify connectivity & dry-run before enabling `cron` (`--network host` is required for local Solarman V5 `:8899`):**
   ```bash
   docker run --rm --network host \
     --env-file /opt/cheap-watts/.env \
     -v /opt/cheap-watts/data:/app/data \
     -v /opt/cheap-watts/logs:/app/logs \
     ghcr.io/rsamborski/cheap-watts:latest probe

   docker run --rm --network host \
     --env-file /opt/cheap-watts/.env \
     -v /opt/cheap-watts/data:/app/data \
     -v /opt/cheap-watts/logs:/app/logs \
     ghcr.io/rsamborski/cheap-watts:latest run --dry-run
   ```

5. **Install the Mini PC `crontab` (`crontab -e`, see [`deploy/crontab.example`](deploy/crontab.example)):**
   Schedule every **30 minutes** (`48 runs/day`, staying within BMW CarData's 50 requests/day quota per account), with image updates decoupled so a transient internet/GHCR outage never blocks local LAN inverter control:
   ```cron
   # Start with --dry-run during burn-in, then drop --dry-run to go live:
   */30 * * * * docker run --rm --network host --env-file /opt/cheap-watts/.env -v /opt/cheap-watts/data:/app/data -v /opt/cheap-watts/logs:/app/logs ghcr.io/rsamborski/cheap-watts:latest run >> /opt/cheap-watts/logs/cron.log 2>&1
   15 * * * * docker pull --quiet ghcr.io/rsamborski/cheap-watts:latest >> /opt/cheap-watts/logs/cron.log 2>&1
   0 4 * * 0 docker image prune -f >> /opt/cheap-watts/logs/cron.log 2>&1
   ```

### Option B: Local `uv` Checkout / Local `docker build`
For running directly from a cloned repo without GHCR:
```cron
# Native uv cron (burn-in with --dry-run first, then remove --dry-run):
*/30 * * * * cd /path/to/cheap-watts && uv run cheap-watts run --dry-run >> /path/to/cheap-watts/logs/cron.log 2>&1
```
Or build and run locally with Docker:
```bash
docker build -t cheap-watts:latest .
docker run --rm --network host \
  --env-file .env \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/logs:/app/logs" \
  cheap-watts:latest run --dry-run
```

---

## 🧪 Development & Quality Gates

Because `main` is branch-protected and publishes to GHCR on merge, develop every change on a descriptive branch (`feat/...`, `fix/...`, `chore/...`, `docs/...`), run all checks locally, and open a Pull Request targeting `main`:

```bash
uv run black src tests
uv run ruff check --fix src tests
uv run pytest --cov=src --cov-report=term-missing
uv run pre-commit run --all-files
```

---

## ⚠️ Hardware Safety Disclaimer & License

`cheap-watts` reads telemetry and writes Time-of-Use (TOU) Modbus holding registers (`146, 148..153, 166..177`) to grid-tied hybrid inverters. Before enabling live writes, verify Modbus register compatibility with your exact inverter firmware and validate schedules using `--dry-run`. Provided "AS IS" without warranty — you operate inverter control automation at your own risk.

Licensed under the [Apache License 2.0](LICENSE).
