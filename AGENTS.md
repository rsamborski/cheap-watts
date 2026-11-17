# 🤖 `cheap-watts` — Agent & Developer Guidelines

This file defines mandatory development conventions and quality gates for contributors and AI coding agents working on `cheap-watts`.

---

## 1. Local Development Environment: WSL + `uv`
- **Native Linux / WSL Environment:** Execute all shell commands (`uv`, `pytest`, `ruff`, `black`, `git`, CLI runs) directly from the repository root inside a native Linux/WSL/macOS shell without `wsl` wrappers or `/mnt/c/` paths.
- **Package & Python Management:** Use `uv` exclusively (Python `>=3.12`, pinned in `.python-version` and `uv.lock`). Do not use global `pip` or Windows-side virtual environments.
  - Install / sync dependencies: `uv sync`
  - Run the CLI locally: `uv run cheap-watts run --dry-run`

---

## 2. Testing & Coverage Target (`> 90%`)
- **Minimum Coverage Gate:** Maintain **> 90% test coverage** across `src/` (`fail_under = 90` is enforced in `pyproject.toml`).
- **Offline & Deterministic Tests:** Unit tests must never make live network calls to Pstryk, Deye Cloud, BMW CarData, Open-Meteo, or SMTP servers. Use local JSON fixtures (`tests/fixtures/`) and mocked transports.
- **Run test suite with coverage:**
  ```bash
  uv run pytest --cov=src --cov-report=term-missing
  ```

---

## 3. Mandatory Linting & Formatting After Every Code Change
After finishing **major** code changes (source or tests), always run both the formatter (`black`) and linter (`ruff`) and resolve all issues before completing the task:

```bash
uv run black src tests
uv run ruff check --fix src tests
uv run black --check src tests
```

Any changes that are smaller or iterative are still going to be checked by:

- **Pre-commit & CI Gates:** Local pre-commit hooks (`.pre-commit-config.yaml`) and GitHub Actions (`.github/workflows/`) enforce `gitleaks`, `ruff`, `black --check`, and `pytest --cov=src` (`> 90%`) on every commit and PR targeting `main`:
  ```bash
  uv run pre-commit install
  uv run pre-commit run --all-files
  ```

---

## 4. Security, Secrets & Safety Guardrails
- **Secrets Hygiene:** Never commit `.env` or real API keys/tokens. Keep secret values **blank** in `.env.example` (per `.gitleaks.toml` / pre-commit `gitleaks`). In tests, use allowlisted dummy values such as `test-api-key`. Wrap secret fields in Pydantic `SecretStr` so credentials never leak into logs.
- **Dry-Run Invariant:** Any code path that mutates inverter settings (Deye Cloud / Solarman TOU writes) **must** check `dry_run` and strictly skip remote writes when `dry_run=True`.
- **Project Tracking:** Keep `docs/planning/tasks.md` updated as tasks and phases are completed, and record significant architectural changes in `docs/planning/`.

---

## 5. Git Branching & Pull Request Workflow
- **Protected `main` Branch:** `main` is protected on GitHub — never commit or push directly to `main`.
- **Branch per Change:** Create a descriptive branch off up-to-date `main` for every feature, bug fix, refactor, or documentation update (e.g., `feat/<topic>`, `fix/<topic>`, `chore/<topic>`, `docs/<topic>`).
- **Pre-PR Verification:** Run `uv run black src tests`, `uv run ruff check --fix src tests`, `uv run pytest --cov=src --cov-report=term-missing`, and `uv run pre-commit run --all-files` locally before pushing your branch and opening a Pull Request targeting `main`.
- **Automated GHCR Publishing:** Merging an approved Pull Request into `main` (after all CI status checks pass) automatically builds and publishes `ghcr.io/rsamborski/cheap-watts:latest` and `:sha-<short_sha>` via `.github/workflows/docker-publish.yml`.
