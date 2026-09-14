"""Config flow for the WuyeBao integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector

from .api import WuyeBaoAPI, WuyeBaoAuthError, WuyeBaoConnectionError
from .const import (
    CONF_AUTH_SCHEME,
    CONF_BASE_URL,
    CONF_DEVICE_ID_FIELD,
    CONF_DEVICE_NAME_FIELD,
    CONF_DEVICES_PATH,
    CONF_LOGIN_PATH,
    CONF_OPEN_METHOD,
    CONF_OPEN_PATH,
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_POLL_INTERVAL,
    DEFAULT_AUTH_SCHEME,
    DEFAULT_DEVICE_ID_FIELD,
    DEFAULT_DEVICE_NAME_FIELD,
    DEFAULT_DEVICES_PATH,
    DEFAULT_LOGIN_PATH,
    DEFAULT_OPEN_METHOD,
    DEFAULT_OPEN_PATH,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


def _build_api(hass: HomeAssistant, phone: str, password: str, options: dict) -> WuyeBaoAPI:
    return WuyeBaoAPI(
        hass,
        phone=phone,
        password=password,
        base_url=options[CONF_BASE_URL],
        login_path=options.get(CONF_LOGIN_PATH, DEFAULT_LOGIN_PATH),
        devices_path=options.get(CONF_DEVICES_PATH, DEFAULT_DEVICES_PATH),
        open_path=options.get(CONF_OPEN_PATH, DEFAULT_OPEN_PATH),
        open_method=options.get(CONF_OPEN_METHOD, DEFAULT_OPEN_METHOD),
        device_id_field=options.get(CONF_DEVICE_ID_FIELD, DEFAULT_DEVICE_ID_FIELD),
        device_name_field=options.get(CONF_DEVICE_NAME_FIELD, DEFAULT_DEVICE_NAME_FIELD),
        auth_scheme=options.get(CONF_AUTH_SCHEME, DEFAULT_AUTH_SCHEME),
    )


async def _validate_login(
    hass: HomeAssistant, phone: str, password: str, options: dict
) -> str | None:
    """Log in to validate the configuration.

    Returns the token on success, or raises for connectivity errors.
    """
    api = _build_api(hass, phone, password, options)
    return await api.login()


class WuyeBaoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for 物业宝."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            phone = user_input[CONF_PHONE]
            password = user_input[CONF_PASSWORD]
            options = {
                CONF_BASE_URL: user_input[CONF_BASE_URL].strip(),
                CONF_LOGIN_PATH: DEFAULT_LOGIN_PATH,
                CONF_DEVICES_PATH: DEFAULT_DEVICES_PATH,
                CONF_OPEN_PATH: DEFAULT_OPEN_PATH,
                CONF_OPEN_METHOD: DEFAULT_OPEN_METHOD,
                CONF_DEVICE_ID_FIELD: DEFAULT_DEVICE_ID_FIELD,
                CONF_DEVICE_NAME_FIELD: DEFAULT_DEVICE_NAME_FIELD,
                CONF_AUTH_SCHEME: DEFAULT_AUTH_SCHEME,
                CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
            }
            try:
                await _validate_login(self.hass, phone, password, options)
            except WuyeBaoAuthError:
                errors["base"] = "invalid_auth"
            except WuyeBaoConnectionError:
                errors["base"] = "cannot_connect"
            except Exception as err:  # noqa: BLE001 - surface unknown errors
                _LOGGER.exception("物业宝配置流程发生未知错误")
                errors["base"] = "unknown"
                if err.__class__.__name__ == "ConfigEntryAuthFailed":
                    errors["base"] = "invalid_auth"

            if not errors:
                await self.async_set_unique_id(f"wuyebao_{phone}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=phone,
                    data={CONF_PHONE: phone, CONF_PASSWORD: password},
                    options=options,
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_PHONE): str,
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Required(CONF_BASE_URL): str,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    async def async_step_reauth(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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
            phone = user_input[CONF_PHONE]
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
    """Handle the options flow: captured API endpoints and fields."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the API endpoints and request details."""
        errors: dict[str, str] = {}
        if user_input is not None:
            options = {
                CONF_BASE_URL: user_input[CONF_BASE_URL].strip(),
                CONF_LOGIN_PATH: user_input[CONF_LOGIN_PATH].strip(),
                CONF_DEVICES_PATH: user_input[CONF_DEVICES_PATH].strip(),
                CONF_OPEN_PATH: user_input[CONF_OPEN_PATH].strip(),
                CONF_OPEN_METHOD: user_input[CONF_OPEN_METHOD],
                CONF_DEVICE_ID_FIELD: user_input[CONF_DEVICE_ID_FIELD].strip(),
                CONF_DEVICE_NAME_FIELD: user_input[CONF_DEVICE_NAME_FIELD].strip(),
                CONF_AUTH_SCHEME: user_input[CONF_AUTH_SCHEME].strip(),
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
                    CONF_BASE_URL, default=opts.get(CONF_BASE_URL, "")
                ): str,
                vol.Optional(
                    CONF_LOGIN_PATH, default=opts.get(CONF_LOGIN_PATH, DEFAULT_LOGIN_PATH)
                ): str,
                vol.Optional(
                    CONF_DEVICES_PATH,
                    default=opts.get(CONF_DEVICES_PATH, DEFAULT_DEVICES_PATH),
                ): str,
                vol.Optional(
                    CONF_OPEN_PATH, default=opts.get(CONF_OPEN_PATH, DEFAULT_OPEN_PATH)
                ): str,
                vol.Optional(
                    CONF_OPEN_METHOD, default=opts.get(CONF_OPEN_METHOD, DEFAULT_OPEN_METHOD)
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["POST", "GET"],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_DEVICE_ID_FIELD,
                    default=opts.get(CONF_DEVICE_ID_FIELD, DEFAULT_DEVICE_ID_FIELD),
                ): str,
                vol.Optional(
                    CONF_DEVICE_NAME_FIELD,
                    default=opts.get(CONF_DEVICE_NAME_FIELD, DEFAULT_DEVICE_NAME_FIELD),
                ): str,
                vol.Optional(
                    CONF_AUTH_SCHEME, default=opts.get(CONF_AUTH_SCHEME, DEFAULT_AUTH_SCHEME)
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
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors
        )
