"""Sensor platform for 物业宝."""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .api import WuYeBaoClient
from .rtsp_server import RTSP_PORT

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up 物业宝 sensors based on a config entry."""
    client: WuYeBaoClient = hass.data[DOMAIN][entry.entry_id]

    # Host for the built-in RTSP server: prefer the HAOS host's LAN IP
    # (the RTSP server runs on the same machine as HA), fall back to the
    # external URL host, then localhost.
    rtsp_host = "localhost"
    api_cfg = getattr(hass.config, "api", None)
    if api_cfg is not None:
        local_ip = getattr(api_cfg, "local_ip", None)
        if local_ip:
            rtsp_host = local_ip
    if rtsp_host == "localhost":
        try:
            ext = hass.config.get_url()
            host = urlparse(ext).hostname
            if host:
                rtsp_host = host
        except Exception:  # noqa: BLE001
            pass

    entities = [
        WuYeBaoCommunitySensor(client, entry.entry_id),
        WuYeBaoUserSensor(client, entry.entry_id),
        WuYeBaoRepairSensor(client, entry.entry_id),
        WuYeBaoVisitorSensor(client, entry.entry_id),
        WuYeBaoNoticeSensor(client, entry.entry_id),
        WuYeBaoAlarmSensor(client, entry.entry_id),
    ]

    # One stream-address sensor per gate device (mounted on the same device
    # as the lock/camera entities).  Uses the same stable gate id as lock.py.
    try:
        gates = await client.ensure_gates()
        _LOGGER.info("Found %d gates for sensors", len(gates))
    except Exception as err:  # noqa: BLE001 - gate list is best-effort
        _LOGGER.error("Failed to get gates for sensors: %s", err)
        gates = []
    entities.extend(
        WuYeBaoGateStreamSensor(client, entry.entry_id, gate, rtsp_host)
        for gate in gates
    )

    async_add_entities(entities)

    if not gates:
        # Transient API hiccup right after HA restart: retry later so the
        # per-gate sensors are created once the cached list becomes available.
        hass.async_create_task(
            _retry_add_sensors(hass, client, entry.entry_id, rtsp_host, async_add_entities)
        )


async def _retry_add_sensors(
    hass: HomeAssistant,
    client: WuYeBaoClient,
    entry_id: str,
    rtsp_host: str,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Re-add per-gate sensors after a transient empty gate list at setup."""
    await asyncio.sleep(45)
    try:
        gates = await client.ensure_gates()
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Retry sensors: failed to fetch gates: %s", err)
        return
    if not gates:
        _LOGGER.warning("Retry sensors: gate list still empty, skipping")
        return
    _LOGGER.info("Retry sensors: adding %d gate sensors", len(gates))
    async_add_entities(
        WuYeBaoGateStreamSensor(client, entry_id, gate, rtsp_host) for gate in gates
    )


class WuYeBaoCommunitySensor(SensorEntity):
    """Community info sensor."""

    _attr_has_entity_name = True
    _attr_name = "小区信息"
    _attr_icon = "mdi:home-city"

    def __init__(self, client: WuYeBaoClient, entry_id: str) -> None:
        """Initialize the sensor."""
        self._client = client
        self._attr_unique_id = f"{entry_id}_community"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_hub")},
            name="物业宝 主站",
            manufacturer="深圳家和云联",
            model="物业宝 集成",
        )

    @property
    def native_value(self) -> str:
        """Return the state of the sensor."""
        return self._client.community_name or "未知小区"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        return {
            "community_id": self._client.community_id,
            "community_code": self._client.community_code,
            "owner_id": self._client.owner_id,
        }


class WuYeBaoUserSensor(SensorEntity):
    """User info sensor."""

    _attr_has_entity_name = True
    _attr_name = "用户信息"
    _attr_icon = "mdi:account"

    def __init__(self, client: WuYeBaoClient, entry_id: str) -> None:
        """Initialize the sensor."""
        self._client = client
        self._attr_unique_id = f"{entry_id}_user"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_hub")},
            name="物业宝 主站",
            manufacturer="深圳家和云联",
            model="物业宝 集成",
        )

    @property
    def native_value(self) -> str:
        """Return the state of the sensor."""
        return self._client.username

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        return {
            "user_id": self._client.user_id,
        }


