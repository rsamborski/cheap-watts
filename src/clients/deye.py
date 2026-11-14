"""Deye Cloud / Solarman Open API read client behind an InverterClient protocol."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import SecretStr

from src.clients.errors import (
    ApiClientError,
    AuthenticationError,
    PayloadParseError,
    parse_json_response,
)
from src.models import InverterStatus, TouSlot

logger = logging.getLogger("pstryk_pilot.clients.deye")

_TOKEN_PATH = "/v1.0/account/token"
_DEVICE_LATEST_PATH = "/v1.0/device/latest"
_CONFIG_TOU_PATH = "/v1.0/config/tou"

_AUTH_ERROR_CODES = frozenset({"2101016", "2101017", "2101018", "2101019", "401", "403"})


@runtime_checkable
class InverterClient(Protocol):
    """Abstract interface for reading inverter telemetry and TOU schedule state."""

    def get_status(self) -> InverterStatus:
        """Fetch current battery SoC, PV/grid/load power, and configured TOU slots."""


def _parse_bool_flag(raw: Any, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return int(raw) != 0
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "on", "yes", "enable", "enabled"}:
            return True
        if lowered in {"0", "false", "off", "no", "disable", "disabled"}:
            return False
    raise PayloadParseError(f"Cannot interpret boolean flag from Deye payload: {raw!r}")


def _normalize_hhmm(raw: str) -> str:
    cleaned = raw.strip()
    if cleaned == "24:00":
        return "00:00"
    parts = cleaned.split(":")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    return cleaned


def _validate_deye_envelope(payload: dict[str, Any], endpoint: str) -> None:
    success = payload.get("success")
    code = str(payload.get("code", ""))
    msg = str(payload.get("msg") or payload.get("message") or "unknown error")

    if success is False or (success is None and code not in {"", "0", "1000000"}):
        if code in _AUTH_ERROR_CODES or "token" in msg.lower() or "auth" in msg.lower():
            raise AuthenticationError(
                f"Deye Cloud auth rejected on {endpoint} (code={code}): {msg}"
            )
        raise ApiClientError(f"Deye Cloud error on {endpoint} (code={code}): {msg}")


class DeyeCloudClient:
    """Read-only Deye Cloud Open API v1.0 client implementing `InverterClient`."""

    def __init__(
        self,
        device_sn: str,
        base_url: str = "https://eu1-developer.deyecloud.com",
        access_token: SecretStr | None = None,
        app_id: str | None = None,
        app_secret: SecretStr | None = None,
        email: str | None = None,
        password: SecretStr | None = None,
        password_is_sha256: bool = False,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not device_sn.strip():
            raise ValueError("device_sn must be non-empty")
        self._device_sn = device_sn.strip()
        self._cached_token: str | None = (
            access_token.get_secret_value() if access_token is not None else None
        )
        self._app_id = app_id
        self._app_secret = app_secret
        self._email = email
        self._password = password
        self._password_is_sha256 = password_is_sha256
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        """Close underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self) -> DeyeCloudClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def authenticate(self) -> str:
        """Obtain and cache a Bearer token via `/v1.0/account/token` if not already cached."""
        if self._cached_token:
            return self._cached_token

        if (
            not self._app_id
            or self._app_secret is None
            or not self._email
            or self._password is None
        ):
            raise AuthenticationError(
                "DeyeCloudClient requires either `access_token` or "
                "(`app_id`, `app_secret`, `email`, `password`)"
            )

        raw_pw = self._password.get_secret_value()
        pw_payload = (
            raw_pw
            if self._password_is_sha256
            else hashlib.sha256(raw_pw.encode("utf-8")).hexdigest()
        )

        try:
            response = self._client.post(
                _TOKEN_PATH,
                params={"appId": self._app_id},
                json={
                    "appSecret": self._app_secret.get_secret_value(),
                    "email": self._email,
                    "password": pw_payload,
                },
            )
        except httpx.HTTPError as exc:
            raise ApiClientError(
                f"Deye Cloud token network error: {exc.__class__.__name__}"
            ) from exc

        payload = parse_json_response(response, service="Deye Cloud")
        _validate_deye_envelope(payload, _TOKEN_PATH)

        data = payload.get("data")
        token: Any = payload.get("accessToken") or payload.get("access_token")
        if token is None and isinstance(data, dict):
            token = data.get("accessToken") or data.get("access_token")

        if not isinstance(token, str) or not token.strip():
            raise PayloadParseError("Deye Cloud token response missing 'accessToken'")

        self._cached_token = token.strip()
        logger.debug(
            "Obtained Deye Cloud access token",
            extra={"event": "deye_token_obtained", "device_sn": self._device_sn},
        )
        return self._cached_token

    def get_status(self) -> InverterStatus:
        """Fetch live telemetry and configured TOU slots for the configured Deye inverter."""
        token = self.authenticate()
        latest_payload = self._post_authenticated(
            _DEVICE_LATEST_PATH,
            token=token,
            body={"deviceList": [self._device_sn]},
        )
        tou_payload = self._post_authenticated(
            _CONFIG_TOU_PATH,
            token=token,
            body={"deviceSn": self._device_sn},
        )

        metrics = self._extract_device_metrics(latest_payload)
        battery_soc = self._parse_required_soc(metrics)
        pv_power_w = max(
            0.0,
            self._lookup_float(
                metrics,
                ("totaldcinputpower", "dcinputpower", "pvpower"),
                default=self._sum_pv_strings(metrics),
            ),
        )
        battery_power_w = self._lookup_float(
            metrics,
            ("batterypower", "bms_power", "batpower"),
            default=0.0,
        )
        grid_power_w = self._lookup_float(
            metrics,
            ("totalgridpower", "gridpower", "pgrid"),
            default=0.0,
        )
        load_power_w = max(
            0.0,
            self._lookup_float(
                metrics,
                ("totalloadpower", "loadpower", "consumptionpower"),
                default=0.0,
            ),
        )

        tou_enabled, tou_slots = self._parse_tou_config(tou_payload)
        status = InverterStatus(
            device_sn=self._device_sn,
            battery_soc=battery_soc,
            pv_power_w=pv_power_w,
            battery_power_w=battery_power_w,
            grid_power_w=grid_power_w,
            load_power_w=load_power_w,
            tou_enabled=tou_enabled,
            tou_slots=tou_slots,
            fetched_at=datetime.now(tz=UTC),
        )
        logger.info(
            "Fetched Deye inverter status",
            extra={
                "event": "deye_status_fetched",
                "device_sn": self._device_sn,
                "battery_soc": status.battery_soc,
                "pv_power_w": status.pv_power_w,
                "tou_enabled": status.tou_enabled,
                "tou_slot_count": len(status.tou_slots),
            },
        )
        return status

    def _post_authenticated(
        self,
        path: str,
        token: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            response = self._client.post(
                path,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise ApiClientError(
                f"Deye Cloud network error on {path}: {exc.__class__.__name__}"
            ) from exc

        payload = parse_json_response(response, service="Deye Cloud")
        _validate_deye_envelope(payload, path)
        return payload

    def _extract_device_metrics(self, payload: dict[str, Any]) -> dict[str, Any]:
        device_list = payload.get("deviceDataList")
        if device_list is None and isinstance(payload.get("data"), dict):
            device_list = payload["data"].get("deviceDataList")

        data_items: Any = None
        if isinstance(device_list, list) and device_list:
            matched_entry = next(
                (
                    item
                    for item in device_list
                    if isinstance(item, dict) and str(item.get("deviceSn")) == self._device_sn
                ),
                device_list[0] if isinstance(device_list[0], dict) else None,
            )
            if isinstance(matched_entry, dict):
                data_items = matched_entry.get("dataList")
        elif isinstance(payload.get("dataList"), list):
            data_items = payload["dataList"]

        if not isinstance(data_items, list):
            raise PayloadParseError("Deye device/latest response missing 'dataList'")

        metrics: dict[str, Any] = {}
        for entry in data_items:
            if isinstance(entry, dict) and "key" in entry:
                key_str = str(entry["key"]).strip().lower()
                metrics[key_str] = entry.get("value")
        return metrics

    @staticmethod
    def _parse_required_soc(metrics: dict[str, Any]) -> int:
        for key in ("soc", "bms_soc", "batterysoc", "b_soc"):
            raw = metrics.get(key)
            if raw is not None:
                try:
                    soc = int(round(float(raw)))
                except (TypeError, ValueError) as exc:
                    raise PayloadParseError(f"Invalid Deye battery SoC value: {raw!r}") from exc
                if not 0 <= soc <= 100:
                    raise PayloadParseError(f"Deye battery SoC out of [0, 100] range: {soc}")
                return soc
        raise PayloadParseError("Deye telemetry did not include battery SoC ('SOC')")

    @staticmethod
    def _lookup_float(
        metrics: dict[str, Any],
        candidate_keys: tuple[str, ...],
        default: float,
    ) -> float:
        for key in candidate_keys:
            raw = metrics.get(key)
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError) as exc:
                    raise PayloadParseError(
                        f"Invalid numeric value for Deye {key!r}: {raw!r}"
                    ) from exc
        return default

    @staticmethod
    def _sum_pv_strings(metrics: dict[str, Any]) -> float:
        string_keys = (
            "pv1power",
            "pv2power",
            "pv3power",
            "pv4power",
            "dc power pv1",
            "dc power pv2",
        )
        values: list[float] = []
        for key in string_keys:
            raw = metrics.get(key)
            if raw is not None:
                try:
                    values.append(float(raw))
                except (TypeError, ValueError):
                    continue
        return sum(values) if values else 0.0

    @staticmethod
    def _parse_tou_config(payload: dict[str, Any]) -> tuple[bool, list[TouSlot]]:
        container: dict[str, Any] = (
            payload["data"] if isinstance(payload.get("data"), dict) else payload
        )
        raw_action = container.get("touAction")
        if raw_action is None:
            raw_action = container.get("touSwitch", container.get("enable", True))
        tou_enabled = _parse_bool_flag(raw_action, default=True)

        raw_items = container.get("timeUseSettingItems")
        if raw_items is None:
            raw_items = container.get("touSlots", [])
        if not isinstance(raw_items, list):
            raise PayloadParseError("Deye TOU response 'timeUseSettingItems' is not a list")

        slots: list[TouSlot] = []
        for idx, item in enumerate(raw_items[:6], start=1):
            if not isinstance(item, dict):
                raise PayloadParseError(f"Deye TOU slot #{idx} is not an object")

            time_range = item.get("time")
            if isinstance(time_range, str) and "-" in time_range:
                raw_start, raw_end = time_range.split("-", maxsplit=1)
            else:
                raw_start = str(item.get("startTime") or "")
                raw_end = str(item.get("endTime") or "")

            if not raw_start or not raw_end:
                raise PayloadParseError(f"Deye TOU slot #{idx} missing start/end time: {item!r}")

            raw_soc = item.get("val")
            if raw_soc is None:
                raw_soc = item.get("soc", item.get("targetSoc"))
            if raw_soc is None:
                raise PayloadParseError(f"Deye TOU slot #{idx} missing target SoC ('val')")

            try:
                target_soc = int(round(float(raw_soc)))
            except (TypeError, ValueError) as exc:
                raise PayloadParseError(f"Invalid Deye TOU slot #{idx} SoC: {raw_soc!r}") from exc

            grid_charge = _parse_bool_flag(
                item.get("gridCharge", item.get("enableGridCharge")),
                default=False,
            )
            sell = _parse_bool_flag(item.get("sell", item.get("enableSell")), default=False)

            slots.append(
                TouSlot(
                    slot_index=int(item.get("slotIndex", idx)),
                    start_time=_normalize_hhmm(raw_start),
                    end_time=_normalize_hhmm(raw_end),
                    target_soc=target_soc,
                    grid_charge=grid_charge,
                    sell=sell,
                )
            )

        slots.sort(key=lambda slot: slot.slot_index)
        return tou_enabled, slots
