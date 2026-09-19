"""Config flow for 物业宝 integration."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_BASE_URL,
    DEFAULT_BASE_URL,
    DOMAIN,
)
from .api import PropertyBaoClient, PropertyBaoAuthError

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_BASE_URL, default=DEFAULT_BASE_URL): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    session = aiohttp.ClientSession()
    client = PropertyBaoClient(
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        base_url=data[CONF_BASE_URL],
        session=session,
    )

    try:
        result = await client.login()
        return {
            "title": f"物业宝 ({data[CONF_USERNAME]})",
            "access_token": client.access_token,
            "refresh_token": client.refresh_token,
        }
    finally:
        await session.close()


class PropertyBaoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for 物业宝."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except PropertyBaoAuthError as err:
                _LOGGER.error("Authentication error: %s", err)
                errors["base"] = "invalid_auth"
            except Exception as err:
                _LOGGER.exception("Unexpected exception: %s", err)
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=info["title"],
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )
