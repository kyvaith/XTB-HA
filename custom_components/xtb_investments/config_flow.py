"""Config flow for XTB Investments."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries

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

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title="XTB",
                data={
                    CONF_EMAIL: user_input[CONF_EMAIL],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_OTP: user_input.get(CONF_OTP, ""),
                    CONF_BRIDGE_URL: DEFAULT_BRIDGE_URL,
                    CONF_SCAN_INTERVAL: max(
                        DEFAULT_SCAN_INTERVAL,
                        MIN_SCAN_INTERVAL,
                    ),
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(),
            errors=errors,
        )


def _user_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_EMAIL): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Optional(CONF_OTP, default=""): str,
        }
    )
