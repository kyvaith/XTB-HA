"""HTTP client for the local XTB bridge add-on."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientTimeout

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DEFAULT_BRIDGE_URL


@dataclass(frozen=True)
class XTBSnapshot:
    """A normalized, Home Assistant-friendly view of the XTB account."""

    account: dict[str, Any]
    summary: dict[str, Any]
    positions: list[dict[str, Any]]
    orders: list[dict[str, Any]]
    quotes: dict[str, dict[str, Any]]
    session: dict[str, Any]
    updated_at: str

    @classmethod
    def from_bridge(cls, payload: dict[str, Any]) -> "XTBSnapshot":
        """Create a snapshot from bridge JSON."""
        return cls(
            account=payload.get("account") or {},
            summary=payload.get("summary") or {},
            positions=payload.get("positions") or [],
            orders=payload.get("orders") or [],
            quotes=payload.get("quotes") or {},
            session=payload.get("session") or {},
            updated_at=payload.get("updated_at") or "",
        )


class XTBBridgeClient:
    """Small client for the local Chromium-capable XTB bridge."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        email: str,
        password: str,
        otp: str,
        bridge_url: str = DEFAULT_BRIDGE_URL,
    ) -> None:
        self._hass = hass
        self._email = email
        self._password = password
        self._otp = otp
        self._bridge_url = bridge_url.rstrip("/")

    async def async_close(self) -> None:
        """Close resources held by the client."""

    async def async_get_snapshot(self) -> XTBSnapshot:
        """Fetch one account snapshot from the bridge."""
        session = async_get_clientsession(self._hass)
        try:
            response = await session.post(
                f"{self._bridge_url}/snapshot",
                json={
                    "email": self._email,
                    "password": self._password,
                    "otp": self._otp,
                },
                timeout=ClientTimeout(total=60),
            )
            try:
                payload = await response.json(content_type=None)
            except Exception as err:  # noqa: BLE001
                raise XTBBridgeError("XTB bridge returned a non-JSON response") from err

            if response.status >= 400:
                detail = payload.get("error") if isinstance(payload, dict) else response.reason
                raise XTBBridgeError(f"XTB bridge returned HTTP {response.status}: {detail}")
        except ClientError as err:
            raise XTBBridgeError(
                "XTB bridge is not reachable. Install and start the XTB Bridge add-on."
            ) from err

        if not isinstance(payload, dict):
            raise XTBBridgeError("XTB bridge returned an invalid payload")

        return XTBSnapshot.from_bridge(payload)


class XTBBridgeError(RuntimeError):
    """Raised when the bridge cannot return account data."""
