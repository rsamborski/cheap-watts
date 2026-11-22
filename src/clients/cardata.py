"""Read-only BMW CarData API client and OAuth 2.0 Device Code flow for BMW & MINI."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import math
import os
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

import httpx
from pydantic import BaseModel, SecretStr

from src.clients.errors import (
    ApiClientError,
    AuthenticationError,
    PayloadParseError,
    RateLimitError,
    parse_json_response,
)
from src.clients.retry import RetryPolicy, request_with_retry
from src.models import VehicleStatus

logger = logging.getLogger("cheap_watts.clients.cardata")

_MAPPINGS_PATH = "/customers/vehicles/mappings"
_CONTAINERS_PATH = "/customers/containers"
_TELEMATIC_PATH_TEMPLATE = "/customers/vehicles/{vin}/telematicData"

_DEVICE_CODE_SCOPE = "authenticate_user openid cardata:api:read"
_DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

_CONTAINER_NAME = "cheap-watts-v2"
_CONTAINER_VERSION = 2
_CONTAINER_PURPOSE = "EV battery SoC, location, and charging status for home energy optimization"
_DEFAULT_CONTAINER_DESCRIPTORS: tuple[str, ...] = (
    "vehicle.drivetrain.batteryManagement.header",
    "vehicle.drivetrain.electricEngine.charging.level",
    "vehicle.powertrain.electric.battery.stateOfCharge.target",
    "vehicle.drivetrain.electricEngine.charging.status",
    "vehicle.drivetrain.electricEngine.charging.timeRemaining",
    "vehicle.body.chargingPort.status",
    "vehicle.cabin.infotainment.navigation.currentLocation.latitude",
    "vehicle.cabin.infotainment.navigation.currentLocation.longitude",
)
_CORE_CONTAINER_DESCRIPTORS: tuple[str, ...] = (
    "vehicle.drivetrain.batteryManagement.header",
    "vehicle.drivetrain.electricEngine.charging.status",
    "vehicle.body.chargingPort.status",
)

_BRAND_VIN_PREFIXES: dict[str, tuple[str, ...]] = {
    "bmw": ("WBA", "WBS", "WBY", "WBX", "5UX", "5YM", "3MW"),
    "mini": ("WMW",),
}

_SOC_KEYS = (
    "vehicle.powertrain.electric.battery.stateOfCharge.displayed",
    "vehicle.powertrain.electric.battery.stateOfCharge",
    "vehicle.drivetrain.batteryManagement.header",
    "vehicle.drivetrain.electricEngine.charging.level",
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
_LATITUDE_KEYS = (
    "vehicle.cabin.infotainment.navigation.currentLocation.latitude",
    "vehicle.position.latitude",
    "latitude",
)
_LONGITUDE_KEYS = (
    "vehicle.cabin.infotainment.navigation.currentLocation.longitude",
    "vehicle.position.longitude",
    "longitude",
)


def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute great-circle Haversine distance in meters between two WGS84 points."""
    earth_radius_m = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lam = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    return earth_radius_m * c


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


def generate_pkce_pair() -> tuple[str, str]:
    """Generate an RFC 7636 PKCE (code_verifier, S256 code_challenge) pair."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


@dataclass(frozen=True)
class DeviceCodeChallenge:
    """OAuth 2.0 Device Authorization response paired with its PKCE verifier."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: int
    interval: int
    code_verifier: str


@dataclass(frozen=True)
class OAuthTokenBundle:
    """Tokens returned by BMW Group OAuth token endpoint."""

    access_token: SecretStr
    refresh_token: SecretStr | None
    expires_in: int


class CarDataTokenRecord(BaseModel):
    """Persisted OAuth tokens and discovered CarData metadata for one vehicle."""

    access_token: SecretStr
    refresh_token: SecretStr | None = None
    expires_at: datetime | None = None
    client_id: str | None = None
    vin: str | None = None
    container_id: str | None = None
    container_version: int = _CONTAINER_VERSION
    updated_at: datetime | None = None

    def has_valid_access_token(
        self,
        now: datetime | None = None,
        skew_seconds: int = 120,
    ) -> bool:
        """Return True if the cached access token is present and not near expiry."""
        token = self.access_token.get_secret_value().strip()
        if not token or self.expires_at is None:
            return False
        current = now or datetime.now(tz=UTC)
        return (current + timedelta(seconds=skew_seconds)) < self.expires_at


