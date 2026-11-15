"""Deye inverter clients (Local Solarman V5 TCP & Deye Cloud API) behind InverterClient."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import SecretStr
from pysolarmanv5 import NoSocketAvailableError, PySolarmanV5, V5FrameError

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
_CONFIG_TOU_UPDATE_PATH = "/v1.0/config/tou/update"

_AUTH_ERROR_CODES = frozenset({"2101016", "2101017", "2101018", "2101019", "401", "403"})


@runtime_checkable
class InverterClient(Protocol):
    """Abstract interface for reading inverter telemetry and mutating TOU schedule state."""

    def get_status(self) -> InverterStatus:
        """Fetch current battery SoC, PV/grid/load power, and configured TOU slots."""

    def write_tou_schedule(
        self,
        slots: Sequence[TouSlot],
        *,
        dry_run: bool,
        enable_tou: bool = True,
    ) -> bool:
        """Apply a 6-slot TOU schedule, strictly skipping remote writes when `dry_run=True`."""


def _validate_six_tou_slots(slots: Sequence[TouSlot]) -> list[TouSlot]:
    """Validate and return a contiguous 6-slot Deye TOU schedule ordered 1..6."""
    if len(slots) != 6:
        raise ValueError(f"Deye TOU write requires exactly 6 slots, got {len(slots)}")
    ordered = sorted(slots, key=lambda s: s.slot_index)
    if [s.slot_index for s in ordered] != [1, 2, 3, 4, 5, 6]:
        raise ValueError("Deye TOU slots must have unique slot_index values 1..6")
    for idx in range(6):
        expected_next = ordered[(idx + 1) % 6].start_time
        if ordered[idx].end_time != expected_next:
            raise ValueError(
                f"Non-contiguous TOU boundary at slot #{idx + 1}: "
                f"end_time={ordered[idx].end_time} != next start_time={expected_next}"
            )
    return ordered


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

    def write_tou_schedule(
        self,
        slots: Sequence[TouSlot],
        *,
        dry_run: bool,
        enable_tou: bool = True,
    ) -> bool:
        """Write 6-slot TOU configuration via Deye Cloud Open API (skipped when `dry_run=True`)."""
        ordered = _validate_six_tou_slots(slots)
        if dry_run:
            logger.info(
                "Skipping Deye Cloud TOU write (dry_run=True)",
                extra={"event": "deye_cloud_write_skipped_dry_run", "device_sn": self._device_sn},
            )
            return False

        token = self.authenticate()
        body: dict[str, Any] = {
            "deviceSn": self._device_sn,
            "timeUseSetting": enable_tou,
            "timeUseSettingItems": [
                {
                    "slotIndex": s.slot_index,
                    "startTime": s.start_time,
                    "endTime": s.end_time,
                    "soc": s.target_soc,
                    "enableGridCharge": s.grid_charge,
                    "enableSell": s.sell,
                }
                for s in ordered
            ],
        }
        self._post_authenticated(_CONFIG_TOU_UPDATE_PATH, token=token, body=body)
        logger.info(
            "Updated Deye TOU schedule via Deye Cloud",
            extra={
                "event": "deye_cloud_tou_written",
                "device_sn": self._device_sn,
                "enable_tou": enable_tou,
            },
        )
        return True

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


# Deye 3-Phase Low-Voltage Hybrid (SUN-5/6/8/10/12K-SG04LP3-EU) Modbus Holding Registers
_REG_TOU_BLOCK_START = 146
_REG_TOU_BLOCK_LEN = 32  # 146..177 inclusive
_REG_TOU_ENABLE = 146  # bit 0 = global TOU enable, bits 1..7 = Mon..Sun
_REG_TOU_TIME_1 = 148  # 148..153 = Slot 1..6 start HHMM (e.g. 400 -> "04:00")
_REG_TOU_SOC_1 = 166  # 166..171 = Slot 1..6 target battery SoC (%)
_REG_TOU_CHARGE_1 = 172  # 172..177 = Slot 1..6 charge flags (bit 0 = grid charge)

_REG_TELEMETRY_START = 588
_REG_TELEMETRY_LEN = 86  # 588..673 inclusive
_REG_BATTERY_SOC = 588
_REG_BATTERY_POWER = 590  # signed int16 (W)
_REG_TOTAL_GRID_POWER = 625  # signed int16 (W)
_REG_TOTAL_LOAD_POWER = 653  # signed int16 (W)
_REG_PV1_POWER = 672  # uint16 (W)
_REG_PV2_POWER = 673  # uint16 (W)


def _to_int16(raw: int) -> int:
    """Interpret an unsigned 16-bit Modbus register value as signed two's-complement int16."""
    masked = int(raw) & 0xFFFF
    return masked - 0x10000 if masked >= 0x8000 else masked


def _decode_modbus_hhmm(raw: int, slot_index: int) -> str:
    """Decode Deye Modbus HHMM decimal integer (e.g. `430` -> `'04:30'`) into `HH:MM`."""
    val = int(raw)
    if val == 2400:
        return "00:00"
    hh, mm = divmod(val, 100)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise PayloadParseError(
            f"Invalid Deye Modbus HHMM register value for TOU slot #{slot_index}: {raw!r}"
        )
    return f"{hh:02d}:{mm:02d}"


