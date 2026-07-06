"""Home Assistant integration for XTB investment statistics."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import XTBBridgeClient
from .const import (
    CONF_BRIDGE_URL,
    CONF_EMAIL,
    CONF_PASSWORD,
    DEFAULT_BRIDGE_URL,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import XTBCoordinator

type XTBConfigEntry = ConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: XTBConfigEntry) -> bool:
    """Set up XTB Investments from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                f"/{DOMAIN}",
                str(Path(__file__).parent / "frontend"),
                cache_headers=True,
            )
        ]
    )

    client = XTBBridgeClient(
        hass,
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
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
