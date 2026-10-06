"""Switch platform for 物业宝 visitor auto-open."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .api import WuYeBaoClient

_LOGGER = logging.getLogger(__name__)

# Keep strong references to backfill tasks so they are not garbage-collected.
_BACKFILL_TASKS: list[asyncio.Task] = []


def _schedule_backfill(coro: Any) -> None:
    """Schedule a gate-list backfill coroutine and keep it alive."""
    task = asyncio.ensure_future(coro)
    _BACKFILL_TASKS.append(task)
    task.add_done_callback(_BACKFILL_TASKS.remove)

# Shared state store under hass.data[DOMAIN]["auto_open"]:
#   {entry_id: {gate_id: bool}}  - per-gate "visitor auto open" switch state,
#   isolated per config entry so multiple users never share each other's state.
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
        gates = await client.ensure_gates()
        _LOGGER.info("Found %d gates for switches", len(gates))
    except Exception as err:  # noqa: BLE001 - gate list is best-effort
        _LOGGER.error("Failed to get gates for switches: %s", err)
        gates = []

    store: dict[str, dict[str, bool]] = hass.data[DOMAIN].setdefault(AUTO_OPEN_KEY, {})
    state: dict[str, bool] = store.setdefault(entry.entry_id, {})

    entities = [
        WuYeBaoAutoOpenSwitch(client, entry.entry_id, gate, state) for gate in gates
    ]
    async_add_entities(entities)

    if not gates:
        # Transient API hiccup right after HA restart: retry later so the
        # per-gate switches are created once the cached list becomes available.
        _schedule_backfill(
            _retry_add_switches(hass, client, entry.entry_id, state, async_add_entities)
        )


async def _retry_add_switches(
    hass: HomeAssistant,
    client: WuYeBaoClient,
    entry_id: str,
    state: dict[str, bool],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Re-add switch entities after a transient empty gate list at setup."""
    for delay in (45, 120, 300):
        await asyncio.sleep(delay)
        try:
            gates = await client.ensure_gates()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Retry switches: failed to fetch gates: %s", err)
            continue
        if not gates:
            _LOGGER.warning("Retry switches: gate list still empty (delay %ss)", delay)
            continue
        _LOGGER.info("Retry switches: adding %d auto-open switches", len(gates))
        async_add_entities(
            WuYeBaoAutoOpenSwitch(client, entry_id, gate, state) for gate in gates
        )
        return


class WuYeBaoAutoOpenSwitch(RestoreEntity, SwitchEntity):
    """Visitor auto-open switch for one gate.

    When ON, the integration's call poller opens this door automatically as
    soon as a new doorbell call for the gate is detected.

    The switch state is persisted via RestoreEntity so it survives HAOS
    restarts (restored on async_added_to_hass).
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

    async def async_added_to_hass(self) -> None:
        """Restore the persisted switch state (survives HAOS restarts)."""
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._state[self._gid] = last.state == "on"

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
