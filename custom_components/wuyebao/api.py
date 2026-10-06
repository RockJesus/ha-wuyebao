"""API client for 物业宝."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

from .const import (
    API_TOKEN,
    API_REFRESH_TOKEN,
    API_CLIENT_TOKEN,
    API_OWNER_COMMUNITY,
    API_GATES,
    API_OWNERS,
    API_CALLS,
    API_ALARMS,
    API_REPAIRS,
    API_INVITE_VISITORS,
    API_INVITE_VISITOR_CREATE,
    API_FACE_INFO,
    API_CONTENTS,
    CONTENT_TYPE_CAROUSEL,
    DEFAULT_BASE_URL,
    DEFAULT_CLIENT_ID,
    DEFAULT_SIP_CLIENT_ID,
    DEFAULT_SIP_CLIENT_SECRET,
)
from .sip import WuYeBaoSipClient

_LOGGER = logging.getLogger(__name__)


class WuYeBaoAuthError(Exception):
    """Authentication error."""


class WuYeBaoApiError(Exception):
    """API error."""


class WuYeBaoClient:
    """API client for 物业宝."""

    def __init__(
        self,
        username: str,
        password: str,
        base_url: str = DEFAULT_BASE_URL,
        client_id: str = DEFAULT_CLIENT_ID,
        sip_jwt: str | None = None,
        sip_sid: str | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Initialize the API client."""
        import uuid

        self.username = username
        self.password = password
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.sip_jwt = sip_jwt
        self.sip_sid = sip_sid or uuid.uuid4().hex[:16]
        self._session = session or aiohttp.ClientSession()

        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires: int = 0
        self._sip_token_expires: int = 0

        self.user_id: str | None = None
        self.owner_id: str | None = None
        self.community_id: str | None = None
        self.community_name: str | None = None
        self.community_code: int | None = None
        self.binding_code: str | None = None
        self.unit_id: str | None = None
        self.owners: list[dict[str, Any]] = []
        # Owner's flat number (e.g. "2702"), used for elevator call
        # ("call_elevator" room) and 户户通 (RM indoor-unit URI).
        self.room: str | None = None

        # Hub-level cached data (repairs / visitors / face / contents / alarms),
        # refreshed periodically by the hub poller in __init__.py.
        self.hub_data: dict[str, Any] = {}

        # Gate list cache: fetched once at setup (pre-warmed after login),
        # shared by every platform so a transient API failure cannot leave
        # some platforms with an empty door list while others succeed.
        self.gates: list[dict[str, Any]] | None = None
        self._gates_lock = asyncio.Lock()

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
                        raise WuYeBaoAuthError("帐号或密码错误")
                    if resp.status == 401 or code_int == 1001:
                        raise WuYeBaoAuthError("令牌无效或已过期")
                    raise WuYeBaoApiError(
                        f"API error: code {code}: {str(data.get('data'))[:200]}"
                    )

            if resp.status >= 400:
                raise WuYeBaoApiError(f"HTTP {resp.status}")

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
            raise WuYeBaoAuthError("登录失败：未返回 accessToken")

        self._token_expires = int(time.time()) + 7 * 24 * 3600
        _LOGGER.info("Login successful")

        # Get community info (optional)
        try:
            await self.get_owner_community()
        except Exception as err:
            _LOGGER.warning("Failed to get community info: %s", err)

        # Auto-obtain SIP token
        try:
            await self.refresh_sip_token()
        except Exception as err:
            _LOGGER.warning("Failed to get SIP token: %s", err)

    async def refresh_sip_token(self) -> None:
        """Auto-obtain SIP access token."""
        headers = {
            "Accept": "application/json",
            "client_id": DEFAULT_SIP_CLIENT_ID,
            "client_secret": DEFAULT_SIP_CLIENT_SECRET,
            "encrypted": "false",
        }

        async with self._session.get(
            f"{self.base_url}{API_CLIENT_TOKEN}",
            headers=headers,
            ssl=False,
        ) as resp:
            data = await resp.json(content_type=None)

            if isinstance(data, dict) and data.get("code") == 0:
                result = data.get("data", {})
                self.sip_jwt = result.get("accessToken")
                self._sip_token_expires = int(time.time()) + 7 * 24 * 3600
                _LOGGER.info("SIP token refreshed successfully")
            else:
                raise WuYeBaoApiError(f"Failed to get SIP token: {data}")

    async def ensure_sip_token(self) -> None:
        """Refresh the SIP JWT if it is missing or close to expiry."""
        if not self.sip_jwt or self._sip_token_expires < time.time() + 300:
            await self.refresh_sip_token()

    async def refresh_access_token(self) -> None:
        """Refresh access token."""
        if not self._refresh_token:
            raise WuYeBaoAuthError("No refresh token available")

        params = {"refreshToken": self._refresh_token}
        data = await self._request("GET", API_REFRESH_TOKEN, params=params)

        new_token = self._find_token(data)
        new_refresh = self._find_refresh_token(data)

        if not new_token:
            raise WuYeBaoAuthError("Refresh failed: no access token")

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
                if owner.get("userId"):
                    self.user_id = str(owner.get("userId"))
            return raw
        return []

    async def get_gates(self) -> list[dict[str, Any]]:
        """Get gate list (ALL gates in the community).

        We intentionally do NOT filter by unitId/type here: the user wants
        every door of the community (wall gates + all unit doors) exposed in
        HA.  Each gate object carries its own community/building/unit fields,
        so per-gate naming stays correct.  Existing entities are keyed by the
        stable gate id, so reloading never drops doors.

        The result is cached on ``self.gates`` (guarded by a lock) and
        retried a few times so a transient API hiccup during parallel
        platform setup cannot leave some platforms with an empty door list.
        """
        if self.gates is not None:
            return self.gates

        async with self._gates_lock:
            # Double-checked: another platform may have filled the cache
            # while we were waiting for the lock.
            if self.gates is not None:
                return self.gates

            last_err: Exception | None = None
            for attempt in range(6):
                try:
                    params = {}
                    if self.community_id:
                        params["communityId"] = self.community_id

                    data = await self._request(
                        "GET", API_GATES, token=self._access_token, params=params
                    )
                    raw = data.get("data", data)
                    if isinstance(raw, list):
                        self.gates = raw
                        _LOGGER.info("Cached %d gates (attempt %d)", len(raw), attempt + 1)
                        return raw
                    # Non-list payload (e.g. empty/odd shape): treat as empty
                    # but retry - the API may need a moment after login.
                    last_err = WuYeBaoApiError(
                        f"Unexpected gate list shape: {type(raw).__name__}"
                    )
                    _LOGGER.warning(
                        "Gate list attempt %d returned non-list payload: %s",
                        attempt + 1,
                        str(raw)[:160],
                    )
                except Exception as err:  # noqa: BLE001 - retry transient errors
                    last_err = err
                    _LOGGER.warning("Gate list attempt %d failed: %s", attempt + 1, err)
                if attempt < 5:
                    # Backoff 2/4/6/8/10 s: covers the HA-startup window where
                    # the property API may not be reachable yet.
                    await asyncio.sleep(2.0 * (attempt + 1))

            _LOGGER.error("Failed to fetch gate list after retries: %s", last_err)
            return []

    async def ensure_gates(self) -> list[dict[str, Any]]:
        """Return the cached gate list (fetching it if necessary)."""
        if self.gates is not None:
            return self.gates
        return await self.get_gates()

    async def get_owners(self) -> list[dict[str, Any]]:
        """Get owner info (contains bindingCode for camera calls)."""
        params = {}
        if self.community_id:
            params["communityId"] = self.community_id

        data = await self._request("GET", API_OWNERS, token=self._access_token, params=params)
        raw = data.get("data", data)
        if isinstance(raw, list):
            self.owners = raw
            # Save bindingCode (indoor unit SIP number) and unitId for the
            # owner matching THIS logged-in account.  The endpoint returns
            # every owner in the community (thousands), so filtering by the
            # account's phone number is required - otherwise we pick the
            # first (wrong) unit and gates resolve to the wrong building.
            norm_self = "".join(ch for ch in str(self.username) if ch.isdigit())
            for owner in raw:
                owner_phone = "".join(ch for ch in str(owner.get("phoneNumber") or "") if ch.isdigit())
                if not owner_phone or owner_phone != norm_self:
                    continue
                binding = owner.get("bindingCode")
                if binding:
                    self.binding_code = str(binding)
                unit = owner.get("unitId")
                if unit:
                    self.unit_id = str(unit)
                if self.binding_code and self.unit_id:
                    break
            if self.binding_code:
                _LOGGER.info("Binding code: %s", self.binding_code)
                self._resolve_room_from_binding()
            if self.unit_id:
                _LOGGER.info("Unit id: %s", self.unit_id)
            return raw
        return []

    def _resolve_room_from_binding(self) -> None:
        """Auto-derive the owner's flat number (e.g. "2702") from bindingCode.

        The binding code is the indoor-unit SIP number captured from the app:
            RM-<communityCode>-<areaCode>-<buildingCode>-<unitCode>-<floor>-<room>
        e.g. RM-840-1-4-1-27-2  ->  floor 27, room 02  ->  "2702"

        This replaces the old manual "房间号" field in the config flow: the
        room is now always fetched automatically after login, so elevator
        calls and 户户通 work without user configuration.
        """
        try:
            parts = str(self.binding_code or "").split("-")
            if len(parts) < 2:
                return
            floor = str(parts[-2]).strip()
            room_code = str(parts[-1]).strip()
            if not floor.isdigit() or not room_code.isdigit():
                return
            room = floor + room_code.zfill(2)
            self.room = room
            _LOGGER.info("Room auto-resolved from binding code: %s", self.room)
        except Exception as err:  # noqa: BLE001 - best effort
            _LOGGER.warning("Failed to resolve room from binding code: %s", err)

    async def get_calls(
        self, page: int = 1, page_size: int = 20
    ) -> list[dict[str, Any]]:
        """Get call records (contain door camera snapshot images).

        IMPORTANT: we deliberately do NOT pass ``callNumber`` (the logged-in
        user's binding code).  The API then returns call records for EVERY
        door in the community, which lets us match each gate to its own
        visitor snapshot.  Passing the binding code would only ever return
        records from the user's own unit door, making every other gate show
        that unit's picture (cross-gate image mix-up).
        """
        params: dict[str, Any] = {}
        if self.community_id:
            params["communityId"] = self.community_id

        path = API_CALLS.format(page=page, page_size=page_size)
        data = await self._request("GET", path, token=self._access_token, params=params)
        raw = data.get("data", data)
        if isinstance(raw, list):
            return raw
        return []

    async def get_latest_call_image(self, gate: dict[str, Any]) -> str | None:
        """Get the latest call-record snapshot URL matching a gate device.

        Strict matching by deviceNumber + devicesType. No fallback to other
        devices' records (avoids showing wrong door images).
        """
        info = await self.get_latest_call_info(gate)
        return info.get("url") if info else None

    def _call_matches_gate(self, call: dict[str, Any], gate: dict[str, Any]) -> bool:
        """True if a call record belongs to the given gate.

        Call records share ``deviceNumber`` (every unit door is numbered "1")
        so deviceNumber alone is NOT unique:
          1. strong match by unitId  (unit doors: call.unitId == gate.unitId)
          2. fallback by buildingId + deviceNumber
          3. plain deviceNumber match only for gates without unit/building ids
             (wall gates a/b are unique).
        """
        device_number = str(gate.get("deviceNumber", ""))
        gate_type = str(gate.get("type", ""))
        call_dev = str(call.get("deviceNumber", ""))
        call_type = str(call.get("devicesType", ""))
        if call_dev != device_number or call_type != gate_type:
            return False
        gate_unit_id = str(gate.get("unitId", ""))
        gate_building_id = str(gate.get("buildingId", ""))
        call_unit_id = str(call.get("unitId", ""))
        call_building_id = str(call.get("buildingId", ""))
        if gate_unit_id and call_unit_id:
            return call_unit_id == gate_unit_id
        if gate_building_id and call_building_id:
            return call_building_id == gate_building_id
        return True

    def _call_token(self, call: dict[str, Any]) -> str:
        """Stable token identifying one specific call event (dedupe key).

        The token must stay identical while the same call is re-fetched and
        change as soon as a NEW call for the same gate appears.
        """
        for key in ("callId", "id", "createTime", "callTime", "time", "visitTime", "createDate"):
            if call.get(key):
                return f"{call.get('deviceNumber', '')}:{call[key]}"
        return f"{call.get('deviceNumber', '')}:{call.get('imageUrl', '')}"

    async def get_latest_calls_per_gate(
        self, gates: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Fetch call records ONCE and return {gate_id: newest call} per gate.

        Shares the exact matching rules with the visitor camera.  Used by the
        visitor auto-open poller so a single API call covers every gate.
        """
        try:
            calls = await self.get_calls(page=1, page_size=20)
        except Exception as err:
            _LOGGER.warning("Failed to get call records: %s", err)
            return {}
        if not calls:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for gate in gates:
            gate_id = str(
                gate.get("id") or gate.get("uid") or gate.get("deviceNumber") or "unknown"
            )
            for call in calls:
                if self._call_matches_gate(call, gate):
                    result[gate_id] = call
                    break
        return result

    async def get_latest_call_info(self, gate: dict[str, Any]) -> dict[str, Any] | None:
        """Latest visitor-call record matched to this gate.

        Call records are created when a visitor presses the doorbell, so
        the imageUrl is exactly the "last visitor snapshot" for that door.

        Matching strategy (call records share ``deviceNumber`` - every unit
        door is numbered "1" - so deviceNumber alone is NOT unique):
          1. strong match by unitId  (unit doors: call.unitId == gate.unitId)
          2. fallback by buildingId + deviceNumber
             (call.buildingId == gate.buildingId and same deviceNumber)
          3. final fallback by deviceNumber + devicesType ONLY for gates
             without a unitId/buildingId (wall gates a/b are unique).
        Returns {"url", "time", "deviceNumber", "devicesType", "callType"}.
        """
        try:
            calls = await self.get_calls(page=1, page_size=20)
        except Exception as err:
            _LOGGER.warning("Failed to get call records: %s", err)
            return None

        if not calls:
            return None

        device_number = str(gate.get("deviceNumber", ""))
        gate_type = str(gate.get("type", ""))
        gate_unit_id = str(gate.get("unitId", ""))
        gate_building_id = str(gate.get("buildingId", ""))

        def _record_matches(call: dict[str, Any]) -> bool:
            call_dev = str(call.get("deviceNumber", ""))
            call_type = str(call.get("devicesType", ""))
            if call_dev != device_number or call_type != gate_type:
                return False
            call_unit_id = str(call.get("unitId", ""))
            call_building_id = str(call.get("buildingId", ""))
            if gate_unit_id and call_unit_id:
                # strongest: same unit door
                return call_unit_id == gate_unit_id
            if gate_building_id and call_building_id:
                # unit doors without unitId match: same building + device no.
                return call_building_id == gate_building_id
            # wall gates (a/b) have no unit/building ids - deviceNumber is
            # unique for them, keep the plain match
            return True

        for call in calls:
            if _record_matches(call):
                url = call.get("imageUrl")
                if url:
                    ts = None
                    for key in ("createTime", "callTime", "time", "visitTime", "createDate"):
                        if call.get(key):
                            ts = call[key]
                            break
                    return {
                        "url": str(url),
                        "time": ts,
                        "deviceNumber": device_number,
                        "devicesType": gate_type,
                        "callType": call.get("callType"),
                    }

        return None

    async def get_alarms(
        self, page: int = 1, page_size: int = 20
    ) -> list[dict[str, Any]]:
        """Get alarm records (may contain door camera snapshots)."""
        params: dict[str, Any] = {}
        if self.community_id:
            params["communityId"] = self.community_id
        params["pageNo"] = page
        params["pageSize"] = page_size

        try:
            data = await self._request(
                "GET", API_ALARMS, token=self._access_token, params=params
            )
        except Exception as err:
            _LOGGER.debug("Alarm records unavailable: %s", err)
            return []
        raw = data.get("data", data)
        if isinstance(raw, list):
            return raw
        # Some APIs wrap the list in {"list": [...]}
        if isinstance(raw, dict):
            for key in ("list", "records", "items"):
                val = raw.get(key)
                if isinstance(val, list):
                    return val
        return []

    async def get_repairs(
        self, page: int = 1, page_size: int = 20
    ) -> list[dict[str, Any]]:
        """Get 报修工单 records for the current owner."""
        params: dict[str, Any] = {"ownerId": self.owner_id} if self.owner_id else {}
        params["pageNo"] = page
        params["pageSize"] = page_size
        try:
            data = await self._request(
                "GET", API_REPAIRS, token=self._access_token, params=params
            )
        except Exception as err:
            _LOGGER.debug("Repair records unavailable: %s", err)
            return []
        raw = data.get("data", data)
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in ("list", "records", "items"):
                val = raw.get(key)
                if isinstance(val, list):
                    return val
        return []

    async def get_invite_visitors(self) -> list[dict[str, Any]]:
        """Get 访客邀请 (invite visitor) records for the current owner."""
        params: dict[str, Any] = {"ownerId": self.owner_id} if self.owner_id else {}
        try:
            data = await self._request(
                "GET", API_INVITE_VISITORS, token=self._access_token, params=params
            )
        except Exception as err:
            _LOGGER.debug("Invite visitor records unavailable: %s", err)
            return []
        raw = data.get("data", data)
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in ("list", "records", "items"):
                val = raw.get(key)
                if isinstance(val, list):
                    return val
        return []

    async def create_invite_visitor(
        self, start_time: int | None = None, end_time: int | None = None
    ) -> dict[str, Any] | None:
        """Create a 访客邀请 and return the visitor door code (6-digit password).

        The app default validity is 1 hour (startTime/endTime are ms epochs).
        """
        import time as _time

        now_ms = int(_time.time() * 1000)
        start = start_time or now_ms
        end = end_time or (now_ms + 3600 * 1000)
        payload = {
            "ownerId": self.owner_id,
            "communityId": self.community_id,
            "unitId": self.unit_id,
            "startTime": start,
            "endTime": end,
        }
        try:
            data = await self._request(
                "POST",
                API_INVITE_VISITOR_CREATE,
                token=self._access_token,
                payload=payload,
            )
        except Exception as err:
            _LOGGER.error("Failed to create invite visitor: %s", err)
            return None
        raw = data.get("data", data)
        return raw if isinstance(raw, dict) else None

    async def get_face_info(self, user_id: str | None = None) -> dict[str, Any] | None:
        """Get 人脸信息 for a user (defaults to the current owner).

        HAR evidence: the faceinfo endpoint keys by a ``userId`` value that
        equals the owner's ``id`` from ``/api/owner/anon/owners/community``
        (e.g. owner id 1061966055548784640 == faceinfo userId).  Fall back to
        ``self.user_id`` only as a last resort.
        """
        uid = user_id or self.owner_id
        if not uid:
            uid = self.user_id
        if not uid:
            return None
        params = {"userId": uid}
        try:
            data = await self._request(
                "GET", API_FACE_INFO, token=self._access_token, params=params
            )
        except Exception as err:
            _LOGGER.debug("Face info unavailable: %s", err)
            return None
        raw = data.get("data", data)
        return raw if isinstance(raw, dict) else None

    async def get_contents(
        self, content_type: str = CONTENT_TYPE_CAROUSEL
    ) -> list[dict[str, Any]]:
        """Get 小区公告/首页轮播 contents (classifyId = communityId)."""
        if not self.community_id:
            return []
        params = {"classifyId": self.community_id, "type": content_type}
        try:
            data = await self._request(
                "GET", API_CONTENTS, token=self._access_token, params=params
            )
        except Exception as err:
            _LOGGER.debug("Contents unavailable: %s", err)
            return []
        raw = data.get("data", data)
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in ("list", "records", "items"):
                val = raw.get(key)
                if isinstance(val, list):
                    return val
        return []

    async def get_gate_snapshot(self, gate: dict[str, Any]) -> str | None:
        """Get the latest snapshot URL for a gate from calls, then alarms."""
        # 1) Call records (unit doors normally appear here)
        url = await self.get_latest_call_image(gate)
        if url:
            return url

        # 2) Alarm records (may cover wall gates / other devices)
        device_number = str(gate.get("deviceNumber", ""))
        gate_type = str(gate.get("type", ""))
        try:
            alarms = await self.get_alarms(page=1, page_size=20)
        except Exception as err:
            _LOGGER.warning("Failed to get alarms: %s", err)
            alarms = []

        for alarm in alarms:
            alarm_dev = str(alarm.get("deviceNumber") or alarm.get("deviceNo") or "")
            alarm_type = str(alarm.get("devicesType") or alarm.get("deviceType") or "")
            image = alarm.get("imageUrl") or alarm.get("image") or alarm.get("url")
            if image and (
                (alarm_dev and alarm_dev == device_number and alarm_type == gate_type)
                or (alarm_dev == device_number)
            ):
                return str(image)

        return None

    async def trigger_snapshot(self, gate: dict[str, Any]) -> bool:
        """Trigger a snapshot by sending a SIP monitor message (best-effort)."""
        try:
            result = await self.start_monitor(gate)
            if result.get("ok"):
                _LOGGER.info("Snapshot trigger sent for %s", gate.get("deviceNumber"))
                return True
            _LOGGER.warning(
                "Snapshot trigger failed for %s: status=%s",
                gate.get("deviceNumber"),
                result.get("status"),
            )
        except Exception as err:
            _LOGGER.warning("Snapshot trigger error: %s", err)
        return False

    async def download_image(self, url: str) -> bytes | None:
        """Download image bytes from a URL."""
        try:
            async with self._session.get(
                url, ssl=False, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    return await resp.read()
                _LOGGER.warning("Image download failed: HTTP %s", resp.status)
        except Exception as err:
            _LOGGER.warning("Failed to download image %s: %s", url, err)
        return None

    async def open_door_sip(self, gate: dict[str, Any]) -> dict[str, Any]:
        """Open door via SIP MESSAGE."""
        # Auto-refresh SIP token if expired
        if not self.sip_jwt or self._sip_token_expires < time.time() + 300:
            try:
                await self.refresh_sip_token()
            except Exception as err:
                raise WuYeBaoApiError(f"Failed to refresh SIP token: {err}")

        if not self.sip_jwt:
            raise WuYeBaoApiError("SIP JWT not available")

        community_code = str(gate.get("communityCode") or self.community_code or "0")
        area_code = str(gate.get("areaCode") or "0")
        building_code = str(gate.get("buildingCode") or "0")
        unit_code = str(gate.get("unitCode") or "0")
        floor_code = str(gate.get("floorCode") or "0")
        device_number = str(gate.get("deviceNumber") or "")
        device_type = str(gate.get("type") or "outdoor")

        if not device_number or not self.owner_id:
            raise WuYeBaoApiError(f"Missing required fields: deviceNumber={device_number}, owner_id={self.owner_id}")

        def _do_unlock() -> dict[str, Any]:
            client = WuYeBaoSipClient(
                user=self.username,
                jwt=self.sip_jwt,
                sid=self.sip_sid,
            )
            return client.unlock(
                owner_id=self.owner_id,
                device_type=device_type,
                device_number=device_number,
                community_code=community_code,
                area_code=area_code,
                building_code=building_code,
                unit_code=unit_code,
                floor_code=floor_code,
            )

        result = await asyncio.to_thread(_do_unlock)

        if not result.get("ok"):
            raise WuYeBaoApiError(
                f"SIP unlock failed: status={result.get('status')} error={result.get('error', '')}"
            )

        return result

    async def start_monitor(self, gate: dict[str, Any]) -> dict[str, Any]:
        """Start monitoring by sending SIP MESSAGE."""
        # Auto-refresh SIP token if expired
        if not self.sip_jwt or self._sip_token_expires < time.time() + 300:
            try:
                await self.refresh_sip_token()
            except Exception as err:
                raise WuYeBaoApiError(f"Failed to refresh SIP token: {err}")

        if not self.sip_jwt:
            raise WuYeBaoApiError("SIP JWT not available")

        community_code = str(gate.get("communityCode") or self.community_code or "0")
        area_code = str(gate.get("areaCode") or "0")
        building_code = str(gate.get("buildingCode") or "0")
        unit_code = str(gate.get("unitCode") or "0")
        floor_code = str(gate.get("floorCode") or "0")
        device_number = str(gate.get("deviceNumber") or "")
        device_type = str(gate.get("type") or "wall")

        if not device_number or not self.owner_id:
            raise WuYeBaoApiError(f"Missing required fields")

        def _do_monitor() -> dict[str, Any]:
            client = WuYeBaoSipClient(
                user=self.username,
                jwt=self.sip_jwt,
                sid=self.sip_sid,
            )
            return client.monitor(
                owner_id=self.owner_id,
                device_type=device_type,
                device_number=device_number,
                community_code=community_code,
                area_code=area_code,
                building_code=building_code,
                unit_code=unit_code,
                floor_code=floor_code,
            )

        result = await asyncio.to_thread(_do_monitor)

        if not result.get("ok"):
            raise WuYeBaoApiError(
                f"SIP monitor failed: status={result.get('status')} error={result.get('error', '')}"
            )

        return result

    async def call_elevator_sip(self, gate: dict[str, Any]) -> dict[str, Any]:
        """Call the elevator via SIP MESSAGE (app: call_elevator).

        The app sends the SAME OD URI used for unlocking the unit door but
        with body {"id":null,"type":"call_elevator","content":{"room":...}}.
        room is the owner's flat number configured in the config flow.
        """
        if not self.room:
            raise WuYeBaoApiError("房间号未配置：请编辑集成配置填写房间号（如 2702）")

        # Auto-refresh SIP token if expired
        if not self.sip_jwt or self._sip_token_expires < time.time() + 300:
            try:
                await self.refresh_sip_token()
            except Exception as err:
                raise WuYeBaoApiError(f"Failed to refresh SIP token: {err}")

        if not self.sip_jwt:
            raise WuYeBaoApiError("SIP JWT not available")

        community_code = str(gate.get("communityCode") or self.community_code or "0")
        area_code = str(gate.get("areaCode") or "0")
        building_code = str(gate.get("buildingCode") or "0")
        unit_code = str(gate.get("unitCode") or "0")
        floor_code = str(gate.get("floorCode") or "0")
        device_number = str(gate.get("deviceNumber") or "")

        if not device_number:
            raise WuYeBaoApiError("Missing required fields: deviceNumber")

        def _do_call() -> dict[str, Any]:
            client = WuYeBaoSipClient(
                user=self.username,
                jwt=self.sip_jwt,
                sid=self.sip_sid,
            )
            return client.call_elevator(
                room=str(self.room),
                device_number=device_number,
                community_code=community_code,
                area_code=area_code,
                building_code=building_code,
                unit_code=unit_code,
                floor_code=floor_code,
            )

        result = await asyncio.to_thread(_do_call)

        if not result.get("ok"):
            raise WuYeBaoApiError(
                f"SIP elevator call failed: status={result.get('status')} error={result.get('error', '')}"
            )

        return result

    def find_own_gate(self, gates: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Find the owner's own unit-door gate (outdoor, unitId == self.unit_id).

        Used to build the RM (indoor-unit) URI for 户户通.  Falls back to the
        first outdoor gate so the feature still works when unitId matching
        fails.
        """
        if not gates:
            return None
        for g in gates:
            if g.get("type") == "outdoor" and self.unit_id and str(
                g.get("unitId") or ""
            ) == str(self.unit_id):
                return g
        for g in gates:
            if g.get("type") == "outdoor":
                return g
        return None

    def build_household_uri(self, gate: dict[str, Any]) -> str | None:
        """Build the RM indoor-unit SIP URI for 户户通.

        App format (captured from pcap):
            INVITE sip:RM-840-1-4-1-27-2@jhws.top;transport=tcp
        RM-<communityCode>-<areaCode>-<buildingCode>-<unitCode>-<floor>-<room>
        room "2702" -> floor 27, room 02 (leading zero trimmed: "2").
        """
        if not self.room or not gate:
            return None
        room = str(self.room)
        if len(room) >= 3:
            floor = room[:-2]
            room_code = str(int(room[-2:]))
        else:
            floor = room
            room_code = str(int(room))
        community = str(gate.get("communityCode") or self.community_code or "0")
        area = str(gate.get("areaCode") or "0")
        building = str(gate.get("buildingCode") or "0")
        unit = str(gate.get("unitCode") or "0")
        return f"RM-{community}-{area}-{building}-{unit}-{floor}-{room_code}"

    async def async_close(self) -> None:
        """Close the session."""
        await self._session.close()
