"""API client for 物业宝."""
from __future__ import annotations

import asyncio
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
    API_DEVICE_GATES,
    API_NOTICE_LIST,
    API_ALARM_LIST,
    DEFAULT_BASE_URL,
    DEFAULT_CLIENT_ID,
    DEFAULT_SIP_SERVER,
    DEFAULT_SIP_PORT,
)

_LOGGER = logging.getLogger(__name__)


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
        sip_server: str = DEFAULT_SIP_SERVER,
        sip_port: int = DEFAULT_SIP_PORT,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Initialize the API client."""
        self.username = username
        self.password = password
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.sip_server = sip_server
        self.sip_port = sip_port
        self._session = session or aiohttp.ClientSession()

        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._sip_token: str | None = None
        self._token_expires: int = 0
        self._user_id: str | None = None
        self._owner_id: str | None = None
        self._community_id: str | None = None
        self._community_name: str | None = None
        self._community_code: int | None = None
        self._device_uuid = str(uuid.uuid4()).replace("-", "")[:16]

    @property
    def access_token(self) -> str | None:
        """Return access token."""
        return self._access_token

    @property
    def refresh_token(self) -> str | None:
        """Return refresh token."""
        return self._refresh_token

    @property
    def sip_token(self) -> str | None:
        """Return SIP token."""
        return self._sip_token

    @property
    def user_id(self) -> str | None:
        """Return user ID."""
        return self._user_id

    @property
    def owner_id(self) -> str | None:
        """Return owner ID."""
        return self._owner_id

    @property
    def community_id(self) -> str | None:
        """Return community ID."""
        return self._community_id

    @property
    def community_name(self) -> str | None:
        """Return community name."""
        return self._community_name

    async def login(self) -> dict[str, Any]:
        """Login with username and password."""
        url = f"{self.base_url}{API_TOKEN}"
        headers = {
            "Content-Type": "application/json",
            "client_id": self.client_id,
            "User-Agent": "Dart/2.19 (dart:io)",
        }
        payload = {
            "account": self.username,
            "password": self.password,
            "uuid": self._device_uuid,
        }

        _LOGGER.debug("Logging in to %s", url)
        async with self._session.post(url, json=payload, headers=headers, ssl=False) as resp:
            data = await resp.json()
            _LOGGER.debug("Login response code: %s", data.get("code"))

            if data.get("code") not in (0, 200):
                raise PropertyBaoAuthError(data.get("data", "Login failed"))

            result = data.get("data", data)
            self._access_token = result.get("access_token") or result.get("accessToken")
            self._refresh_token = result.get("refresh_token") or result.get("refreshToken")
            self._sip_token = result.get("sip_token") or result.get("sipToken")

            if result.get("expires_in"):
                self._token_expires = int(time.time()) + result["expires_in"]
            else:
                self._token_expires = int(time.time()) + 7 * 24 * 3600

            # Get user info from token
            import base64
            if self._access_token:
                parts = self._access_token.split(".")
                if len(parts) >= 2:
                    payload_data = parts[1] + "=" * (4 - len(parts[1]) % 4)
                    token_data = json.loads(base64.urlsafe_b64decode(payload_data))
                    self._user_id = str(token_data.get("id"))

            return result

    async def refresh_access_token(self) -> dict[str, Any]:
        """Refresh access token."""
        if not self._refresh_token:
            raise PropertyBaoAuthError("No refresh token available")

        url = f"{self.base_url}{API_REFRESH_TOKEN}"
        headers = {
            "Content-Type": "application/json",
            "client_id": self.client_id,
            "User-Agent": "Dart/2.19 (dart:io)",
        }
        payload = {
            "refresh_token": self._refresh_token,
        }

        _LOGGER.debug("Refreshing token")
        async with self._session.post(url, json=payload, headers=headers, ssl=False) as resp:
            data = await resp.json()

            if data.get("code") not in (0, 200):
                raise PropertyBaoAuthError(data.get("data", "Token refresh failed"))

            result = data.get("data", data)
            self._access_token = result.get("access_token") or result.get("accessToken")
            self._refresh_token = result.get("refresh_token") or result.get("refreshToken")
            self._sip_token = result.get("sip_token") or result.get("sipToken")
            self._token_expires = int(time.time()) + 7 * 24 * 3600

            return result

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        json: dict | list | None = None,
    ) -> Any:
        """Make an authenticated request."""
        # Auto refresh token if needed
        if self._access_token and self._token_expires < time.time() + 300:
            try:
                await self.refresh_access_token()
            except PropertyBaoAuthError:
                await self.login()

        url = f"{self.base_url}{endpoint}"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "client_id": self.client_id,
            "User-Agent": "Dart/2.19 (dart:io)",
        }
        if json is not None:
            headers["Content-Type"] = "application/json"

        async with self._session.request(
            method,
            url,
            params=params,
            json=json,
            headers=headers,
            ssl=False,
        ) as resp:
            data = await resp.json()

            if data.get("code") == 401:
                try:
                    await self.refresh_access_token()
                except PropertyBaoAuthError:
                    await self.login()

                headers["Authorization"] = f"Bearer {self._access_token}"
                async with self._session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=headers,
                    ssl=False,
                ) as resp:
                    data = await resp.json()

            if data.get("code") not in (0, 200):
                raise PropertyBaoApiError(data.get("data", "API error"))

            return data.get("data", data)

    async def get_default_owner(self, phone: str) -> dict[str, Any]:
        """Get default owner info."""
        result = await self._request(
            "GET",
            API_OWNER_COMMUNITY,
            params={"phone": phone},
        )
        if result:
            if isinstance(result, list) and len(result) > 0:
                owner = result[0]
            else:
                owner = result
            self._community_id = str(owner.get("communityId", ""))
            self._community_name = owner.get("communityName", "")
            self._community_code = owner.get("communityCode")
            self._owner_id = str(owner.get("id", ""))
        return result

    async def get_device_gates(self, community_id: str | None = None) -> list[dict[str, Any]]:
        """Get device gates list."""
        cid = community_id or self._community_id
        result = await self._request(
            "GET",
            API_DEVICE_GATES,
            params={"communityId": cid, "type": "outdoor"},
        )
        return result if isinstance(result, list) else []

    async def get_notice_list(self, community_id: str | None = None) -> list[dict[str, Any]]:
        """Get notice list."""
        cid = community_id or self._community_id
        result = await self._request(
            "GET",
            API_NOTICE_LIST,
            params={"communityId": cid},
        )
        return result if isinstance(result, list) else []

    async def get_alarm_list(self, community_id: str | None = None) -> list[dict[str, Any]]:
        """Get alarm list."""
        cid = community_id or self._community_id
        result = await self._request(
            "GET",
            API_ALARM_LIST,
            params={"communityId": cid},
        )
        return result if isinstance(result, list) else []

    def _build_sip_target(self, gate: dict[str, Any]) -> str:
        """Build SIP target address from gate info.

        Wall gates: GT-{communityCode}-{areaCode}-0-0-0-{deviceNumber}
        Outdoor gates: OD-{communityCode}-{buildingCode}-{unitCode}-0-{floorCode}-{roomCode}
        """
        gate_type = gate.get("type", "outdoor")
        community_code = self._community_code or gate.get("communityCode", 0)
        device_number = gate.get("deviceNumber", "1")

        if gate_type == "wall":
            area_code = gate.get("areaCode", 1)
            return f"GT-{community_code}-{area_code}-0-0-0-{device_number}"
        else:
            building_code = gate.get("buildingCode", 1)
            unit_code = gate.get("unitCode", 1)
            return f"OD-{community_code}-{building_code}-{unit_code}-0-0-0"

    async def open_door_via_sip(self, gate: dict[str, Any]) -> bool:
        """Open door via SIP MESSAGE.

        Sends a SIP MESSAGE to the gate device with unlock command.
        """
        if not self._sip_token:
            raise PropertyBaoApiError("No SIP token available")

        target = self._build_sip_target(gate)
        gate_type = gate.get("type", "outdoor")
        device_number = gate.get("deviceNumber", "1")

        # Build unlock message
        message = {
            "id": str(uuid.uuid4()),
            "type": "unlock",
            "content": {
                "device": gate_type,
                "ownerId": self._owner_id or self._user_id,
                "deviceNumber": device_number,
            }
        }

        _LOGGER.info("Sending SIP unlock to %s", target)
        _LOGGER.debug("SIP message: %s", json.dumps(message))

        # TODO: Implement actual SIP MESSAGE sending
        # This requires a SIP stack. For now, log the intent.
        # In production, use pjsip or similar library to send SIP MESSAGE.
        _LOGGER.warning(
            "SIP door open not yet fully implemented. Target: %s, Message: %s",
            target,
            json.dumps(message),
        )
        return True

    async def async_close(self) -> None:
        """Close the session."""
        await self._session.close()
