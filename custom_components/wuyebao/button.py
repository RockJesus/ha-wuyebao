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
from .api import WuYeBaoClient, WuYeBaoApiError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up 物业宝 buttons based on a config entry."""
    client: WuYeBaoClient = hass.data[DOMAIN][entry.entry_id]

    try:
        gates = await client.ensure_gates()
    except Exception as err:
        _LOGGER.error("Failed to get gates for buttons: %s", err)
        gates = []

    manager = hass.data[DOMAIN].get("video_manager")

    entities: list[ButtonEntity] = [
        # One "生成访客码" button mounted on the hub device (visitor codes
        # are bound to the owner's unit, not to a single gate).
        WuYeBaoVisitorButton(client, entry.entry_id, hass),
        # "户户通呼叫" button mounted on the hub device: calls the owner's
        # own indoor unit (RM-...) so the intercom session is established.
        WuYeBaoHouseholdButton(client, entry.entry_id, hass, manager),
    ]

    # One "呼叫电梯" button per unit door (outdoor device): the app targets
    # the unit door's OD URI with a call_elevator MESSAGE body.
    for gate in gates:
        if gate.get("type") == "outdoor":
            entities.append(WuYeBaoElevatorButton(client, entry.entry_id, gate))

    async_add_entities(entities)


def _gate_id(gate: dict[str, Any]) -> str:
    """Compute a stable gate id (must match lock.py / camera.py)."""
    return str(
        gate.get("id") or gate.get("uid") or gate.get("deviceNumber") or "unknown"
    )


def _build_device_info(entry_id: str, gate: dict[str, Any]) -> DeviceInfo:
    """Device info must match lock.py so entities merge under one device."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_{_gate_id(gate)}")},
        name="物业宝 门禁",
        manufacturer="深圳家和云联",
        model="物业宝 云门禁",
        sw_version="1.1.1.51",
    )


def _hub_device_info(entry_id: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_hub")},
        name="物业宝 主站",
        manufacturer="深圳家和云联",
        model="物业宝 集成",
    )


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
        self._attr_device_info = _hub_device_info(entry_id)

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


class WuYeBaoElevatorButton(ButtonEntity):
    """Call the elevator of this unit door (SIP MESSAGE call_elevator).

    The app sends the request to the same OD URI used for unlocking the
    unit door, with body {"id":null,"type":"call_elevator",
    "content":{"room":"2702"}} - room is the owner's flat number.
    """

    _attr_has_entity_name = True
    _attr_name = "呼叫电梯"
    _attr_icon = "mdi:elevator"

    def __init__(
        self,
        client: WuYeBaoClient,
        entry_id: str,
        gate: dict[str, Any],
    ) -> None:
        """Initialize the button."""
        self._client = client
        self._entry_id = entry_id
        self._gate = gate
        self._gid = _gate_id(gate)
        self._attr_unique_id = f"{entry_id}_elevator_{self._gid}"
        self._attr_device_info = _build_device_info(entry_id, gate)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Gate context for debugging."""
        return {
            "gate_id": self._gid,
            "device_number": self._gate.get("deviceNumber", ""),
            "room": self._client.room or "",
        }

    async def async_press(self) -> None:
        """Call the elevator."""
        if not self._client.room:
            raise RuntimeError(
                "房间号自动获取失败：请重新登录集成（房间号由登录后的绑定编码自动解析）"
            )
        result = await self._client.call_elevator_sip(self._gate)
        _LOGGER.info(
            "Elevator call OK for gate %s (OD=%s, room=%s)",
            self._gid,
            result.get("od_uri"),
            self._client.room,
        )


class WuYeBaoHouseholdButton(ButtonEntity):
    """Call the owner's own indoor unit (户户通).

    Sends a SIP INVITE to the RM-... indoor-unit URI (captured from the
    app's pcap: RM-<community>-<area>-<building>-<unit>-<floor>-<room>).
    The intercom media (H264 RTP) is exposed on the built-in RTSP server:
        rtsp://<haos-host>:8556/rm-<room>
    so you can watch the indoor unit's camera / speak via go2rtc etc.
    """

    _attr_has_entity_name = True
    _attr_name = "户户通呼叫"
    _attr_icon = "mdi:phone-in-talk"

    def __init__(
        self,
        client: WuYeBaoClient,
        entry_id: str,
        hass: HomeAssistant,
        manager: Any = None,
    ) -> None:
        """Initialize the button."""
        self._client = client
        self._entry_id = entry_id
        self._hass = hass
        self._manager = manager
        self._attr_unique_id = f"{entry_id}_household_button"
        self._attr_device_info = _hub_device_info(entry_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the RM URI and RTSP URL for go2rtc / ffmpeg."""
        gates: list[dict[str, Any]] = []
        try:
            gates = self._client.hub_data.get("gates") or []
        except Exception:  # noqa: BLE001
            pass
        gate = self._client.find_own_gate(gates) if gates else None
        rm_uri = self._client.build_household_uri(gate) if gate else None
        room = self._client.room or ""
        return {
            "room": room,
            "rm_uri": rm_uri or "",
            "rtsp_url": f"rtsp://127.0.0.1:8556/rm-{room}" if room else "",
            "state": "呼叫后建立会话，用 rtsp_url 观看室内机画面",
        }

    async def async_press(self) -> None:
        """Call the indoor unit (户户通)."""
        if not self._client.room:
            raise RuntimeError(
                "房间号自动获取失败：请重新登录集成（房间号由登录后的绑定编码自动解析）"
            )
        if self._manager is None:
            raise RuntimeError("视频管理器未就绪：请重新加载集成")

        gates: list[dict[str, Any]] = []
        try:
            gates = await self._client.get_gates()
            self._client.hub_data["gates"] = gates
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed to get gates: %s", err)

        gate = self._client.find_own_gate(gates)
        rm_uri = self._client.build_household_uri(gate)
        if not rm_uri:
            raise RuntimeError("无法构建户户通目标：请检查房间号配置")

        _LOGGER.info("户户通 calling %s (room=%s)", rm_uri, self._client.room)
        call = await self._manager.start_household(
            f"rm-{self._client.room}", rm_uri, self._client
        )
        if call is None:
            raise RuntimeError("户户通呼叫失败：室内机未应答或无视频（请查看日志）")
        _LOGGER.info(
            "户户通 established: %s | rtsp://127.0.0.1:8556/rm-%s",
            rm_uri,
            self._client.room,
        )
