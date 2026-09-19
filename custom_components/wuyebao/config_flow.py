"""Config flow for the 物业宝（家和云联） integration.

The user enters their 物业宝(业主) app phone number and password; the
integration logs in to https://wuye.jhws.top/api/client/anon/token directly,
so no packet capture is required.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector

from .api import WuyeBaoAPI, WuyeBaoAuthError, WuyeBaoConnectionError
from .const import (
    CONF_BASE_URL,
    CONF_CLIENT_ID,
    CONF_COMMUNITY_FIELD,
    CONF_COMMUNITY_ID,
    CONF_COMMUNITY_NAME,
    CONF_OPEN_METHOD,
    CONF_OPEN_PATH,
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_SIP_JWT,
    CONF_SIP_SID,
    CONF_POLL_INTERVAL,
    DEFAULT_BASE_URL,
    DEFAULT_CLIENT_ID,
    DEFAULT_COMMUNITY_FIELD,
    DEFAULT_OPEN_METHOD,
    DEFAULT_OPEN_PATH,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

FLOW_COMMUNITIES = "communities"
FLOW_PHONE = "phone"
FLOW_PASSWORD = "password"
FLOW_OPTIONS = "options"


def _community_label(community: dict[str, Any]) -> str:
    """Human-readable label for a community record."""
    name = community.get("communityName") or community.get("name") or "未命名小区"
    community_id = community.get("communityId") or community.get("id")
    if community_id is not None:
        return f"{name}（{community_id}）"
    return str(name)


def _build_api(hass: HomeAssistant, phone: str, password: str, options: dict) -> WuyeBaoAPI:
    return WuyeBaoAPI(
        hass,
        phone=phone,
        password=password,
        base_url=options.get(CONF_BASE_URL, DEFAULT_BASE_URL),
        client_id=options.get(CONF_CLIENT_ID, DEFAULT_CLIENT_ID),
        open_path=options.get(CONF_OPEN_PATH, DEFAULT_OPEN_PATH),
        open_method=options.get(CONF_OPEN_METHOD, DEFAULT_OPEN_METHOD),
        community_field=options.get(CONF_COMMUNITY_FIELD, DEFAULT_COMMUNITY_FIELD),
    )


async def _validate_login(
    hass: HomeAssistant, phone: str, password: str, options: dict
) -> WuyeBaoAPI:
    """Log in to validate the configuration and return the API client."""
    api = _build_api(hass, phone, password, options)
    await api.login()
    return api


class WuyeBaoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for 物业宝（家和云联）."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step: phone + password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            phone = user_input[CONF_PHONE].strip()
            password = user_input[CONF_PASSWORD]
            options = {
                CONF_BASE_URL: DEFAULT_BASE_URL,
                CONF_CLIENT_ID: DEFAULT_CLIENT_ID,
                CONF_OPEN_PATH: DEFAULT_OPEN_PATH,
                CONF_OPEN_METHOD: DEFAULT_OPEN_METHOD,
                CONF_COMMUNITY_FIELD: DEFAULT_COMMUNITY_FIELD,
                CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
            }
            api: WuyeBaoAPI | None = None
            try:
                api = await _validate_login(self.hass, phone, password, options)
            except WuyeBaoAuthError:
                errors["base"] = "invalid_auth"
            except WuyeBaoConnectionError:
                errors["base"] = "cannot_connect"
            except Exception as err:  # noqa: BLE001 - surface unknown errors
                _LOGGER.exception("物业宝配置流程发生未知错误")
                errors["base"] = "unknown"
                if err.__class__.__name__ == "ConfigEntryAuthFailed":
                    errors["base"] = "invalid_auth"

            if api is not None:
                # The gates endpoint requires a selected community: fetch the
                # communities bound to this phone and let the user choose.
                communities: list[dict[str, Any]] = []
                try:
                    communities = await api.get_communities()
                except WuyeBaoConnectionError:
                    errors["base"] = "cannot_connect"
                if not errors:
                    if not communities:
                        errors["base"] = "no_community"
                    elif len(communities) == 1:
                        return await self._create_entry(phone, password, options, communities[0])
                    else:
                        self.context[FLOW_COMMUNITIES] = communities
                        self.context[FLOW_PHONE] = phone
                        self.context[FLOW_PASSWORD] = password
                        self.context[FLOW_OPTIONS] = options
                        return await self.async_step_community()

        schema = vol.Schema(
            {
                vol.Required(CONF_PHONE): str,
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    async def async_step_community(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick the community when several are bound to the phone."""
        communities: list[dict[str, Any]] = self.context.get(FLOW_COMMUNITIES, [])
        if not communities:
            return self.async_abort(reason="no_community")

        if user_input is not None:
            index = user_input[CONF_COMMUNITY_NAME]
            try:
                community = next(
                    c for c in communities if _community_label(c) == index
                )
            except StopIteration:
                return self.async_abort(reason="no_community")
            return await self._create_entry(
                self.context.get(FLOW_PHONE, ""),
                self.context.get(FLOW_PASSWORD, ""),
                self.context.get(FLOW_OPTIONS, {}),
                community,
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_COMMUNITY_NAME): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[_community_label(c) for c in communities],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="community", data_schema=schema)

    async def _create_entry(
        self,
        phone: str,
        password: str,
        options: dict[str, Any],
        community: dict[str, Any],
    ) -> ConfigFlowResult:
        """Create the config entry with the selected community."""
        community_id = community.get("communityId") or community.get("id")
        community_name = (
            community.get("communityName")
            or community.get("name")
            or str(community_id or "未命名小区")
        )
        data = {
            CONF_PHONE: phone,
            CONF_PASSWORD: password,
            CONF_COMMUNITY_ID: str(community_id) if community_id is not None else None,
            CONF_COMMUNITY_NAME: str(community_name),
        }
        await self.async_set_unique_id(f"wuyebao_{phone}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"物业宝 {community_name}",
            data=data,
            options=options,
        )

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start reauthentication after a persistent login failure."""
        return await self.async_step_reauth_confirm(user_input)

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauthentication."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="reauth_successful")

        if user_input is not None:
            phone = user_input[CONF_PHONE].strip()
            password = user_input[CONF_PASSWORD]
            options = {**entry.options}
            try:
                await _validate_login(self.hass, phone, password, options)
            except WuyeBaoAuthError:
                errors["base"] = "invalid_auth"
            except WuyeBaoConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("物业宝重新认证发生未知错误")
                errors["base"] = "unknown"

            if not errors:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={CONF_PHONE: phone, CONF_PASSWORD: password},
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        schema = vol.Schema(
            {
                vol.Required(CONF_PHONE, default=entry.data.get(CONF_PHONE, "")): str,
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=schema, errors=errors
        )

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return WuyeBaoOptionsFlow(config_entry)


class WuyeBaoOptionsFlow(OptionsFlow):
    """Handle the options flow: server / unlock details."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the server settings and unlock request details."""
        errors: dict[str, str] = {}
        if user_input is not None:
            options = {
                CONF_BASE_URL: user_input[CONF_BASE_URL].strip().rstrip("/") + "/",
                CONF_CLIENT_ID: user_input[CONF_CLIENT_ID].strip(),
                CONF_OPEN_PATH: user_input[CONF_OPEN_PATH].strip().lstrip("/"),
                CONF_OPEN_METHOD: user_input[CONF_OPEN_METHOD],
                CONF_COMMUNITY_FIELD: user_input[CONF_COMMUNITY_FIELD].strip(),
                CONF_POLL_INTERVAL: int(user_input[CONF_POLL_INTERVAL]),
            }
            entry = self.config_entry
            try:
                await _validate_login(
                    self.hass,
                    entry.data[CONF_PHONE],
                    entry.data[CONF_PASSWORD],
                    options,
                )
            except WuyeBaoAuthError:
                errors["base"] = "invalid_auth"
            except WuyeBaoConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("物业宝选项保存时发生未知错误")
                errors["base"] = "unknown"

            if not errors:
                self.hass.config_entries.async_update_entry(
                    entry,
                    options=options,
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_create_entry(title="", data=options)

        opts = {**self.config_entry.options}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_BASE_URL, default=opts.get(CONF_BASE_URL, DEFAULT_BASE_URL)
                ): str,
                vol.Optional(
                    CONF_CLIENT_ID, default=opts.get(CONF_CLIENT_ID, DEFAULT_CLIENT_ID)
                ): str,
                vol.Optional(
                    CONF_OPEN_PATH, default=opts.get(CONF_OPEN_PATH, DEFAULT_OPEN_PATH)
                ): str,
                vol.Optional(
                    CONF_OPEN_METHOD,
                    default=opts.get(CONF_OPEN_METHOD, DEFAULT_OPEN_METHOD),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["POST", "GET"],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_COMMUNITY_FIELD,
                    default=opts.get(CONF_COMMUNITY_FIELD, DEFAULT_COMMUNITY_FIELD),
                ): str,
                vol.Optional(
                    CONF_POLL_INTERVAL,
                    default=opts.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_POLL_INTERVAL,
                        max=MAX_POLL_INTERVAL,
                        step=10,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_SIP_JWT,
                    description={"suggested_value": opts.get(CONF_SIP_JWT, "")},
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Optional(
                    CONF_SIP_SID,
                    description={"suggested_value": opts.get(CONF_SIP_SID, "")},
                ): str,
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors
        )
