"""Switch platform for 物业宝 visitor auto-open."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .api import WuYeBaoClient

_LOGGER = logging.getLogger(__name__)

# Shared state store under hass.data[DOMAIN]["auto_open"]:
#   {gate_id: bool}  - per-gate "visitor auto open" switch state.
# Written by the switch entities, read by the call poller in __init__.py.
AUTO_OPEN_KEY = "auto_open"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up 物业宝 switches based on a config entry."""
    client: WuYeBaoClient = hass.data[DOMAIN][entry.entry_id]

    try:
        gates = await client.get_gates()
        _LOGGER.info("Found %d gates for switches", len(gates))
    except Exception as err:  # noqa: BLE001 - gate list is best-effort
        _LOGGER.error("Failed to get gates for switches: %s", err)
        gates = []

    state: dict[str, bool] = hass.data[DOMAIN].setdefault(AUTO_OPEN_KEY, {})

    entities = [
        WuYeBaoAutoOpenSwitch(client, entry.entry_id, gate, state) for gate in gates
    ]
    async_add_entities(entities)


class WuYeBaoAutoOpenSwitch(SwitchEntity):
    """Visitor auto-open switch for one gate.

    When ON, the integration's call poller opens this door automatically as
    soon as a new doorbell call for the gate is detected.
    """

    _attr_has_entity_name = True
    _attr_name = "来访自动开门"
    _attr_icon = "mdi:door-open"

    def __init__(
        self,
        client: WuYeBaoClient,
        entry_id: str,
        gate: dict[str, Any],
        state: dict[str, bool],
    ) -> None:
        """Initialize the switch."""
        self._client = client
        self._gate = gate
        self._state = state
        gate_id = str(
            gate.get("id") or gate.get("uid") or gate.get("deviceNumber") or "unknown"
        )
        self._gid = gate_id
        self._attr_unique_id = f"{entry_id}_autoopen_{gate_id}"
        # Same identifiers as lock.py / camera.py so the switch mounts on the
        # gate device instead of creating a new one.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{gate_id}")},
            name=self._build_device_name(),
            manufacturer="深圳家和云联",
            model="物业宝 门禁设备",
        )

    def _build_device_name(self) -> str:
        """Build a human-friendly device name from gate data (same as lock.py)."""
        alias = self._gate.get("alias", "")
        if alias:
            return alias
        community = self._gate.get("communityName", "")
        area = self._gate.get("areaName", "")
        building = self._gate.get("buildingName", "")
        unit = self._gate.get("unitName", "")
        device_number = self._gate.get("deviceNumber", "")
        gate_type = self._gate.get("type", "")
        parts = []
        if community:
            parts.append(community)
        if area:
            parts.append(area)
        if building:
            parts.append(building)
        if unit:
            parts.append(unit)
        if gate_type == "wall":
            parts.append("围墙门")
        if device_number:
            parts.append(f"门口机{device_number}")
        return " ".join(parts) if parts else f"门禁-{device_number or self._gid}"

    @property
    def is_on(self) -> bool:
        """Return the switch state."""
        return bool(self._state.get(self._gid, False))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on (auto-open enabled for this gate)."""
        self._state[self._gid] = True
        self.async_write_ha_state()
        _LOGGER.info("Auto-open enabled for gate %s", self._gid)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off (auto-open disabled for this gate)."""
        self._state[self._gid] = False
        self.async_write_ha_state()
        _LOGGER.info("Auto-open disabled for gate %s", self._gid)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        return {
            "gate_id": self._gid,
            "device_number": self._gate.get("deviceNumber", ""),
            "gate_type": self._gate.get("type", ""),
        }