class WuYeBaoGateStreamSensor(SensorEntity):
    """Gate-ID sensor attached to each gate device.

    Exposes the stable gate id used by the SIP monitor / door unlock, handy
    for automations and diagnostics (e.g. building rtsp://host:8556/<gate-id>
    streams).  The device is shared with the lock/camera entities of the same
    gate so it appears mounted under the gate device.
    """

    _attr_has_entity_name = True
    _attr_name = "门禁流媒体地址"
    _attr_icon = "mdi:video-stream"

    def __init__(
        self,
        client: WuYeBaoClient,
        entry_id: str,
        gate: dict[str, Any],
        rtsp_host: str,
    ) -> None:
        """Initialize the sensor."""
        self._client = client
        self._gate = gate
        self._rtsp_host = rtsp_host
        gate_id = str(
            gate.get("id") or gate.get("uid") or gate.get("deviceNumber") or "unknown"
        )
        self._gate_id = gate_id
        self._rtsp_url = f"rtsp://{rtsp_host}:{RTSP_PORT}/{gate_id}"
        self._attr_unique_id = f"{entry_id}_gate_{gate_id}_gateid"
        # Same identifiers as lock.py / camera.py so this sensor mounts on the
        # gate device instead of creating a new one.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{gate_id}")},
            name=self._build_device_name(),
            manufacturer="深圳家和云联",
            model="物业宝 门禁设备",
        )

    def _build_device_name(self) -> str:
        """Build a human-friendly device name from gate data (same as lock.py)."""
        alias = self._gate.get("alias", "")
        if alias:
            return alias
        community = self._gate.get("communityName") or self._client.community_name or ""
        area = self._gate.get("areaName", "")
        building = self._gate.get("buildingName", "")
        unit = self._gate.get("unitName", "")
        device_number = self._gate.get("deviceNumber", "")
        gate_type = self._gate.get("type", "")
        parts = []
        if community:
            parts.append(community)
        if area:
            parts.append(area)
        if building:
            parts.append(building)
        if unit:
            parts.append(unit)
        if gate_type == "wall":
            parts.append("围墙门")
        if device_number:
            parts.append(f"门口机{device_number}")
        return " ".join(parts) if parts else f"门禁-{device_number or self._gate.get('id', 'unknown')}"

    @property
    def native_value(self) -> str:
        """Return the RTSP stream address for this gate."""
        return self._rtsp_url

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        return {
            "gate_id": self._gate_id,
            "rtsp_url": self._rtsp_url,
            "rtsp_host": self._rtsp_host,
            "rtsp_port": RTSP_PORT,
            "gate_uid": self._gate.get("uid"),
            "device_number": self._gate.get("deviceNumber"),
            "gate_type": self._gate.get("type"),
            "building_name": self._gate.get("buildingName"),
            "unit_name": self._gate.get("unitName"),
            "area_name": self._gate.get("areaName"),
            "community_name": self._gate.get("communityName"),
        }


