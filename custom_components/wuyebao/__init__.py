"""物业宝（家和云联） integration for Home Assistant.

A dedicated integration for the JHCloud 物业宝(业主) app: phone + password
login against https://wuye.jhws.top/ (no packet capture), gate sensors and
open-door buttons.
"""

from __future__ import annotations

import logging
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .api import WuyeBaoAPI
from .const import (
    CONF_BASE_URL,
    CONF_CLIENT_ID,
    CONF_OPEN_METHOD,
    CONF_OPEN_PATH,
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_POLL_INTERVAL,
    DEFAULT_BASE_URL,
    DEFAULT_CLIENT_ID,
    DEFAULT_OPEN_METHOD,
    DEFAULT_OPEN_PATH,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)
from .coordinator import WuyeBaoCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BUTTON, Platform.SENSOR]

SERVICE_OPEN_GATE = "open_gate"
SERVICE_OPEN_GATE_SCHEMA = vol.Schema(
    {
        vol.Required("gate_id"): cv.string,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration (register services)."""
    async def _handle_open_gate(call: ServiceCall) -> None:
        """Open a gate by id through the matching config entry."""
        gate_id: str = call.data["gate_id"]
        for coordinator in list(hass.data.get(DOMAIN, {}).values()):
            gates = coordinator.data.get("gates", []) if coordinator.data else []
            if any(g["gate_id"] == gate_id for g in gates):
                await coordinator.open_gate(gate_id)
                return
        raise ValueError(f"未找到 gate_id={gate_id} 对应的门禁")

    hass.services.async_register(DOMAIN, SERVICE_OPEN_GATE, _handle_open_gate, SERVICE_OPEN_GATE_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up 物业宝 from a config entry."""
    options = {**entry.options}
    api = WuyeBaoAPI(
        hass,
        phone=entry.data[CONF_PHONE],
        password=entry.data[CONF_PASSWORD],
        base_url=options.get(CONF_BASE_URL, DEFAULT_BASE_URL),
        client_id=options.get(CONF_CLIENT_ID, DEFAULT_CLIENT_ID),
        open_path=options.get(CONF_OPEN_PATH, DEFAULT_OPEN_PATH),
        open_method=options.get(CONF_OPEN_METHOD, DEFAULT_OPEN_METHOD),
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
