"""The 物业宝 integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

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
    Platform.SWITCH,
    Platform.CAMERA,
]

# Visitor auto-open: poll call records and open doors whose switch is ON.
CALL_POLL_INTERVAL = 3.0


async def _visitor_call_poller(
    hass: HomeAssistant,
    client: WuYeBaoClient,
    auto_open_state: dict[str, bool],
    gates: list[dict[str, Any]],
) -> None:
    """Poll call records; auto-open gates whose "来访自动开门" switch is ON.

    On startup the poller only learns the current call tokens (so an old call
    is never re-triggered).  Afterwards, whenever a NEW call token appears for
    a gate and that gate's switch is ON, the door is opened immediately.
    """
    _LOGGER.info("Visitor auto-open poller started for %d gates", len(gates))
    seen: dict[str, str] = {}
    while True:
        try:
            per_gate = await client.get_latest_calls_per_gate(gates)
            for gid, call in per_gate.items():
                token = client._call_token(call)
                prev = seen.get(gid)
                if prev is None:
                    seen[gid] = token
                    continue
                if token != prev:
                    seen[gid] = token
                    if auto_open_state.get(gid):
                        gate = next(
                            (
                                g
                                for g in gates
                                if str(
                                    g.get("id")
                                    or g.get("uid")
                                    or g.get("deviceNumber")
                                    or "unknown"
                                )
                                == gid
                            ),
                            None,
                        )
                        if gate is None:
                            continue
                        _LOGGER.info("Visitor call at gate %s -> auto open", gid)
                        try:
                            await client.open_door_sip(gate)
                        except Exception as err:  # noqa: BLE001
                            _LOGGER.warning("Auto-open gate %s failed: %s", gid, err)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - polling must never die
            _LOGGER.debug("Visitor call poll error: %s", err)
        await asyncio.sleep(CALL_POLL_INTERVAL)


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
            and (
                ent.unique_id.startswith(f"{entry.entry_id}_monitor_")
                # legacy "流地址" sensors removed in 6.6.8 (RTSP URL now
                # exposed as an attribute on the 实时监控 camera entity)
                or ent.unique_id.startswith(f"{entry.entry_id}_stream_")
            )
        ):
            ent_reg.async_remove(entity_id)

    # Live video: RTSP server + session manager.  Shared across config
    # entries (multiple user accounts) so the second entry reuses the already
    # bound server instead of fighting for the same port.
    manager: VideoSessionManager | None = hass.data[DOMAIN].get("video_manager")
    if manager is None:
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
        else:
            # Watchdog: rebuild monitor sessions whose RTP stream went silent
            # (gates stop pushing video after ~30s -> picture freezes).
            manager.start_watchdog()

    # Visitor auto-open: per-gate switch state (isolated per config entry so
    # multiple users never share state) + call poller.
    auto_open_store: dict[str, dict[str, bool]] = hass.data[DOMAIN].setdefault("auto_open", {})
    auto_open_state: dict[str, bool] = auto_open_store.setdefault(entry.entry_id, {})
    try:
        poll_gates = await client.get_gates()
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Failed to get gates for auto-open poller: %s", err)
        poll_gates = []
    poll_task = entry.async_create_background_task(
        hass,
        _visitor_call_poller(hass, client, auto_open_state, poll_gates),
        "wuyebao-visitor-auto-open",
    )
    hass.data[DOMAIN]["visitor_poll_task"] = poll_task

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # stop the visitor auto-open poller
        poll_task = hass.data[DOMAIN].get("visitor_poll_task")
        if poll_task:
            poll_task.cancel()
        # stop live video sessions / RTSP server
        rtsp_server = hass.data[DOMAIN].get("rtsp_server")
        manager = hass.data[DOMAIN].get("video_manager")
        if manager:
            await manager.shutdown()
        if rtsp_server:
            await rtsp_server.stop()
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
