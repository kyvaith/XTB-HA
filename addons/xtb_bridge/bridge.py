"""Local HTTP bridge for XTB xStation5.

The Home Assistant integration intentionally does not install Chromium. This add-on
does that in its own container and exposes a small localhost API.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import time
from typing import Any

from aiohttp import web

from xtb_api import XTBClient
from xtb_api.auth.browser_auth import BrowserCASAuth, LOGIN_URL, OTP_WAIT_TIMEOUT, _STEALTH_JS
from xtb_api.auth.cas_client import CASClient, CASClientConfig
from xtb_api.exceptions import CASError
from xtb_api.types.websocket import CASLoginSuccess, CASLoginTwoFactorRequired

DATA_DIR = Path(os.environ.get("XTB_BRIDGE_DATA", "/data"))
SESSION_DIR = DATA_DIR / "sessions"
DEBUG_DIR = DATA_DIR / "debug"
DEFAULT_PORT = 8765
PENDING_LOGIN_TTL_SECONDS = 300
BROWSER_LOGIN_TIMEOUT_SECONDS = int(os.environ.get("XTB_BROWSER_LOGIN_TIMEOUT", "90"))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
LOGGER = logging.getLogger("xtb_bridge")
PENDING_LOCK = asyncio.Lock()


class ManagedClient:
    """A cached XTB client with automatic account-number discovery."""

    def __init__(self, *, email: str, password: str) -> None:
        self.email = email
        self.password = password
        self.account_number: int | None = None
        self.client: XTBClient | None = None
        self.lock = asyncio.Lock()
        self.session_file = _session_file(email, password)

    async def get_snapshot(self) -> dict[str, Any]:
        async with self.lock:
            client = await self._get_client()
            return await _snapshot(client)

    async def _get_client(self) -> XTBClient:
        if self.client and self.client.is_connected and self.client.is_authenticated:
            return self.client

        await self.close()
        if self.account_number is None:
            self.account_number = await self._discover_account_number()

        self.client = XTBClient(
            email=self.email,
            password=self.password,
            account_number=self.account_number,
            account_type="real",
            session_file=self.session_file,
        )
        await self.client.connect()
        return self.client

    async def _discover_account_number(self) -> int:
        LOGGER.info("Discovering XTB account number for %s", self.email)
        probe = XTBClient(
            email=self.email,
            password=self.password,
            account_number=0,
            account_type="real",
            session_file=self.session_file,
        )
        try:
            await probe.connect()
            account_number = int(probe.ws.get_account_number())
            if account_number <= 0:
                raise RuntimeError("XTB login succeeded but no account number was returned")
            LOGGER.info("Discovered XTB account number %s for %s", account_number, self.email)
            return account_number
        finally:
            await probe.disconnect()

    async def close(self) -> None:
        if self.client is None:
            return
        try:
            await self.client.disconnect()
        finally:
            self.client = None


CLIENTS: dict[str, ManagedClient] = {}
PENDING_LOGINS: dict[str, "PendingLogin"] = {}


@dataclass
class PendingLogin:
    """A login challenge waiting for a one-time OTP code."""

    email: str
    password: str
    cas: CASClient
    challenge: CASLoginTwoFactorRequired
    browser_auth: bool
    expires_at: float


class ResilientBrowserCASAuth(BrowserCASAuth):
    """Browser login flow with UI-based OTP detection and better diagnostics."""

    async def login(self, email: str, password: str) -> CASLoginSuccess | CASLoginTwoFactorRequired:
        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import async_playwright
        except ImportError as err:
            raise CASError(
                "BROWSER_AUTH_MISSING_DEPENDENCY",
                "Playwright is required for browser auth.",
            ) from err

        needs_cleanup = True
        self._playwright = await async_playwright().start()
        try:
            try:
                self._browser = await self._playwright.chromium.launch(
                    headless=self._headless,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--no-sandbox",
                    ],
                )
            except PlaywrightError as err:
                raise CASError("BROWSER_CHROMIUM_FAILED", str(err)) from err

            context = await self._browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                locale="pl-PL",
                timezone_id="Europe/Warsaw",
            )
            await context.add_init_script(_STEALTH_JS)

            self._page = await context.new_page()
            self._page.on("response", self._on_response)

            fingerprint = hashlib.sha256(b"xStation5/2.94.1 (Linux x86_64)").hexdigest().upper()
            await context.add_init_script(
                f"""
                try {{
                    localStorage.setItem('deviceFingerprint', '{fingerprint}');
                    localStorage.setItem('fingerprint', '{fingerprint}');
                }} catch(e) {{}}
                """
            )

            LOGGER.info("Navigating browser to %s", LOGIN_URL)
            await self._page.goto(LOGIN_URL, wait_until="commit", timeout=30_000)

            email_input = self._page.get_by_role("textbox").first
            await email_input.wait_for(state="visible", timeout=30_000)
            LOGGER.info("Browser login form found")

            await email_input.click()
            await email_input.fill("")
            await self._page.keyboard.type(email, delay=30)

            password_input = self._page.get_by_role("textbox").nth(1)
            await password_input.click()
            await password_input.fill("")
            await self._page.keyboard.type(password, delay=30)

            await self._page.wait_for_timeout(300)
            await self._submit_login_form(password_input)
            LOGGER.info("Browser login form submitted")

            tgt_task = asyncio.create_task(self._tgt_event.wait())
            response_2fa_task = asyncio.create_task(self._two_factor_detected.wait())
            ui_2fa_task = asyncio.create_task(self._wait_for_otp_ui())
            error_task = asyncio.create_task(self._wait_for_login_error())

            done, pending = await asyncio.wait(
                {tgt_task, response_2fa_task, ui_2fa_task, error_task},
                timeout=BROWSER_LOGIN_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

            if self._tgt:
                await self.close()
                return CASLoginSuccess(tgt=self._tgt, expires_at=time.time() + 8 * 3600)

            if response_2fa_task in done or (ui_2fa_task in done and ui_2fa_task.result()):
                needs_cleanup = False
                info = self._two_factor_info or {}
                login_ticket = self._login_ticket or "browser-2fa"
                return CASLoginTwoFactorRequired(
                    login_ticket=login_ticket,
                    session_id=login_ticket,
                    two_factor_auth_type=info.get("twoFactorAuthType", "SMS"),
                    methods=[info.get("twoFactorAuthType", "SMS")],
                    expires_at=time.time() + OTP_WAIT_TIMEOUT,
                )

            if error_task in done and (message := error_task.result()):
                raise CASError("BROWSER_AUTH_REJECTED", message)

            await self._save_debug_artifacts("login-timeout")
            raise CASError(
                "BROWSER_AUTH_TIMEOUT",
                f"Login timed out after {BROWSER_LOGIN_TIMEOUT_SECONDS}s - no TGT or OTP screen detected",
            )
        except BaseException:
            if needs_cleanup:
                await self.close()
            raise

    async def submit_otp(self, code: str) -> CASLoginSuccess:
        if not self._page:
            raise CASError("BROWSER_AUTH_NO_PAGE", "Browser page not available")

        try:
            LOGGER.info("Submitting OTP code via browser")
            otp_input = await self._find_visible_otp_input(timeout_ms=30_000)
            await otp_input.fill(code)

            button = self._page.get_by_role(
                "button",
                name=re.compile(r"(verify|verification|submit|continue|potwier|weryfik|dalej)", re.I),
            )
            if await button.first.is_visible(timeout=3000):
                await button.first.click()
            else:
                await otp_input.press("Enter")

            try:
                await asyncio.wait_for(self._tgt_event.wait(), timeout=60)
            except TimeoutError as err:
                await self._save_debug_artifacts("otp-timeout")
                raise CASError("BROWSER_AUTH_OTP_TIMEOUT", "Timed out waiting for TGT after OTP") from err

            if not self._tgt:
                raise CASError("BROWSER_AUTH_NO_TGT", "OTP submitted but no TGT received")
            return CASLoginSuccess(tgt=self._tgt, expires_at=time.time() + 8 * 3600)
        finally:
            await self.close()

    async def _on_response(self, response) -> None:
        try:
            url = response.url
            if "v2/tickets" in url and "serviceTicket" not in url:
                body = None
                with contextlib.suppress(Exception):
                    body = await response.json()
                if isinstance(body, dict):
                    login_phase = body.get("loginPhase")
                    ticket = body.get("ticket") or body.get("tgt")

                    if login_phase == "TGT_CREATED" and ticket and ticket.startswith("TGT-"):
                        LOGGER.info("TGT intercepted from v2/tickets response")
                        self._tgt = ticket
                        self._tgt_event.set()
                        return

                    if login_phase == "TWO_FACTOR_REQUIRED":
                        self._login_ticket = (
                            body.get("ticket") or body.get("loginTicket") or body.get("sessionId") or ""
                        )
                        self._two_factor_info = body
                        self._two_factor_detected.set()
                        LOGGER.info("2FA required according to v2/tickets response")
                        return

            set_cookie = response.headers.get("set-cookie", "")
            if "CASTGT=" in set_cookie:
                for part in set_cookie.split(";"):
                    part = part.strip()
                    if part.startswith("CASTGT="):
                        tgt = part[len("CASTGT=") :]
                        if tgt.startswith("TGT-"):
                            LOGGER.info("TGT intercepted from CASTGT cookie")
                            self._tgt = tgt
                            self._tgt_event.set()
                            return
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("Error intercepting browser response: %s", err)

    async def _submit_login_form(self, password_input) -> None:
        for name in ("Login", "Log in", "Sign in", "Zaloguj", "Zaloguj sie"):
            button = self._page.get_by_role("button", name=name)
            if await button.is_visible(timeout=1000):
                await button.click()
                return
        await password_input.press("Enter")

    async def _wait_for_otp_ui(self) -> bool:
        deadline = time.monotonic() + BROWSER_LOGIN_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._tgt_event.is_set() or self._two_factor_detected.is_set():
                return self._two_factor_detected.is_set()
            with contextlib.suppress(Exception):
                await self._find_visible_otp_input(timeout_ms=500)
                LOGGER.info("OTP screen detected from browser UI")
                return True
            with contextlib.suppress(Exception):
                body = (await self._page.locator("body").inner_text(timeout=500)).lower()
                if ("otp" in body or "sms" in body or "kod" in body) and (
                    "weryfik" in body or "code" in body or "verification" in body
                ):
                    LOGGER.info("OTP screen text detected from browser UI")
                    return True
            await asyncio.sleep(0.5)
        return False

    async def _wait_for_login_error(self) -> str | None:
        deadline = time.monotonic() + BROWSER_LOGIN_TIMEOUT_SECONDS
        patterns = (
            "invalid",
            "incorrect",
            "nieprawid",
            "bled",
            "wrong",
            "zablok",
        )
        while time.monotonic() < deadline:
            if self._tgt_event.is_set() or self._two_factor_detected.is_set():
                return None
            with contextlib.suppress(Exception):
                body = (await self._page.locator("body").inner_text(timeout=500)).lower()
                for pattern in patterns:
                    if pattern in body:
                        return "XTB rejected the login form. Check login/password or account status."
            await asyncio.sleep(1)
        return None

    async def _find_visible_otp_input(self, *, timeout_ms: int):
        locators = [
            self._page.get_by_placeholder(re.compile(r"(otp|code|kod)", re.I)),
            self._page.locator("input[inputmode='numeric']"),
            self._page.locator("input[type='tel']"),
            self._page.locator("input[autocomplete='one-time-code']"),
            self._page.locator("input[maxlength='6']"),
        ]
        per_locator_timeout = max(250, min(timeout_ms // len(locators), 2000))
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            for locator in locators:
                candidate = locator.first
                if await candidate.is_visible(timeout=per_locator_timeout):
                    return candidate
            await asyncio.sleep(0.25)
        raise CASError("BROWSER_AUTH_NO_OTP_INPUT", "OTP input was not visible")

    async def _save_debug_artifacts(self, prefix: str) -> None:
        if not self._page:
            return
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        base = DEBUG_DIR / f"{prefix}-{stamp}"
        with contextlib.suppress(Exception):
            await self._page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        with contextlib.suppress(Exception):
            base.with_suffix(".html").write_text(await self._page.content(), encoding="utf-8")


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def login_start(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    email = str(payload.get("email") or "").strip()
    password = str(payload.get("password") or "")

    if not email or not password:
        return web.json_response({"error": "Email and password are required"}, status=400)

    await _cleanup_pending_logins()
    cas = _new_cas(email)
    try:
        result, browser_auth = await _login_with_fallback(cas, email, password)
    except Exception as err:  # noqa: BLE001
        LOGGER.exception("Login start failed for %s", email)
        await _close_cas(cas)
        return web.json_response({"error": str(err)}, status=401)

    return await _login_result_response(
        email=email,
        password=password,
        cas=cas,
        result=result,
        browser_auth=browser_auth,
    )


async def login_complete(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    challenge_id = str(payload.get("challenge_id") or "").strip()
    otp = str(payload.get("otp") or "").replace(" ", "")

    if not challenge_id or not otp:
        return web.json_response({"error": "Challenge ID and OTP are required"}, status=400)

    await _cleanup_pending_logins()
    async with PENDING_LOCK:
        pending = PENDING_LOGINS.get(challenge_id)
        if pending is None:
            return web.json_response({"error": "OTP challenge expired. Start login again."}, status=410)

    try:
        if pending.browser_auth:
            result = await pending.cas.submit_browser_otp(otp)
        else:
            result = await pending.cas.login_with_two_factor(
                pending.challenge.login_ticket,
                otp,
                pending.challenge.two_factor_auth_type,
                session_id=pending.challenge.session_id,
            )
    except Exception as err:  # noqa: BLE001
        LOGGER.warning("OTP completion failed for %s: %s", pending.email, err)
        return web.json_response({"error": str(err)}, status=401)

    if isinstance(result, CASLoginTwoFactorRequired):
        pending.challenge = result
        pending.expires_at = min(result.expires_at, time.time() + PENDING_LOGIN_TTL_SECONDS)
        return _otp_required_response(challenge_id, result)

    PENDING_LOGINS.pop(challenge_id, None)
    return await _login_result_response(
        email=pending.email,
        password=pending.password,
        cas=pending.cas,
        result=result,
        browser_auth=False,
    )


async def snapshot(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    email = str(payload.get("email") or "").strip()
    password = str(payload.get("password") or "")

    if not email or not password:
        return web.json_response({"error": "Email and password are required"}, status=400)

    key = _cache_key(email, password)
    client = CLIENTS.get(key)
    if client is None:
        client = ManagedClient(email=email, password=password)
        CLIENTS[key] = client

    try:
        data = await client.get_snapshot()
    except Exception as err:  # noqa: BLE001 - bridge must return actionable errors
        LOGGER.exception("Snapshot failed for %s", email)
        await client.close()
        if _needs_reauth(err):
            return web.json_response(
                {"error": "XTB session expired. Reauthentication with a fresh OTP is required."},
                status=428,
            )
        return web.json_response({"error": str(err)}, status=502)

    return web.json_response(data)


async def _login_result_response(
    *,
    email: str,
    password: str,
    cas: CASClient,
    result: CASLoginSuccess | CASLoginTwoFactorRequired,
    browser_auth: bool,
) -> web.Response:
    if isinstance(result, CASLoginTwoFactorRequired):
        challenge_id = secrets.token_urlsafe(24)
        async with PENDING_LOCK:
            PENDING_LOGINS[challenge_id] = PendingLogin(
                email=email,
                password=password,
                cas=cas,
                challenge=result,
                browser_auth=browser_auth,
                expires_at=min(result.expires_at, time.time() + PENDING_LOGIN_TTL_SECONDS),
            )
        return _otp_required_response(challenge_id, result)

    _save_session_file(_session_file(email, password), result.tgt, result.expires_at)
    try:
        account_number = await _discover_account_number(email, password)
        return web.json_response(
            {
                "status": "ok",
                "account_number": account_number,
            }
        )
    finally:
        await _close_cas(cas)


def _otp_required_response(challenge_id: str, challenge: CASLoginTwoFactorRequired) -> web.Response:
    return web.json_response(
        {
            "status": "requires_otp",
            "challenge_id": challenge_id,
            "two_factor_auth_type": challenge.two_factor_auth_type,
            "methods": challenge.methods,
            "expires_at": datetime.fromtimestamp(challenge.expires_at, UTC).isoformat(),
        }
    )


async def _login_with_fallback(
    cas: CASClient,
    email: str,
    password: str,
) -> tuple[CASLoginSuccess | CASLoginTwoFactorRequired, bool]:
    try:
        return await cas.login(email, password), False
    except CASError as err:
        if "UNAUTHORIZED" in err.code:
            raise
        LOGGER.info("REST CAS login failed (%s), trying browser fallback", err.code)
        return await _login_with_browser(cas, email, password), True
    except Exception as err:
        LOGGER.info("REST CAS login failed (%s), trying browser fallback", err)
        return await _login_with_browser(cas, email, password), True


async def _login_with_browser(
    cas: CASClient,
    email: str,
    password: str,
) -> CASLoginSuccess | CASLoginTwoFactorRequired:
    cas._browser_auth = ResilientBrowserCASAuth(headless=True)
    return await cas._browser_auth.login(email, password)


async def _discover_account_number(email: str, password: str) -> int:
    managed = CLIENTS.get(_cache_key(email, password))
    if managed is None:
        managed = ManagedClient(email=email, password=password)
        CLIENTS[_cache_key(email, password)] = managed

    await managed.close()
    managed.account_number = None
    account_number = await managed._discover_account_number()
    managed.account_number = account_number
    return account_number


async def _snapshot(client: XTBClient) -> dict[str, Any]:
    balance_raw = await client.get_balance()
    positions_raw = await client.get_positions()
    orders_raw = await client.get_orders()

    account = _normalize_account(balance_raw)
    positions = [_normalize_position(position) for position in positions_raw]
    orders = [_normalize_order(order) for order in orders_raw]

    symbols = sorted(
        {
            *(position["symbol"] for position in positions if position.get("symbol")),
            *(order["symbol"] for order in orders if order.get("symbol")),
        }
    )

    quotes: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        try:
            quote = await client.get_quote(symbol)
            quotes[symbol] = _normalize_quote(symbol, quote)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("Unable to fetch quote for %s: %s", symbol, err)
            quotes[symbol] = {"symbol": symbol, "available": False, "error": str(err)}

    return {
        "account": account,
        "summary": _build_summary(account, positions, orders, quotes),
        "positions": positions,
        "orders": orders,
        "quotes": quotes,
        "session": _normalize_session(client),
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _normalize_account(raw: Any) -> dict[str, Any]:
    data = _to_dict(raw)
    return {
        "account_number": _int(data.get("account_number")),
        "balance": _float(data.get("balance")),
        "equity": _float(data.get("equity")),
        "free_margin": _float(data.get("free_margin")),
        "currency": data.get("currency") or "",
    }


def _normalize_position(raw: Any) -> dict[str, Any]:
    data = _to_dict(raw)
    symbol = str(data.get("symbol") or "").upper()
    current_price = _float(data.get("current_price")) or 0.0
    open_price = _float(data.get("open_price")) or 0.0
    price_change = current_price - open_price if current_price and open_price else None
    price_change_percent = (
        (price_change / open_price) * 100 if price_change is not None and open_price else None
    )

    return {
        "symbol": symbol,
        "side": data.get("side"),
        "volume": _float(data.get("volume")) or 0.0,
        "current_price": current_price,
        "open_price": open_price,
        "price_change": _rounded(price_change),
        "price_change_percent": _rounded(price_change_percent),
        "profit_net": _float(data.get("profit_net")) or 0.0,
        "profit_percent": _float(data.get("profit_percent")) or 0.0,
        "stop_loss": _float(data.get("stop_loss")),
        "take_profit": _float(data.get("take_profit")),
        "swap": _float(data.get("swap")),
        "commission": _float(data.get("commission")),
        "margin": _float(data.get("margin")),
        "order_id": data.get("order_id"),
        "instrument_id": _int(data.get("instrument_id")),
        "open_time": data.get("open_time"),
    }


def _normalize_order(raw: Any) -> dict[str, Any]:
    data = _to_dict(raw)
    return {
        "symbol": str(data.get("symbol") or "").upper(),
        "side": data.get("side"),
        "volume": _float(data.get("volume")) or 0.0,
        "price": _float(data.get("price")) or 0.0,
        "stop_loss": _float(data.get("stop_loss")),
        "take_profit": _float(data.get("take_profit")),
        "order_id": data.get("order_id"),
        "order_type": data.get("order_type"),
        "instrument_id": _int(data.get("instrument_id")),
        "expiration": data.get("expiration"),
        "open_time": data.get("open_time"),
    }


def _normalize_quote(symbol: str, raw: Any) -> dict[str, Any]:
    if raw is None:
        return {"symbol": symbol.upper(), "available": False}

    data = _to_dict(raw)
    bid = _float(data.get("bid"))
    ask = _float(data.get("ask"))
    mid = ((bid + ask) / 2) if bid is not None and ask is not None else None
    spread = _float(data.get("spread"))
    if spread is None and bid is not None and ask is not None:
        spread = ask - bid

    previous = _first_float(data, "previous_close", "prev_close", "close", "open")
    daily_change = _first_float(data, "daily_change", "change")
    if daily_change is None and mid is not None and previous:
        daily_change = mid - previous

    daily_change_percent = _first_float(data, "daily_change_percent", "change_percent")
    if daily_change_percent is None and daily_change is not None and previous:
        daily_change_percent = (daily_change / previous) * 100

    return {
        "symbol": str(data.get("symbol") or symbol).upper(),
        "available": True,
        "bid": bid,
        "ask": ask,
        "mid": _rounded(mid),
        "spread": _rounded(spread),
        "spread_percent": _rounded((spread / mid) * 100 if spread is not None and mid else None),
        "high": _float(data.get("high")),
        "low": _float(data.get("low")),
        "previous": previous,
        "daily_change": _rounded(daily_change),
        "daily_change_percent": _rounded(daily_change_percent),
        "time": data.get("time"),
    }


def _normalize_session(client: XTBClient) -> dict[str, Any]:
    expires_at = getattr(client, "session_expires_at", None)
    expires_at_iso = None
    if isinstance(expires_at, int | float):
        expires_at_iso = datetime.fromtimestamp(expires_at, UTC).isoformat()

    source = getattr(client, "session_source", None)
    return {
        "connected": bool(getattr(client, "is_connected", False)),
        "authenticated": bool(getattr(client, "is_authenticated", False)),
        "source": str(source) if source is not None else None,
        "expires_at": expires_at_iso,
        "account_number": getattr(client, "account_number", None),
    }


def _build_summary(
    account: dict[str, Any],
    positions: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    quotes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    balance = account.get("balance") or 0.0
    equity = account.get("equity") or 0.0
    free_margin = account.get("free_margin") or 0.0
    used_margin = max(equity - free_margin, 0.0) if equity else 0.0
    open_profit_net = sum(position.get("profit_net") or 0.0 for position in positions)
    open_profit_percent = ((equity - balance) / balance) * 100 if balance else None
    quote_values = [quote for quote in quotes.values() if quote.get("available")]
    daily_changes = [
        quote["daily_change_percent"]
        for quote in quote_values
        if quote.get("daily_change_percent") is not None
    ]

    return {
        "account_number": account.get("account_number"),
        "currency": account.get("currency") or "",
        "balance": _rounded(balance),
        "equity": _rounded(equity),
        "free_margin": _rounded(free_margin),
        "used_margin": _rounded(used_margin),
        "margin_level_percent": _rounded((equity / used_margin) * 100 if used_margin else None),
        "open_positions": len(positions),
        "pending_orders": len(orders),
        "open_profit_net": _rounded(open_profit_net),
        "open_profit_percent": _rounded(open_profit_percent),
        "quotes_available": len(quote_values),
        "average_daily_change_percent": _rounded(
            sum(daily_changes) / len(daily_changes) if daily_changes else None
        ),
    }


def _to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    return {
        key: attr
        for key in dir(value)
        if not key.startswith("_") and not callable(attr := getattr(value, key))
    }


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_float(data: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float(data.get(key))
        if value is not None:
            return value
    return None


def _rounded(value: float | None, precision: int = 4) -> float | None:
    if value is None:
        return None
    return round(value, precision)


def _cache_key(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _session_file(email: str, password: str) -> Path:
    return SESSION_DIR / f"{_cache_key(email, password)}.json"


def _cookies_file(email: str) -> Path:
    return SESSION_DIR / f"{_cache_key(email)}_cookies.json"


def _new_cas(email: str) -> CASClient:
    return CASClient(CASClientConfig(cookies_file=_cookies_file(email)))


def _save_session_file(path: Path, tgt: str, expires_at: float) -> None:
    extracted_at = datetime.now(UTC)
    expires_at_dt = datetime.fromtimestamp(expires_at, tz=UTC)
    data = {
        "tgt": tgt,
        "extracted_at": extracted_at.isoformat(),
        "expires_at": expires_at_dt.isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=2)
    fd = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)


async def _cleanup_pending_logins() -> None:
    now = time.time()
    async with PENDING_LOCK:
        expired = [
            challenge_id
            for challenge_id, pending in PENDING_LOGINS.items()
            if pending.expires_at < now
        ]
        pending_to_close = [
            pending
            for challenge_id in expired
            if (pending := PENDING_LOGINS.pop(challenge_id, None)) is not None
        ]
    await asyncio.gather(*(_close_cas(pending.cas) for pending in pending_to_close), return_exceptions=True)


async def _close_cas(cas: CASClient) -> None:
    browser_auth = getattr(cas, "_browser_auth", None)
    if browser_auth is not None:
        with contextlib.suppress(Exception):
            await browser_auth.close()
        cas._browser_auth = None
    await cas.aclose()


def _needs_reauth(err: Exception) -> bool:
    if isinstance(err, CASError):
        code = err.code.upper()
        return "2FA" in code or "TGT_EXPIRED" in code or "NO_SECRET" in code
    text = str(err).upper()
    return "2FA" in text or "TWO" in text and "FACTOR" in text or "OTP" in text


def _port() -> int:
    options_file = DATA_DIR / "options.json"
    if options_file.exists():
        try:
            options = json.loads(options_file.read_text(encoding="utf-8"))
            return int(options.get("port", DEFAULT_PORT))
        except Exception as err:  # noqa: BLE001
            LOGGER.warning("Unable to read add-on options: %s", err)
    return int(os.environ.get("PORT", DEFAULT_PORT))


def main() -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/", health)
    app.router.add_post("/login/start", login_start)
    app.router.add_post("/login/complete", login_complete)
    app.router.add_post("/snapshot", snapshot)
    port = _port()
    LOGGER.info("Starting XTB Bridge on 0.0.0.0:%s", port)
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
