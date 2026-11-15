"""Application configuration loaded from environment variables and .env."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed configuration for pstryk-pilot."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Pstryk API ---
    pstryk_api_key: SecretStr | None = None
    pstryk_base_url: str = "https://api.pstryk.pl"

    # --- Deye Inverter / Storage ---
    deye_modbus_host: str | None = None
    deye_modbus_port: int = 8899
    deye_logger_serial: str | None = None
    deye_app_id: str | None = None
    deye_app_secret: SecretStr | None = None
    deye_email: str | None = None
    deye_password: SecretStr | None = None
    deye_access_token: SecretStr | None = None
    deye_api_base_url: str = "https://eu1-developer.deyecloud.com"
    deye_device_sn: str | None = None
    deye_station_id: str | None = None

    # --- Deye Home Battery Parameters ---
    battery_capacity_kwh: float = Field(default=10.0, gt=0)
    battery_target_soc: int = Field(default=95, ge=10, le=100)
    battery_min_soc: int = Field(default=20, ge=5, le=90)
    battery_max_charge_kw: float = Field(default=5.0, gt=0)

    # --- BMW CarData (BMW & MINI) ---
    cardata_base_url: str = "https://api-cardata.bmwgroup.com"
    cardata_token_url: str = "https://customer.bmwgroup.com/gcdm/oauth/token"
    bmw_cardata_client_id: str | None = None
    bmw_vin: str | None = None
    bmw_cardata_access_token: SecretStr | None = None
    bmw_cardata_refresh_token: SecretStr | None = None
    mini_cardata_client_id: str | None = None
    mini_vin: str | None = None
    mini_cardata_access_token: SecretStr | None = None
    mini_cardata_refresh_token: SecretStr | None = None
    bmw_soc_threshold: int = Field(default=20, ge=5, le=95)
    mini_soc_threshold: int = Field(default=40, ge=5, le=95)
    ev_slow_charge_amps: int = Field(default=8, ge=6, le=16)

    # --- Solar / PV Forecast ---
    solar_base_url: str = "https://api.open-meteo.com"
    solar_latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    solar_longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    solar_kwp: float | None = Field(default=None, gt=0)
    solar_tilt_degrees: float = Field(default=35.0, ge=0.0, le=90.0)
    solar_azimuth_degrees: float = Field(default=0.0, ge=-180.0, le=180.0)
    solar_kwp_2: float | None = Field(default=None, gt=0)
    solar_tilt_degrees_2: float | None = Field(default=None, ge=0.0, le=90.0)
    solar_azimuth_degrees_2: float | None = Field(default=None, ge=-180.0, le=180.0)
    solar_system_efficiency: float = Field(default=0.85, gt=0.0, le=1.0)
    solar_sufficient_kwh_threshold: float = Field(default=12.0, ge=0.0)

    # --- Email Notifications (SMTP) ---
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_use_tls: bool = True
    smtp_from_email: str | None = None
    smtp_to_email: str | None = None

    # --- Runtime, Logging & State ---
    dry_run: bool = False
    timezone: str = "Europe/Warsaw"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_dir: Path = Path("logs")
    state_file: Path = Path("data/state.json")
    alert_cooldown_hours: float = Field(default=12.0, ge=0.0)

    @field_validator(
        "pstryk_api_key",
        "deye_modbus_host",
        "deye_logger_serial",
        "deye_app_id",
        "deye_app_secret",
        "deye_email",
        "deye_password",
        "deye_access_token",
        "deye_device_sn",
        "deye_station_id",
        "bmw_cardata_client_id",
        "bmw_vin",
        "bmw_cardata_access_token",
        "bmw_cardata_refresh_token",
        "mini_cardata_client_id",
        "mini_vin",
        "mini_cardata_access_token",
        "mini_cardata_refresh_token",
        "solar_latitude",
        "solar_longitude",
        "solar_kwp",
        "solar_kwp_2",
        "solar_tilt_degrees_2",
        "solar_azimuth_degrees_2",
        "smtp_host",
        "smtp_username",
        "smtp_password",
        "smtp_from_email",
        "smtp_to_email",
        mode="before",
    )
    @classmethod
    def _empty_string_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    def missing_live_settings(self) -> list[str]:
        """Return names of required settings that are not yet configured for live execution."""
        missing: list[str] = []
        if self.pstryk_api_key is None:
            missing.append("PSTRYK_API_KEY")

        has_local_deye = bool(self.deye_modbus_host and self.deye_logger_serial)
        has_cloud_deye = bool(
            self.deye_device_sn
            and (
                self.deye_access_token is not None
                or (
                    self.deye_app_id
                    and self.deye_app_secret is not None
                    and self.deye_email
                    and self.deye_password is not None
                )
            )
        )
        if not has_local_deye and not has_cloud_deye:
            if not self.deye_modbus_host:
                missing.append("DEYE_MODBUS_HOST")
            if not self.deye_logger_serial:
                missing.append("DEYE_LOGGER_SERIAL")

        for env_name, val in (
            ("BMW_CARDATA_CLIENT_ID", self.bmw_cardata_client_id),
            ("MINI_CARDATA_CLIENT_ID", self.mini_cardata_client_id),
            ("SOLAR_LATITUDE", self.solar_latitude),
            ("SOLAR_LONGITUDE", self.solar_longitude),
            ("SOLAR_KWP", self.solar_kwp),
            ("SMTP_HOST", self.smtp_host),
            ("SMTP_FROM_EMAIL", self.smtp_from_email),
            ("SMTP_TO_EMAIL", self.smtp_to_email),
        ):
            if val is None:
                missing.append(env_name)
        return missing


def load_settings(env_file: str | Path | None = ".env") -> Settings:
    """Load application settings, optionally overriding the .env file path."""
    if env_file is None:
        return Settings(_env_file=None)
    return Settings(_env_file=env_file)
