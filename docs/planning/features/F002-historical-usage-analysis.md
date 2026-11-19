# F002: Historical Usage Analysis

- **Feature ID:** `F002`
- **Status:** Planned (Post-v1)
- **Target:** `src/clients/deye.py`, `src/main.py`, `src/state.py`, `src/engine/planner.py`, `deploy/crontab.example`

---

## Goal

Replace or refine the static `TYPICAL_DAILY_USAGE_KWH` setting (currently used both for total 24h demand and for a constant `TYPICAL_DAILY_USAGE_KWH / 24` hourly discharge projection until the scheduled grid-charging window) by running a daily analysis job that reads historical household consumption from the Deye API, learns typical daily/hourly usage patterns, and feeds that into the grid-charging planner with a configurable 10–20% safety buffer.

---

## Requirements

1. **Daily CLI Mode:**
   - Add a dedicated subcommand/mode on the same CLI (e.g., `cheap-watts analyze-usage`) meant to be triggered once daily via `cron`.
2. **Historical Consumption Retrieval:**
   - Investigate Deye Cloud Open API / Solarman Open API endpoints for historical daily/hourly load consumption (since local Solarman V5 `:8899` only exposes live/cumulative counters unless sampled continuously).
   - Fetch the past $N$ days (e.g., 7–14 days) of real electrical usage.
3. **Pattern Learning & Buffering:**
   - Compute typical daily consumption (e.g., rolling median/mean or weekday/weekend-aware profile).
   - Apply a configurable `10–20%` buffer (`USAGE_BUFFER_PCT`) so the battery charges only as much as needed to cover expected daily usage.
4. **Planner Integration & Fallback:**
   - Persist the learned usage profile in `state.py` (`data/state.json`).
   - Use the learned profile in `planner.py` when available and fresh; fall back cleanly to static `TYPICAL_DAILY_USAGE_KWH` if historical data is missing or stale.

---

## Implementation Checklist

- [ ] Investigate Deye Cloud / Solarman Open API endpoints for fetching historical daily/hourly electrical consumption.
- [ ] Implement historical usage fetch method in `src/clients/deye.py` with offline JSON fixtures.
- [ ] Add `cheap-watts analyze-usage` CLI subcommand in `src/main.py` and document daily `cron` schedule in `deploy/crontab.example`.
- [ ] Persist learned usage profile in `src/state.py` and integrate into `src/engine/planner.py` with configurable 10–20% buffer and static fallback.
- [ ] Add unit tests covering historical payload parsing, pattern calculation, buffer math, and fallback behavior.
