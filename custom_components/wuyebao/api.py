"""HTTP client for the WuyeBao backend.

The products named "物业宝" in the wild are operated by many different vendors
and none of them expose a public, documented door-control API. This client
implements the shape that these apps share (login -> token -> device list ->
open door) and lets the user configure the real endpoints captured from their
own app via a packet capture.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin

import aiohttp

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api_utils import (
    build_auth_headers,
    build_login_payload,
    build_open_payload,
    find_devices,
    find_token,
    normalize_devices,
)

_LOGGER = logging.getLogger(__name__)


class WuyeBaoError(Exception):
    """Base error for the WuyeBao API client."""


class WuyeBaoAuthError(WuyeBaoError):
    """Authentication failed (bad credentials or an expired token)."""


class WuyeBaoConnectionError(WuyeBaoError):
    """Network or server error."""


class WuyeBaoAPI:
    """Async client for the WuyeBao backend."""

    def __init__(
        self,
        hass,
        phone: str,
        password: str,
        base_url: str,
        login_path: str = "/api/login",
        devices_path: str = "/api/device/list",
        open_path: str = "/api/device/open",
        open_method: str = "POST",
        device_id_field: str = "deviceId",
        device_name_field: str = "name",
        auth_scheme: str = "Bearer",
        timeout: int = 15,
    ) -> None:
        self._hass = hass
        self._phone = phone
        self._password = password
        self._base_url = base_url.rstrip("/")
        self._login_path = login_path
        self._devices_path = devices_path
        self._open_path = open_path
        self._open_method = (open_method or "POST").upper()
        self._device_id_field = device_id_field
        self._device_name_field = device_name_field
        self._auth_scheme = auth_scheme or ""
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def _request(
        self,
        method: str,
        path: str,
        token: str | None = None,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Perform a request and return the decoded JSON (dict/list/None)."""
        url = urljoin(self._base_url + "/", path.lstrip("/"))
        headers: dict[str, str] = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers.update(build_auth_headers(token, self._auth_scheme))

        session = async_get_clientsession(self._hass)
        try:
            async with session.request(
                method,
                url,
                headers=headers,
                json=payload if payload is not None else None,
                params=params,
                timeout=self._timeout,
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise WuyeBaoConnectionError(
                        f"{method} {url} -> HTTP {resp.status}: {text[:200]}"
                    )
                if not text:
                    return None
                try:
                    return await resp.json()
                except (ValueError, TypeError) as err:
                    raise WuyeBaoConnectionError(
                        f"{method} {url} -> 响应不是有效 JSON: {text[:200]}"
                    ) from err
        except WuyeBaoError:
            raise
        except aiohttp.ClientError as err:
            raise WuyeBaoConnectionError(f"请求 {url} 失败: {err}") from err
        except TimeoutError as err:
            raise WuyeBaoConnectionError(f"请求 {url} 超时") from err

    async def login(self) -> str:
        """Log in with phone + password and return an auth token."""
        payload = build_login_payload(self._phone, self._password)
        data = await self._request("POST", self._login_path, payload=payload)
        token = find_token(data)
        if not token:
            raise WuyeBaoAuthError("登录接口未返回 token，请检查账号密码或接口字段")
        _LOGGER.debug("物业宝登录成功")
        return token

    async def get_devices(self, token: str) -> list[dict[str, Any]]:
        """Fetch the normalized device (door) list."""
        data = await self._request("GET", self._devices_path, token=token)
        raw_devices = find_devices(data)
        if raw_devices is None:
            _LOGGER.warning("设备列表接口未返回可识别的设备数组: %s", str(data)[:300])
            return []
        return normalize_devices(
            raw_devices,
            id_field=self._device_id_field,
            name_field=self._device_name_field,
        )

    async def open_door(self, token: str, device_id: str) -> None:
        """Trigger the open-door action for a device."""
        open_payload = build_open_payload(device_id, self._device_id_field)
        if self._open_method == "GET":
            await self._request("GET", self._open_path, token=token, params=open_payload)
        else:
            await self._request("POST", self._open_path, token=token, payload=open_payload)