class CarDataTokenStore:
    """Atomic JSON store (`0600`) for rotated BMW/MINI CarData OAuth tokens."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load_all(self) -> dict[str, CarDataTokenRecord]:
        """Load all stored vehicle token records from disk."""
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        vehicles_raw = raw.get("vehicles", raw)
        if not isinstance(vehicles_raw, dict):
            return {}
        records: dict[str, CarDataTokenRecord] = {}
        for key, item in vehicles_raw.items():
            if isinstance(item, dict) and item.get("access_token"):
                normalized = dict(item)
                if "container_version" not in normalized and normalized.get("container_id"):
                    normalized["container_version"] = 1
                records[str(key)] = CarDataTokenRecord.model_validate(normalized)
        return records

    def get(self, vehicle_key: str) -> CarDataTokenRecord | None:
        """Return the stored token record for a vehicle key, if present."""
        return self.load_all().get(vehicle_key)

    def upsert(self, vehicle_key: str, record: CarDataTokenRecord) -> None:
        """Atomically insert or update a single vehicle's token record with mode 0600."""
        records = self.load_all()
        records[vehicle_key] = record
        self._save_all(records)

    def _save_all(self, records: dict[str, CarDataTokenRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        serialized_vehicles: dict[str, dict[str, Any]] = {}
        for key, rec in records.items():
            serialized_vehicles[key] = {
                "access_token": rec.access_token.get_secret_value(),
                "refresh_token": (
                    rec.refresh_token.get_secret_value() if rec.refresh_token is not None else None
                ),
                "expires_at": rec.expires_at.isoformat() if rec.expires_at else None,
                "client_id": rec.client_id,
                "vin": rec.vin,
                "container_id": rec.container_id,
                "container_version": rec.container_version,
                "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
            }
        payload = json.dumps({"vehicles": serialized_vehicles}, indent=2)
        tmp_path.write_text(f"{payload}\n", encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, self.path)


@dataclass(frozen=True)
class CarDataVehicleConfig:
    """Per-vehicle credentials and metadata for BMW Group CarData polling."""

    vehicle_key: Literal["bmw", "mini"]
    display_name: str
    client_id: str | None = None
    vin: str | None = None
    container_id: str | None = None
    access_token: SecretStr | None = None
    refresh_token: SecretStr | None = None

    @property
    def is_configured(self) -> bool:
        """Return True if inline access token or OAuth refresh credentials exist."""
        return self.access_token is not None or (
            bool(self.client_id) and self.refresh_token is not None
        )

    def has_credentials(self, token_store: CarDataTokenStore | None = None) -> bool:
        """Return True if inline credentials or matching stored OAuth tokens exist."""
        if self.is_configured:
            return True
        if not self.client_id or token_store is None:
            return False
        record = token_store.get(self.vehicle_key)
        if record is None:
            return False
        if record.client_id and record.client_id != self.client_id:
            return False
        return record.has_valid_access_token() or record.refresh_token is not None


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


def _parse_json_any(response: httpx.Response, service: str) -> dict[str, Any] | list[Any]:
    """Validate HTTP status code and decode a JSON object or array."""
    status = response.status_code
    if status in (401, 403):
        raise AuthenticationError(f"{service} authentication failed (HTTP {status})")
    if status == 429:
        raise RateLimitError(f"{service} rate limit exceeded (HTTP 429)")
    if status >= 400:
        raise ApiClientError(f"{service} request failed with HTTP {status}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise PayloadParseError(f"{service} returned invalid JSON") from exc

    if not isinstance(payload, (dict, list)):
        raise PayloadParseError(
            f"{service} expected JSON object or array, got {type(payload).__name__}"
        )
    return payload


class CarDataClient:
    """Read-only BMW Group CarData client supporting BMW and MINI vehicles."""

    def __init__(
        self,
        base_url: str = "https://api-cardata.bmwgroup.com",
        token_url: str = "https://customer.bmwgroup.com/gcdm/oauth/token",
        device_code_url: str = "https://customer.bmwgroup.com/gcdm/oauth/device/code",
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
        token_store: CarDataTokenStore | None = None,
        home_latitude: float | None = None,
        home_longitude: float | None = None,
        home_radius_meters: float = 200.0,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_url = token_url
        self._device_code_url = device_code_url
        self._token_store = token_store
        self._home_latitude = home_latitude
        self._home_longitude = home_longitude
        self._home_radius_meters = home_radius_meters
        self._token_cache: dict[str, str] = {}
        self._retry_policy = retry_policy or RetryPolicy()
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

    def request_device_code(self, client_id: str) -> DeviceCodeChallenge:
        """Initiate OAuth 2.0 Device Code + PKCE flow for BMW / MINI CarData."""
        verifier, challenge = generate_pkce_pair()
        try:
            response = request_with_retry(
                self._client,
                "POST",
                self._device_code_url,
                service="BMW CarData OAuth",
                policy=self._retry_policy,
                data={
                    "client_id": client_id,
                    "response_type": "device_code",
                    "scope": _DEVICE_CODE_SCOPE,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise ApiClientError(
                f"BMW CarData device code network error: {exc.__class__.__name__}"
            ) from exc

        payload = parse_json_response(response, service="BMW CarData OAuth device code")
        device_code = payload.get("device_code")
        user_code = payload.get("user_code")
        verification_uri = payload.get("verification_uri")
        if (
            not isinstance(device_code, str)
            or not device_code.strip()
            or not isinstance(user_code, str)
            or not user_code.strip()
            or not isinstance(verification_uri, str)
            or not verification_uri.strip()
        ):
            raise PayloadParseError(
                "BMW CarData device code response missing required verification fields"
            )

        uri_complete = payload.get("verification_uri_complete")
        expires_in = self._parse_optional_int(payload.get("expires_in"), 30, 86_400) or 900
        interval = self._parse_optional_int(payload.get("interval"), 1, 120) or 5

        return DeviceCodeChallenge(
            device_code=device_code.strip(),
            user_code=user_code.strip(),
            verification_uri=verification_uri.strip(),
            verification_uri_complete=(
                uri_complete.strip()
                if isinstance(uri_complete, str) and uri_complete.strip()
                else None
            ),
            expires_in=expires_in,
            interval=interval,
            code_verifier=verifier,
        )

    def poll_device_token(
        self,
        client_id: str,
        challenge: DeviceCodeChallenge,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> OAuthTokenBundle:
        """Poll BMW Group token endpoint until browser authorization completes."""
        started = monotonic_fn()
        interval = float(challenge.interval)

        while (monotonic_fn() - started) < challenge.expires_in:
            sleep_fn(interval)
            try:
                response = request_with_retry(
                    self._client,
                    "POST",
                    self._token_url,
                    service="BMW CarData OAuth",
                    policy=self._retry_policy,
                    data={
                        "client_id": client_id,
                        "device_code": challenge.device_code,
                        "grant_type": _DEVICE_CODE_GRANT_TYPE,
                        "code_verifier": challenge.code_verifier,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            except httpx.HTTPError as exc:
                raise ApiClientError(
                    f"BMW CarData token polling network error: {exc.__class__.__name__}"
                ) from exc

            if response.status_code == 200:
                payload = parse_json_response(response, service="BMW CarData OAuth token")
                return self._parse_oauth_bundle(payload, context="device code flow")

            if response.status_code in (400, 401, 403):
                err_code = self._extract_oauth_error_code(response)
                if err_code == "authorization_pending":
                    continue
                if err_code == "slow_down":
                    interval += 5.0
                    continue
                if err_code == "expired_token":
                    raise AuthenticationError("BMW CarData device code expired before approval")
                if err_code == "access_denied":
                    raise AuthenticationError("BMW CarData authorization was denied in browser")
                raise AuthenticationError(
                    f"BMW CarData OAuth authorization failed "
                    f"(HTTP {response.status_code}, error={err_code or 'unknown'})"
                )

            parse_json_response(response, service="BMW CarData OAuth token")

        raise AuthenticationError("Timed out waiting for BMW CarData browser approval")

    def ensure_container(
        self,
        token: str,
        descriptors: Sequence[str] = _DEFAULT_CONTAINER_DESCRIPTORS,
    ) -> str:
        """Reuse an active `cheap-watts` CarData container or create one."""
        list_url = f"{self._base_url}{_CONTAINERS_PATH}"
        headers = {
            "Authorization": f"Bearer {token}",
            "x-version": "v1",
        }
        try:
            list_resp = request_with_retry(
                self._client,
                "GET",
                list_url,
                service="BMW CarData",
                policy=self._retry_policy,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise ApiClientError(
                f"BMW CarData container lookup network error: {exc.__class__.__name__}"
            ) from exc

        parsed = _parse_json_any(list_resp, service="BMW CarData containers")
        containers = parsed.get("containers", []) if isinstance(parsed, dict) else parsed
        if isinstance(containers, list):
            for item in containers:
                if not isinstance(item, dict):
                    continue
                cid = item.get("containerId")
                name = str(item.get("name", "")).strip()
                state = str(item.get("state", "ACTIVE")).strip().upper()
                if (
                    name == _CONTAINER_NAME
                    and state == "ACTIVE"
                    and isinstance(cid, str)
                    and cid.strip()
                ):
                    return cid.strip()

        return self._create_container(token, descriptors=descriptors)

    def _create_container(self, token: str, descriptors: Sequence[str]) -> str:
        url = f"{self._base_url}{_CONTAINERS_PATH}"
        headers = {
            "Authorization": f"Bearer {token}",
            "x-version": "v1",
            "Content-Type": "application/json",
        }
        body = {
            "name": _CONTAINER_NAME,
            "purpose": _CONTAINER_PURPOSE,
            "technicalDescriptors": list(descriptors),
        }
        try:
            response = request_with_retry(
                self._client,
                "POST",
                url,
                service="BMW CarData",
                policy=self._retry_policy,
                headers=headers,
                json=body,
            )
        except httpx.HTTPError as exc:
            raise ApiClientError(
                f"BMW CarData container creation network error: {exc.__class__.__name__}"
            ) from exc

        if response.status_code == 400 and tuple(descriptors) != _CORE_CONTAINER_DESCRIPTORS:
            logger.warning(
                "Optional CarData descriptor rejected; retrying with core EV descriptors",
                extra={"event": "cardata_container_fallback"},
            )
            return self._create_container(token, descriptors=_CORE_CONTAINER_DESCRIPTORS)

        payload = parse_json_response(response, service="BMW CarData create container")
        cid = payload.get("containerId")
        if not isinstance(cid, str) or not cid.strip():
            raise PayloadParseError("BMW CarData create container response missing 'containerId'")
        return cid.strip()

    def discover_vin(
        self,
        token: str,
        vehicle_key: Literal["bmw", "mini"] | None = None,
    ) -> str:
        """Discover the mapped VIN, preferring brand-matching WMI prefixes when present."""
        url = f"{self._base_url}{_MAPPINGS_PATH}"
        try:
            response = request_with_retry(
                self._client,
                "GET",
                url,
                service="BMW CarData",
                policy=self._retry_policy,
                headers={
                    "Authorization": f"Bearer {token}",
                    "x-version": "v1",
                },
            )
        except httpx.HTTPError as exc:
            raise ApiClientError(
                f"BMW CarData VIN discovery network error: {exc.__class__.__name__}"
            ) from exc

        parsed = _parse_json_any(response, service="BMW CarData VIN mappings")
        raw_list = (
            parsed.get("mappings") or parsed.get("vehicles") if isinstance(parsed, dict) else parsed
        )
        if not isinstance(raw_list, list) or not raw_list:
            raise PayloadParseError("BMW CarData mappings response contained no vehicles")

        valid_entries = [
            item
            for item in raw_list
            if isinstance(item, dict) and isinstance(item.get("vin"), str) and item["vin"].strip()
        ]
        if not valid_entries:
            raise PayloadParseError("BMW CarData mappings entry did not include a valid 'vin'")

        if vehicle_key is not None:
            prefixes = _BRAND_VIN_PREFIXES.get(vehicle_key, ())
            brand_matches = [
                item for item in valid_entries if item["vin"].strip().upper().startswith(prefixes)
            ]
            if brand_matches:
                primary_brand = next(
                    (
                        item
                        for item in brand_matches
                        if str(item.get("mappingType", "")).upper() == "PRIMARY"
                    ),
                    brand_matches[0],
                )
                return str(primary_brand["vin"]).strip()

        primary = next(
            (
                item
                for item in valid_entries
                if str(item.get("mappingType", "")).upper() == "PRIMARY"
            ),
            valid_entries[0],
        )
        return str(primary["vin"]).strip()

    def fetch_vehicle_status(self, config: CarDataVehicleConfig) -> VehicleStatus:
        """Fetch SoC, plug status, and active charging flag for a single vehicle."""
        token = self._resolve_access_token(config)
        stored = self._load_compatible_record(config)

        vin = (
            config.vin.strip()
            if config.vin and config.vin.strip()
            else (
                stored.vin.strip()
                if stored and stored.vin and stored.vin.strip()
                else self.discover_vin(token, vehicle_key=config.vehicle_key)
            )
        )
        container_id = (
            config.container_id.strip()
            if config.container_id and config.container_id.strip()
            else (
                stored.container_id.strip()
                if stored and stored.container_id and stored.container_id.strip()
                else None
            )
        )
        if (
            config.container_id is None
            and stored is not None
            and stored.container_id is not None
            and stored.container_version < _CONTAINER_VERSION
        ):
            try:
                container_id = self.ensure_container(token)
                self._persist_metadata_if_applicable(
                    config,
                    vin=vin,
                    container_id=container_id,
                    container_version=_CONTAINER_VERSION,
                )
            except ApiClientError as exc:
                logger.warning(
                    "Failed to upgrade CarData container to v2; continuing with existing container",
                    extra={
                        "event": "cardata_container_upgrade_failed",
                        "vehicle": config.vehicle_key,
                        "error": str(exc),
                    },
                )

        response = self._get_telematic_response(
            token=token,
            vin=vin,
            container_id=container_id,
            vehicle_key=config.vehicle_key,
        )
        if response.status_code == 400 and container_id is None:
            container_id = self.ensure_container(token)
            self._persist_metadata_if_applicable(
                config,
                vin=vin,
                container_id=container_id,
                container_version=_CONTAINER_VERSION,
            )
            response = self._get_telematic_response(
                token=token,
                vin=vin,
                container_id=container_id,
                vehicle_key=config.vehicle_key,
            )

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

        lat = self._parse_optional_float(_first_present(data, _LATITUDE_KEYS), -90.0, 90.0)
        lon = self._parse_optional_float(_first_present(data, _LONGITUDE_KEYS), -180.0, 180.0)
        distance_from_home_m: float | None = None
        is_at_home: bool | None = None
        if (
            lat is not None
            and lon is not None
            and not (lat == 0.0 and lon == 0.0)
            and self._home_latitude is not None
            and self._home_longitude is not None
        ):
            distance_from_home_m = round(
                haversine_distance_m(self._home_latitude, self._home_longitude, lat, lon),
                1,
            )
            is_at_home = distance_from_home_m <= self._home_radius_meters

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
            distance_from_home_m=distance_from_home_m,
            is_at_home=is_at_home,
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
                "is_at_home": status.is_at_home,
                "distance_from_home_m": status.distance_from_home_m,
            },
        )
        return status

    def fetch_all(self, vehicles: Sequence[CarDataVehicleConfig]) -> list[VehicleStatus]:
        """Fetch telemetry for every configured vehicle in order."""
        results: list[VehicleStatus] = []
        for vehicle in vehicles:
            if not vehicle.has_credentials(self._token_store):
                logger.debug(
                    "Skipping unconfigured CarData vehicle",
                    extra={"event": "cardata_vehicle_skipped", "vehicle": vehicle.vehicle_key},
                )
                continue
            results.append(self.fetch_vehicle_status(vehicle))
        return results

    def _get_telematic_response(
        self,
        token: str,
        vin: str,
        container_id: str | None,
        vehicle_key: str,
    ) -> httpx.Response:
        url = f"{self._base_url}{_TELEMATIC_PATH_TEMPLATE.format(vin=vin)}"
        params = {"containerId": container_id} if container_id else None
        try:
            return request_with_retry(
                self._client,
                "GET",
                url,
                service=f"BMW CarData ({vehicle_key})",
                policy=self._retry_policy,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "x-version": "v1",
                },
            )
        except httpx.HTTPError as exc:
            raise ApiClientError(
                f"BMW CarData network error for {vehicle_key}: {exc.__class__.__name__}"
            ) from exc

    def _load_compatible_record(self, config: CarDataVehicleConfig) -> CarDataTokenRecord | None:
        if self._token_store is None or not config.client_id:
            return None
        record = self._token_store.get(config.vehicle_key)
        if record is None:
            return None
        if record.client_id and record.client_id != config.client_id:
            return None
        return record

    def _persist_metadata_if_applicable(
        self,
        config: CarDataVehicleConfig,
        vin: str,
        container_id: str,
        container_version: int = _CONTAINER_VERSION,
    ) -> None:
        record = self._load_compatible_record(config)
        if record is None or self._token_store is None:
            return
        updated = record.model_copy(
            update={
                "vin": vin,
                "container_id": container_id,
                "container_version": container_version,
                "updated_at": datetime.now(tz=UTC),
            }
        )
        self._token_store.upsert(config.vehicle_key, updated)

    def _resolve_access_token(self, config: CarDataVehicleConfig) -> str:
        cached = self._token_cache.get(config.vehicle_key)
        if cached:
            return cached

        if config.access_token is not None:
            token = config.access_token.get_secret_value().strip()
            if token:
                self._token_cache[config.vehicle_key] = token
                return token

        stored = self._load_compatible_record(config)
        if stored is not None and stored.has_valid_access_token():
            token = stored.access_token.get_secret_value().strip()
            self._token_cache[config.vehicle_key] = token
            return token

        refresh_secret = (
            stored.refresh_token
            if stored is not None and stored.refresh_token is not None
            else config.refresh_token
        )
        if not config.client_id or refresh_secret is None:
            raise AuthenticationError(
                f"Missing CarData credentials for {config.display_name}: run "
                "`uv run cheap-watts cardata-auth` or provide `access_token` / "
                "(`client_id` + `refresh_token`)"
            )

        try:
            response = request_with_retry(
                self._client,
                "POST",
                self._token_url,
                service=f"BMW CarData OAuth ({config.display_name})",
                policy=self._retry_policy,
                data={
                    "grant_type": "refresh_token",
                    "client_id": config.client_id,
                    "refresh_token": refresh_secret.get_secret_value(),
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
        bundle = self._parse_oauth_bundle(payload, context=config.display_name)
        resolved = bundle.access_token.get_secret_value()
        self._token_cache[config.vehicle_key] = resolved

        if self._token_store is not None:
            now = datetime.now(tz=UTC)
            new_refresh = (
                bundle.refresh_token if bundle.refresh_token is not None else refresh_secret
            )
            self._token_store.upsert(
                config.vehicle_key,
                CarDataTokenRecord(
                    access_token=bundle.access_token,
                    refresh_token=new_refresh,
                    expires_at=now + timedelta(seconds=bundle.expires_in),
                    client_id=config.client_id,
                    vin=config.vin or (stored.vin if stored else None),
                    container_id=config.container_id or (stored.container_id if stored else None),
                    container_version=(stored.container_version if stored else _CONTAINER_VERSION),
                    updated_at=now,
                ),
            )

        return resolved

    @classmethod
    def _parse_oauth_bundle(cls, payload: dict[str, Any], context: str) -> OAuthTokenBundle:
        token_val = payload.get("access_token") or payload.get("id_token")
        if not isinstance(token_val, str) or not token_val.strip():
            raise PayloadParseError(
                f"BMW CarData OAuth response for {context} missing 'access_token'"
            )
        raw_refresh = payload.get("refresh_token")
        refresh_secret = (
            SecretStr(raw_refresh.strip())
            if isinstance(raw_refresh, str) and raw_refresh.strip()
            else None
        )
        expires_in = cls._parse_optional_int(payload.get("expires_in"), 60, 86_400) or 3600
        return OAuthTokenBundle(
            access_token=SecretStr(token_val.strip()),
            refresh_token=refresh_secret,
            expires_in=expires_in,
        )

    @staticmethod
    def _extract_oauth_error_code(response: httpx.Response) -> str | None:
        try:
            body = response.json()
        except ValueError:
            return None
        if isinstance(body, dict) and isinstance(body.get("error"), str):
            return body["error"].strip().lower()
        return None

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

    @staticmethod
    def _parse_optional_float(raw: Any, min_val: float, max_val: float) -> float | None:
        if raw is None:
            return None
        try:
            parsed = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed):
            return None
        if min_val <= parsed <= max_val:
            return parsed
        return None
