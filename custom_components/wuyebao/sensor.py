"""Sensor platform for 物业宝."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .api import WuYeBaoClient

_LOGGER = logging.getLogger(__name__)

RTSP_BASE = "rtsp://127.0.0.1:8556"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up 物业宝 sensors based on a config entry."""
    client: WuYeBaoClient = hass.data[DOMAIN][entry.entry_id]

    entities = [
        WuYeBaoCommunitySensor(client, entry.entry_id),
        WuYeBaoUserSensor(client, entry.entry_id),
    ]

    # One "流地址" sensor per camera-capable gate, mounted on the same
    # device as the lock/cameras so go2rtc URLs are easy to copy.
    try:
        gates = await client.get_gates()
        for gate in gates:
            if gate.get("type", "") in ("wall", "outdoor"):
                entities.append(WuYeBaoStreamSourceSensor(entry.entry_id, gate))
    except Exception as err:
        _LOGGER.warning("Failed to get gates for stream sensors: %s", err)

    async_add_entities(entities)


def _gate_id(gate: dict[str, Any]) -> str:
    """Stable gate id - must match camera.py / lock.py."""
    return str(
        gate.get("id") or gate.get("uid") or gate.get("deviceNumber") or "unknown"
    )


def _build_device_name(gate: dict[str, Any]) -> str:
    """Human-friendly device name - same as camera.py so entities merge."""
    alias = gate.get("alias", "")
    if alias and len(str(alias).strip()) > 0:
        return str(alias).strip()

    parts: list[str] = []
    community = gate.get("communityName")
    if community:
        parts.append(str(community))
    area = gate.get("areaName", "")
    if area:
        parts.append(str(area))
    building = gate.get("buildingName", "")
    if building:
        parts.append(str(building))
    unit = gate.get("unitName", "")
    if unit:
        parts.append(str(unit))

    device_number = gate.get("deviceNumber", "")
    gate_type = gate.get("type", "")
    if gate_type == "wall":
        if device_number == "a":
            parts.append("南门")
        elif device_number == "b":
            parts.append("北门")
        else:
            parts.append(f"围墙门-{device_number}")
    else:
        if device_number:
            parts.append(f"门口机{device_number}")
        else:
            parts.append("单元门")

    return " ".join(parts) if parts else f"门禁-{device_number or 'unknown'}"


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


class WuYeBaoStreamSourceSensor(SensorEntity):
    """RTSP stream URL sensor for a gate device (go2rtc copy helper).

    The state is the full rtsp:// URL of the gate's live video stream.
    It is mounted on the same device as the gate's lock and cameras.
    """

    _attr_has_entity_name = True
    _attr_name = "流地址"
    _attr_icon = "mdi:access-point"

    def __init__(self, entry_id: str, gate: dict[str, Any]) -> None:
        """Initialize the sensor."""
        self._entry_id = entry_id
        self._gate = gate
        self._gid = _gate_id(gate)
        self._rtsp_url = f"{RTSP_BASE}/{self._gid}"

        self._attr_unique_id = f"{entry_id}_stream_{self._gid}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{self._gid}")},
            name=_build_device_name(gate),
            manufacturer="深圳家和云联",
            model="物业宝 云门禁",
            sw_version="1.1.1.51",
        )

    @property
    def native_value(self) -> str:
        """Return the RTSP stream URL."""
        return self._rtsp_url

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        return {
            "gate_id": self._gid,
            "device_type": self._gate.get("type", ""),
            "rtsp_url": self._rtsp_url,
            "device_number": self._gate.get("deviceNumber", ""),
        }
