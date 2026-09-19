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
    CONF_SIP_JWT,
    CONF_SIP_SID,
    DOMAIN,
)
from .sip import JHSipClient, mask_secret

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
        self, token: str, gate_id: str, raw: dict | None, sip_token: str | None = None
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
        # A dedicated SIP token (if the call-path probe found one).
        if sip_token:
            if phone:
                identities.append(("phone+sipToken", phone, sip_token))
            if user_id:
                identities.append(("userId+sipToken", user_id, sip_token))
            if call_number:
                identities.append(("callNumber+sipToken", call_number, sip_token))

        targets: list[tuple[str, str]] = []
        # High-value INVITE targets only (the full set was probed across
        # earlier rounds without a single non-zero response; credentials are
        # the deciding factor, and any valid target then answers non-zero).
        if call_number:
            targets.append(("callNumber", call_number))
        if mn_number:
            targets.append(("callNumber-MN", mn_number))
        targets.append(("gate_id", gate_id))
        if gate_uid:
            targets.append(("uid", gate_uid))
        if binding_code:
            targets.append(("bindingCode", binding_code))
        if device_number:
            targets.append(("deviceNumber", device_number))

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

            # new-sip.jhws.top answers OPTIONS with 484 and has silently
            # dropped every REGISTER/INVITE across all rounds; probe it only.
            if host.startswith("new-sip."):
                return log

            ok_reg: tuple[str, str] | None = None
            for label, user, secret in identities:
                if not user or not secret:
                    continue
                # Standard user-less registrar Request-URI only: the
                # user-qualified variant was probed across multiple rounds
                # with identical silent-drop results.
                try:
                    res = client.register(user, secret, challenge=challenge, uri_user=None)
                except Exception as err:  # noqa: BLE001
                    log.append(
                        {
                            "server": f"{host}:{port}",
                            "step": f"register:{label}:std",
                            "error": str(err),
                        }
                    )
                    continue
                log.append(
                    {
                        "server": f"{host}:{port}",
                        "step": f"register:{label}:std",
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
    def _extract_sip_token(tok_diag: list[dict]) -> str | None:
        """Look through the call-path probe results for a token-shaped value."""
        keys = (
            "sipToken", "sipAccessToken", "accessToken", "token",
            "secret", "sipSecret", "password", "sipPassword", "credential",
        )
        for entry in tok_diag:
            if not entry.get("ok"):
                continue
            data = entry.get("data") or ""
            text = str(data)
            for key in keys:
                marker = f"'{key}':"
                pos = text.find(marker)
                if pos < 0:
                    marker = f'"{key}":'
                    pos = text.find(marker)
                if pos >= 0:
                    value = text[pos + len(marker):].strip().strip(chr(39) + chr(34)).split(',')[0]
                    value = value.strip()
                    if value and len(value) >= 8 and "{" not in value:
                        return value
        return None

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

    async def _sip_unlock(self, gate_id: str, raw: dict | None) -> dict:
        """Open a gate via the reverse-engineered SIP MESSAGE protocol.

        Protocol (pcap-verified 2026-09-19):
          REGISTER sip:new-sip.jhws.top;transport=tcp
            From: <sip:<phone>@jhws.top>
            SID: <client sid>
            JWT: <client-token JWT>
          MESSAGE sip:GT-<communityCode>-<areaCode>-<buildingCode>-<unitCode>-<floorCode>-<deviceNumber>@jhws.top
            Route: <sip:new-sip.jhws.top;transport=tcp;lr>
            Body: {"id":"<uuid>","type":"unlock",
                   "content":{"device":"wall|outdoor",
                              "ownerId":"<owner record id>",
                              "deviceNumber":"<deviceNumber>"}}
        """
        raw = raw or {}
        phone = str(self.entry.data.get(CONF_PHONE) or "")
        sip_jwt = str(self.entry.data.get(CONF_SIP_JWT) or "")
        sip_sid = str(self.entry.data.get(CONF_SIP_SID) or "")
        if not phone or not sip_jwt or not sip_sid:
            return {
                "ok": False,
                "status": 0,
                "error": "缺少 SIP 凭据：请在集成选项中填写 sip_jwt 和 sip_sid",
            }

        # Gate identity fields
        community_code = str(raw.get("communityCode") or "0")
        area_code = str(raw.get("areaCode") or "0")
        building_code = str(raw.get("buildingCode") or "0")
        unit_code = str(raw.get("unitCode") or "0")
        floor_code = str(raw.get("floorCode") or "0")
        device_number = str(raw.get("deviceNumber") or "")
        # App uses type=="wall" for perimeter gates, "outdoor" for unit entrances.
        device_type = str(raw.get("type") or "outdoor")
        # ownerId is the owner record's id (NOT bindingId).
        owners = self.data.get(ATTR_OWNER) if self.data else None
        owner_id = ""
        if isinstance(owners, dict):
            owner_id = str(owners.get("id") or owners.get("userId") or "")

        if not device_number or not owner_id:
            return {
                "ok": False,
                "status": 0,
                "error": (
                    f"门禁数据缺字段: deviceNumber={device_number!r} owner_id={owner_id!r}"
                ),
            }

        def _do() -> dict:
            client = JHSipClient(
                user=phone,
                jwt=sip_jwt,
                sid=sip_sid,
            )
            return client.unlock(
                gate=raw,
                owner_id=owner_id,
                device=device_type,
                device_number=device_number,
                community_code=community_code,
                area_code=area_code,
                building_code=building_code,
                unit_code=unit_code,
                floor_code=floor_code,
            )

        try:
            return await asyncio.to_thread(_do)
        except Exception as err:  # noqa: BLE001
            return {"ok": False, "status": 0, "error": f"exception: {err!r}"}

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
                    # No HTTP endpoint: probe the call paths for a dedicated
                    # SIP token, then try the SIP cloud-intercom path.
                    sip_token: str | None = None
                    try:
                        tok_diag = await self.api.sip_token_diag(
                            token,
                            raw,
                            community_id=self._community_id,
                            call_number=self._last_call_number,
                        )
                        _LOGGER.info(
                            "SIP令牌探测(诊断): %s",
                            json.dumps(tok_diag, ensure_ascii=False, default=str),
                        )
                        sip_token = self._extract_sip_token(tok_diag)
                        if sip_token:
                            _LOGGER.info("SIP令牌探测: 疑似取得 SIP token (长度 %d)", len(sip_token))
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.warning("SIP令牌探测失败: %s", err)
                    # HTTP endpoint does not exist (all 404). The app opens
                    # doors over SIP MESSAGE with JWT auth (see sip.py). Use the
                    # credentials the user supplied in the integration options.
                    sip_result = await self._sip_unlock(gate_id, raw)
                    if sip_result.get("ok"):
                        result = "success"
                        _LOGGER.info(
                            "SIP 开门成功: %s (status=%s, GT=%s)",
                            sip_result.get("gt_uri"),
                            sip_result.get("status"),
                            sip_result.get("gt_uri"),
                        )
                    else:
                        result = "failed"
                        _LOGGER.warning(
                            "SIP 开门失败: status=%s error=%s",
                            sip_result.get("status"),
                            sip_result.get("error") or sip_result.get("raw", "")[:500],
                        )
                        raise WuyeBaoConnectionError(
                            f"SIP 开门失败: status={sip_result.get('status')} "
                            f"reason={sip_result.get('reason')}. "
                            "请检查配置中的 SIP JWT/SID 是否有效（JWT 约 7 天过期）。"
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
