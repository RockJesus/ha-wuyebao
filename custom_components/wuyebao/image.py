"""Image platform for 物业宝: 人脸信息 & 呼叫记录 snapshots.

These replace the old "人脸信息" / "呼叫记录" sensors: the user wants the
actual picture (not a count or URL), so they are now image entities on the
hub device.  The hub poller in __init__.py refreshes ``hub_data["face"]``
and ``hub_data["call_records"]`` every 60 s; these entities push a new
image whenever the snapshot URL changes.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .api import WuYeBaoClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up 物业宝 image entities on the hub device."""
    client: WuYeBaoClient = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            WuYeBaoFaceImage(hass, client, entry.entry_id),
            WuYeBaoCallsImage(hass, client, entry.entry_id),
        ]
    )


def _hub_device_info(entry_id: str) -> DeviceInfo:
    """Device info must match sensor.py / button.py hub entities."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_hub")},
        name="物业宝 主站",
        manufacturer="深圳家和云联",
        model="物业宝 集成",
    )


class _WuYeBaoHubImage(ImageEntity):
    """Base class: image entity fed by hub_data cache + URL change push.

    The picture is served by implementing ``async_image()`` (returns raw
    bytes, as the HA core image proxy expects) — the API client downloads
    the snapshot with its own HTTP session, so the proxy never has to reach
    the upstream URL itself.  ``image_url`` stays UNDEFINED so the core
    never tries its own URL-fetch path.
    """

    _attr_has_entity_name = True
    # ImageEntity does not poll by default; we need periodic polls to notice
    # when the snapshot URL in hub_data changes and push a new picture.
    _attr_should_poll = True

    def __init__(
        self,
        hass: HomeAssistant,
        client: WuYeBaoClient,
        entry_id: str,
        key: str,
    ) -> None:
        """Initialize the image entity."""
        super().__init__(hass)
        self._client = client
        self._key = key
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_device_info = _hub_device_info(entry_id)
        # Initial timestamp: without it the frontend treats the image as stale
        # and may not render.  Set from the first poll below.
        self._attr_image_last_updated = dt_util.utcnow()
        # Internal latest snapshot URL + cached bytes.  Deliberately NOT
        # exposed through ``_attr_image_url`` (HA would proxy-fetch it and
        # fail on the upstream URL); picture bytes come from ``async_image()``.
        self._current_url: str | None = None
        self._cached_content: bytes | None = None

    def _extract_url(self, data: Any) -> str | None:
        raise NotImplementedError

    async def _download(self, url: str) -> bytes | None:
        """Download picture bytes through the API client's HTTP session."""
        try:
            return await self._client.download_image(url)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Image %s download failed: %s", self._attr_name, err)
            return None

    async def async_update(self) -> None:
        """Refresh image URL from the hub cache and push if it changed."""
        data = self._client.hub_data.get(self._key)
        url = self._extract_url(data)
        if not url:
            # No fresh snapshot: keep showing the last known picture instead
            # of blanking the entity on a transient gap.
            return
        if url == self._current_url:
            return
        self._current_url = url
        content = await self._download(url)
        if content:
            self._cached_content = content
            self._attr_image_last_updated = dt_util.utcnow()
            self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        """Return the latest snapshot picture bytes (HA image proxy path).

        Modern HA core calls ``async_image()`` and wraps the returned raw
        bytes into an ``Image`` object itself — returning an ``Image`` here
        (v7.0.9) or advertising ``image_url`` (v7.0.7) both made the proxy
        answer 500.
        """
        if self._cached_content:
            return self._cached_content
        if not self._current_url:
            return None
        content = await self._download(self._current_url)
        if content:
            self._cached_content = content
            self._attr_image_last_updated = dt_util.utcnow()
            self.async_write_ha_state()
        return content

    def image(self) -> bytes | None:
        """Synchronous fallback used if core ever takes the executor path."""
        return self._cached_content


class WuYeBaoFaceImage(_WuYeBaoHubImage):
    """人脸信息: latest face-registration snapshot picture."""

    _attr_name = "人脸信息"
    _attr_icon = "mdi:face-recognition"

    def __init__(self, hass: HomeAssistant, client: WuYeBaoClient, entry_id: str) -> None:
        """Initialize the face image entity."""
        super().__init__(hass, client, entry_id, "face")

    def _extract_url(self, data: Any) -> str | None:
        if not isinstance(data, dict):
            return None
        faces = data.get("faces") or []
        for f in faces:
            url = f.get("image") or f.get("imageUrl")
            if url:
                return str(url)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Face metadata for diagnostics."""
        data = self._client.hub_data.get("face") or {}
        faces = data.get("faces") or []
        auths = data.get("faceAuths") or []
        return {
            "faces": len(faces),
            "face_valid_time": data.get("faceValidTime"),
            "auth_gates": [
                {
                    "community": a.get("communityName"),
                    "area": a.get("areaName"),
                    "building": a.get("buildingName"),
                    "unit": a.get("unitName"),
                }
                for a in auths[:10]
            ],
        }


class WuYeBaoCallsImage(_WuYeBaoHubImage):
    """呼叫记录: latest doorbell-call snapshot picture."""

    _attr_name = "呼叫记录"
    _attr_icon = "mdi:phone-log"

    def __init__(self, hass: HomeAssistant, client: WuYeBaoClient, entry_id: str) -> None:
        """Initialize the calls image entity."""
        super().__init__(hass, client, entry_id, "call_records")

    def _extract_url(self, data: Any) -> str | None:
        if not isinstance(data, list) or not data:
            return None
        for c in data:
            url = c.get("imageUrl")
            if url:
                return str(url)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Call metadata for diagnostics."""
        data = self._client.hub_data.get("call_records")
        if not isinstance(data, list) or not data:
            return {"count": 0, "latest": None}
        import time as _time

        latest = []
        for c in data[:20]:
            ts = c.get("time")
            ts_fmt = None
            if isinstance(ts, (int, float)):
                ts_fmt = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(int(ts)))
            latest.append(
                {
                    "time": ts_fmt,
                    "device": c.get("accessInfo") or c.get("deviceNumber"),
                    "call_number": c.get("callNumber"),
                    "type": c.get("devicesType"),
                    "state": c.get("state"),
                }
            )
        return {"count": len(data), "latest": latest[0] if latest else None}
