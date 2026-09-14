"""Sensor platform: gate info sensors, owner sensor and status sensors."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTR_GATES, ATTR_LAST_OPEN, ATTR_OWNER, DOMAIN
from .coordinator import WuyeBaoCoordinator

_LOGGER = logging.getLogger(__name__)


def _gate_attributes(gate: dict) -> dict:
    """Pick a few readable attributes from a gate record."""
    raw = gate.get("raw") or {}
    attrs: dict[str, object] = {}
    for key, label in (
        ("communityName", "小区"),
        ("community", "小区"),
        ("buildingName", "楼栋"),
        ("building", "楼栋"),
        ("unitName", "单元"),
        ("unit", "单元"),
        ("floorName", "楼层"),
        ("floor", "楼层"),
        ("roomName", "房间"),
        ("room", "房间"),
        ("callNumber", "对讲号码"),
        ("deviceNumber", "设备编号"),
        ("contactNumber", "联系电话"),
        ("deviceType", "设备类型"),
        ("status", "状态"),
        ("lockState", "锁状态"),
    ):
        value = raw.get(key)
        if value is not None and str(value) not in ("", "null"):
            attrs[label] = value
    return attrs


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensors."""
    coordinator: WuyeBaoCoordinator = hass.data[DOMAIN][entry.entry_id]
    gates = coordinator.data.get(ATTR_GATES, []) if coordinator.data else []
    entities: list[SensorEntity] = [
        WuyeBaoOwnerSensor(coordinator, entry),
        WuyeBaoLastOpenSensor(coordinator, entry),
        WuyeBaoGateCountSensor(coordinator, entry),
    ]
    entities.extend(WuyeBaoGateSensor(coordinator, entry, gate) for gate in gates)
    async_add_entities(entities)


class WuyeBaoOwnerSensor(CoordinatorEntity[WuyeBaoCoordinator], SensorEntity):
    """Shows the current owner account info."""

    _attr_has_entity_name = True
    _attr_name = "业主"
    _attr_icon = "mdi:account"

    def __init__(self, coordinator: WuyeBaoCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_owner"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="物业宝",
            manufacturer="深圳家和云联",
            model="云门禁",
        )

    @property
    def native_value(self) -> str:
        owner = self.coordinator.data.get(ATTR_OWNER) if self.coordinator.data else None
        if not owner:
            return "未知"
        for key in ("name", "ownerName", "realName", "phoneNumber", "phone"):
            if owner.get(key):
                return str(owner[key])
        return "未知"

    @property
    def extra_state_attributes(self) -> dict:
        owner = self.coordinator.data.get(ATTR_OWNER) if self.coordinator.data else None
        return dict(owner) if owner else {}


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
            manufacturer="深圳家和云联",
            model="云门禁",
        )

    @property
    def native_value(self) -> str:
        last_open = self.coordinator.data.get(ATTR_LAST_OPEN) if self.coordinator.data else None
        if last_open is None:
            return "未操作"
        return "成功" if last_open.get("result") == "success" else "失败"

    @property
    def extra_state_attributes(self) -> dict:
        last_open = self.coordinator.data.get(ATTR_LAST_OPEN) if self.coordinator.data else None
        if last_open is None:
            return {}
        return {
            "gate_id": last_open.get("gate_id"),
            "time": last_open.get("time"),
        }


class WuyeBaoGateCountSensor(CoordinatorEntity[WuyeBaoCoordinator], SensorEntity):
    """Number of gates discovered."""

    _attr_has_entity_name = True
    _attr_name = "门禁数量"
    _attr_icon = "mdi:counter"

    def __init__(self, coordinator: WuyeBaoCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_gate_count"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="物业宝",
            manufacturer="深圳家和云联",
            model="云门禁",
        )

    @property
    def native_value(self) -> int:
        gates = self.coordinator.data.get(ATTR_GATES, []) if self.coordinator.data else []
        return len(gates)


class WuyeBaoGateSensor(CoordinatorEntity[WuyeBaoCoordinator], SensorEntity):
    """A per-gate info sensor."""

    _attr_has_entity_name = False

    def __init__(
        self,
        coordinator: WuyeBaoCoordinator,
        entry: ConfigEntry,
        gate: dict,
    ) -> None:
        super().__init__(coordinator)
        self._gate_id = gate["gate_id"]
        self._attr_unique_id = f"{entry.entry_id}_gate_{self._gate_id}_info"
        self._attr_name = f"{gate['name']} 状态"
        self._attr_icon = "mdi:door"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{self._gate_id}")},
            name=gate["name"],
            manufacturer="深圳家和云联",
            model="物业宝 云门禁",
        )

    @property
    def native_value(self) -> str:
        raw = next(
            (
                g.get("raw")
                for g in (self.coordinator.data.get(ATTR_GATES, []) if self.coordinator.data else [])
                if g.get("gate_id") == self._gate_id
            ),
            None,
        )
        if raw:
            for key in ("status", "lockState", "state", "online"):
                value = raw.get(key)
                if value is not None and str(value) not in ("", "null"):
                    return str(value)
        return "未知"

    @property
    def extra_state_attributes(self) -> dict:
        raw = next(
            (
                g.get("raw")
                for g in (self.coordinator.data.get(ATTR_GATES, []) if self.coordinator.data else [])
                if g.get("gate_id") == self._gate_id
            ),
            None,
        )
        return _gate_attributes({"raw": raw}) if raw else {}
