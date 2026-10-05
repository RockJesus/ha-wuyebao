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
from .api import WuYeBaoClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up 物业宝 buttons based on a config entry."""
    client: WuYeBaoClient = hass.data[DOMAIN][entry.entry_id]

    # One "生成访客码" button mounted on the hub device (visitor codes are
    # bound to the owner's unit, not to a single gate).
    async_add_entities([WuYeBaoVisitorButton(client, entry.entry_id, hass)])


class WuYeBaoVisitorButton(ButtonEntity):
    """Button to create a visitor invite and obtain a 6-digit door code.

    The generated code is valid for 1 hour (matching the app default) and is
    shown in a persistent notification plus cached on the client for the
    访客邀请 sensor.
    """

    _attr_has_entity_name = True
    _attr_name = "生成访客码"
    _attr_icon = "mdi:account-key-plus"

    def __init__(
        self,
        client: WuYeBaoClient,
        entry_id: str,
        hass: HomeAssistant,
    ) -> None:
        """Initialize the button."""
        self._client = client
        self._entry_id = entry_id
        self._hass = hass
        self._attr_unique_id = f"{entry_id}_visitor_button"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_hub")},
            name="物业宝 主站",
            manufacturer="深圳家和云联",
            model="物业宝 集成",
        )

    async def async_press(self) -> None:
        """Create a visitor invite (1 hour validity) and notify the code."""
        result = await self._client.create_invite_visitor()
        if not result:
            raise RuntimeError("创建访客码失败：请检查 HA 日志")

        password = result.get("password")
        start = result.get("startTime")
        end = result.get("endTime")
        _LOGGER.info("Visitor code created: %s", password)

        # Refresh the cached visitor list so the 访客邀请 sensor picks it up.
        try:
            self._client.hub_data["visitors"] = await self._client.get_invite_visitors()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed to refresh visitor list: %s", err)

        try:
            from homeassistant.components import persistent_notification

            persistent_notification.async_create(
                self._hass,
                f"访客开门密码：**{password}**\n\n有效期至：{end}",
                title="物业宝 访客码已生成",
                notification_id=f"wuyebao_visitor_{self._entry_id}",
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Persistent notification failed: %s", err)
