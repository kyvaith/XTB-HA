"""Data coordinator for XTB Investments."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import XTBBridgeClient, XTBSnapshot
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN, MIN_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


class XTBCoordinator(DataUpdateCoordinator[XTBSnapshot]):
    """Coordinate polling the XTB account once for all entities."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: XTBBridgeClient,
    ) -> None:
        self.client = client
        scan_interval = int(
            entry.options.get(
                CONF_SCAN_INTERVAL,
                entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            )
        )
        scan_interval = max(scan_interval, MIN_SCAN_INTERVAL)

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=scan_interval),
        )

    async def _async_update_data(self) -> XTBSnapshot:
        try:
            return await self.client.async_get_snapshot()
        except Exception as err:
            raise UpdateFailed(str(err)) from err