class _WuYeBaoHubSensor(SensorEntity):
    """Base class for hub-level sensors (mounted on the 物业宝 主站 device).

    Data is cached on the API client by the hub poller in __init__.py and
    read here on every HA poll (default 30 s).
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:home"

    def __init__(self, client: WuYeBaoClient, entry_id: str, key: str) -> None:
        """Initialize the sensor."""
        self._client = client
        self._key = key
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_hub")},
            name="物业宝 主站",
            manufacturer="深圳家和云联",
            model="物业宝 集成",
        )
        self._attr_native_value: Any = None
        self._attr_extra_state_attributes: dict[str, Any] = {}

    async def async_update(self) -> None:
        """Refresh state from the client cache (fresh data is fetched by the
        hub poller in __init__.py)."""
        data = self._client.hub_data.get(self._key)
        self._attr_native_value = self._value_from(data)
        self._attr_extra_state_attributes = self._attrs_from(data)

    def _value_from(self, data: Any) -> Any:
        raise NotImplementedError

    def _attrs_from(self, data: Any) -> dict[str, Any]:
        return {}


class WuYeBaoRepairSensor(_WuYeBaoHubSensor):
    """报修工单 sensor (count of records + latest items)."""

    _attr_name = "报修记录"
    _attr_icon = "mdi:hammer-wrench"

    def __init__(self, client: WuYeBaoClient, entry_id: str) -> None:
        """Initialize the sensor."""
        super().__init__(client, entry_id, "repairs")

    def _value_from(self, data: Any) -> Any:
        return len(data) if isinstance(data, list) else None

    def _attrs_from(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, list) or not data:
            return {"count": 0, "items": []}
        items = []
        for r in data[:10]:
            items.append(
                {
                    "id": r.get("id"),
                    "title": r.get("title") or r.get("content") or r.get("type") or "",
                    "state": r.get("state"),
                    "create_time": r.get("createTime"),
                }
            )
        return {"count": len(data), "items": items}


class WuYeBaoVisitorSensor(_WuYeBaoHubSensor):
    """访客开门码 sensor (active visitor codes).

    Primary value = the password of the newest valid visitor code; attributes
    carry the formatted start/end window plus validity for each code.
    """

    _attr_name = "访客开门码"
    _attr_icon = "mdi:account-key"

    def __init__(self, client: WuYeBaoClient, entry_id: str) -> None:
        """Initialize the sensor."""
        super().__init__(client, entry_id, "visitors")

    def _value_from(self, data: Any) -> Any:
        if not isinstance(data, list) or not data:
            return None
        import time as _time

        now_ms = int(_time.time() * 1000)
        newest_valid = None
        newest_any = None
        for v in data:
            start = v.get("startTime") or v.get("start_time")
            end = v.get("endTime") or v.get("end_time")
            pwd = v.get("password")
            if newest_any is None:
                newest_any = pwd
            try:
                if pwd and int(start) <= now_ms <= int(end):
                    newest_valid = pwd
            except (TypeError, ValueError):
                pass
        return newest_valid or newest_any

    def _attrs_from(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, list) or not data:
            return {"count": 0, "active": 0, "codes": []}
        import time as _time

        now_ms = int(_time.time() * 1000)
        codes = []
        for v in data[:10]:
            start = v.get("startTime") or v.get("start_time")
            end = v.get("endTime") or v.get("end_time")
            valid = None
            start_fmt = end_fmt = None
            if isinstance(start, (int, str)) and isinstance(end, (int, str)):
                try:
                    s = int(start)
                    e = int(end)
                    valid = s <= now_ms <= e
                    start_fmt = _time.strftime(
                        "%Y-%m-%d %H:%M", _time.localtime(s / 1000)
                    )
                    end_fmt = _time.strftime(
                        "%Y-%m-%d %H:%M", _time.localtime(e / 1000)
                    )
                except (TypeError, ValueError):
                    valid = None
            codes.append(
                {
                    "id": v.get("id"),
                    "password": v.get("password"),
                    "start_time": start_fmt,
                    "end_time": end_fmt,
                    "start_time_ms": start,
                    "end_time_ms": end,
                    "valid": valid,
                }
            )
        active = [c for c in codes if c.get("valid") is True]
        return {
            "count": len(data),
            "active": len(active),
            "password": codes[0].get("password"),
            "valid": codes[0].get("valid"),
            "start_time": codes[0].get("start_time"),
            "end_time": codes[0].get("end_time"),
            "codes": codes,
        }


class WuYeBaoNoticeSensor(_WuYeBaoHubSensor):
    """小区公告 sensor (homepage carousel ads / notices)."""

    _attr_name = "小区公告"
    _attr_icon = "mdi:bullhorn"

    def __init__(self, client: WuYeBaoClient, entry_id: str) -> None:
        """Initialize the sensor."""
        super().__init__(client, entry_id, "contents")

    def _value_from(self, data: Any) -> Any:
        if not isinstance(data, list) or not data:
            return None
        # data: [{"classify": {...}, "contents": [...]}]
        contents = data[0].get("contents") or []
        return len(contents)

    def _attrs_from(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, list) or not data:
            return {"count": 0, "images": []}
        classify = data[0].get("classify") or {}
        contents = data[0].get("contents") or []
        return {
            "count": len(contents),
            "type": classify.get("type"),
            "classify_id": classify.get("classifyId"),
            "images": [c.get("imageUrl") for c in contents[:20]],
            "latest_image": contents[0].get("imageUrl") if contents else None,
        }


class WuYeBaoAlarmSensor(_WuYeBaoHubSensor):
    """门禁报警 sensor (alarm records count + latest)."""

    _attr_name = "门禁报警"
    _attr_icon = "mdi:shield-alert"

    def __init__(self, client: WuYeBaoClient, entry_id: str) -> None:
        """Initialize the sensor."""
        super().__init__(client, entry_id, "alarms")

    def _value_from(self, data: Any) -> Any:
        return len(data) if isinstance(data, list) else None

    def _attrs_from(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, list) or not data:
            return {"count": 0, "latest": []}
        latest = []
        for a in data[:10]:
            latest.append(
                {
                    "id": a.get("id"),
                    "device": a.get("deviceNumber") or a.get("deviceNo"),
                    "type": a.get("devicesType") or a.get("deviceType"),
                    "image": a.get("imageUrl") or a.get("image") or a.get("url"),
                    "time": a.get("createTime") or a.get("alarmTime"),
                }
            )
        return {"count": len(data), "latest": latest}
