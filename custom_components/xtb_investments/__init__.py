"""Home Assistant integration for XTB investment statistics."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from .api import XTBBridgeClient
from .const import (
    CARD_RESOURCE_URL,
    CARD_RESOURCE_URL_VERSIONED,
    CONF_ACCOUNT_NUMBER,
    CONF_BRIDGE_URL,
    CONF_EMAIL,
    CONF_PASSWORD,
    DEFAULT_BRIDGE_URL,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import XTBCoordinator

type XTBConfigEntry = ConfigEntry

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: XTBConfigEntry) -> bool:
    """Set up XTB Investments from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    await _register_frontend(hass)

    client = XTBBridgeClient(
        hass,
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        account_number=entry.data.get(CONF_ACCOUNT_NUMBER),
        bridge_url=entry.data.get(CONF_BRIDGE_URL, DEFAULT_BRIDGE_URL),
    )
    coordinator = XTBCoordinator(hass, entry, client)

    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: XTBConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    coordinator: XTBCoordinator | None = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)

    if coordinator is not None:
        await coordinator.client.async_close()

    if not hass.data.get(DOMAIN):
        hass.data.pop(DOMAIN, None)

    return unload_ok


async def _register_frontend(hass: HomeAssistant) -> None:
    """Serve the card module and add it to Lovelace resources."""
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                f"/{DOMAIN}",
                str(Path(__file__).parent / "frontend"),
                cache_headers=True,
            )
        ]
    )
    await _register_lovelace_resource(hass)


async def _register_lovelace_resource(hass: HomeAssistant) -> None:
    """Register the dashboard card as a Lovelace module resource."""
    try:
        if not await async_setup_component(hass, "lovelace", {}):
            _LOGGER.warning("Unable to set up Lovelace; XTB card resource was not registered")
            return

        resources = _lovelace_resources(hass)
        if resources is None:
            _LOGGER.warning("Lovelace resources are unavailable; add %s manually", CARD_RESOURCE_URL_VERSIONED)
            return

        if not getattr(resources, "loaded", True):
            await resources.async_load()

        existing = next(
            (
                item
                for item in resources.async_items()
                if str(item.get("url", "")).split("?", 1)[0] == CARD_RESOURCE_URL
            ),
            None,
        )
        payload = {"res_type": "module", "url": CARD_RESOURCE_URL_VERSIONED}
        if existing is None:
            await resources.async_create_item(payload)
            _LOGGER.info("Registered XTB Lovelace card resource: %s", CARD_RESOURCE_URL_VERSIONED)
            return

        if existing.get("res_type") != "module" or existing.get("url") != CARD_RESOURCE_URL_VERSIONED:
            item_id = existing.get("id")
            if item_id is None:
                _LOGGER.warning("XTB Lovelace resource exists without an id; add %s manually", CARD_RESOURCE_URL_VERSIONED)
                return
            await resources.async_update_item(item_id, payload)
            _LOGGER.info("Updated XTB Lovelace card resource: %s", CARD_RESOURCE_URL_VERSIONED)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "Unable to register XTB Lovelace card resource automatically; add %s manually: %s",
            CARD_RESOURCE_URL_VERSIONED,
            err,
        )


def _lovelace_resources(hass: HomeAssistant) -> Any | None:
    lovelace_data = hass.data.get("lovelace")
    resources = getattr(lovelace_data, "resources", None)
    if resources is not None:
        return resources
    if isinstance(lovelace_data, dict):
        return lovelace_data.get("resources")
    return None
