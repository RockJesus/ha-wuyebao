"""HTTP client for the JHCloud "物业宝（业主）" backend.

Reverse-engineered from the official Android app (WuYeBao 1.1.1.51):

  * Login ...... POST api/client/anon/token
                headers: client_id: <client id>, Content-Type: application/json
                body:    {"username": <phone>, "password": <password>}
                ok:      {"code":0,"data":{"accessToken":...,"refreshToken":...}}
                bad creds: HTTP 400 {"code":1003,"data":"帐号或密码错误！"}
  * Refresh .... GET api/client/anon/refresh_token?refreshToken=<rt>
                (requires the same client_id header)
  * Gate list .. GET api/device/grant/gates   (Authorization: Bearer <token>)
  * Owner list . GET api/owner/grant/owners   (Authorization: Bearer <token>)
  * Open gate .. configurable; default POST api/device/grant/gates/{gateId}/unlock

No packet capture is needed: credentials are entered in the Home Assistant
config flow and the integration talks to the official API directly.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urljoin

import aiohttp

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api_utils import (
    build_auth_headers,
    build_login_payload,
    build_open_path,
    find_gates,
    find_refresh_token,
    find_token,
    get_data,
    is_success,
    normalize_gates,
)
from .const import (
    API_COMMUNITIES_PATH,
    API_GATES_PATH,
    API_LOGIN_PATH,
    API_OWNERS_PATH,
    API_REFRESH_PATH,
    DEFAULT_BASE_URL,
    DEFAULT_CLIENT_ID,
    DEFAULT_COMMUNITY_FIELD,
    DEFAULT_OPEN_METHOD,
    DEFAULT_OPEN_PATH,
)

_LOGGER = logging.getLogger(__name__)


class WuyeBaoError(Exception):
    """Base error for the WuyeBao API client."""


class WuyeBaoAuthError(WuyeBaoError):
    """Authentication failed (bad credentials or an expired token)."""


class WuyeBaoConnectionError(WuyeBaoError):
    """Network, server or protocol error."""


class WuyeBaoAPI:
    """Async client for the JHCloud 物业宝 backend."""

    def __init__(
        self,
        hass,
        phone: str,
        password: str,
        base_url: str = DEFAULT_BASE_URL,
        client_id: str = DEFAULT_CLIENT_ID,
        open_path: str = DEFAULT_OPEN_PATH,
        open_method: str = DEFAULT_OPEN_METHOD,
        community_field: str = DEFAULT_COMMUNITY_FIELD,
        timeout: int = 15,
    ) -> None:
        self._hass = hass
        self._phone = phone
        self._password = password
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/") + "/"
        self._client_id = client_id or DEFAULT_CLIENT_ID
        self._open_path = open_path or DEFAULT_OPEN_PATH
        self._open_method = (open_method or DEFAULT_OPEN_METHOD).upper()
        self._community_field = community_field or DEFAULT_COMMUNITY_FIELD
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    # ------------------------------------------------------------------
    # Low-level request
    # ------------------------------------------------------------------
    def _base_headers(self, token: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "client_id": self._client_id,
        }
        if token:
            headers.update(build_auth_headers(token))
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        token: str | None = None,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Perform a request and return the decoded JSON."""
        url = urljoin(self._base_url, path.lstrip("/"))
        headers = self._base_headers(token)
        if payload is not None:
            headers["Content-Type"] = "application/json"

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
                data: Any = None
                if text:
                    try:
                        data = await resp.json()
                    except (ValueError, TypeError):
                        data = None

                # Decode the JHCloud envelope even when HTTP status is an error.
                if isinstance(data, dict) and "code" in data:
                    code = data.get("code")
                    try:
                        code_int = int(code)
                    except (TypeError, ValueError):
                        code_int = None
                    if code_int not in (0, None) or (code_int == 0 and resp.status >= 400):
                        if resp.status == 401 or code_int == 1001:
                            raise WuyeBaoAuthError(
                                f"令牌无效或已过期: {str(data.get('data'))[:120]}"
                            )
                        if code_int == 1003:
                            raise WuyeBaoAuthError("帐号或密码错误")
                        raise WuyeBaoConnectionError(
                            f"{method} {url} -> code {code}: {str(data.get('data'))[:200]}"
                        )

                if resp.status >= 400:
                    raise WuyeBaoConnectionError(
                        f"{method} {url} -> HTTP {resp.status}: {text[:200]}"
                    )
                return data
        except WuyeBaoError:
            raise
        except aiohttp.ClientError as err:
            raise WuyeBaoConnectionError(f"请求 {url} 失败: {err}") from err
        except TimeoutError as err:
            raise WuyeBaoConnectionError(f"请求 {url} 超时") from err

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    async def login(self) -> tuple[str, str | None]:
        """Log in with phone + password.

        Returns (access_token, refresh_token). Raises WuyeBaoAuthError on
        bad credentials and WuyeBaoConnectionError on network problems.
        """
        payload = build_login_payload(self._phone, self._password)
        data = await self._request("POST", API_LOGIN_PATH, payload=payload)
        access_token = find_token(data)
        if not access_token:
            raise WuyeBaoAuthError("登录接口未返回 accessToken，请检查账号密码")
        refresh_token = find_refresh_token(data)
        # Diagnostic: the full login payload may carry SIP credentials
        # (e.g. sipToken / sipServer) needed for the SIP open-door path.
        _LOGGER.info(
            "物业宝登录响应(完整字段): %s",
            json.dumps(data, ensure_ascii=False, default=str),
        )
        _LOGGER.debug("物业宝登录成功")
        return access_token, refresh_token

    async def refresh(self, refresh_token: str) -> tuple[str, str | None]:
        """Exchange a refresh token for a fresh access token.

        Returns (access_token, refresh_token). Raises WuyeBaoAuthError when
        the refresh token is rejected.
        """
        params = {"refreshToken": refresh_token}
        data = await self._request("GET", API_REFRESH_PATH, params=params)
        access_token = find_token(data)
        if not access_token:
            raise WuyeBaoAuthError("刷新令牌接口未返回 accessToken")
        new_refresh = find_refresh_token(data) or refresh_token
        return access_token, new_refresh

    # ------------------------------------------------------------------
    # Business data
    # ------------------------------------------------------------------
    async def get_communities(self) -> list[dict[str, Any]]:
        """Fetch the communities bound to this phone number (anonymous endpoint).

        Returns records with communityId / communityName / communityCode.
        """
        params = {"phoneNumber": self._phone}
        data = await self._request("GET", API_COMMUNITIES_PATH, params=params)
        raw = get_data(data)
        if isinstance(raw, dict):
            for key in ("list", "rows", "items", "communities"):
                if isinstance(raw.get(key), list):
                    return [x for x in raw[key] if isinstance(x, dict)]
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        return []

    async def get_owners(self, token: str) -> list[dict[str, Any]]:
        """Fetch the owner list for the logged-in account."""
        data = await self._request("GET", API_OWNERS_PATH, token=token)
        raw = get_data(data)
        if isinstance(raw, dict):
            for key in ("list", "rows", "items", "owners", "ownerList"):
                if isinstance(raw.get(key), list):
                    return [x for x in raw[key] if isinstance(x, dict)]
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        return []

    async def get_gates(
        self,
        token: str,
        community_id: str | None = None,
        community_field: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch and normalize the gate (door) list.

        The server requires a selected community. The primary field name is
        configurable (default communityId); if the server rejects it with the
        "必须选择一个小区" error, communityCode is tried automatically.
        """
        field = community_field or self._community_field or DEFAULT_COMMUNITY_FIELD
        candidates = [field]
        if field != "communityCode":
            candidates.append("communityCode")

        last_error: Exception | None = None
        for name in candidates:
            if community_id is None:
                params = None
            else:
                params = {name: community_id}
            try:
                data = await self._request("GET", API_GATES_PATH, token=token, params=params)
                raw_gates = find_gates(data)
                if raw_gates is None:
                    _LOGGER.warning("门禁接口未返回可识别的门禁数组: %s", str(data)[:300])
                    return []
                return normalize_gates(raw_gates)
            except WuyeBaoConnectionError as err:
                message = str(err)
                if "小区" not in message:
                    raise
                last_error = err
                _LOGGER.info("门禁接口提示需要小区参数（%s），尝试改用 communityCode", name)
        raise WuyeBaoConnectionError(
            "获取门禁列表失败：服务器要求选择小区，且 communityId / communityCode "
            "参数均未通过。请确认配置中的小区 ID。"
        ) from last_error

    async def get_call_records(self, token: str, community_id: str | None = None) -> dict:
        """Fetch intercom call records (api/call/{pageNo}/{pageSize}/grant/calls).

        The app logs every SIP call here; records may expose the real SIP
        callee identifiers (callNumber / uri) used by the app.
        """
        params: dict[str, Any] = {}
        if community_id:
            params["communityId"] = community_id
        try:
            data = await self._request(
                "GET", "api/call/1/50/grant/calls", token, params=params
            )
        except Exception as err:  # noqa: BLE001 - diagnostic only
            return {"error": str(err)}
        return data if isinstance(data, dict) else {"raw": str(data)[:500]}

    async def sip_token_diag(
        self,
        token: str,
        raw: dict[str, Any] | None,
        community_id: str | None = None,
        call_number: str | None = None,
    ) -> list[dict[str, Any]]:
        """Probe the intercom call paths for a call-origination / SIP-token
        interface.

        Field evidence: GET api/call/1/1/grant/calls?gateId=... returns code 0
        and records, so the handler reads @RequestParam (query/form). Every
        POST variant tried so far put its parameters in the JSON body and got
        code 500, which means the POST handler exists but never saw its
        parameters. This probe therefore sends parameters as query strings,
        and also tries callType / full-gate-record bodies (the app source
        carries CALL_TYEP_NORMAL / CALL_TYEP_MONITOR / CallElevator).
        """
        raw = raw or {}
        gate_id = str(raw.get("id") or "") or None
        gate_uid = str(raw.get("uid") or "") or None
        device_number = str(raw.get("deviceNumber") or "") or None
        community = community_id or str(raw.get("communityId") or "") or None

        candidates: list[tuple[str, str, dict, dict]] = []

        def add(method: str, path: str, params: dict | None = None, payload: dict | None = None):
            candidates.append((method, path, params or {}, payload or {}))

        for base in ("api/call/grant/calls", "api/call/1/1/grant/calls"):
            # 1) POST with query parameters (the likely @RequestParam contract)
            if call_number and community:
                add("POST", base, {"callNumber": call_number, "communityId": community})
            if gate_id and community:
                add("POST", base, {"gateId": gate_id, "communityId": community})
            if gate_id and community:
                add("POST", base, {"id": gate_id, "communityId": community})
            if device_number and community:
                add("POST", base, {"deviceNumber": device_number, "communityId": community})
            # 2) POST with callType (app constants: CALL_TYEP_NORMAL / MONITOR)
            if call_number and community:
                add("POST", base, {}, {"callNumber": call_number, "communityId": community, "callType": "NORMAL"})
                add("POST", base, {}, {"callNumber": call_number, "communityId": community, "callType": 0})
                add("POST", base, {}, {"callNumber": call_number, "communityId": community, "callType": "CALL_TYEP_NORMAL"})
            # 3) POST the whole gate record (Spring DTO may want it all)
            if gate_id:
                full = {k: v for k, v in raw.items()}
                if community and "communityId" not in full:
                    full["communityId"] = community
                add("POST", base, {}, full)

        # 4) GET the full (50) call records for this gate, data kept long.
        if gate_id and community:
            add("GET", "api/call/1/50/grant/calls", {"gateId": gate_id, "communityId": community})
            add("GET", "api/call/1/50/grant/calls", {"gateId": gate_id, "communityId": community, "callNumber": call_number or ""})

        results: list[dict[str, Any]] = []
        for method, path, params, payload in candidates:
            entry: dict[str, Any] = {
                "method": method,
                "path": path,
                "params": params,
                "payload": {k: (str(v)[:20] + "…" if isinstance(v, str) and len(str(v)) > 20 else v) for k, v in payload.items()},
            }
            try:
                data = await self._request(
                    method, path, token=token, params=params or None, payload=payload or None
                )
                entry["ok"] = True
                if isinstance(data, dict):
                    entry["code"] = data.get("code")
                    # Keep the record list long enough to inspect all fields.
                    text = str(data.get("data"))
                    entry["data"] = text[:3000]
                else:
                    entry["data"] = str(data)[:1000]
            except WuyeBaoConnectionError as err:
                entry["ok"] = False
                entry["error"] = str(err)[:300]
            results.append(entry)
        return results

    async def open_gate(self, token: str, gate_id: str) -> None:
        """Trigger the open-door action for a gate."""
        path = build_open_path(self._open_path, gate_id)
        if self._open_method == "GET":
            await self._request("GET", path, token=token)
        else:
            await self._request("POST", path, token=token, payload={})

    async def open_gate_diag(
        self,
        token: str,
        gate_id: str,
        raw: dict[str, Any] | None,
        community_id: str | None = None,
        call_number: str | None = None,
    ) -> list[dict[str, Any]]:
        """Try the most plausible HTTP open-door variants and report each result.

        The official app opens doors via SIP (cloud intercom), and the HTTP
        endpoint is not documented; the default path is configurable. This
        diagnostic tries a small, strictly open-door-oriented set of variants
        built from the real gate record, logs every outcome, and lets us
        discover a working HTTP endpoint (if the server has one) or confirm
        that SIP is the only path.
        """
        raw = raw or {}
        gate_uid = str(raw.get("uid") or "") or None
        gate_pwd = str(raw.get("password") or "") or None
        area_id = str(raw.get("areaId") or "") or None
        community = community_id or str(raw.get("communityId") or "") or None
        call_number = call_number or str(raw.get("callNumber") or "") or None

        candidates: list[tuple[str, str, dict, dict]] = []
        base = build_open_path(self._open_path, gate_id)

        def add(method: str, path: str, params: dict | None = None, payload: dict | None = None):
            candidates.append((method, path, params or {}, payload))

        q_common: dict[str, str] = {}
        if community:
            q_common["communityId"] = community
        q_with_pwd = dict(q_common)
        if gate_pwd:
            q_with_pwd["password"] = gate_pwd
        if gate_uid:
            q_with_pwd["uid"] = gate_uid

        # 1) configured default path, then with community / password / uid
        add(self._open_method, base)
        add(self._open_method, base, q_common)
        add(self._open_method, base, q_with_pwd)
        # 2) password in body instead of query
        add(self._open_method, base, q_common, {"password": gate_pwd} if gate_pwd else None)
        # 3) /open and /remoteOpen variants
        for suffix in ("open", "remoteOpen"):
            add(
                "POST",
                build_open_path(
                    f"api/device/grant/gates/{{gate_id}}/{suffix}", gate_id
                ),
                q_with_pwd,
            )
        # 4) uid as the device id
        if gate_uid:
            add(
                "POST",
                build_open_path("api/device/grant/gates/{gate_id}/unlock", gate_uid),
                q_with_pwd,
            )
        # 5) area-scoped path (gate record carries areaId)
        if area_id and community:
            add("POST", f"api/device/grant/areas/{area_id}/gates/{gate_id}/unlock", q_with_pwd)
        # 6) GET variant of the configured path
        add("GET", base, q_with_pwd)
        # 7) call-based HTTP candidates (the app logs calls via
        #    api/call/.../grant/calls; the same path may accept a POST to
        #    originate a call with the callNumber identifier)
        if call_number:
            add("POST", "api/call/grant/calls", q_common, {"callNumber": call_number})
            add("POST", "api/call/grant/calls", {"callNumber": call_number})
            add("POST", "api/call/1/1/grant/calls", q_common, {"callNumber": call_number})
            add("POST", f"api/device/grant/gates/{gate_id}/call", q_with_pwd, {"callNumber": call_number})
            add("POST", f"api/device/grant/gates/{gate_id}/callNumber", q_common, {"callNumber": call_number})
            add("POST", f"api/device/grant/gates/{gate_id}/sip", q_common, {"callNumber": call_number})

        results: list[dict[str, Any]] = []
        for method, path, params, payload in candidates:
            entry: dict[str, Any] = {"method": method, "path": path, "params": params}
            try:
                data = await self._request(
                    method, path, token=token, params=params or None, payload=payload or None
                )
                entry["ok"] = True
                entry["code"] = data.get("code")
                entry["data"] = str(data.get("data"))[:200]
            except WuyeBaoConnectionError as err:
                entry["ok"] = False
                entry["error"] = str(err)[:300]
            results.append(entry)
        return results
