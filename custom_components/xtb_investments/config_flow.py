"""Config flow for XTB Investments."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries

from .api import XTBBridgeError, XTBBridgeSetupClient
from .const import (
    CONF_BRIDGE_URL,
    CONF_EMAIL,
    CONF_OTP,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    DEFAULT_BRIDGE_URL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)


class XTBInvestmentsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for XTB Investments."""

    VERSION = 1
    _email: str
    _password: str
    _challenge_id: str
    _reauth: bool = False

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._reauth = False
            self._email = user_input[CONF_EMAIL].strip()
            self._password = user_input[CONF_PASSWORD]

            await self.async_set_unique_id(self._email.lower())
            self._abort_if_unique_id_configured()

            setup = XTBBridgeSetupClient(self.hass, bridge_url=DEFAULT_BRIDGE_URL)
            try:
                result = await setup.async_start_login(email=self._email, password=self._password)
            except XTBBridgeError:
                errors["base"] = "cannot_connect"
            else:
                return await self._handle_login_result(result)

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(),
            errors=errors,
        )

    async def async_step_otp(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle the one-time OTP step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            setup = XTBBridgeSetupClient(self.hass, bridge_url=DEFAULT_BRIDGE_URL)
            try:
                result = await setup.async_complete_login(
                    challenge_id=self._challenge_id,
                    otp=user_input[CONF_OTP],
                )
            except XTBBridgeError:
                errors["base"] = "invalid_auth"
            else:
                return await self._handle_login_result(result, reauth=self._reauth)

        return self.async_show_form(
            step_id="otp",
            data_schema=vol.Schema({vol.Required(CONF_OTP): str}),
            errors=errors,
        )

    async def async_step_reauth(
        self,
        entry_data: dict[str, Any],
    ) -> config_entries.ConfigFlowResult:
        """Start reauthentication when the cached bridge session expires."""
        self._reauth = True
        self._email = entry_data[CONF_EMAIL]
        self._password = entry_data[CONF_PASSWORD]

        setup = XTBBridgeSetupClient(self.hass, bridge_url=entry_data.get(CONF_BRIDGE_URL, DEFAULT_BRIDGE_URL))
        try:
            result = await setup.async_start_login(email=self._email, password=self._password)
        except XTBBridgeError:
            return self.async_abort(reason="cannot_connect")

        return await self._handle_login_result(result, reauth=True)

    async def _handle_login_result(
        self,
        result: dict[str, Any],
        *,
        reauth: bool = False,
    ) -> config_entries.ConfigFlowResult:
        self._reauth = reauth
        if result.get("status") == "requires_otp":
            self._challenge_id = str(result["challenge_id"])
            return await self.async_step_otp()

        if result.get("status") == "ok":
            if reauth:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={
                        CONF_EMAIL: self._email,
                        CONF_PASSWORD: self._password,
                        CONF_BRIDGE_URL: DEFAULT_BRIDGE_URL,
                    },
                )

            return self.async_create_entry(
                title="XTB",
                data={
                    CONF_EMAIL: self._email,
                    CONF_PASSWORD: self._password,
                    CONF_BRIDGE_URL: DEFAULT_BRIDGE_URL,
                    CONF_SCAN_INTERVAL: max(DEFAULT_SCAN_INTERVAL, MIN_SCAN_INTERVAL),
                },
            )

        return self.async_abort(reason="unknown")


def _user_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_EMAIL): str,
            vol.Required(CONF_PASSWORD): str,
        }
    )
