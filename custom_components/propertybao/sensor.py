"""Sensor platform for 物业宝."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .api import PropertyBaoClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up 物业宝 sensors based on a config entry."""
    client: PropertyBaoClient = hass.data[DOMAIN][entry.entry_id]

    entities = [
        PropertyBaoUserSensor(client, "username", "用户名", client.username),
        PropertyBaoUserSensor(client, "community", "小区", client.community_name or "未知小区", "mdi:home-city"),
    ]

    async_add_entities(entities)


class PropertyBaoUserSensor(SensorEntity):
    """Representation of a 物业宝 user sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        client: PropertyBaoClient,
        unique_id: str,
        name: str,
        value: str,
        icon: str = "mdi:account",
    ) -> None:
        """Initialize the sensor."""
        self._client = client
        self._attr_unique_id = f"propertybao_{unique_id}"
        self._attr_name = name
        self._attr_native_value = value
        self._attr_icon = icon
