"""Lock platform for 物业宝."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lock import LockEntity
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
    """Set up 物业宝 locks based on a config entry."""
    client: PropertyBaoClient = hass.data[DOMAIN][entry.entry_id]

    try:
        gates = await client.get_gates()
        _LOGGER.info("Found %d gates", len(gates))
        # Log raw gates for debugging
        for i, gate in enumerate(gates):
            _LOGGER.debug("Gate %d: %s", i, gate)
    except Exception as err:
        _LOGGER.error("Failed to get gates: %s", err)
        gates = []

    entities = [PropertyBaoLock(client, entry.entry_id, gate) for gate in gates]
    async_add_entities(entities)


class PropertyBaoLock(LockEntity):
    """Representation of a 物业宝 door lock."""

    _attr_has_entity_name = True
    _attr_name = "开门"

    def __init__(
        self,
        client: PropertyBaoClient,
        entry_id: str,
        gate: dict[str, Any],
    ) -> None:
        """Initialize the lock."""
        self._client = client
        self._gate = gate
        self._entry_id = entry_id

        gate_id = str(gate.get("id") or gate.get("uid") or gate.get("deviceNumber") or "unknown")
        self._attr_unique_id = f"{entry_id}_gate_{gate_id}"

        # Build device name from available fields
        device_name = self._build_device_name(client, gate)

        # Build device info
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{gate_id}")},
            name=device_name,
            manufacturer="深圳家和云联",
            model="物业宝 云门禁",
            sw_version="1.1.1.51",
        )

        self._attr_is_locked = True

    def _build_device_name(self, client: PropertyBaoClient, gate: dict[str, Any]) -> str:
        """Build a human-friendly device name from gate data."""
        # Prefer alias
        alias = gate.get("alias", "")
        if alias and len(str(alias).strip()) > 0:
            return str(alias).strip()

        # Build from location fields
        parts = []

        # Community name (from client or gate)
        community = gate.get("communityName") or client.community_name
        if community:
            parts.append(str(community))

        # Area name
        area = gate.get("areaName", "")
        if area:
            parts.append(str(area))

        # Building name
        building = gate.get("buildingName", "")
        if building:
            parts.append(str(building))

        # Unit name
        unit = gate.get("unitName", "")
        if unit:
            parts.append(str(unit))

        # Device number
        device_number = gate.get("deviceNumber", "")
        gate_type = gate.get("type", "")

        if gate_type == "wall":
            # Wall gate / 围墙门
            if device_number:
                parts.append(f"南门-{device_number}" if device_number == "a" else f"门-{device_number}")
            else:
                parts.append("围墙门")
        else:
            # Unit door / 单元门
            if device_number:
                parts.append(f"门禁-{device_number}")
            else:
                parts.append("单元门")

        return " ".join(parts) if parts else f"门禁-{device_number or gate.get('id', 'unknown')}"

    @property
    def device_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        return {
            "device_id": self._gate.get("id"),
            "device_uid": self._gate.get("uid"),
            "device_type": self._gate.get("type"),
            "device_number": self._gate.get("deviceNumber"),
            "community_id": self._gate.get("communityId"),
            "community_name": self._gate.get("communityName"),
            "building_name": self._gate.get("buildingName"),
            "unit_name": self._gate.get("unitName"),
            "state": self._gate.get("state"),
        }

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the door via SIP MESSAGE."""
        try:
            result = await self._client.open_door_sip(self._gate)
            _LOGGER.info("SIP unlock result: %s", result)
            self._attr_is_locked = False
            self.async_write_ha_state()
            self.hass.loop.call_later(10, self._set_locked)
            _LOGGER.info("Door unlock command sent: %s", self.device_info.name)
        except Exception as err:
            _LOGGER.error("Failed to unlock door: %s", err)
            raise

    def _set_locked(self) -> None:
        """Set locked state."""
        self._attr_is_locked = True
        self.async_write_ha_state()

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the door (not supported)."""
        _LOGGER.warning("Locking is not supported by 物业宝")
