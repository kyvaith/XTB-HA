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
        account_number: int | None = None,
        bridge_url: str = DEFAULT_BRIDGE_URL,
    ) -> None:
        self._hass = hass
        self._email = email
        self._password = password
        self._account_number = account_number
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
                    "account_number": self._account_number,
                },
                timeout=ClientTimeout(total=60),
            )
            try:
                payload = await response.json(content_type=None)
            except Exception as err:  # noqa: BLE001
                raise XTBBridgeError("XTB bridge returned a non-JSON response") from err

            if response.status >= 400:
                detail = payload.get("error") if isinstance(payload, dict) else response.reason
                if response.status == 428:
                    raise XTBBridgeAuthRequired(str(detail))
                raise XTBBridgeError(f"XTB bridge returned HTTP {response.status}: {detail}")
        except ClientError as err:
            raise XTBBridgeError(
                "XTB bridge is not reachable. Install and start the XTB Bridge add-on."
            ) from err

        if not isinstance(payload, dict):
            raise XTBBridgeError("XTB bridge returned an invalid payload")

        return XTBSnapshot.from_bridge(payload)


class XTBBridgeSetupClient:
    """Client used by config and reauth flows."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        bridge_url: str = DEFAULT_BRIDGE_URL,
    ) -> None:
        self._hass = hass
        self._bridge_url = bridge_url.rstrip("/")

    async def async_start_login(self, *, email: str, password: str) -> dict[str, Any]:
        """Start a login and return either success or an OTP challenge."""
        return await self._post(
            "/login/start",
            {
                "email": email,
                "password": password,
            },
        )

    async def async_complete_login(self, *, challenge_id: str, otp: str) -> dict[str, Any]:
        """Complete an OTP challenge."""
        return await self._post(
            "/login/complete",
            {
                "challenge_id": challenge_id,
                "otp": otp,
            },
        )

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = async_get_clientsession(self._hass)
        try:
            response = await session.post(
                f"{self._bridge_url}{path}",
                json=payload,
                timeout=ClientTimeout(total=120),
            )
            try:
                data = await response.json(content_type=None)
            except Exception as err:  # noqa: BLE001
                raise XTBBridgeError("XTB bridge returned a non-JSON response") from err

            if response.status >= 400:
                detail = data.get("error") if isinstance(data, dict) else response.reason
                message = f"XTB bridge returned HTTP {response.status}: {detail}"
                if _is_otp_expired_response(response.status, detail):
                    raise XTBBridgeOTPExpired(message)
                raise XTBBridgeError(message)
        except ClientError as err:
            raise XTBBridgeError(
                "XTB bridge is not reachable. Install and start the XTB Bridge add-on."
            ) from err

        if not isinstance(data, dict):
            raise XTBBridgeError("XTB bridge returned an invalid payload")
        return data


class XTBBridgeError(RuntimeError):
    """Raised when the bridge cannot return account data."""


class XTBBridgeOTPExpired(XTBBridgeError):
    """Raised when an OTP challenge expired and a new code is required."""


class XTBBridgeAuthRequired(XTBBridgeError):
    """Raised when the cached bridge session expired and OTP is required again."""


def _is_otp_expired_response(status: int, detail: Any) -> bool:
    if status == 410:
        return True

    text = str(detail or "").lower()
    if "browser_auth_otp_timeout" in text:
        return True
    return "otp" in text and (
        "expired" in text or "timeout" in text or "timed out" in text
    )
