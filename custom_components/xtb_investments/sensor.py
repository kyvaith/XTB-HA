"""Sensors for XTB Investments."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import XTBSnapshot
from .const import ATTRIBUTION, DOMAIN
from .coordinator import XTBCoordinator

StateValue = str | int | float | None


@dataclass(frozen=True, kw_only=True)
class XTBSensorDescription(SensorEntityDescription):
    """Description for an aggregate XTB sensor."""

    value_fn: Callable[[XTBSnapshot], StateValue]
    attrs_fn: Callable[[XTBSnapshot], dict[str, Any]] | None = None
    currency_unit: bool = False


SENSORS: tuple[XTBSensorDescription, ...] = (
    XTBSensorDescription(
        key="portfolio",
        translation_key="portfolio",
        icon="mdi:briefcase",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        currency_unit=True,
        value_fn=lambda data: data.summary.get("portfolio_value"),
        attrs_fn=lambda data: {
            "account": data.account,
            "summary": data.summary,
            "positions": data.positions,
            "orders": data.orders,
            "quotes": data.quotes,
            "session": data.session,
            "updated_at": data.updated_at,
            "attribution": ATTRIBUTION,
        },
    ),
    XTBSensorDescription(
        key="balance",
        translation_key="balance",
        icon="mdi:cash",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        currency_unit=True,
        value_fn=lambda data: data.summary.get("cash_balance"),
    ),
    XTBSensorDescription(
        key="free_margin",
        translation_key="free_margin",
        icon="mdi:cash-check",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        currency_unit=True,
        value_fn=lambda data: data.summary.get("free_margin"),
    ),
    XTBSensorDescription(
        key="profit",
        translation_key="profit",
        icon="mdi:chart-line",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        currency_unit=True,
        value_fn=lambda data: data.summary.get("profit_net"),
        attrs_fn=lambda data: {
            "profit_percent": data.summary.get("profit_percent"),
            "position_profit_net": data.summary.get("position_profit_net"),
            "currency": data.summary.get("currency"),
        },
    ),
    XTBSensorDescription(
        key="profit_percent",
        translation_key="profit_percent",
        icon="mdi:percent",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.summary.get("profit_percent"),
    ),
    XTBSensorDescription(
        key="open_positions",
        translation_key="open_positions",
        icon="mdi:format-list-bulleted",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.summary.get("open_positions"),
        attrs_fn=lambda data: {
            "positions": data.positions,
            "position_profit_net": data.summary.get("position_profit_net"),
            "currency": data.summary.get("currency"),
        },
    ),
    XTBSensorDescription(
        key="pending_orders",
        translation_key="pending_orders",
        icon="mdi:clipboard-clock",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.summary.get("pending_orders"),
        attrs_fn=lambda data: {"orders": data.orders},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up XTB sensors."""
    coordinator: XTBCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        XTBAggregateSensor(coordinator, entry, description) for description in SENSORS
    ]

    for symbol in sorted(coordinator.data.quotes):
        entities.append(XTBQuoteSensor(coordinator, entry, symbol))

    async_add_entities(entities)


class XTBBaseSensor(CoordinatorEntity[XTBCoordinator], SensorEntity):
    """Base class for XTB sensors."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: XTBCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        account = coordinator.data.summary.get("account_number") if coordinator.data else None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="XTB",
            model="xStation5 unofficial API",
            name=f"XTB {account or entry.title}",
        )


class XTBAggregateSensor(XTBBaseSensor):
    """Aggregate account sensor."""

    entity_description: XTBSensorDescription

    def __init__(
        self,
        coordinator: XTBCoordinator,
        entry: ConfigEntry,
        description: XTBSensorDescription,
    ) -> None:
        super().__init__(coordinator, entry)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_entity_category = description.entity_category
        self._attr_entity_registry_enabled_default = description.entity_registry_enabled_default

    @property
    def native_value(self) -> StateValue:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self.entity_description.currency_unit:
            return self.coordinator.data.summary.get("currency") or None
        return self.entity_description.native_unit_of_measurement

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return {"attribution": ATTRIBUTION}

        attrs = self.entity_description.attrs_fn(self.coordinator.data)
        attrs.setdefault("attribution", ATTRIBUTION)
        return attrs


class XTBQuoteSensor(XTBBaseSensor):
    """Quote sensor for a watched or open-position symbol."""

    _attr_icon = "mdi:chart-timeline-variant"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: XTBCoordinator, entry: ConfigEntry, symbol: str) -> None:
        super().__init__(coordinator, entry)
        self._symbol = symbol
        self._attr_unique_id = f"{entry.entry_id}_quote_{symbol.lower().replace('.', '_')}"
        self._attr_name = symbol

    @property
    def native_value(self) -> StateValue:
        quote = self.coordinator.data.quotes.get(self._symbol, {})
        return quote.get("mid") or quote.get("bid")

    @property
    def available(self) -> bool:
        return super().available and bool(
            self.coordinator.data.quotes.get(self._symbol, {}).get("available")
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        quote = self.coordinator.data.quotes.get(self._symbol, {})
        return {
            **quote,
            "attribution": ATTRIBUTION,
        }
