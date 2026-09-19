"""API client for 物业宝."""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import aiohttp

from .const import (
    API_TOKEN,
    API_REFRESH_TOKEN,
    API_OWNER_COMMUNITY,
    API_GATES,
    DEFAULT_BASE_URL,
    DEFAULT_CLIENT_ID,
    DEFAULT_SIP_SERVER,
    DEFAULT_SIP_PORT,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "content-type": "application/json;charset=UTF-8",
    "User-Agent": "Dart/2.19 (dart:io)",
}


class PropertyBaoAuthError(Exception):
    """Authentication error."""


class PropertyBaoApiError(Exception):
    """API error."""


class PropertyBaoClient:
    """API client for 物业宝."""

    def __init__(
        self,
        username: str,
        password: str,
        base_url: str = DEFAULT_BASE_URL,
        client_id: str = DEFAULT_CLIENT_ID,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Initialize the API client."""
        self.username = username
        self.password = password
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self._session = session or aiohttp.ClientSession()

        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires: int = 0

        self.user_id: str | None = None
        self.owner_id: str | None = None
        self.community_id: str | None = None
        self.community_name: str | None = None
        self.community_code: int | None = None
        self.sip_server: str = DEFAULT_SIP_SERVER
        self.sip_port: int = DEFAULT_SIP_PORT
        self.sip_token: str | None = None

        self._device_uuid = uuid.uuid4().hex[:16]

    @property
    def access_token(self) -> str | None:
        """Return access token."""
        return self._access_token

    @property
    def refresh_token(self) -> str | None:
        """Return refresh token."""
        return self._refresh_token

    async def login(self) -> None:
        """Login with username and password."""
        url = f"{self.base_url}{API_TOKEN}"
        headers = {
            **DEFAULT_HEADERS,
            "client_id": self.client_id,
        }
        payload = {
            "account": self.username,
            "password": self.password,
            "uuid": self._device_uuid,
        }

        _LOGGER.debug("Logging in to %s", url)
        async with self._session.post(url, json=payload, headers=headers, ssl=False) as resp:
            data = await resp.json()

            if data.get("code") not in (0, 200):
                raise PropertyBaoAuthError(data.get("data", "Login failed"))

            result = data.get("data", data)
            self._access_token = result.get("access_token")
            self._refresh_token = result.get("refresh_token")
            self._token_expires = int(time.time()) + 7 * 24 * 3600

            # Decode user ID from JWT
            if self._access_token:
                import base64
                parts = self._access_token.split(".")
                if len(parts) >= 2:
                    payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
                    token_data = json.loads(base64.urlsafe_b64decode(payload_b64))
                    self.user_id = str(token_data.get("id"))

        # Get community info (optional, don't fail login if this fails)
        try:
            await self.get_owner_community()
        except Exception as err:
            _LOGGER.warning("Failed to get owner community info: %s", err)

    async def refresh_access_token(self) -> None:
        """Refresh access token."""
        if not self._refresh_token:
            raise PropertyBaoAuthError("No refresh token available")

        url = f"{self.base_url}{API_REFRESH_TOKEN}"
        headers = {
            **DEFAULT_HEADERS,
            "client_id": self.client_id,
        }
        payload = {"refresh_token": self._refresh_token}

        async with self._session.post(url, json=payload, headers=headers, ssl=False) as resp:
            data = await resp.json()

            if data.get("code") not in (0, 200):
                raise PropertyBaoAuthError(data.get("data", "Token refresh failed"))

            result = data.get("data", data)
            self._access_token = result.get("access_token")
            self._refresh_token = result.get("refresh_token")
            self._token_expires = int(time.time()) + 7 * 24 * 3600

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        json: dict | list | None = None,
    ) -> Any:
        """Make an authenticated request."""
        # Auto refresh token
        if self._access_token and self._token_expires < time.time() + 300:
            try:
                await self.refresh_access_token()
            except PropertyBaoAuthError:
                await self.login()

        url = f"{self.base_url}{endpoint}"
        headers = {
            **DEFAULT_HEADERS,
            "client_id": self.client_id,
            "Authorization": f"Bearer {self._access_token}",
        }

        async with self._session.request(
            method, url, params=params, json=json, headers=headers, ssl=False
        ) as resp:
            data = await resp.json()

            if data.get("code") == 401:
                await self.login()
                headers["Authorization"] = f"Bearer {self._access_token}"
                async with self._session.request(
                    method, url, params=params, json=json, headers=headers, ssl=False
                ) as resp:
                    data = await resp.json()

            if data.get("code") not in (0, 200):
                raise PropertyBaoApiError(data.get("data", "API error"))

            return data.get("data", data)

    async def get_owner_community(self) -> list[dict[str, Any]]:
        """Get owner community info (anonymous endpoint)."""
        url = f"{self.base_url}{API_OWNER_COMMUNITY}"
        params = {"phoneNumber": self.username}

        async with self._session.get(
            url, params=params, headers=DEFAULT_HEADERS, ssl=False
        ) as resp:
            data = await resp.json()

            if data.get("code") not in (0, 200):
                raise PropertyBaoApiError(data.get("data", "Failed to get owner info"))

            result = data.get("data", [])
            if result:
                owner = result[0] if isinstance(result, list) else result
                self.community_id = str(owner.get("communityId", ""))
                self.community_name = owner.get("communityName", "")
                self.community_code = owner.get("communityCode")
                self.owner_id = str(owner.get("id", ""))

            return result if isinstance(result, list) else []

    async def get_gates(self, unit_id: str | None = None) -> list[dict[str, Any]]:
        """Get gate/device list."""
        params = {
            "communityId": self.community_id,
            "type": "indoor",
        }
        if unit_id:
            params["unitId"] = unit_id

        result = await self._request("GET", API_GATES, params=params)
        return result if isinstance(result, list) else []

    def _build_sip_target(self, gate: dict[str, Any]) -> str:
        """Build SIP target address from gate info.

        Wall gates: GT-{communityCode}-{areaCode}-0-0-0-{deviceNumber}
        Outdoor gates: OD-{communityCode}-{buildingCode}-{unitCode}-0-0-0
        """
        gate_type = gate.get("type", "outdoor")
        community_code = self.community_code or gate.get("communityCode", 0)
        device_number = gate.get("deviceNumber", "1")

        if gate_type == "wall":
            area_code = gate.get("areaCode", 1)
            return f"GT-{community_code}-{area_code}-0-0-0-{device_number}"
        else:
            building_code = gate.get("buildingCode", 1)
            unit_code = gate.get("unitCode", 1)
            return f"OD-{community_code}-{building_code}-{unit_code}-0-0-0"

    async def open_door_sip(self, gate: dict[str, Any]) -> bool:
        """Open door via SIP MESSAGE.

        Sends a SIP MESSAGE to the gate device with unlock command.
        Requires pjsua2 library for SIP stack.
        """
        target = self._build_sip_target(gate)
        gate_type = gate.get("type", "outdoor")
        device_number = gate.get("deviceNumber", "1")

        message_body = json.dumps({
            "id": str(uuid.uuid4()),
            "type": "unlock",
            "content": {
                "device": gate_type,
                "ownerId": self.owner_id or self.user_id,
                "deviceNumber": device_number,
            }
        })

        _LOGGER.info("Sending SIP unlock to %s", target)
        _LOGGER.debug("SIP message: %s", message_body)

        # TODO: Implement SIP MESSAGE sending using pjsua2
        # This is a placeholder - actual implementation requires SIP stack
        _LOGGER.warning(
            "SIP door open is not yet fully implemented. "
            "Target: sip:%s@%s, Message: %s",
            target,
            self.sip_server,
            message_body,
        )
        return True

    async def async_close(self) -> None:
        """Close the session."""
        await self._session.close()
