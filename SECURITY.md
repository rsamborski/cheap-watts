# Security Policy

## Supported Versions

Security updates are applied to the latest release on `main`:

| Version | Supported |
| :--- | :--- |
| `0.1.x` (`main`) | Yes |

## Reporting a Vulnerability

Please **do not** report security vulnerabilities through public GitHub issues.

Instead, report vulnerabilities privately via GitHub Security Advisories:
- [Open a private security advisory](https://github.com/rsamborski/cheap-watts/security/advisories/new)

Please include a clear description of the issue, steps to reproduce, and potential impact. You can expect an acknowledgment within a few days and coordinated disclosure once a fix is ready.

## Credential & Token Storage

- Runtime secrets (`PSTRYK_API_KEY`, Deye Cloud credentials, `SMTP_PASSWORD`) are loaded from `.env` (never committed to git or baked into Docker images) and wrapped in Pydantic `SecretStr` to prevent accidental exposure in logs or tracebacks.
- Rotated BMW CarData OAuth tokens are stored in `data/cardata_tokens.json` with `0600` file permissions. Keep `data/` and `.env` restricted (`0700` / `0600`) on the host system.

## Inverter Hardware Safety

`cheap-watts` writes Time-of-Use (TOU) Modbus holding registers (`146, 148..153, 166..177`) directly to grid-tied hybrid inverters over local Solarman V5 TCP (`:8899`). Always verify register compatibility with your inverter model/firmware and validate schedule behavior using `--dry-run` before enabling live writes.
