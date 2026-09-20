"""Button platform for 物业宝."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
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
    """Set up 物业宝 buttons based on a config entry."""
    client: PropertyBaoClient = hass.data[DOMAIN][entry.entry_id]

    try:
        gates = await client.get_gates()
        _LOGGER.info("Found %d gates for buttons", len(gates))
    except Exception as err:
        _LOGGER.error("Failed to get gates: %s", err)
        gates = []

    # Create monitor buttons for all gates (wall and outdoor)
    entities = []
    for gate in gates:
        gate_type = gate.get("type", "")
        # Wall gates and unit doors both have cameras
        if gate_type in ("wall", "outdoor"):
            entities.append(PropertyBaoMonitorButton(client, entry.entry_id, gate))

    async_add_entities(entities)


class PropertyBaoMonitorButton(ButtonEntity):
    """Button to start monitoring a gate."""

    _attr_has_entity_name = True
    _attr_name = "查看监控"
    _attr_icon = "mdi:cctv"

    def __init__(
        self,
        client: PropertyBaoClient,
        entry_id: str,
        gate: dict[str, Any],
    ) -> None:
        """Initialize the button."""
        self._client = client
        self._gate = gate
        self._entry_id = entry_id

        gate_id = str(gate.get("id") or gate.get("uid") or gate.get("deviceNumber") or "unknown")
        self._attr_unique_id = f"{entry_id}_monitor_{gate_id}"

        # Build device name
        device_name = self._build_device_name(gate)

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{gate_id}")},
            name=device_name,
            manufacturer="深圳家和云联",
            model="物业宝 云门禁",
            sw_version="1.1.1.51",
        )

    def _build_device_name(self, gate: dict[str, Any]) -> str:
        """Build device name."""
        alias = gate.get("alias", "")
        if alias:
            return str(alias).strip()

        parts = []
        community = gate.get("communityName")
        if community:
            parts.append(str(community))

        area = gate.get("areaName", "")
        if area:
            parts.append(str(area))

        device_number = gate.get("deviceNumber", "")
        if device_number:
            if device_number == "a":
                parts.append("南门")
            elif device_number == "b":
                parts.append("北门")
            else:
                parts.append(f"门-{device_number}")

        return " ".join(parts) if parts else f"门禁-{device_number}"

    async def async_press(self) -> None:
        """Handle the button press."""
        try:
            result = await self._client.start_monitor(self._gate)
            _LOGGER.info("Monitor started: %s", result)
        except Exception as err:
            _LOGGER.error("Failed to start monitor: %s", err)
            raise
