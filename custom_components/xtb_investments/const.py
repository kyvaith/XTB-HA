"""Constants for the XTB Investments integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "xtb_investments"
NAME = "XTB Investments"
VERSION = "0.1.26"

PLATFORMS: list[Platform] = [Platform.SENSOR]

CARD_FILENAME = "xtb-investments-card.js"
CARD_RESOURCE_URL = f"/{DOMAIN}/{CARD_FILENAME}"
CARD_RESOURCE_URL_VERSIONED = f"{CARD_RESOURCE_URL}?v={VERSION}"

CONF_ACCOUNT_NUMBER = "account_number"
CONF_BRIDGE_URL = "bridge_url"
CONF_EMAIL = "email"
CONF_OTP = "otp"
CONF_PASSWORD = "password"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_BRIDGE_URL = "http://127.0.0.1:8765"
DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 30

ATTRIBUTION = "Data provided through an unofficial xStation5 API client."
