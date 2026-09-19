"""The 物业宝 integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_BASE_URL,
    DEFAULT_BASE_URL,
    DOMAIN,
)
from .api import PropertyBaoClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.LOCK, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up 物业宝 from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    session = async_get_clientsession(hass)
    client = PropertyBaoClient(
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        base_url=entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL),
        session=session,
    )

    # Login to get tokens
    try:
        await client.login()
        _LOGGER.info("Successfully logged in to 物业宝 as %s", client.username)
    except Exception as err:
        _LOGGER.error("Failed to login: %s", err)
        return False

    # Get default owner info (community)
    try:
        await client.get_default_owner(client.username)
        _LOGGER.info("Community: %s (ID: %s)", client.community_name, client.community_id)
    except Exception as err:
        _LOGGER.warning("Failed to get owner info: %s", err)

    hass.data[DOMAIN][entry.entry_id] = client

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
