"""Sensor platform for 物业宝."""
from __future__ import annotations

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
    ]

    # One stream-address sensor per gate device (mounted on the same device
    # as the lock/camera entities).  Uses the same stable gate id as lock.py.
    try:
        gates = await client.get_gates()
        _LOGGER.info("Found %d gates for sensors", len(gates))
    except Exception as err:  # noqa: BLE001 - gate list is best-effort
        _LOGGER.error("Failed to get gates for sensors: %s", err)
        gates = []
    entities.extend(
        WuYeBaoGateStreamSensor(client, entry.entry_id, gate, rtsp_host)
        for gate in gates
    )

    async_add_entities(entities)


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
