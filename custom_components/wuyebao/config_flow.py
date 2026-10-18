"""Config flow for 物业宝 integration."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_PASSWORD, CONF_USERNAME, DOMAIN
from .api import WuYeBaoClient, WuYeBaoAuthError

_LOGGER = logging.getLogger(__name__)

# Login only needs username + password.  The owner's flat number (room) is
# derived automatically from the bindingCode returned by the owners API, so
# there is no manual 房间号 field anymore.
STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class WuYeBaoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for 物业宝."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        return await self._show_login_form(user_input)

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Re-authenticate when the stored credentials stop working."""
        return await self._show_login_form(user_input, reauth=True)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Re-configure an existing entry (HA 2026.x frontend default)."""
        return await self._show_login_form(user_input, reconfigure=True)

    async def _show_login_form(
        self,
        user_input: dict[str, Any] | None,
        reauth: bool = False,
        reconfigure: bool = False,
    ) -> FlowResult:
        """Shared login form for initial setup / reauth / reconfigure."""
        errors: dict[str, str] = {}

        if user_input is not None:
            session = aiohttp.ClientSession()
            client = WuYeBaoClient(
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                session=session,
            )

            try:
                await client.login()
                await session.close()
                if reauth:
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(), data=user_input
                    )
                if reconfigure:
                    return self.async_update_reload_and_abort(
                        self._get_reconfigure_entry(), data=user_input
                    )
                title = f"物业宝 ({client.community_name or user_input[CONF_USERNAME]})"
                return self.async_create_entry(title=title, data=user_input)
            except WuYeBaoAuthError as err:
                _LOGGER.error("Auth error: %s", err)
                errors["base"] = "invalid_auth"
            except Exception as err:
                _LOGGER.exception("Unexpected error: %s", err)
                errors["base"] = "unknown"
            finally:
                await session.close()

        defaults: dict[str, Any] = {}
        if reauth:
            entry = self._get_reauth_entry()
            if entry and CONF_USERNAME in (entry.data or {}):
                defaults[CONF_USERNAME] = entry.data[CONF_USERNAME]
        elif reconfigure:
            entry = self._get_reconfigure_entry()
            if entry and CONF_USERNAME in (entry.data or {}):
                defaults[CONF_USERNAME] = entry.data[CONF_USERNAME]

        schema = STEP_USER_DATA_SCHEMA
        if defaults:
            schema = self.add_suggested_values_to_schema(schema, defaults)

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )
