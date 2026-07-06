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
    current_quote_ids = {
        _quote_unique_id(entry, symbol) for symbol in coordinator.data.quotes
    }
    current_position_profit_ids = {
        _position_profit_unique_id(entry, position, index)
        for index, position in enumerate(coordinator.data.positions)
    }
    _remove_legacy_entities(
        hass,
        entry,
        current_quote_ids=current_quote_ids,
        current_position_profit_ids=current_position_profit_ids,
    )
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
        self._attr_unique_id = _quote_unique_id(entry, symbol)
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
        self._attr_unique_id = _position_profit_unique_id(entry, position, index)
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
            "order_id": position.get("order_id"),
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


def _remove_legacy_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    current_quote_ids: set[str],
    current_position_profit_ids: set[str],
) -> None:
    registry = er.async_get(hass)
    for unique_id in (f"{entry.entry_id}_portfolio",):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id is not None:
            registry.async_remove(entity_id)

    stale_prefixes = {
        f"{entry.entry_id}_quote_": current_quote_ids,
        f"{entry.entry_id}_position_profit_": current_position_profit_ids,
    }
    for entity in list(registry.entities.values()):
        if entity.domain != "sensor" or entity.platform != DOMAIN:
            continue
        if entity.config_entry_id not in (None, entry.entry_id):
            continue
        for prefix, current_ids in stale_prefixes.items():
            if entity.unique_id.startswith(prefix) and entity.unique_id not in current_ids:
                registry.async_remove(entity.entity_id)
                break


def _quote_unique_id(entry: ConfigEntry, symbol: str) -> str:
    return f"{entry.entry_id}_quote_{symbol.lower().replace('.', '_')}"


def _position_profit_unique_id(
    entry: ConfigEntry,
    position: dict[str, Any],
    index: int,
) -> str:
    return f"{entry.entry_id}_position_profit_{_position_key(position, index)}"


def _account_value(data: XTBSnapshot) -> StateValue:
    cash_balance = _first_present(data.summary.get("cash_balance"), data.account.get("cash_balance"))
    asset_value = _first_present(data.summary.get("asset_value"), data.account.get("asset_value"))
    total_equity = _first_present(data.summary.get("total_equity"), data.account.get("total_equity"))
    profit_net = _first_present(data.summary.get("profit_net"), data.account.get("profit_net"))
    calculated_value = None
    cash_balance_number = _as_float(cash_balance)
    asset_value_number = _as_float(asset_value)
    if cash_balance_number is not None and asset_value_number is not None:
        calculated_value = cash_balance_number + asset_value_number
    total_equity_with_profit = None
    total_equity_number = _as_float(total_equity)
    profit_net_number = _as_float(profit_net)
    if total_equity_number is not None and profit_net_number is not None:
        total_equity_with_profit = total_equity_number + profit_net_number

    return _first_present(
        data.summary.get("side_bar_account_value"),
        data.account.get("side_bar_account_value"),
        total_equity_with_profit,
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
    return _first_present(
        quote.get("daily_change_percent"),
        quote.get("change_percent"),
    )


def _change_percent_source(data: XTBSnapshot, symbol: str) -> str | None:
    quote = data.quotes.get(symbol, {})
    if quote.get("daily_change_percent") is not None:
        return quote.get("daily_change_percent_source") or "quote_daily_change_percent"
    if quote.get("change_percent") is not None:
        return "quote_change_percent"
    return None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
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
