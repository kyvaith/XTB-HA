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
from homeassistant.helpers import entity_registry as er
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
        key="balance",
        translation_key="balance",
        icon="mdi:cash",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        currency_unit=True,
        value_fn=lambda data: _account_value(data),
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
        key="free_margin",
        translation_key="free_margin",
        icon="mdi:cash-check",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        currency_unit=True,
        value_fn=lambda data: (
            _cash_value(data)
        ),
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
    _remove_legacy_entities(hass, entry)
    entities: list[SensorEntity] = [
        XTBAggregateSensor(coordinator, entry, description) for description in SENSORS
    ]

    for symbol in sorted(coordinator.data.quotes):
        entities.append(XTBQuoteSensor(coordinator, entry, symbol))

    for index, position in enumerate(coordinator.data.positions):
        entities.append(XTBPositionProfitSensor(coordinator, entry, position, index))

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
    """Daily percent change sensor for a watched or open-position symbol."""

    _attr_icon = "mdi:percent"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: XTBCoordinator, entry: ConfigEntry, symbol: str) -> None:
        super().__init__(coordinator, entry)
        self._symbol = symbol
        self._attr_unique_id = f"{entry.entry_id}_quote_{symbol.lower().replace('.', '_')}"
        self._attr_name = f"{_instrument_name(coordinator.data, symbol)} dzienna zmiana"

    @property
    def native_value(self) -> StateValue:
        return _change_percent_value(self.coordinator.data, self._symbol)

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        quote = self.coordinator.data.quotes.get(self._symbol, {})
        position = _position_for_symbol(self.coordinator.data.positions, self._symbol)
        return {
            **quote,
            "name": _instrument_name(self.coordinator.data, self._symbol),
            "change_percent_source": _change_percent_source(self.coordinator.data, self._symbol),
            "position_profit_loss": position.get("profit_loss") if position else None,
            "position_market_value": position.get("market_value") if position else None,
            "currency": self.coordinator.data.summary.get("currency"),
            "attribution": ATTRIBUTION,
        }


class XTBPositionProfitSensor(XTBBaseSensor):
    """Profit/loss sensor for one open position."""

    _attr_icon = "mdi:chart-line"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: XTBCoordinator,
        entry: ConfigEntry,
        position: dict[str, Any],
        index: int,
    ) -> None:
        super().__init__(coordinator, entry)
        self._position_key = _position_key(position, index)
        symbol = _position_name(position) or f"pozycja {index + 1}"
        self._attr_unique_id = f"{entry.entry_id}_position_profit_{self._position_key}"
        self._attr_name = f"{symbol} zysk/strata"

    @property
    def native_value(self) -> StateValue:
        position = self._current_position()
        if position is None:
            return None
        return position.get("profit_loss") if position.get("profit_loss") is not None else position.get("profit_net")

    @property
    def native_unit_of_measurement(self) -> str | None:
        return self.coordinator.data.summary.get("currency") or None

    @property
    def available(self) -> bool:
        return super().available and self._current_position() is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        position = self._current_position() or {}
        return {
            "name": _position_name(position),
            "symbol": position.get("symbol"),
            "account_number": position.get("account_number"),
            "daily_change_percent": position.get("daily_change_percent"),
            "profit_loss_percent": position.get("profit_loss_percent") or position.get("profit_percent"),
            "market_value": position.get("market_value"),
            "volume": position.get("volume"),
            "currency": self.coordinator.data.summary.get("currency"),
            "attribution": ATTRIBUTION,
        }

    def _current_position(self) -> dict[str, Any] | None:
        for index, position in enumerate(self.coordinator.data.positions):
            if _position_key(position, index) == self._position_key:
                return position
        return None


def _position_for_symbol(
    positions: list[dict[str, Any]],
    symbol: str,
) -> dict[str, Any] | None:
    target = symbol.upper()
    for position in positions:
        if str(position.get("symbol") or "").upper() == target:
            return position
    return None


def _remove_legacy_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    registry = er.async_get(hass)
    for unique_id in (f"{entry.entry_id}_portfolio",):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id is not None:
            registry.async_remove(entity_id)


def _account_value(data: XTBSnapshot) -> StateValue:
    cash_balance = _first_present(data.summary.get("cash_balance"), data.account.get("cash_balance"))
    asset_value = _first_present(data.summary.get("asset_value"), data.account.get("asset_value"))
    calculated_value = None
    if cash_balance is not None and asset_value is not None:
        calculated_value = cash_balance + asset_value

    return _first_present(
        data.summary.get("account_value"),
        data.summary.get("portfolio_value"),
        data.account.get("account_value"),
        data.account.get("portfolio_value"),
        calculated_value,
        data.summary.get("equity"),
        data.account.get("equity"),
        data.summary.get("balance"),
    )


def _cash_value(data: XTBSnapshot) -> StateValue:
    return _first_present(
        data.summary.get("cash_balance"),
        data.account.get("cash_balance"),
        data.summary.get("free_margin"),
        data.account.get("free_margin"),
    )


def _instrument_name(data: XTBSnapshot, symbol: str) -> str:
    quote = data.quotes.get(symbol, {})
    position = _position_for_symbol(data.positions, symbol) or {}
    return (
        _first_present(
            quote.get("name"),
            quote.get("display_name"),
            quote.get("description"),
            position.get("name"),
            position.get("display_name"),
            position.get("description"),
            symbol,
        )
        or symbol
    )


def _position_name(position: dict[str, Any]) -> str | None:
    return _first_present(
        position.get("name"),
        position.get("display_name"),
        position.get("description"),
        position.get("symbol"),
    )


def _change_percent_value(data: XTBSnapshot, symbol: str) -> StateValue:
    quote = data.quotes.get(symbol, {})
    position = _position_for_symbol(data.positions, symbol) or {}
    return _first_present(
        quote.get("daily_change_percent"),
        position.get("daily_change_percent"),
        quote.get("change_percent"),
        position.get("price_change_percent"),
        position.get("profit_loss_percent"),
        position.get("profit_percent"),
    )


def _change_percent_source(data: XTBSnapshot, symbol: str) -> str | None:
    quote = data.quotes.get(symbol, {})
    position = _position_for_symbol(data.positions, symbol) or {}
    candidates = (
        ("quote_daily_change_percent", quote.get("daily_change_percent")),
        ("position_daily_change_percent", position.get("daily_change_percent")),
        ("quote_change_percent", quote.get("change_percent")),
        ("position_price_change_percent", position.get("price_change_percent")),
        ("position_profit_loss_percent", position.get("profit_loss_percent")),
        ("position_profit_percent", position.get("profit_percent")),
    )
    for source, value in candidates:
        if value is not None:
            return source
    return None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _position_key(position: dict[str, Any], index: int) -> str:
    raw_key = "_".join(
        str(part)
        for part in (
            position.get("account_number"),
            position.get("order_id"),
            position.get("symbol"),
            index,
        )
        if part not in (None, "")
    )
    return "".join(char if char.isalnum() else "_" for char in raw_key.lower()).strip("_")
