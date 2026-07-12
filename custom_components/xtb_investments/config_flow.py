"""Config flow for XTB Investments."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries

from .api import XTBBridgeError, XTBBridgeOTPExpired, XTBBridgeSetupClient
from .const import (
    CONF_ACCOUNT_NUMBER,
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
    _accounts: list[dict[str, Any]]
    _account_number: int | None = None
    _bridge_url: str = DEFAULT_BRIDGE_URL
    _reauth: bool = False

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._reauth = False
            self._bridge_url = DEFAULT_BRIDGE_URL
            self._email = user_input[CONF_EMAIL].strip()
            self._password = user_input[CONF_PASSWORD]

            await self.async_set_unique_id(self._email.lower())
            self._abort_if_unique_id_configured()

            setup = XTBBridgeSetupClient(self.hass, bridge_url=self._bridge_url)
            try:
                result = await setup.async_start_login(
                    email=self._email,
                    password=self._password,
                    source="initial",
                )
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
            setup = XTBBridgeSetupClient(self.hass, bridge_url=self._bridge_url)
            try:
                result = await setup.async_complete_login(
                    challenge_id=self._challenge_id,
                    otp=user_input[CONF_OTP],
                )
            except XTBBridgeOTPExpired:
                return await self._restart_expired_otp_challenge()
            except XTBBridgeError:
                errors["base"] = "invalid_auth"
            else:
                return await self._handle_login_result(result, reauth=self._reauth)

        return self.async_show_form(
            step_id="otp",
            data_schema=_otp_schema(),
            errors=errors,
        )

    async def async_step_account(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Let the user choose which XTB account should be tracked."""
        if user_input is not None:
            self._account_number = int(user_input[CONF_ACCOUNT_NUMBER])
            return self._create_config_entry()

        choices = _account_choices(self._accounts)
        default = str(self._account_number or next(iter(choices)))
        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ACCOUNT_NUMBER, default=default): vol.In(choices),
                }
            ),
        )

    async def async_step_reauth(
        self,
        entry_data: dict[str, Any],
    ) -> config_entries.ConfigFlowResult:
        """Start reauthentication when the cached bridge session expires."""
        self._reauth = True
        self._email = entry_data[CONF_EMAIL]
        self._password = entry_data[CONF_PASSWORD]
        self._bridge_url = entry_data.get(CONF_BRIDGE_URL, DEFAULT_BRIDGE_URL)

        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Ask the user before starting an XTB login that can send an OTP SMS."""
        errors: dict[str, str] = {}

        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({}),
                errors=errors,
            )

        setup = XTBBridgeSetupClient(self.hass, bridge_url=self._bridge_url)
        try:
            result = await setup.async_start_login(
                email=self._email,
                password=self._password,
                source="reauth_manual",
            )
        except XTBBridgeError:
            errors["base"] = "cannot_connect"
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({}),
                errors=errors,
            )

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
            self._accounts = result.get("accounts") or []
            self._account_number = _default_account_number(
                self._accounts,
                result.get("account_number"),
            )

            if reauth:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={
                        CONF_EMAIL: self._email,
                        CONF_PASSWORD: self._password,
                        CONF_BRIDGE_URL: self._bridge_url,
                        CONF_ACCOUNT_NUMBER: self._get_reauth_entry().data.get(
                            CONF_ACCOUNT_NUMBER,
                            self._account_number,
                        ),
                    },
                )

            if len(self._accounts) > 1:
                return await self.async_step_account()

            return self._create_config_entry()

        return self.async_abort(reason="unknown")

    def _create_config_entry(self) -> config_entries.ConfigFlowResult:
        return self.async_create_entry(
            title=f"XTB {self._account_number}" if self._account_number else "XTB",
            data={
                CONF_EMAIL: self._email,
                CONF_PASSWORD: self._password,
                CONF_BRIDGE_URL: self._bridge_url,
                CONF_ACCOUNT_NUMBER: self._account_number,
                CONF_SCAN_INTERVAL: max(DEFAULT_SCAN_INTERVAL, MIN_SCAN_INTERVAL),
            },
        )

    async def _restart_expired_otp_challenge(self) -> config_entries.ConfigFlowResult:
        """Start a fresh login when the previous OTP challenge expired."""
        setup = XTBBridgeSetupClient(self.hass, bridge_url=self._bridge_url)
        try:
            result = await setup.async_start_login(
                email=self._email,
                password=self._password,
                source="otp_retry",
                force_new_challenge=True,
            )
        except XTBBridgeError:
            return self.async_show_form(
                step_id="otp",
                data_schema=_otp_schema(),
                errors={"base": "cannot_connect"},
            )

        if result.get("status") == "requires_otp":
            self._challenge_id = str(result["challenge_id"])
            return self.async_show_form(
                step_id="otp",
                data_schema=_otp_schema(),
                errors={"base": "otp_expired"},
            )

        return await self._handle_login_result(result, reauth=self._reauth)


def _user_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_EMAIL): str,
            vol.Required(CONF_PASSWORD): str,
        }
    )


def _otp_schema() -> vol.Schema:
    return vol.Schema({vol.Required(CONF_OTP): str})


def _default_account_number(accounts: list[dict[str, Any]], fallback: Any) -> int | None:
    fallback_int = _maybe_int(fallback)
    if fallback_int:
        return fallback_int
    if not accounts:
        return None
    return _maybe_int(accounts[0].get("account_number"))


def _account_choices(accounts: list[dict[str, Any]]) -> dict[str, str]:
    choices: dict[str, str] = {}
    for account in accounts:
        account_number = _maybe_int(account.get("account_number"))
        if account_number is None:
            continue
        currency = str(account.get("currency") or "").upper()
        endpoint_type = str(account.get("endpoint_type") or "").upper()
        label_parts = [str(account_number)]
        if currency:
            label_parts.append(currency)
        if endpoint_type:
            label_parts.append(endpoint_type)
        choices[str(account_number)] = " - ".join(label_parts)
    return choices


def _maybe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
