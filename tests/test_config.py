"""Tests for configuration loading, blank env handling, and secret masking."""

from __future__ import annotations

from pathlib import Path

from src.config import Settings, load_settings


def test_env_example_loads_cleanly_with_blank_values() -> None:
    """Copying .env.example to .env with blank fields must not fail validation."""
    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    settings = load_settings(env_file=env_example)

    assert settings.pstryk_api_key is None
    assert settings.deye_app_secret is None
    assert settings.solar_latitude is None
    assert settings.bmw_soc_threshold == 20
    assert settings.mini_soc_threshold == 40
    assert settings.ev_max_charge_kw == 11.0
    assert settings.home_radius_meters == 200.0
    assert settings.max_missed_ev_renotifications == 3
    assert settings.dry_run is False
    assert settings.api_max_retries == 3
    assert settings.api_retry_backoff_seconds == 2.0
    assert "PSTRYK_API_KEY" in settings.missing_live_settings()


def test_secrets_are_redacted_in_repr(tmp_path: Path) -> None:
    """SecretStr fields must never appear in plain text in repr() or str()."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PSTRYK_API_KEY=test-api-key\n"
        "DEYE_APP_SECRET=test-api-key\n"
        "SMTP_PASSWORD=test-api-key\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)
    rendered = f"{settings!r} {settings}"

    assert "test-api-key" not in rendered
    assert settings.pstryk_api_key is not None
    assert settings.pstryk_api_key.get_secret_value() == "test-api-key"
