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
        PropertyBaoCommunitySensor(client, entry.entry_id),
        PropertyBaoUserSensor(client, entry.entry_id),
    ]
    async_add_entities(entities)


class PropertyBaoCommunitySensor(SensorEntity):
    """Community info sensor."""

    _attr_has_entity_name = True
    _attr_name = "小区信息"
    _attr_icon = "mdi:home-city"

    def __init__(self, client: PropertyBaoClient, entry_id: str) -> None:
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


class PropertyBaoUserSensor(SensorEntity):
    """User info sensor."""

    _attr_has_entity_name = True
    _attr_name = "用户信息"
    _attr_icon = "mdi:account"

    def __init__(self, client: PropertyBaoClient, entry_id: str) -> None:
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
