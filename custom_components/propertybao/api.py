"""API client for 物业宝."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import aiohttp

from .const import (
    API_TOKEN,
    API_REFRESH_TOKEN,
    API_OWNER_COMMUNITY,
    API_GATES,
    DEFAULT_BASE_URL,
    DEFAULT_CLIENT_ID,
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

    @property
    def access_token(self) -> str | None:
        """Return access token."""
        return self._access_token

    @property
    def refresh_token(self) -> str | None:
        """Return refresh token."""
        return self._refresh_token

    def _base_headers(self, token: str | None = None) -> dict[str, str]:
        """Build base request headers."""
        headers = {
            "Accept": "application/json",
            "client_id": self.client_id,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        token: str | None = None,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Perform a request and return decoded JSON."""
        url = f"{self.base_url}{path}"
        headers = self._base_headers(token)
        if payload is not None:
            headers["Content-Type"] = "application/json"

        async with self._session.request(
            method,
            url,
            headers=headers,
            json=payload if payload is not None else None,
            params=params,
            ssl=False,
        ) as resp:
            data = await resp.json(content_type=None)

            if isinstance(data, dict) and "code" in data:
                code = data.get("code")
                try:
                    code_int = int(code)
                except (TypeError, ValueError):
                    code_int = None

                if code_int not in (0,):
                    if code_int == 1003:
                        raise PropertyBaoAuthError("帐号或密码错误")
                    if resp.status == 401 or code_int == 1001:
                        raise PropertyBaoAuthError("令牌无效或已过期")
                    raise PropertyBaoApiError(
                        f"API error: code {code}: {str(data.get('data'))[:200]}"
                    )

            if resp.status >= 400:
                raise PropertyBaoApiError(f"HTTP {resp.status}")

            return data

    async def login(self) -> None:
        """Login with username and password."""
        payload = {
            "username": self.username,
            "password": self.password,
        }

        _LOGGER.debug("Logging in...")
        data = await self._request("POST", API_TOKEN, payload=payload)

        self._access_token = self._find_token(data)
        self._refresh_token = self._find_refresh_token(data)

        if not self._access_token:
            raise PropertyBaoAuthError("登录失败：未返回 accessToken")

        self._token_expires = int(time.time()) + 7 * 24 * 3600
        _LOGGER.info("Login successful")

        # Get community info (optional)
        try:
            await self.get_owner_community()
        except Exception as err:
            _LOGGER.warning("Failed to get community info: %s", err)

    async def refresh_access_token(self) -> None:
        """Refresh access token."""
        if not self._refresh_token:
            raise PropertyBaoAuthError("No refresh token available")

        params = {"refreshToken": self._refresh_token}
        data = await self._request("GET", API_REFRESH_TOKEN, params=params)

        new_token = self._find_token(data)
        new_refresh = self._find_refresh_token(data)

        if not new_token:
            raise PropertyBaoAuthError("Refresh failed: no access token")

        self._access_token = new_token
        self._refresh_token = new_refresh or self._refresh_token
        self._token_expires = int(time.time()) + 7 * 24 * 3600

    def _find_token(self, data: Any) -> str | None:
        """Find access token in response."""
        return self._find_key(data, ("accessToken", "access_token", "token"))

    def _find_refresh_token(self, data: Any) -> str | None:
        """Find refresh token in response."""
        return self._find_key(data, ("refreshToken", "refresh_token"))

    def _find_key(self, data: Any, keys: tuple[str, ...], depth: int = 0) -> str | None:
        """Recursively find a key in data."""
        if data is None or depth > 5:
            return None
        if isinstance(data, dict):
            for key in keys:
                val = data.get(key)
                if isinstance(val, str) and val:
                    return val
            for val in data.values():
                found = self._find_key(val, keys, depth + 1)
                if found:
                    return found
        elif isinstance(data, list):
            for item in data:
                found = self._find_key(item, keys, depth + 1)
                if found:
                    return found
        return None

    async def get_owner_community(self) -> list[dict[str, Any]]:
        """Get owner community info."""
        params = {"phoneNumber": self.username}
        data = await self._request("GET", API_OWNER_COMMUNITY, params=params)

        raw = data.get("data", data)
        if isinstance(raw, list):
            if raw:
                owner = raw[0]
                self.community_id = str(owner.get("communityId", ""))
                self.community_name = owner.get("communityName", "")
                self.community_code = owner.get("communityCode")
                self.owner_id = str(owner.get("id", ""))
            return raw
        return []

    async def get_gates(self) -> list[dict[str, Any]]:
        """Get gate list."""
        params = {}
        if self.community_id:
            params["communityId"] = self.community_id

        data = await self._request("GET", API_GATES, token=self._access_token, params=params)
        raw = data.get("data", data)
        if isinstance(raw, list):
            return raw
        return []

    async def async_close(self) -> None:
        """Close the session."""
        await self._session.close()
