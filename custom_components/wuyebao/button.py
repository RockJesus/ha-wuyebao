"""Button platform: one button per door / gate device."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTR_DEVICES, DOMAIN
from .coordinator import WuyeBaoCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up door buttons from the coordinator data."""
    coordinator: WuyeBaoCoordinator = hass.data[DOMAIN][entry.entry_id]
    devices = coordinator.data.get(ATTR_DEVICES, []) if coordinator.data else []
    async_add_entities(
        WuyeBaoDoorButton(coordinator, entry, device) for device in devices
    )


class WuyeBaoDoorButton(CoordinatorEntity[WuyeBaoCoordinator], ButtonEntity):
    """A button that opens one door."""

    _attr_has_entity_name = False
    _attr_translation_key = "door"

    def __init__(
        self,
        coordinator: WuyeBaoCoordinator,
        entry: ConfigEntry,
        device: dict,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device["device_id"]
        self._attr_unique_id = f"{entry.entry_id}_door_{self._device_id}"
        self._attr_name = device["name"]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="物业宝",
            manufacturer="物业宝",
            model="云门禁",
        )

    async def async_press(self) -> None:
        """Send the open-door command."""
        await self.coordinator.open_door(self._device_id)
