"""SIP Video client for 物业宝 monitoring."""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

_LOGGER = logging.getLogger(__name__)


class PropertyBaoSipVideoClient:
    """SIP Video client for monitoring."""

    def __init__(
        self,
        user: str,
        jwt: str,
        sid: str,
        host: str = "new-sip.jhws.top",
        port: int = 58583,
    ) -> None:
        """Initialize SIP video client."""
        self.user = user
        self.jwt = jwt
        self.sid = sid
        self.host = host
        self.port = port

    async def start_monitor(self, gate: dict[str, Any], owner_id: str) -> dict[str, Any]:
        """Start monitoring by sending SIP INVITE."""
        # TODO: Implement SIP INVITE for video monitoring
        # This requires:
        # 1. SIP REGISTER (already have)
        # 2. Build SDP offer (H.264 video)
        # 3. Send SIP INVITE
        # 4. Wait for 200 OK
        # 5. Send ACK
        # 6. Start receiving RTP video stream

        gate_type = gate.get("type", "outdoor")
        community_code = str(gate.get("communityCode", "0"))
        area_code = str(gate.get("areaCode", "0"))
        building_code = str(gate.get("buildingCode", "0"))
        unit_code = str(gate.get("unitCode", "0"))
        floor_code = str(gate.get("floorCode", "0"))
        device_number = str(gate.get("deviceNumber", ""))

        # Build target URI
        if gate_type == "wall":
            target = f"GT-{community_code}-{area_code}-0-0-0-{device_number}"
        else:
            target = f"OD-{community_code}-{building_code}-{unit_code}-0-0-0"

        _LOGGER.info("Starting monitor call to %s", target)
        _LOGGER.debug("Gate: %s", gate)

        # Placeholder - actual implementation requires pjsua2 or similar
        return {
            "status": "not_implemented",
            "target": target,
            "message": "SIP video monitoring requires pjsua2 library (not yet implemented)",
        }

    async def stop_monitor(self) -> None:
        """Stop monitoring call."""
        _LOGGER.info("Stopping monitor call")
        # TODO: Send BYE
