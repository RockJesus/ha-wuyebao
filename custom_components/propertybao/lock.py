"""Lock platform for 物业宝."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .api import PropertyBaoClient, PropertyBaoApiError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up 物业宝 locks based on a config entry."""
    client: PropertyBaoClient = hass.data[DOMAIN][entry.entry_id]

    try:
        gates = await client.get_device_gates()
        _LOGGER.info("Found %d gates", len(gates))
    except Exception as err:
        _LOGGER.error("Failed to get gates: %s", err)
        gates = []

    entities = []
    for gate in gates:
        entities.append(PropertyBaoLock(client, gate))

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

        # Build name
        building_name = gate.get("buildingName", "")
        unit_name = gate.get("unitName", "")
        alias = gate.get("alias", "")

        if alias:
            self._attr_name = alias
        elif building_name and unit_name:
            self._attr_name = f"{building_name}{unit_name}"
        elif building_name:
            self._attr_name = building_name
        else:
            self._attr_name = gate.get("deviceNumber", "门禁")

        self._attr_is_locked = True
        self._attr_code_format = r"^\d{6}$"

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
            "sip_target": self._client._build_sip_target(self._gate),
        }

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the door via SIP."""
        try:
            await self._client.open_door_via_sip(self._gate)
            self._attr_is_locked = False
            self.async_write_ha_state()
            self.hass.loop.call_later(10, self._set_locked)
            _LOGGER.info("Door unlock command sent: %s", self.name)
        except PropertyBaoApiError as err:
            _LOGGER.error("Failed to unlock door: %s", err)
            raise

    def _set_locked(self) -> None:
        """Set locked state."""
        self._attr_is_locked = True
        self.async_write_ha_state()

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the door (not supported)."""
        _LOGGER.warning("Locking is not supported by 物业宝")
