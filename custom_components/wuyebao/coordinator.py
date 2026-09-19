"""Data coordinator for the 物业宝（家和云联） integration.

Keeps the access token fresh (refresh -> re-login), polls the gate list and
exposes the owner info for the sensors/buttons.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import WuyeBaoAPI, WuyeBaoAuthError, WuyeBaoConnectionError
from .const import (
    ATTR_GATES,
    ATTR_LAST_OPEN,
    ATTR_OWNER,
    CONF_COMMUNITY_FIELD,
    CONF_COMMUNITY_ID,
    CONF_PHONE,
    DOMAIN,
)
from .sip import SipClient, mask_secret

_LOGGER = logging.getLogger(__name__)


class WuyeBaoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll the gate list and keep the auth token fresh."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: WuyeBaoAPI,
        poll_interval: int = 60,
    ) -> None:
        self.api = api
        self.entry = entry
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self.last_open: dict[str, Any] | None = None
        self._community_id: str | None = entry.data.get(CONF_COMMUNITY_ID)
        self._last_call_number: str | None = None
        self._community_field: str = entry.options.get(
            CONF_COMMUNITY_FIELD, "communityId"
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=max(10, poll_interval)),
        )

    async def _ensure_token(self) -> str:
        """Return a valid access token, refreshing or logging in as needed."""
        if self._access_token is not None:
            return self._access_token

        if self._refresh_token:
            try:
                access, refresh = await self.api.refresh(self._refresh_token)
                self._access_token = access
                self._refresh_token = refresh
                return access
            except WuyeBaoAuthError:
                _LOGGER.info("物业宝刷新令牌已失效，改用账号密码重新登录")
                self._refresh_token = None
            except WuyeBaoConnectionError:
                _LOGGER.debug("物业宝刷新令牌请求失败，改用账号密码登录")

        access, refresh = await self.api.login()
        self._access_token = access
        self._refresh_token = refresh
        return access

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch the gate list (and refresh credentials when the token expires)."""
        try:
            token = await self._ensure_token()
        except WuyeBaoAuthError as err:
            raise ConfigEntryAuthFailed(f"物业宝登录失败: {err}") from err

        try:
            gates = await self.api.get_gates(
                token, community_id=self._community_id, community_field=self._community_field
            )
        except WuyeBaoAuthError:
            # Token expired: drop it and retry once with fresh credentials.
            _LOGGER.info("物业宝 token 已失效，尝试重新登录")
            self._access_token = None
            try:
                token = await self._ensure_token()
                gates = await self.api.get_gates(
                    token, community_id=self._community_id, community_field=self._community_field
                )
            except WuyeBaoAuthError as err:
                raise ConfigEntryAuthFailed(f"物业宝重新登录失败: {err}") from err
        except WuyeBaoConnectionError as err:
            raise UpdateFailed(f"获取门禁列表失败: {err}") from err

        # Diagnostic aid: once, dump ALL raw gate records so the correct
        # open-door endpoint / SIP identity can be determined when the default
        # HTTP path is rejected by the server (records may carry callNumber
        # / sip fields per gate type).
        if gates and not getattr(self, "_logged_gate_dump", False):
            self._logged_gate_dump = True
            _LOGGER.info(
                "门禁列表共 %d 条；全部原始记录: %s",
                len(gates),
                json.dumps(
                    [g.get("raw") for g in gates],
                    ensure_ascii=False,
                    default=str,
                ),
            )

        # Owner info is best-effort: a failure here must not break the update.
        owner: dict[str, Any] | None = None
        try:
            owners = await self.api.get_owners(token)
            if owners:
                if not getattr(self, "_logged_owner_dump", False):
                    self._logged_owner_dump = True
                    _LOGGER.info(
                        "业主响应(第一条完整): %s",
                        json.dumps(owners[0], ensure_ascii=False, default=str),
                    )
                owner = dict(owners[0])
                owner.pop("raw", None)
        except Exception as err:  # noqa: BLE001 - non-fatal
            _LOGGER.debug("获取业主信息失败（忽略）: %s", err)

        return {ATTR_GATES: gates, ATTR_OWNER: owner, ATTR_LAST_OPEN: self.last_open}

    async def _sip_open_diag(
        self, token: str, gate_id: str, raw: dict | None
    ) -> None:
        """Probe SIP registration and call candidates; log every outcome.

        Transport: TCP (the official app's pjsua2 stack uses
        `sip:sip.jhws.top;transport=tcp`). The three known SIP server
        endpoints are probed **in parallel**. Credential candidates:
        (phone | userId) x accessToken (login response carries no
        refreshToken). Call targets cover every identifier carried by the
        gate record (uid / id / deviceNumber / buildingId / unitId / areaId
        / communityCode / bindingCode) plus common prefixed forms. All
        secrets are masked.
        """
        raw = raw or {}
        gate_uid = str(raw.get("uid") or "") or None
        device_number = str(raw.get("deviceNumber") or "") or None
        user_id = str(raw.get("userId") or raw.get("uid") or "") or None
        building_id = str(raw.get("buildingId") or "") or None
        unit_id = str(raw.get("unitId") or "") or None
        area_id = str(raw.get("areaId") or "") or None
        area_code = str(raw.get("areaCode") or "") or None
        community_code = str(raw.get("communityCode") or "") or None
        binding_code = str(raw.get("bindingCode") or "") or None
        building_code = str(raw.get("buildingCode") or "") or None
        unit_code = str(raw.get("unitCode") or "") or None
        floor_code = str(raw.get("floorCode") or "") or None
        gate_pwd = str(raw.get("password") or "") or None
        # The intercom call records show the app's real callee identifier
        # format: RM-<community>-<area>-<building>-<unit>-<floor>-<device>
        # (e.g. RM-840-1-4-2-24-2) and MN-<community>-0-0-0-0-<device> for
        # walls/vehicle gates. Build the same identifiers for this gate.
        call_number: str | None = None
        mn_number: str | None = None
        if community_code and device_number:
            segs = [community_code]
            for part in (area_code, building_code, unit_code, floor_code):
                segs.append(part if part is not None else "0")
            call_number = f"RM-{'-'.join(segs)}-{device_number}"
            mn_number = f"MN-{community_code}-0-0-0-0-{device_number}"
        # Prefer the account owner record for the SIP identity.
        owners = (self.data or {}).get(ATTR_OWNER)
        if isinstance(owners, dict):
            user_id = str(owners.get("userId") or user_id or "") or None
            if not binding_code and owners.get("bindingCode"):
                binding_code = str(owners.get("bindingCode")) or None

        phone = str(self.entry.data.get(CONF_PHONE) or "") or None

        # Fetch intercom call records first: the app logs every SIP call
        # here, so the records may expose the real callee identifiers
        # (callNumber / uri / account) that the app uses.
        community_id = str(self.entry.data.get(CONF_COMMUNITY_ID) or "") or None
        try:
            records = await self.api.get_call_records(token, community_id)
        except Exception as err:  # noqa: BLE001
            records = {"error": str(err)}
        _LOGGER.info("呼叫记录(诊断): %s", str(records)[:2000])

        identities: list[tuple[str, str, str]] = []
        # The app's cloud-intercom account is very likely the same as the
        # owner account: username = phone/userId, password = the login
        # password the user entered (not the HTTP JWT). Test both.
        login_pwd = getattr(self.api, "_password", None)
        if phone and login_pwd:
            identities.append(("phone+pwd", phone, login_pwd))
        if user_id and login_pwd:
            identities.append(("userId+pwd", user_id, login_pwd))
        if phone and token:
            identities.append(("phone+access", phone, token))
        if user_id and token:
            identities.append(("userId+access", user_id, token))
        # The callNumber identifier as the SIP username, with the login
        # password / access token / the gate's own password as candidates.
        if call_number and login_pwd:
            identities.append(("callNumber+pwd", call_number, login_pwd))
        if call_number and token:
            identities.append(("callNumber+access", call_number, token))
        if call_number and gate_pwd:
            identities.append(("callNumber+gate", call_number, gate_pwd))
        if phone and gate_pwd:
            identities.append(("phone+gate", phone, gate_pwd))

        targets: list[tuple[str, str]] = []
        if gate_uid:
            targets.append(("uid", gate_uid))
        targets.append(("gate_id", gate_id))
        if device_number:
            targets.append(("deviceNumber", device_number))
        if call_number:
            targets.append(("callNumber", call_number))
        if mn_number:
            targets.append(("callNumber-MN", mn_number))
        if building_id:
            targets.append(("buildingId", building_id))
        if unit_id:
            targets.append(("unitId", unit_id))
        if area_id:
            targets.append(("areaId", area_id))
        if community_code:
            targets.append(("communityCode", community_code))
        if binding_code:
            targets.append(("bindingCode", binding_code))
        # Common prefixed forms (area/community + device number).
        if device_number and area_code:
            targets.append(("areaCode-deviceNumber", f"{area_code}-{device_number}"))
        if device_number and community_code:
            targets.append(("communityCode-deviceNumber", f"{community_code}-{device_number}"))

        servers = [
            ("new-sip.jhws.top", 58583),
            ("sip.jhws.top", 5060),
            ("sip.jhws.top", 58583),
        ]

        def probe_server(
            host: str, port: int
        ) -> list[dict]:
            """Full synchronous probe of one endpoint (runs in executor)."""
            log: list[dict] = []
            try:
                client = SipClient(host, port, transport="tcp", timeout=3.0, retries=1)
            except Exception as err:  # noqa: BLE001
                log.append({"server": f"{host}:{port}", "init_error": str(err)})
                return log

            try:
                probe = client.options(phone or "probe", None)
            except Exception as err:  # noqa: BLE001
                log.append({"server": f"{host}:{port}", "options_error": str(err)})
                return log
            log.append({"server": f"{host}:{port}", "options": probe.get("status")})
            if not probe.get("status"):
                # Unreachable from this network.
                return log

            # The proxy answers OPTIONS with 407 and a nonce; REGISTER/INVITE
            # are silently dropped unless they already carry a valid
            # Proxy-Authorization, so pre-compute it from this challenge.
            challenge: tuple[str, str] | None = None
            realm = probe.get("realm")
            nonce = probe.get("nonce")
            if realm and nonce:
                challenge = (realm, nonce)
            log.append(
                {
                    "server": f"{host}:{port}",
                    "step": "challenge",
                    "realm": realm,
                    "preauth": bool(challenge),
                }
            )

            ok_reg: tuple[str, str] | None = None
            for label, user, secret in identities:
                if not user or not secret:
                    continue
                # Two Request-URI forms: user-less (standard registrar URI)
                # and user-qualified (some endpoints reject the user-less one).
                for vlabel, uri_user in (("std", None), ("user", user)):
                    try:
                        res = client.register(user, secret, challenge=challenge, uri_user=uri_user)
                    except Exception as err:  # noqa: BLE001
                        log.append(
                            {
                                "server": f"{host}:{port}",
                                "step": f"register:{label}:{vlabel}",
                                "error": str(err),
                            }
                        )
                        continue
                    log.append(
                        {
                            "server": f"{host}:{port}",
                            "step": f"register:{label}:{vlabel}",
                            "status": res.get("status"),
                            "reason": res.get("reason"),
                            "pwd": mask_secret(secret),
                        }
                    )
                    if res.get("status") == 200 and ok_reg is None:
                        ok_reg = (user, secret)

            call_ids = (
                [(ok_reg[0], ok_reg[1])]
                if ok_reg
                else [
                    (u, s)
                    for _l, u, s in identities
                    if _l.endswith("+pwd")
                ]
            )
            if not challenge:
                # Without realm+nonce an authenticated INVITE is impossible and
                # an unauthenticated one is silently dropped; skip to save time.
                log.append(
                    {"server": f"{host}:{port}", "step": "invite", "note": "skipped (no challenge)"}
                )
                return log
            for user, secret in call_ids:
                for tlabel, target in targets:
                    uri = f"sip:{target}@{client.host}"
                    try:
                        res = client.invite(user, secret, uri, challenge=challenge)
                    except Exception as err:  # noqa: BLE001
                        log.append(
                            {
                                "server": f"{host}:{port}",
                                "step": f"invite:{tlabel}",
                                "user": user,
                                "uri": uri,
                                "error": str(err),
                            }
                        )
                        continue
                    log.append(
                        {
                            "server": f"{host}:{port}",
                            "step": f"invite:{tlabel}",
                            "user": user,
                            "uri": uri,
                            "status": res.get("status"),
                            "reason": res.get("reason"),
                            "pwd": mask_secret(secret),
                        }
                    )
                    if res.get("status") in (100, 180, 183, 200):
                        _LOGGER.info(
                            "SIP 呼叫疑似成功: %s %s -> %s (status=%s) —— 可据此确定开门方案",
                            host,
                            user,
                            uri,
                            res.get("status"),
                        )
            return log

        # Probe the three endpoints in parallel.
        logs = await asyncio.gather(
            *[
                self.hass.async_add_executor_job(probe_server, host, port)
                for host, port in servers
            ]
        )
        log: list[dict] = []
        for part in logs:
            log.extend(part)

        _LOGGER.info("SIP 开门诊断结果: %s", json.dumps(log, ensure_ascii=False, default=str))

    @staticmethod
    def _derive_call_number(raw: dict | None) -> str | None:
        """Build the app-style intercom identifier:
        RM-<community>-<area>-<building>-<unit>-<floor>-<device>."""
        raw = raw or {}
        community_code = str(raw.get("communityCode") or "") or None
        device_number = str(raw.get("deviceNumber") or "") or None
        if not community_code or not device_number:
            return None
        segs = [community_code]
        for key in ("areaCode", "buildingCode", "unitCode", "floorCode"):
            part = str(raw.get(key) or "") or None
            segs.append(part if part is not None else "0")
        return f"RM-{'-'.join(segs)}-{device_number}"

    async def open_gate(self, gate_id: str) -> None:
        """Open a gate and record the result.

        Runs the configured path first (fast path). If it fails, runs the
        diagnostic candidate set and logs every outcome so a working endpoint
        can be identified (or SIP confirmed as the only option).
        """
        result = "success"
        gates = self.data.get(ATTR_GATES, []) if self.data else []
        raw = next((g.get("raw") for g in gates if g.get("gate_id") == gate_id), None)
        try:
            token = await self._ensure_token()
            # Derive the app-style callNumber (RM-...) for the HTTP call-based
            # diagnostics and the SIP diag.
            try:
                self._last_call_number = self._derive_call_number(raw)
            except Exception:  # noqa: BLE001
                self._last_call_number = None
            try:
                await self.api.open_gate(token, gate_id)
            except Exception:
                # Fast path failed -> diagnostic sweep.
                diag = await self.api.open_gate_diag(
                    token,
                    gate_id,
                    raw,
                    community_id=self._community_id,
                    call_number=self._last_call_number,
                )
                hits = [d for d in diag if d.get("ok")]
                _LOGGER.info(
                    "开门诊断结果: %s",
                    json.dumps(diag, ensure_ascii=False, default=str),
                )
                if hits:
                    # A diagnostic candidate succeeded; remember its path so it
                    # can be promoted to the configured open path later.
                    best = hits[0]
                    _LOGGER.info(
                        "开门候选成功: %s %s (code=%s) —— 可把它设为「选项」中的开门接口路径",
                        best.get("method"),
                        best.get("path"),
                        best.get("code"),
                    )
                    self._open_hit = best
                else:
                    # No HTTP endpoint: try the SIP cloud-intercom path.
                    await self._sip_open_diag(token, gate_id, raw)
                    result = "failed"
                    raise WuyeBaoConnectionError(
                        "HTTP 开门接口与候选方案均未成功，SIP 诊断结果见日志。"
                        "若 SIP 注册/呼叫状态为 200，请把日志发给维护者确认凭据组合。"
                    )
        except WuyeBaoAuthError:
            self._access_token = None
            try:
                token = await self._ensure_token()
                await self.api.open_gate(token, gate_id)
            except WuyeBaoAuthError:
                result = "auth_failed"
                raise
            except Exception as err:  # noqa: BLE001 - record and re-raise
                result = "failed"
                raise
        except WuyeBaoConnectionError:
            result = "failed"
            raise
        finally:
            self.last_open = {
                "gate_id": gate_id,
                "result": result,
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            data = dict(self.data) if self.data else {}
            data[ATTR_LAST_OPEN] = self.last_open
            self.async_set_updated_data(data)
