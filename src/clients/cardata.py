"""Read-only BMW CarData API client for BMW and MINI EV telemetry."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Literal

import httpx
from pydantic import SecretStr

from src.clients.errors import (
    ApiClientError,
    AuthenticationError,
    PayloadParseError,
    parse_json_response,
)
from src.models import VehicleStatus

logger = logging.getLogger("pstryk_pilot.clients.cardata")

_MAPPINGS_PATH = "/v1/customers/vehicles/mappings"
_TELEMATIC_PATH_TEMPLATE = "/v1/customers/vehicles/{vin}/telematicData"

_SOC_KEYS = (
    "vehicle.powertrain.electric.battery.stateOfCharge.displayed",
    "vehicle.powertrain.electric.battery.stateOfCharge",
    "vehicle.drivetrain.batteryManagement.header",
    "soc",
)
_TARGET_SOC_KEYS = (
    "vehicle.powertrain.electric.battery.stateOfCharge.target",
    "vehicle.drivetrain.electricEngine.charging.chargingTarget",
)
_CHARGING_STATUS_KEYS = (
    "vehicle.drivetrain.electricEngine.charging.status",
    "vehicle.drivetrain.electricEngine.charging.hvStatus",
    "chargingStatus",
)
_PLUG_STATUS_KEYS = (
    "vehicle.body.chargingPort.status",
    "vehicle.drivetrain.electricEngine.charging.connectorStatus",
    "connectionStatus",
)
_TIME_REMAINING_KEYS = (
    "vehicle.drivetrain.electricEngine.charging.timeRemaining",
    "timeRemaining",
)

_ACTIVE_CHARGING_VALUES = frozenset(
    {
        "CHARGINGACTIVE",
        "CHARGING",
        "CHARGING_IN_PROGRESS",
        "AC_CHARGING",
        "DC_CHARGING",
    }
)
_CONNECTED_PLUG_VALUES = frozenset(
    {
        "CONNECTED",
        "PLUGGED",
        "PLUGGED_IN",
        "LOCKED",
    }
)


@dataclass(frozen=True)
class CarDataVehicleConfig:
    """Per-vehicle credentials and metadata for BMW Group CarData polling."""

    vehicle_key: Literal["bmw", "mini"]
    display_name: str
    client_id: str | None = None
    vin: str | None = None
    access_token: SecretStr | None = None
    refresh_token: SecretStr | None = None

    @property
    def is_configured(self) -> bool:
        """Return True if either a direct access token or OAuth refresh credentials exist."""
        return self.access_token is not None or (
            bool(self.client_id) and self.refresh_token is not None
        )


def _unwrap_descriptor_value(entry: Any) -> Any:
    if isinstance(entry, dict) and "value" in entry:
        return entry["value"]
    return entry


def _extract_telematic_map(payload: dict[str, Any]) -> dict[str, Any]:
    raw_container = payload.get("telematicData", payload)
    if isinstance(raw_container, dict):
        return {str(k): _unwrap_descriptor_value(v) for k, v in raw_container.items()}
    if isinstance(raw_container, list):
        mapped: dict[str, Any] = {}
        for item in raw_container:
            if isinstance(item, dict) and ("name" in item or "key" in item):
                descriptor = str(item.get("name") or item.get("key"))
                mapped[descriptor] = item.get("value")
        return mapped
    raise PayloadParseError("BMW CarData response missing valid 'telematicData' container")


def _first_present(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        val = data.get(key)
        if val is not None and val != "":
            return val
    return None


class CarDataClient:
    """Read-only BMW Group CarData client supporting BMW and MINI."""

    def __init__(
        self,
        base_url: str = "https://api-cardata.bmwgroup.com",
        token_url: str = "https://customer.bmwgroup.com/gcdm/oauth/token",
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_url = token_url
        self._token_cache: dict[str, str] = {}
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        """Close underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self) -> CarDataClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def fetch_vehicle_status(self, config: CarDataVehicleConfig) -> VehicleStatus:
        """Fetch SoC, plug status, and active charging flag for a single vehicle."""
        token = self._resolve_access_token(config)
        vin = config.vin.strip() if config.vin and config.vin.strip() else self._discover_vin(token)

        url = f"{self._base_url}{_TELEMATIC_PATH_TEMPLATE.format(vin=vin)}"
        try:
            response = self._client.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "x-version": "v1",
                },
            )
        except httpx.HTTPError as exc:
            raise ApiClientError(
                f"BMW CarData network error for {config.vehicle_key}: {exc.__class__.__name__}"
            ) from exc

        payload = parse_json_response(response, service=f"BMW CarData ({config.display_name})")
        data = _extract_telematic_map(payload)

        soc_pct = self._parse_required_soc(data, config.display_name)
        target_soc_pct = self._parse_optional_int(
            _first_present(data, _TARGET_SOC_KEYS),
            min_val=0,
            max_val=100,
        )
        time_remaining = self._parse_optional_int(
            _first_present(data, _TIME_REMAINING_KEYS),
            min_val=0,
            max_val=100_000,
        )

        raw_charging = _first_present(data, _CHARGING_STATUS_KEYS)
        charging_str = str(raw_charging).strip() if raw_charging is not None else None
        is_charging = (
            charging_str.upper() in _ACTIVE_CHARGING_VALUES if charging_str is not None else False
        )

        raw_plug = _first_present(data, _PLUG_STATUS_KEYS)
        plug_str = str(raw_plug).strip().upper() if raw_plug is not None else None
        is_plugged_in = is_charging or (
            plug_str in _CONNECTED_PLUG_VALUES if plug_str is not None else False
        )

        status = VehicleStatus(
            vehicle_key=config.vehicle_key,
            display_name=config.display_name,
            vin=vin,
            soc_pct=soc_pct,
            is_plugged_in=is_plugged_in,
            is_charging=is_charging,
            charging_status_raw=charging_str,
            target_soc_pct=target_soc_pct,
            time_remaining_minutes=time_remaining,
            fetched_at=datetime.now(tz=UTC),
        )
        logger.info(
            "Fetched CarData vehicle telemetry",
            extra={
                "event": "cardata_vehicle_fetched",
                "vehicle": config.vehicle_key,
                "soc_pct": status.soc_pct,
                "is_plugged_in": status.is_plugged_in,
                "is_charging": status.is_charging,
            },
        )
        return status

    def fetch_all(self, vehicles: Sequence[CarDataVehicleConfig]) -> list[VehicleStatus]:
        """Fetch telemetry for every configured vehicle in order."""
        results: list[VehicleStatus] = []
        for vehicle in vehicles:
            if not vehicle.is_configured:
                logger.debug(
                    "Skipping unconfigured CarData vehicle",
                    extra={"event": "cardata_vehicle_skipped", "vehicle": vehicle.vehicle_key},
                )
                continue
            results.append(self.fetch_vehicle_status(vehicle))
        return results

    def _resolve_access_token(self, config: CarDataVehicleConfig) -> str:
        cached = self._token_cache.get(config.vehicle_key)
        if cached:
            return cached

        if config.access_token is not None:
            token = config.access_token.get_secret_value().strip()
            if token:
                self._token_cache[config.vehicle_key] = token
                return token

        if not config.client_id or config.refresh_token is None:
            raise AuthenticationError(
                f"Missing CarData credentials for {config.display_name}: provide "
                "`access_token` or (`client_id` + `refresh_token`)"
            )

        try:
            response = self._client.post(
                self._token_url,
                data={
                    "grant_type": "refresh_token",
                    "client_id": config.client_id,
                    "refresh_token": config.refresh_token.get_secret_value(),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            err_name = exc.__class__.__name__
            raise ApiClientError(
                f"BMW CarData OAuth network error for {config.vehicle_key}: {err_name}"
            ) from exc

        payload = parse_json_response(
            response,
            service=f"BMW CarData OAuth ({config.display_name})",
        )
        token_val = payload.get("access_token") or payload.get("id_token")
        if not isinstance(token_val, str) or not token_val.strip():
            raise PayloadParseError(
                f"BMW CarData OAuth response for {config.display_name} missing 'access_token'"
            )

        resolved = token_val.strip()
        self._token_cache[config.vehicle_key] = resolved
        return resolved

    def _discover_vin(self, token: str) -> str:
        url = f"{self._base_url}{_MAPPINGS_PATH}"
        try:
            response = self._client.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "x-version": "v1",
                },
            )
        except httpx.HTTPError as exc:
            raise ApiClientError(
                f"BMW CarData VIN discovery network error: {exc.__class__.__name__}"
            ) from exc

        payload = parse_json_response(response, service="BMW CarData VIN mappings")
        mappings = payload.get("mappings") or payload.get("vehicles")
        if not isinstance(mappings, list) or not mappings:
            raise PayloadParseError("BMW CarData mappings response contained no vehicles")

        primary = next(
            (
                item
                for item in mappings
                if isinstance(item, dict)
                and str(item.get("mappingType", "")).upper() == "PRIMARY"
                and item.get("vin")
            ),
            mappings[0] if isinstance(mappings[0], dict) else None,
        )
        if not isinstance(primary, dict) or not isinstance(primary.get("vin"), str):
            raise PayloadParseError("BMW CarData mappings entry did not include a valid 'vin'")
        return primary["vin"].strip()

    @staticmethod
    def _parse_required_soc(data: dict[str, Any], display_name: str) -> int:
        raw_soc = _first_present(data, _SOC_KEYS)
        if raw_soc is None:
            raise PayloadParseError(f"BMW CarData telemetry for {display_name} missing SoC")
        try:
            soc = int(round(float(raw_soc)))
        except (TypeError, ValueError) as exc:
            raise PayloadParseError(
                f"Invalid BMW CarData SoC for {display_name}: {raw_soc!r}"
            ) from exc
        if not 0 <= soc <= 100:
            raise PayloadParseError(
                f"BMW CarData SoC out of [0, 100] range for {display_name}: {soc}"
            )
        return soc

    @staticmethod
    def _parse_optional_int(raw: Any, min_val: int, max_val: int) -> int | None:
        if raw is None:
            return None
        try:
            parsed = int(round(float(raw)))
        except (TypeError, ValueError):
            return None
        if min_val <= parsed <= max_val:
            return parsed
        return None