def _encode_modbus_hhmm(hhmm: str) -> int:
    """Encode `HH:MM` (e.g. `'04:30'`) into Deye Modbus HHMM decimal integer (`430`)."""
    hh_str, mm_str = hhmm.strip().split(":", maxsplit=1)
    hh, mm = int(hh_str), int(mm_str)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"Invalid HH:MM value for Modbus encoding: {hhmm!r}")
    return hh * 100 + mm


class SolarmanV5Client:
    """Local LAN Solarman V5 Modbus-over-TCP client (`:8899`) implementing `InverterClient`."""

    def __init__(
        self,
        host: str,
        logger_serial: int | str,
        port: int = 8899,
        slave_id: int = 1,
        device_sn: str | None = None,
        timeout: float = 15.0,
        modbus_factory: Callable[..., Any] | None = None,
    ) -> None:
        cleaned_host = host.strip()
        if not cleaned_host:
            raise ValueError("host must be non-empty")
        if not 1 <= port <= 65535:
            raise ValueError("port must be in 1..65535")
        try:
            parsed_serial = int(str(logger_serial).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"logger_serial must be a numeric SN, got {logger_serial!r}") from exc
        if parsed_serial <= 0:
            raise ValueError("logger_serial must be a positive integer")

        self._host = cleaned_host
        self._serial = parsed_serial
        self._port = port
        self._slave_id = slave_id
        self._device_sn = (
            device_sn.strip() if device_sn and device_sn.strip() else str(parsed_serial)
        )
        self._timeout = max(1, int(round(timeout)))
        self._modbus_factory = modbus_factory or PySolarmanV5
        self._modbus: Any | None = None

    def close(self) -> None:
        """Close the underlying Solarman V5 TCP socket if open."""
        if self._modbus is not None:
            try:
                self._modbus.disconnect()
            except Exception:
                logger.debug("Ignored error while disconnecting Solarman V5 socket", exc_info=True)
            finally:
                self._modbus = None

    def __enter__(self) -> SolarmanV5Client:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _ensure_connected(self) -> Any:
        if self._modbus is None:
            self._modbus = self._modbus_factory(
                self._host,
                self._serial,
                port=self._port,
                mb_slave_id=self._slave_id,
                socket_timeout=self._timeout,
                auto_reconnect=True,
                logger=logging.getLogger("pstryk_pilot.clients.solarman"),
            )
        return self._modbus

    def get_status(self) -> InverterStatus:
        """Read live telemetry and 6-slot TOU schedule directly from the local Wi-Fi stick."""
        try:
            conn = self._ensure_connected()
            telemetry_regs: list[int] = conn.read_holding_registers(
                register_addr=_REG_TELEMETRY_START,
                quantity=_REG_TELEMETRY_LEN,
            )
            tou_regs: list[int] = conn.read_holding_registers(
                register_addr=_REG_TOU_BLOCK_START,
                quantity=_REG_TOU_BLOCK_LEN,
            )
        except (NoSocketAvailableError, V5FrameError, OSError, TimeoutError) as exc:
            self.close()
            raise ApiClientError(
                f"Deye local Solarman V5 error ({self._host}:{self._port}): {exc}"
            ) from exc
        except Exception as exc:
            if isinstance(exc, ApiClientError):
                raise
            self.close()
            raise ApiClientError(
                f"Deye local Modbus error ({self._host}:{self._port}): {exc}"
            ) from exc

        if len(telemetry_regs) < _REG_TELEMETRY_LEN:
            raise PayloadParseError(
                f"Expected {_REG_TELEMETRY_LEN} telemetry registers, got {len(telemetry_regs)}"
            )
        if len(tou_regs) < _REG_TOU_BLOCK_LEN:
            raise PayloadParseError(
                f"Expected {_REG_TOU_BLOCK_LEN} TOU registers, got {len(tou_regs)}"
            )

        def tel(addr: int) -> int:
            return int(telemetry_regs[addr - _REG_TELEMETRY_START])

        def tou(addr: int) -> int:
            return int(tou_regs[addr - _REG_TOU_BLOCK_START])

        battery_soc = tel(_REG_BATTERY_SOC)
        if not 0 <= battery_soc <= 100:
            raise PayloadParseError(f"Deye Modbus battery SoC out of [0, 100] range: {battery_soc}")

        battery_power_w = float(_to_int16(tel(_REG_BATTERY_POWER)))
        grid_power_w = float(_to_int16(tel(_REG_TOTAL_GRID_POWER)))
        load_power_w = max(0.0, float(_to_int16(tel(_REG_TOTAL_LOAD_POWER))))
        pv1_w = max(0, tel(_REG_PV1_POWER) & 0xFFFF)
        pv2_w = max(0, tel(_REG_PV2_POWER) & 0xFFFF)
        pv_power_w = float(pv1_w + pv2_w)

        tou_enabled = bool(tou(_REG_TOU_ENABLE) & 0x01)
        start_times = [
            _decode_modbus_hhmm(tou(_REG_TOU_TIME_1 + idx), slot_index=idx + 1) for idx in range(6)
        ]

        slots: list[TouSlot] = []
        for idx in range(6):
            slot_num = idx + 1
            raw_soc = tou(_REG_TOU_SOC_1 + idx)
            if not 0 <= raw_soc <= 100:
                raise PayloadParseError(
                    f"Deye Modbus TOU slot #{slot_num} SoC out of [0, 100] range: {raw_soc}"
                )
            charge_flags = tou(_REG_TOU_CHARGE_1 + idx)
            slots.append(
                TouSlot(
                    slot_index=slot_num,
                    start_time=start_times[idx],
                    end_time=start_times[(idx + 1) % 6],
                    target_soc=raw_soc,
                    grid_charge=bool(charge_flags & 0x01),
                    sell=False,
                )
            )

        status = InverterStatus(
            device_sn=self._device_sn,
            battery_soc=battery_soc,
            pv_power_w=pv_power_w,
            battery_power_w=battery_power_w,
            grid_power_w=grid_power_w,
            load_power_w=load_power_w,
            tou_enabled=tou_enabled,
            tou_slots=slots,
            fetched_at=datetime.now(tz=UTC),
        )
        logger.info(
            "Fetched Deye inverter status over local Solarman V5",
            extra={
                "event": "deye_local_status_fetched",
                "host": self._host,
                "port": self._port,
                "battery_soc": status.battery_soc,
                "pv_power_w": status.pv_power_w,
                "tou_enabled": status.tou_enabled,
            },
        )
        return status

    def write_tou_schedule(
        self,
        slots: Sequence[TouSlot],
        *,
        dry_run: bool,
        enable_tou: bool = True,
    ) -> bool:
        """Write 6-slot TOU registers (`146, 148..153, 166..177`) over local Solarman V5."""
        ordered = _validate_six_tou_slots(slots)
        encoded_times = [_encode_modbus_hhmm(s.start_time) for s in ordered]
        for idx in range(1, 6):
            if encoded_times[idx] <= encoded_times[idx - 1]:
                raise ValueError(
                    "Deye Modbus TOU start_time values must be strictly ascending across slots 1..6"
                )

        if dry_run:
            logger.info(
                "Skipping local Solarman V5 TOU write (dry_run=True)",
                extra={
                    "event": "deye_local_write_skipped_dry_run",
                    "host": self._host,
                    "port": self._port,
                },
            )
            return False

        try:
            conn = self._ensure_connected()
            current_tou: list[int] = conn.read_holding_registers(
                register_addr=_REG_TOU_BLOCK_START,
                quantity=_REG_TOU_BLOCK_LEN,
            )
            if len(current_tou) < _REG_TOU_BLOCK_LEN:
                raise PayloadParseError(
                    f"Expected {_REG_TOU_BLOCK_LEN} TOU registers before write, "
                    f"got {len(current_tou)}"
                )

            cur_enable = int(current_tou[_REG_TOU_ENABLE - _REG_TOU_BLOCK_START]) & 0xFFFF
            if enable_tou:
                # Preserve existing weekday/upper bits; enable Mon..Sun (0xFE) if unset
                day_mask = (cur_enable & 0xFFFE) if (cur_enable & 0x00FE) else (cur_enable | 0x00FE)
                target_enable = day_mask | 0x0001
            else:
                target_enable = cur_enable & ~0x0001

            if target_enable != cur_enable:
                conn.write_multiple_holding_registers(
                    register_addr=_REG_TOU_ENABLE,
                    values=[target_enable],
                )

            conn.write_multiple_holding_registers(
                register_addr=_REG_TOU_TIME_1,
                values=encoded_times,
            )

            soc_values = [int(s.target_soc) for s in ordered]
            charge_offset = _REG_TOU_CHARGE_1 - _REG_TOU_BLOCK_START
            charge_values = [
                (
                    (int(current_tou[charge_offset + idx]) & 0xFFFE)
                    | (0x0001 if ordered[idx].grid_charge else 0x0000)
                )
                for idx in range(6)
            ]
            conn.write_multiple_holding_registers(
                register_addr=_REG_TOU_SOC_1,
                values=soc_values + charge_values,
            )
        except (NoSocketAvailableError, V5FrameError, OSError, TimeoutError) as exc:
            self.close()
            raise ApiClientError(
                f"Deye local Solarman V5 write error ({self._host}:{self._port}): {exc}"
            ) from exc
        except Exception as exc:
            if isinstance(exc, ApiClientError):
                raise
            self.close()
            raise ApiClientError(
                f"Deye local Modbus write error ({self._host}:{self._port}): {exc}"
            ) from exc

        logger.info(
            "Updated Deye TOU schedule over local Solarman V5",
            extra={
                "event": "deye_local_tou_written",
                "host": self._host,
                "port": self._port,
                "enable_tou": enable_tou,
            },
        )
        return True
