"""Data coordinator for the 物业宝（家和云联） integration.

Keeps the access token fresh (refresh -> re-login), polls the gate list and
exposes the owner info for the sensors/buttons.
"""

from __future__ import annotations

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

        Credential candidates: (phone | userId) x (accessToken | refreshToken).
        Call targets: gate uid / gate id / deviceNumber on the SIP realm.
        All sensitive values are masked in logs.
        """
        raw = raw or {}
        gate_uid = str(raw.get("uid") or "") or None
        device_number = str(raw.get("deviceNumber") or "") or None
        user_id = str(raw.get("userId") or raw.get("uid") or "") or None
        # Prefer the account owner record for the SIP identity.
        owners = (self.data or {}).get(ATTR_OWNER)
        if isinstance(owners, dict):
            user_id = str(owners.get("userId") or user_id or "") or None

        phone = str(self.entry.data.get(CONF_PHONE) or "") or None
        refresh_token = self._refresh_token

        identities: list[tuple[str, str, str]] = []
        if phone and token:
            identities.append(("phone+access", phone, token))
        if user_id and token:
            identities.append(("userId+access", user_id, token))
        if phone and refresh_token:
            identities.append(("phone+refresh", phone, refresh_token))
        if user_id and refresh_token:
            identities.append(("userId+refresh", user_id, refresh_token))

        targets: list[tuple[str, str]] = []
        if gate_uid:
            targets.append(("uid", gate_uid))
        targets.append(("gate_id", gate_id))
        if device_number:
            targets.append(("deviceNumber", device_number))

        try:
            client = SipClient()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("初始化 SIP 客户端失败: %s", err)
            return

        log: list[dict] = []
        ok_reg: tuple[str, str] | None = None

        # Reachability first.
        try:
            probe = await self.hass.async_add_executor_job(client.options, phone or "probe", None)
            log.append({"step": "options", **probe})
        except Exception as err:  # noqa: BLE001
            log.append({"step": "options", "error": str(err)})

        for label, user, secret in identities:
            if not user or not secret:
                continue
            try:
                res = await self.hass.async_add_executor_job(
                    client.register, user, secret
                )
            except Exception as err:  # noqa: BLE001
                log.append({"step": f"register:{label}", "error": str(err)})
                continue
            log.append(
                {
                    "step": f"register:{label}",
                    "status": res.get("status"),
                    "reason": res.get("reason"),
                    "pwd": mask_secret(secret),
                }
            )
            if res.get("status") == 200 and ok_reg is None:
                ok_reg = (user, secret)

        # Call with the first successful identity, then with every identity if
        # none registered, against every plausible callee.
        call_ids = (
            [(ok_reg[0], ok_reg[1])] if ok_reg else [(u, s) for _l, u, s in identities]
        )
        for user, secret in call_ids:
            for tlabel, target in targets:
                uri = f"sip:{target}@{client.host}"
                try:
                    res = await self.hass.async_add_executor_job(
                        client.invite, user, secret, uri
                    )
                except Exception as err:  # noqa: BLE001
                    log.append(
                        {
                            "step": f"invite:{tlabel}",
                            "user": user,
                            "uri": uri,
                            "error": str(err),
                        }
                    )
                    continue
                log.append(
                    {
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
                        "SIP 呼叫疑似成功: %s -> %s (status=%s) —— 可据此确定开门方案",
                        user,
                        uri,
                        res.get("status"),
                    )

        _LOGGER.info("SIP 开门诊断结果: %s", json.dumps(log, ensure_ascii=False, default=str))

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
            try:
                await self.api.open_gate(token, gate_id)
            except Exception:
                # Fast path failed -> diagnostic sweep.
                diag = await self.api.open_gate_diag(
                    token,
                    gate_id,
                    raw,
                    community_id=self._community_id,
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
