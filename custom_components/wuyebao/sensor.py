"""Sensor platform: last open result and device count."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTR_DEVICES, ATTR_LAST_OPEN, DOMAIN
from .coordinator import WuyeBaoCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the status sensors."""
    coordinator: WuyeBaoCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            WuyeBaoLastOpenSensor(coordinator, entry),
            WuyeBaoDeviceCountSensor(coordinator, entry),
        ]
    )


class WuyeBaoLastOpenSensor(CoordinatorEntity[WuyeBaoCoordinator], SensorEntity):
    """Shows the result of the most recent open-door action."""

    _attr_has_entity_name = True
    _attr_name = "最近开门结果"
    _attr_icon = "mdi:door-open"

    def __init__(self, coordinator: WuyeBaoCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_last_open"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="物业宝",
            manufacturer="物业宝",
            model="云门禁",
        )

    @property
    def native_value(self) -> str:
        """State of the last open action."""
        last_open = self.coordinator.data.get(ATTR_LAST_OPEN) if self.coordinator.data else None
        if last_open is None:
            return "未操作"
        return "成功" if last_open.get("result") == "success" else "失败"

    @property
    def extra_state_attributes(self) -> dict:
        """Details of the last open action."""
        last_open = self.coordinator.data.get(ATTR_LAST_OPEN) if self.coordinator.data else None
        if last_open is None:
            return {}
        return {
            "device_id": last_open.get("device_id"),
            "time": last_open.get("time"),
        }


class WuyeBaoDeviceCountSensor(CoordinatorEntity[WuyeBaoCoordinator], SensorEntity):
    """Number of doors/gates discovered."""

    _attr_has_entity_name = True
    _attr_name = "门禁设备数"
    _attr_icon = "mdi:counter"

    def __init__(self, coordinator: WuyeBaoCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_device_count"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="物业宝",
            manufacturer="物业宝",
            model="云门禁",
        )

    @property
    def native_value(self) -> int:
        devices = self.coordinator.data.get(ATTR_DEVICES, []) if self.coordinator.data else []
        return len(devices)
