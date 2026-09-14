"""物业宝 (WuyeBao) integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .api import WuyeBaoAPI
from .const import (
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)
from .coordinator import WuyeBaoCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BUTTON, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up 物业宝 from a config entry."""
    options = {**entry.options}
    api = WuyeBaoAPI(
        hass,
        phone=entry.data[CONF_PHONE],
        password=entry.data[CONF_PASSWORD],
        **options,
    )
    coordinator = WuyeBaoCoordinator(
        hass,
        entry=entry,
        api=api,
        poll_interval=int(options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)),
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
