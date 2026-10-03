"""Camera platform for 物业宝 door-gate monitoring.

Two camera types are provided per gate device:
- PropertyBaoCamera: static monitor, shows the latest call/alarm snapshot
  strictly matched to that gate (no cross-device fallback).
- PropertyBaoLiveCamera: live monitor, sends a SIP monitor message before
  polling the latest matched snapshot, served as an MJPEG stream.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .api import PropertyBaoClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up 物业宝 cameras based on a config entry."""
    client: PropertyBaoClient = hass.data[DOMAIN][entry.entry_id]

    try:
        gates = await client.get_gates()
        _LOGGER.info("Found %d gates for cameras", len(gates))
    except Exception as err:
        _LOGGER.error("Failed to get gates: %s", err)
        gates = []

    # Create camera entities for wall gates and unit doors (they have cameras)
    entities: list[Camera] = []
    for gate in gates:
        gate_type = gate.get("type", "")
        if gate_type in ("wall", "outdoor"):
            entities.append(PropertyBaoCamera(client, entry.entry_id, gate))
            entities.append(PropertyBaoLiveCamera(client, entry.entry_id, gate))
            # Register gate with the live-video manager so RTSP clients
            # can start a SIP monitor call for this device.
            manager = hass.data[DOMAIN].get("video_manager")
            if manager is not None:
                manager.register_gate(_gate_id(gate), gate, client)

    async_add_entities(entities)


def _gate_id(gate: dict[str, Any]) -> str:
    """Compute a stable gate id (must match lock.py / button.py)."""
    return str(
        gate.get("id") or gate.get("uid") or gate.get("deviceNumber") or "unknown"
    )


def _build_device_name(gate: dict[str, Any]) -> str:
    """Build a human-friendly device name from gate data (same as lock.py)."""
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


class PropertyBaoCamera(Camera):
    """Static monitor camera: latest matched call/alarm snapshot."""

    _attr_has_entity_name = True
    _attr_name = "监控"
    _attr_icon = "mdi:cctv"
    _attr_frame_interval = 30.0

    def __init__(
        self,
        client: PropertyBaoClient,
        entry_id: str,
        gate: dict[str, Any],
    ) -> None:
        """Initialize the camera."""
        super().__init__()
        self._client = client
        self._gate = gate
        self._entry_id = entry_id
        self._gid = _gate_id(gate)

        self._attr_unique_id = f"{entry_id}_camera_{self._gid}"
        self._attr_device_info = self._build_device_info(gate)

    def _build_device_info(self, gate: dict[str, Any]) -> DeviceInfo:
        """Device info must match lock.py so entities merge under one device."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry_id}_{self._gid}")},
            name=_build_device_name(gate),
            manufacturer="深圳家和云联",
            model="物业宝 云门禁",
            sw_version="1.1.1.51",
        )

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the latest matched snapshot for this gate only."""
        try:
            image_url = await self._client.get_gate_snapshot(self._gate)
            if image_url:
                data = await self._client.download_image(image_url)
                if data:
                    return data
                _LOGGER.warning("Camera %s: image download failed", self.name)
        except Exception as err:
            _LOGGER.warning("Camera %s: failed to get image: %s", self.name, err)
        return None


class PropertyBaoLiveCamera(Camera):
    """Live monitor camera.

    - stream_source points at the integration's built-in RTSP server,
      which starts a real SIP monitor call (INVITE -> H264 RTP) when a
      client connects.
    - async_camera_image() remains as a static-snapshot fallback so the
      entity always shows something even when no stream client is active.
    """

    _attr_has_entity_name = True
    _attr_name = "实时监控"
    _attr_icon = "mdi:video-wireless"
    _attr_frame_interval = 10.0

    def __init__(
        self,
        client: PropertyBaoClient,
        entry_id: str,
        gate: dict[str, Any],
    ) -> None:
        """Initialize the live camera."""
        super().__init__()
        self._client = client
        self._gate = gate
        self._entry_id = entry_id
        self._gid = _gate_id(gate)

        self._attr_unique_id = f"{entry_id}_live_camera_{self._gid}"
        self._attr_device_info = self._build_device_info(gate)

    @property
    def stream_source(self) -> str | None:
        """Return the RTSP stream URL for this gate's live video."""
        return f"rtsp://127.0.0.1:8555/{self._gid}"

    def _build_device_info(self, gate: dict[str, Any]) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry_id}_{self._gid}")},
            name=_build_device_name(gate),
            manufacturer="深圳家和云联",
            model="物业宝 云门禁",
            sw_version="1.1.1.51",
        )

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Trigger a snapshot via SIP, then return the latest matched image."""
        try:
            # Best-effort trigger; ignore failures and still try to fetch.
            await self._client.trigger_snapshot(self._gate)
            image_url = await self._client.get_gate_snapshot(self._gate)
            if image_url:
                data = await self._client.download_image(image_url)
                if data:
                    return data
        except Exception as err:
            _LOGGER.debug("Live camera %s: %s", self.name, err)
        return None
