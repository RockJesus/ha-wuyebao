"""Button platform: one open-door button per gate."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTR_GATES, DOMAIN
from .coordinator import WuyeBaoCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up gate buttons from the coordinator data."""
    coordinator: WuyeBaoCoordinator = hass.data[DOMAIN][entry.entry_id]
    gates = coordinator.data.get(ATTR_GATES, []) if coordinator.data else []
    async_add_entities(
        WuyeBaoGateButton(coordinator, entry, gate) for gate in gates
    )


class WuyeBaoGateButton(CoordinatorEntity[WuyeBaoCoordinator], ButtonEntity):
    """A button that opens one gate."""

    _attr_has_entity_name = False
    _attr_translation_key = "open_gate"
    _attr_icon = "mdi:door-open"

    def __init__(
        self,
        coordinator: WuyeBaoCoordinator,
        entry: ConfigEntry,
        gate: dict,
    ) -> None:
        super().__init__(coordinator)
        self._gate_id = gate["gate_id"]
        self._attr_unique_id = f"{entry.entry_id}_gate_{self._gate_id}_open"
        self._attr_name = f"{gate['name']} 开门"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{self._gate_id}")},
            name=gate["name"],
            manufacturer="深圳家和云联",
            model="物业宝 云门禁",
        )

    async def async_press(self) -> None:
        """Send the open-door command."""
        await self.coordinator.open_gate(self._gate_id)
