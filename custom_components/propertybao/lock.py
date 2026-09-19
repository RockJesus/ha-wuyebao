"""Lock platform for 物业宝."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
    except Exception as err:
        _LOGGER.error("Failed to get gates: %s", err)
        gates = []

    entities = [PropertyBaoLock(client, gate) for gate in gates]
    async_add_entities(entities)


class PropertyBaoLock(LockEntity):
    """Representation of a 物业宝 door lock."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, client: PropertyBaoClient, gate: dict[str, Any]) -> None:
        """Initialize the lock."""
        self._client = client
        self._gate = gate
        self._attr_unique_id = gate.get("id", gate.get("uid", "unknown"))

        alias = gate.get("alias", "")
        area_name = gate.get("areaName", "")
        device_number = gate.get("deviceNumber", "")

        if alias:
            self._attr_name = alias
        elif area_name and device_number:
            self._attr_name = f"{area_name}-{device_number}"
        else:
            self._attr_name = f"门禁-{device_number}"

        self._attr_is_locked = True

    @property
    def device_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        return {
            "device_id": self._gate.get("id"),
            "device_uid": self._gate.get("uid"),
            "device_type": self._gate.get("type"),
            "device_number": self._gate.get("deviceNumber"),
            "community_id": self._gate.get("communityId"),
            "unlock_password": self._gate.get("password"),
            "state": self._gate.get("state"),
        }

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the door (via SIP, not yet implemented)."""
        _LOGGER.warning(
            "Door unlock not yet implemented. Gate: %s, deviceNumber: %s",
            self.name,
            self._gate.get("deviceNumber"),
        )
        # TODO: Implement SIP MESSAGE unlock
        # For now, just simulate unlock
        self._attr_is_locked = False
        self.async_write_ha_state()
        self.hass.loop.call_later(10, self._set_locked)

    def _set_locked(self) -> None:
        """Set locked state."""
        self._attr_is_locked = True
        self.async_write_ha_state()

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the door (not supported)."""
        _LOGGER.warning("Locking is not supported by 物业宝")
