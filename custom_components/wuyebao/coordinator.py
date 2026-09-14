"""Data coordinator for the WuyeBao integration."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import WuyeBaoAPI, WuyeBaoAuthError, WuyeBaoConnectionError
from .const import ATTR_DEVICES, ATTR_LAST_OPEN, DOMAIN

_LOGGER = logging.getLogger(__name__)


class WuyeBaoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll the WuyeBao device list and keep the auth token fresh."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: WuyeBaoAPI,
        poll_interval: int = 60,
    ) -> None:
        self.api = api
        self.entry = entry
        self.token: str | None = None
        self.last_open: dict[str, Any] | None = None
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=max(10, poll_interval)),
        )

    async def _ensure_token(self) -> str:
        """Return a valid token, logging in first if needed."""
        if self.token is None:
            self.token = await self.api.login()
        return self.token

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch the device list (and re-login when the token expires)."""
        try:
            token = await self._ensure_token()
        except WuyeBaoAuthError as err:
            # Login itself failed -> the stored credentials are wrong.
            raise ConfigEntryAuthFailed(f"物业宝登录失败: {err}") from err

        try:
            devices = await self.api.get_devices(token)
        except WuyeBaoAuthError:
            # Token expired: drop it and retry once with a fresh login.
            _LOGGER.info("物业宝 token 已失效，尝试重新登录")
            self.token = None
            try:
                token = await self._ensure_token()
                devices = await self.api.get_devices(token)
            except WuyeBaoAuthError as err:
                raise ConfigEntryAuthFailed(f"物业宝重新登录失败: {err}") from err
        except WuyeBaoConnectionError as err:
            raise UpdateFailed(f"获取设备列表失败: {err}") from err

        return {ATTR_DEVICES: devices, ATTR_LAST_OPEN: self.last_open}

    async def open_door(self, device_id: str) -> None:
        """Open a door and record the result."""
        try:
            token = await self._ensure_token()
            await self.api.open_door(token, device_id)
        except WuyeBaoAuthError:
            # Token expired: re-login once and retry.
            self.token = None
            token = await self._ensure_token()
            await self.api.open_door(token, device_id)
        self.last_open = {
            "device_id": device_id,
            "result": "success",
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        devices = self.data.get(ATTR_DEVICES, []) if self.data else []
        self.async_set_updated_data({ATTR_DEVICES: devices, ATTR_LAST_OPEN: self.last_open})
