"""Data coordinator for the 物业宝（家和云联） integration.

Keeps the access token fresh (refresh -> re-login), polls the gate list and
exposes the owner info for the sensors/buttons.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import WuyeBaoAPI, WuyeBaoAuthError, WuyeBaoConnectionError
from .const import ATTR_GATES, ATTR_LAST_OPEN, ATTR_OWNER, DOMAIN

_LOGGER = logging.getLogger(__name__)


class WuyeBaoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll the gate list and keep the auth token fresh."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: WuyeBaoAPI,
        poll_interval: int = 60,
    ) -> None:
        self.api = api
        self.entry = entry
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self.last_open: dict[str, Any] | None = None
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=max(10, poll_interval)),
        )

    async def _ensure_token(self) -> str:
        """Return a valid access token, refreshing or logging in as needed."""
        if self._access_token is not None:
            return self._access_token

        if self._refresh_token:
            try:
                access, refresh = await self.api.refresh(self._refresh_token)
                self._access_token = access
                self._refresh_token = refresh
                return access
            except WuyeBaoAuthError:
                _LOGGER.info("物业宝刷新令牌已失效，改用账号密码重新登录")
                self._refresh_token = None
            except WuyeBaoConnectionError:
                _LOGGER.debug("物业宝刷新令牌请求失败，改用账号密码登录")

        access, refresh = await self.api.login()
        self._access_token = access
        self._refresh_token = refresh
        return access

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch the gate list (and refresh credentials when the token expires)."""
        try:
            token = await self._ensure_token()
        except WuyeBaoAuthError as err:
            raise ConfigEntryAuthFailed(f"物业宝登录失败: {err}") from err

        try:
            gates = await self.api.get_gates(token)
            owners = await self.api.get_owners(token)
        except WuyeBaoAuthError:
            # Token expired: drop it and retry once with fresh credentials.
            _LOGGER.info("物业宝 token 已失效，尝试重新登录")
            self._access_token = None
            try:
                token = await self._ensure_token()
                gates = await self.api.get_gates(token)
                owners = await self.api.get_owners(token)
            except WuyeBaoAuthError as err:
                raise ConfigEntryAuthFailed(f"物业宝重新登录失败: {err}") from err
        except WuyeBaoConnectionError as err:
            raise UpdateFailed(f"获取门禁列表失败: {err}") from err

        owner: dict[str, Any] | None = None
        if owners:
            owner = dict(owners[0])
            owner.pop("raw", None)

        return {ATTR_GATES: gates, ATTR_OWNER: owner, ATTR_LAST_OPEN: self.last_open}

    async def open_gate(self, gate_id: str) -> None:
        """Open a gate and record the result."""
        result = "success"
        try:
            token = await self._ensure_token()
            await self.api.open_gate(token, gate_id)
        except WuyeBaoAuthError:
            self._access_token = None
            try:
                token = await self._ensure_token()
                await self.api.open_gate(token, gate_id)
            except WuyeBaoAuthError:
                result = "auth_failed"
                raise
            except Exception as err:  # noqa: BLE001 - record and re-raise
                result = "failed"
                raise
        except Exception as err:  # noqa: BLE001 - record and re-raise
            result = "failed"
            raise
        finally:
            self.last_open = {
                "gate_id": gate_id,
                "result": result,
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            data = dict(self.data) if self.data else {}
            data[ATTR_LAST_OPEN] = self.last_open
            self.async_set_updated_data(data)
