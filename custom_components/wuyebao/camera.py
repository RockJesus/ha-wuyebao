"""Camera platform for 物业宝 door-gate monitoring.

Two camera types are provided per gate device:
- WuYeBaoCamera: static monitor, shows the latest call/alarm snapshot
  strictly matched to that gate (no cross-device fallback).
- WuYeBaoLiveCamera: live monitor, sends a SIP monitor message before
  polling the latest matched snapshot, served as an MJPEG stream.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .api import WuYeBaoClient

_LOGGER = logging.getLogger(__name__)

# Keep strong references to backfill tasks so they are not garbage-collected.
_BACKFILL_TASKS: list[asyncio.Task] = []


def _schedule_backfill(coro: Any) -> None:
    """Schedule a gate-list backfill coroutine and keep it alive."""
    task = asyncio.ensure_future(coro)
    _BACKFILL_TASKS.append(task)
    task.add_done_callback(_BACKFILL_TASKS.remove)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up 物业宝 cameras based on a config entry."""
    client: WuYeBaoClient = hass.data[DOMAIN][entry.entry_id]

    try:
        gates = await client.ensure_gates()
        _LOGGER.info("Found %d gates for cameras", len(gates))
    except Exception as err:
        _LOGGER.error("Failed to get gates: %s", err)
        gates = []

    # Create camera entities for wall gates and unit doors (they have cameras)
    entities: list[Camera] = []
    manager = hass.data[DOMAIN].get("video_manager")
    for gate in gates:
        gate_type = gate.get("type", "")
        if gate_type in ("wall", "outdoor"):
            entities.append(WuYeBaoCamera(client, entry.entry_id, gate))
            entities.append(WuYeBaoLiveCamera(client, entry.entry_id, gate, manager))
            # Register gate with the live-video manager so RTSP clients
            # can start a SIP monitor call for this device.
            if manager is not None:
                manager.register_gate(_gate_id(gate), gate, client)

    async_add_entities(entities)

    if not gates:
        # Transient API hiccup right after HA restart: retry later so the
        # camera entities are created once the cached list becomes available.
        _schedule_backfill(
            _retry_add_cameras(hass, client, entry.entry_id, manager, async_add_entities)
        )


async def _retry_add_cameras(
    hass: HomeAssistant,
    client: WuYeBaoClient,
    entry_id: str,
    manager: Any,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Re-add camera entities after a transient empty gate list at setup."""
    for delay in (45, 120, 300):
        await asyncio.sleep(delay)
        try:
            gates = await client.ensure_gates()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Retry cameras: failed to fetch gates: %s", err)
            continue
        if not gates:
            _LOGGER.warning("Retry cameras: gate list still empty (delay %ss)", delay)
            continue
        _LOGGER.info("Retry cameras: adding camera entities for %d gates", len(gates))
        entities: list[Camera] = []
        for gate in gates:
            if gate.get("type", "") in ("wall", "outdoor"):
                entities.append(WuYeBaoCamera(client, entry_id, gate))
                entities.append(WuYeBaoLiveCamera(client, entry_id, gate, manager))
                if manager is not None:
                    manager.register_gate(_gate_id(gate), gate, client)
        async_add_entities(entities)
        return


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


class WuYeBaoCamera(Camera):
    """Latest visitor camera: snapshot of the most recent doorbell call.

    The value shown is the last visitor's doorbell snapshot for this gate
    (from call records, strictly matched by deviceNumber + devicesType).
    """

    _attr_has_entity_name = True
    _attr_name = "最近访客"
    _attr_icon = "mdi:account-arrow-right-outline"
    _attr_frame_interval = 30.0

    def __init__(
        self,
        client: WuYeBaoClient,
        entry_id: str,
        gate: dict[str, Any],
    ) -> None:
        """Initialize the camera."""
        super().__init__()
        self._client = client
        self._gate = gate
        self._entry_id = entry_id
        self._gid = _gate_id(gate)
        self._last_visit_time: Any = None

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

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Visitor snapshot metadata."""
        return {
            "gate_id": self._gid,
            "device_number": self._gate.get("deviceNumber", ""),
            "最近访客时间": self._last_visit_time,
        }

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the latest matched snapshot for this gate only."""
        try:
            info = await self._client.get_latest_call_info(self._gate)
            if info:
                if info.get("time") and info.get("time") != self._last_visit_time:
                    self._last_visit_time = info.get("time")
                    self.async_write_ha_state()
                data = await self._client.download_image(info["url"])
                if data:
                    return data
                _LOGGER.warning("Camera %s: image download failed", self.name)
        except Exception as err:
            _LOGGER.warning("Camera %s: failed to get image: %s", self.name, err)
        return None


class WuYeBaoLiveCamera(Camera):
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
        client: WuYeBaoClient,
        entry_id: str,
        gate: dict[str, Any],
        manager: Any = None,
    ) -> None:
        """Initialize the live camera."""
        super().__init__()
        self._client = client
        self._gate = gate
        self._entry_id = entry_id
        self._gid = _gate_id(gate)
        self._manager = manager

        self._attr_unique_id = f"{entry_id}_live_camera_{self._gid}"
        self._attr_device_info = self._build_device_info(gate)

    async def stream_source(self) -> str | None:
        """Return the RTSP stream URL for this gate's live video.

        NOTE: HA >= 2024.x calls ``await camera.stream_source()`` as an
        async method (not a property); returning a str from a property
        raises ``TypeError: 'str' object is not callable`` in
        camera/webrtc.py's async_get_supported_provider.
        """
        return f"rtsp://127.0.0.1:8556/{self._gid}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """RTSP URL exposed here since the standalone 流地址 sensor was
        removed in v6.6.8 (go2rtc / ffmpeg users copy it from attributes)."""
        debug = ""
        if self._manager is not None:
            sess = self._manager.get_session(self._gid)
            if sess is not None:
                debug = getattr(sess, "last_debug", "") or ""
        return {
            "gate_id": self._gid,
            "rtsp_url": f"rtsp://127.0.0.1:8556/{self._gid}",
            "device_number": self._gate.get("deviceNumber", ""),
            "sip_debug": debug,
        }

    @property
    def supported_features(self) -> CameraEntityFeature:
        """Advertise STREAM so the HA frontend enables live playback
        (without this flag the UI only shows the snapshot image)."""
        return CameraEntityFeature.STREAM

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
