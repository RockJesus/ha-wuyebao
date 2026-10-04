"""The 物业宝 integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import entity_registry as er

from .const import CONF_PASSWORD, CONF_USERNAME, DOMAIN
from .api import WuYeBaoClient
from .rtsp_server import RtspServer, VideoSessionManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LOCK,
    Platform.SENSOR,
    Platform.CAMERA,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up 物业宝 from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    session = async_get_clientsession(hass)
    client = WuYeBaoClient(
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        session=session,
    )

    try:
        await client.login()
        _LOGGER.info("Logged in as %s, community: %s", client.username, client.community_name)
        _LOGGER.info("SIP token: %s", "obtained" if client.sip_jwt else "not available")
        # Fetch owner info (contains bindingCode for camera call records)
        try:
            await client.get_owners()
        except Exception as err:
            _LOGGER.warning("Failed to get owner info: %s", err)
    except Exception as err:
        _LOGGER.error("Failed to login: %s", err)
        return False

    hass.data[DOMAIN][entry.entry_id] = client

    # Remove legacy "查看监控" button entities (button platform removed in
    # 6.6.6; live monitoring is exposed directly by the camera entities).
    ent_reg = er.async_get(hass)
    for entity_id, ent in list(ent_reg.entities.items()):
        if (
            ent.config_entry_id == entry.entry_id
            and ent.unique_id
            and ent.unique_id.startswith(f"{entry.entry_id}_monitor_")
        ):
            ent_reg.async_remove(entity_id)

    # Live video: RTSP server + session manager (one per integration instance)
    manager = VideoSessionManager()
    rtsp_server = RtspServer(manager)
    hass.data[DOMAIN]["video_manager"] = manager
    hass.data[DOMAIN]["rtsp_server"] = rtsp_server
    if not await rtsp_server.start():
        _LOGGER.warning(
            "RTSP server could not bind %s:%s - live camera streams disabled",
            rtsp_server._host,
            rtsp_server._port,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # stop live video sessions / RTSP server
        rtsp_server = hass.data[DOMAIN].get("rtsp_server")
        manager = hass.data[DOMAIN].get("video_manager")
        if manager:
            await manager.shutdown()
        if rtsp_server:
            await rtsp_server.stop()
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
