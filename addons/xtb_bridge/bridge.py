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
from xtb_api.types.enums import SubscriptionEid
from xtb_api.types.websocket import CASLoginSuccess, CASLoginTwoFactorRequired

DATA_DIR = Path(os.environ.get("XTB_BRIDGE_DATA", "/data"))
SESSION_DIR = DATA_DIR / "sessions"
DEBUG_DIR = DATA_DIR / "debug"
DEFAULT_PORT = 8765
PENDING_LOGIN_TTL_SECONDS = 300
BROWSER_LOGIN_TIMEOUT_SECONDS = int(os.environ.get("XTB_BROWSER_LOGIN_TIMEOUT", "90"))
BALANCE_SNAPSHOT_POLL_MS = 200
BALANCE_SNAPSHOT_MAX_WAIT_MS = 3000

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
LOGGER = logging.getLogger("xtb_bridge")
PENDING_LOCK = asyncio.Lock()


class ManagedClient:
    """A cached XTB client with automatic account-number discovery."""

    def __init__(
        self,
        *,
        email: str,
        password: str,
        account_number: int | None = None,
    ) -> None:
        self.email = email
        self.password = password
        self.account_number = account_number
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
            accounts = _accounts_from_client(probe)
            account = _select_account(accounts, requested_account_number=self.account_number)
            if account is None:
                raise RuntimeError("XTB login succeeded but no account number was returned")
            account_number = int(account["account_number"])
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
    account_number = _int(payload.get("account_number"))

    if not email or not password:
        return web.json_response({"error": "Email and password are required"}, status=400)

    try:
        accounts = await _discover_accounts(email, password)
        selected_account = _select_account(accounts, requested_account_number=account_number)
        if selected_account is None:
            key = _client_key(email, password, account_number)
            client = CLIENTS.get(key)
            if client is None:
                client = ManagedClient(email=email, password=password, account_number=account_number)
                CLIENTS[key] = client
            data = await client.get_snapshot()
        else:
            tracked_accounts = _tracked_accounts(accounts, selected_account)
            data = await _snapshot_for_accounts(
                email=email,
                password=password,
                accounts=tracked_accounts,
                primary_account_number=int(selected_account["account_number"]),
            )
    except Exception as err:  # noqa: BLE001 - bridge must return actionable errors
        LOGGER.exception("Snapshot failed for %s", email)
        await _close_clients_for(email, password)
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
        accounts = await _discover_accounts(email, password)
        account = _select_account(accounts)
        if account is None:
            raise RuntimeError("XTB login succeeded but no account number was returned")
        account_number = int(account["account_number"])
        return web.json_response(
            {
                "status": "ok",
                "account_number": account_number,
                "accounts": accounts,
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


async def _discover_accounts(email: str, password: str) -> list[dict[str, Any]]:
    probe = XTBClient(
        email=email,
        password=password,
        account_number=0,
        account_type="real",
        session_file=_session_file(email, password),
    )
    try:
        await probe.connect()
        accounts = _accounts_from_client(probe)
        if not accounts:
            fallback = int(probe.ws.get_account_number())
            accounts = [
                {
                    "account_number": fallback,
                    "currency": "",
                    "endpoint_type": "",
                }
            ]
        LOGGER.info("Discovered %d XTB account(s) for %s", len(accounts), email)
        return accounts
    finally:
        await probe.disconnect()


def _accounts_from_client(client: XTBClient) -> list[dict[str, Any]]:
    login_result = client.ws.account_info
    if not login_result:
        return []

    accounts: list[dict[str, Any]] = []
    for account in login_result.accountList:
        account_number = _int(getattr(account, "accountNo", None))
        if account_number is None:
            continue
        accounts.append(
            {
                "account_number": account_number,
                "currency": str(getattr(account, "currency", "") or "").upper(),
                "endpoint_type": str(getattr(account, "endpointType", "") or "").upper(),
            }
        )
    return accounts


def _select_account(
    accounts: list[dict[str, Any]],
    *,
    requested_account_number: int | None = None,
) -> dict[str, Any] | None:
    if not accounts:
        return None

    if requested_account_number is not None:
        for account in accounts:
            if _int(account.get("account_number")) == requested_account_number:
                return account
        raise RuntimeError(f"Requested XTB account {requested_account_number} is not available")

    def score(account: dict[str, Any]) -> tuple[int, int, int]:
        currency = str(account.get("currency") or "").upper()
        endpoint_type = str(account.get("endpoint_type") or "").upper()
        is_demo = "DEMO" in endpoint_type
        is_real = "REAL" in endpoint_type or not is_demo
        return (
            1 if is_real else 0,
            1 if currency == "PLN" else 0,
            0 if is_demo else 1,
        )

    return max(accounts, key=score)


def _tracked_accounts(
    accounts: list[dict[str, Any]],
    selected_account: dict[str, Any],
) -> list[dict[str, Any]]:
    """Track the selected account plus related real accounts in the same currency."""
    selected_number = _int(selected_account.get("account_number"))
    selected_currency = str(selected_account.get("currency") or "").upper()

    if not selected_currency or not _is_real_account(selected_account):
        return [selected_account]

    tracked = [
        account
        for account in accounts
        if _is_real_account(account)
        and str(account.get("currency") or "").upper() == selected_currency
    ]
    if selected_number is not None and not any(
        _int(account.get("account_number")) == selected_number for account in tracked
    ):
        tracked.insert(0, selected_account)

    tracked = sorted(
        tracked,
        key=lambda account: (
            0 if _int(account.get("account_number")) == selected_number else 1,
            _int(account.get("account_number")) or 0,
        ),
    )
    LOGGER.info(
        "Tracking XTB account(s): %s",
        ", ".join(str(account.get("account_number")) for account in tracked),
    )
    return tracked or [selected_account]


def _is_real_account(account: dict[str, Any]) -> bool:
    endpoint_type = str(account.get("endpoint_type") or "").upper()
    return "DEMO" not in endpoint_type and ("REAL" in endpoint_type or not endpoint_type)


async def _snapshot_for_accounts(
    *,
    email: str,
    password: str,
    accounts: list[dict[str, Any]],
    primary_account_number: int,
) -> dict[str, Any]:
    snapshots: list[dict[str, Any]] = []
    for account in accounts:
        account_number = _int(account.get("account_number"))
        if account_number is None:
            continue
        key = _client_key(email, password, account_number)
        client = CLIENTS.get(key)
        if client is None:
            client = ManagedClient(email=email, password=password, account_number=account_number)
            CLIENTS[key] = client
        snapshots.append(await client.get_snapshot())

    if not snapshots:
        raise RuntimeError("XTB login succeeded but no account could be tracked")
    if len(snapshots) == 1:
        return snapshots[0]
    return _merge_snapshots(snapshots, primary_account_number)


async def _close_clients_for(email: str, password: str) -> None:
    close_tasks = [
        client.close()
        for client in CLIENTS.values()
        if client.email == email and client.password == password
    ]
    if close_tasks:
        await asyncio.gather(*close_tasks, return_exceptions=True)


def _merge_snapshots(
    snapshots: list[dict[str, Any]],
    primary_account_number: int,
) -> dict[str, Any]:
    primary = next(
        (
            snapshot
            for snapshot in snapshots
            if _int(snapshot.get("account", {}).get("account_number")) == primary_account_number
        ),
        snapshots[0],
    )

    accounts = [snapshot.get("account", {}) for snapshot in snapshots]
    positions = [
        position
        for snapshot in snapshots
        for position in snapshot.get("positions", [])
    ]
    orders = [
        order
        for snapshot in snapshots
        for order in snapshot.get("orders", [])
    ]
    quotes: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        quotes.update(snapshot.get("quotes", {}))

    merged_account = {
        "account_number": primary_account_number,
        "account_numbers": [
            account.get("account_number")
            for account in accounts
            if account.get("account_number") is not None
        ],
        "account_count": len(accounts),
        "currency": primary.get("account", {}).get("currency") or accounts[0].get("currency") or "",
        "balance": _sum_values(accounts, "balance"),
        "cash_balance": _sum_values(accounts, "cash_balance"),
        "side_bar_account_value": _sum_values(accounts, "side_bar_account_value"),
        "total_equity": _sum_values(accounts, "total_equity"),
        "equity": _sum_values(accounts, "equity"),
        "free_margin": _sum_values(accounts, "free_margin"),
        "asset_value": _sum_values(accounts, "asset_value"),
        "account_value": _sum_values(accounts, "account_value"),
        "portfolio_value": _sum_values(accounts, "portfolio_value"),
        "profit_net": _sum_values(accounts, "profit_net"),
        "value_source": "aggregate",
        "accounts": accounts,
    }

    return {
        "account": merged_account,
        "summary": _build_summary(merged_account, positions, orders, quotes),
        "positions": positions,
        "orders": orders,
        "quotes": quotes,
        "session": primary.get("session", {}),
        "updated_at": datetime.now(UTC).isoformat(),
    }


async def _snapshot(client: XTBClient) -> dict[str, Any]:
    account_meta = _current_account_meta(client)
    balance_raw = await _fetch_balance_data(client)
    positions_raw = await _fetch_trade_data(client, SubscriptionEid.POSITIONS, "getPositions")
    orders_raw = await _fetch_trade_data(client, SubscriptionEid.ORDERS, "getAllOrders")

    account = _normalize_account(balance_raw, account_meta)
    positions = [_normalize_position(position, account) for position in positions_raw]
    orders = [_normalize_order(order, account) for order in orders_raw]

    symbols = sorted(
        {
            *(position["symbol"] for position in positions if position.get("symbol")),
            *(order["symbol"] for order in orders if order.get("symbol")),
        }
    )
    instruments: dict[str, dict[str, Any]] = {}
    symbol_keys: dict[str, str] = {}
    for item in [*positions, *orders]:
        symbol = item.get("symbol")
        symbol_key = item.get("symbol_key")
        if symbol and symbol_key:
            symbol_keys[str(symbol)] = str(symbol_key)

    for symbol in symbols:
        instruments[symbol] = await _fetch_instrument_info(client, symbol)
        if instruments[symbol].get("symbol_key"):
            symbol_keys.setdefault(symbol, str(instruments[symbol]["symbol_key"]))

    for item in [*positions, *orders]:
        _apply_instrument_info(item, instruments.get(str(item.get("symbol"))))

    quotes: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        try:
            quote = await _fetch_quote_data(client, symbol, symbol_keys.get(symbol))
            if quote is None:
                quote = await client.get_quote(symbol)
            quotes[symbol] = _normalize_quote(symbol, quote)
            _apply_instrument_info(quotes[symbol], instruments.get(symbol))
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("Unable to fetch raw quote for %s: %s", symbol, err)
            try:
                quotes[symbol] = _normalize_quote(symbol, await client.get_quote(symbol))
                _apply_instrument_info(quotes[symbol], instruments.get(symbol))
            except Exception as fallback_err:  # noqa: BLE001
                LOGGER.debug("Unable to fetch fallback quote for %s: %s", symbol, fallback_err)
                quotes[symbol] = {
                    "symbol": symbol,
                    "available": False,
                    "error": str(fallback_err),
                }
                _apply_instrument_info(quotes[symbol], instruments.get(symbol))

    for position in positions:
        quote = quotes.get(position["symbol"], {})
        if position.get("current_price") in (None, 0) and quote.get("mid") is not None:
            position["current_price"] = quote.get("mid")
        if position.get("daily_change_percent") is None:
            position["daily_change_percent"] = quote.get("daily_change_percent")
        if position.get("daily_change") is None:
            position["daily_change"] = quote.get("daily_change")
        current_price = _float(position.get("current_price"))
        open_price = _float(position.get("open_price"))
        volume = _float(position.get("volume"))
        if position.get("market_value") is None and current_price is not None and volume:
            position["market_value"] = _rounded(current_price * volume)
        if position.get("price_change") is None and current_price is not None and open_price:
            price_change = current_price - open_price
            position["price_change"] = _rounded(price_change)
            position["price_change_percent"] = _rounded((price_change / open_price) * 100)

    calculated_asset_value = sum(
        value
        for position in positions
        if (value := _float(position.get("market_value"))) is not None
    )
    if account.get("asset_value") is None and calculated_asset_value:
        account["asset_value"] = _rounded(calculated_asset_value)
    if account.get("portfolio_value") is None:
        cash_balance = _float(account.get("cash_balance"))
        asset_value = _float(account.get("asset_value"))
        if cash_balance is not None and asset_value is not None:
            account["portfolio_value"] = _rounded(cash_balance + asset_value)
            account["account_value"] = account["portfolio_value"]
            account["value_source"] = "cash_plus_assets"

    return {
        "account": account,
        "summary": _build_summary(account, positions, orders, quotes),
        "positions": positions,
        "orders": orders,
        "quotes": quotes,
        "session": _normalize_session(client),
        "updated_at": datetime.now(UTC).isoformat(),
    }


async def _fetch_balance_data(client: XTBClient) -> dict[str, Any]:
    deadline = time.monotonic() + BALANCE_SNAPSHOT_MAX_WAIT_MS / 1000
    last_balance: dict[str, Any] = {}

    while True:
        res = await client.ws.send(
            "getBalance",
            {"getAndSubscribeElement": {"eid": SubscriptionEid.TOTAL_BALANCE}},
        )
        balance = _first_element_payload(client.ws._extract_elements(res), "xtotalbalance")
        if balance:
            return balance
        last_balance = balance
        if time.monotonic() >= deadline:
            return last_balance
        await asyncio.sleep(BALANCE_SNAPSHOT_POLL_MS / 1000)


async def _fetch_trade_data(
    client: XTBClient,
    eid: SubscriptionEid,
    command_name: str,
) -> list[dict[str, Any]]:
    res = await client.ws.send(
        command_name,
        {"getAndSubscribeElement": {"eid": eid}},
        timeout_ms=30000,
    )
    trades: list[dict[str, Any]] = []
    for element in client.ws._extract_elements(res):
        trade = (element or {}).get("value", {}).get("xcfdtrade")
        if isinstance(trade, dict):
            trades.append(trade)
    return trades


async def _fetch_quote_data(
    client: XTBClient,
    symbol: str,
    symbol_key: str | None,
) -> dict[str, Any] | None:
    keys = _quote_candidate_keys(symbol, symbol_key)
    with contextlib.suppress(Exception):
        matches = await client.search_instrument(symbol)
        exact = [
            match.symbol_key
            for match in matches
            if match.symbol.upper() == symbol.upper()
        ]
        related = [match.symbol_key for match in matches[:3]]
        keys = _dedupe_strings([*exact, *keys, *related])

    for key in keys:
        try:
            res = await client.ws.subscribe_ticks(key)
            try:
                tick = _first_element_payload(client.ws._extract_elements(res), "xcfdtick")
            finally:
                with contextlib.suppress(Exception):
                    await client.ws.unsubscribe_ticks(key)
            if tick:
                return tick
        except Exception:
            continue
    return None


async def _fetch_instrument_info(client: XTBClient, symbol: str) -> dict[str, Any]:
    try:
        matches = await client.search_instrument(symbol)
    except Exception as err:  # noqa: BLE001
        LOGGER.debug("Unable to fetch instrument metadata for %s: %s", symbol, err)
        return {"symbol": symbol, "name": symbol}

    if not matches:
        return {"symbol": symbol, "name": symbol}

    exact = next(
        (match for match in matches if str(match.symbol).upper() == symbol.upper()),
        matches[0],
    )
    data = _to_dict(exact)
    name = _first_str(data, "name", "description", "display_name", "displayName") or symbol
    description = _first_str(data, "description", "full_description", "fullDescription") or name
    market_info = _normalize_quote(symbol, data)
    return {
        "symbol": symbol,
        "name": name,
        "display_name": name,
        "description": description,
        "symbol_key": _first_str(data, "symbol_key", "symbolKey"),
        "instrument_id": _int(_lookup_value(data, "instrument_id")),
        "bid": market_info.get("bid"),
        "ask": market_info.get("ask"),
        "mid": market_info.get("mid"),
        "spread": market_info.get("spread"),
        "spread_percent": market_info.get("spread_percent"),
        "high": market_info.get("high"),
        "low": market_info.get("low"),
        "previous": market_info.get("previous"),
        "daily_change": market_info.get("daily_change"),
        "daily_change_percent": market_info.get("daily_change_percent"),
        "time": market_info.get("time"),
    }


def _apply_instrument_info(item: dict[str, Any], info: dict[str, Any] | None) -> None:
    if not info:
        return
    for key in (
        "name",
        "display_name",
        "description",
        "symbol_key",
        "instrument_id",
        "bid",
        "ask",
        "mid",
        "spread",
        "spread_percent",
        "high",
        "low",
        "previous",
        "daily_change",
        "daily_change_percent",
        "time",
    ):
        if item.get(key) in (None, "") and info.get(key) not in (None, ""):
            item[key] = info[key]


def _quote_candidate_keys(symbol: str, symbol_key: str | None) -> list[str]:
    candidates = []
    if symbol_key:
        candidates.append(symbol_key)
    if "_" in symbol:
        candidates.append(symbol)
    else:
        candidates.extend([f"9_{symbol}_6", symbol])

    seen: set[str] = set()
    result: list[str] = []
    for candidate in candidates:
        candidate = str(candidate or "").strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        value = str(value or "").strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _first_element_payload(elements: list[dict[str, Any]], payload_key: str) -> dict[str, Any]:
    for element in elements:
        payload = (element or {}).get("value", {}).get(payload_key)
        if isinstance(payload, dict):
            return payload
    return {}


def _current_account_meta(client: XTBClient) -> dict[str, Any]:
    account_number = _int(getattr(client, "account_number", None))
    currency = ""
    endpoint_type = ""

    login_result = client.ws.account_info
    if login_result:
        for account in login_result.accountList:
            if account_number is None or _int(getattr(account, "accountNo", None)) == account_number:
                account_number = _int(getattr(account, "accountNo", None))
                currency = str(getattr(account, "currency", "") or "").upper()
                endpoint_type = str(getattr(account, "endpointType", "") or "").upper()
                break

    return {
        "account_number": account_number,
        "currency": currency,
        "endpoint_type": endpoint_type,
    }


def _normalize_account(raw: Any, meta: dict[str, Any]) -> dict[str, Any]:
    data = _to_dict(raw)
    raw_balance = _first_float(data, "balance")
    cash_balance = _first_float(
        data,
        "freeFunds",
        "free_funds",
        "cashBalance",
        "cash_balance",
        "availableCash",
        "cash",
        "balance",
    )
    free_margin = _first_float(data, "freeMargin", "free_margin", "freeFunds", "free_funds")
    equity = _first_float(data, "equity", "netLiquidationValue", "net_liquidation_value")
    asset_value = _first_float(
        data,
        "assetValue",
        "asset_value",
        "assetsValue",
        "securitiesValue",
        "stockValue",
        "stocksValue",
        "marketValue",
        "portfolioAssetValue",
    )
    total_equity = _first_float(data, "totalEquity", "total_equity")
    side_bar_value = _first_float(
        data,
        "sideBarAccountValue",
        "side_bar_account_value",
        "sidebarAccountValue",
        "sideBarBalance",
        "sidebarBalance",
    )
    generic_value = _first_float(
        data,
        "accountValue",
        "account_value",
        "totalValue",
    )
    raw_portfolio_value = _first_float(data, "portfolioValue", "portfolio_value")
    largest_value, largest_key = _largest_balance_value(data)
    portfolio_value = side_bar_value
    value_source = "side_bar_account_value" if side_bar_value is not None else None

    profit_net = _first_float(
        data,
        "totalNetProfit",
        "totalNetProfitLabel",
        "total_net_profit",
        "total_net_profit_label",
        "sideBarTotalNetProfit",
        "side_bar_total_net_profit",
        "netProfit",
        "net_profit",
        "profitNet",
        "profit_net",
        "profit",
        "openProfit",
        "floatingProfit",
    )

    if portfolio_value is None and total_equity is not None and profit_net is not None:
        portfolio_value = total_equity + profit_net
        value_source = "total_equity_plus_net_profit"
    if portfolio_value is None and generic_value is not None:
        portfolio_value = generic_value
        value_source = "account_value"
    if portfolio_value is None and raw_portfolio_value is not None:
        portfolio_value = raw_portfolio_value
        value_source = "portfolio_value"
    if portfolio_value is None and cash_balance is not None and asset_value is not None:
        portfolio_value = cash_balance + asset_value
        value_source = "cash_plus_assets"
    if portfolio_value is None and largest_value is not None:
        portfolio_value = largest_value
        value_source = f"largest_balance_field:{largest_key}"
    if portfolio_value is None:
        portfolio_value = equity if equity is not None else raw_balance
        value_source = "equity_or_balance"

    return {
        "account_number": _int(data.get("account_number")) or meta.get("account_number"),
        "balance": _rounded(raw_balance),
        "cash_balance": _rounded(cash_balance),
        "side_bar_account_value": _rounded(side_bar_value),
        "total_equity": _rounded(total_equity),
        "equity": _rounded(equity),
        "free_margin": _rounded(free_margin),
        "asset_value": _rounded(asset_value),
        "account_value": _rounded(portfolio_value),
        "portfolio_value": _rounded(portfolio_value),
        "profit_net": _rounded(profit_net),
        "profit_percent": _rounded(
            _first_float(
                data,
                "totalNetProfitPercent",
                "totalNetProfitPercentage",
                "profitPercent",
                "profit_percentage",
            )
        ),
        "currency": data.get("currency") or meta.get("currency") or "",
        "endpoint_type": meta.get("endpoint_type") or "",
        "value_source": value_source,
        "raw_numeric_balance_fields": _numeric_debug_fields(data),
    }


def _normalize_position(raw: Any, account: dict[str, Any]) -> dict[str, Any]:
    data = _to_dict(raw)
    symbol = str(data.get("symbol") or "").upper()
    current_price = _first_float(
        data,
        "currentPrice",
        "current_price",
        "closePrice",
        "marketPrice",
        "price",
    )
    open_price = _first_float(data, "openPrice", "open_price", "priceOpen") or 0.0
    price_change = current_price - open_price if current_price and open_price else None
    price_change_percent = (
        (price_change / open_price) * 100 if price_change is not None and open_price else None
    )
    side = _normalize_side(_lookup_value(data, "side"))
    profit_loss = _first_float(
        data,
        "profitNet",
        "profit_net",
        "netProfit",
        "net_profit",
        "profit",
        "grossProfit",
        "profitInAccountCurrency",
        "profitInCurrency",
        "pnl",
        "pl",
    )
    profit_loss_percent = _first_float(
        data,
        "profitPercent",
        "profit_percent",
        "profitPercentage",
        "rateOfReturn",
        "returnPercent",
    )
    market_value = _first_float(
        data,
        "marketValue",
        "positionValue",
        "value",
        "nominalValue",
        "grossValue",
    )
    volume = _first_float(data, "volume", "size", "amount") or 0.0
    if market_value is None and current_price is not None and volume:
        market_value = current_price * volume

    return {
        "symbol": symbol,
        "name": _first_str(data, "name", "description", "displayName", "display_name", "instrumentName"),
        "display_name": _first_str(data, "displayName", "display_name", "name", "description", "instrumentName"),
        "description": _first_str(data, "description", "fullDescription", "full_description", "name"),
        "side": side,
        "volume": volume,
        "current_price": _rounded(current_price),
        "open_price": open_price,
        "price_change": _rounded(price_change),
        "price_change_percent": _rounded(price_change_percent),
        "daily_change": _rounded(
            _first_float(
                data,
                "dailyChange",
                "daily_change",
                "dayChange",
                "day_change",
                "dailyPriceChange",
                "dayPriceChange",
                "todayChange",
            )
        ),
        "daily_change_percent": _rounded(
            _first_float(
                data,
                "dailyChangePercent",
                "daily_change_percent",
                "dayChangePercent",
                "day_change_percent",
                "dailyPercentageChange",
                "dayPercentageChange",
                "dailyChangePct",
                "dayChangePct",
                "todayChangePercent",
                "todayChangePercentage",
            )
        ),
        "profit_loss": _rounded(profit_loss),
        "profit_loss_percent": _rounded(profit_loss_percent),
        "profit_net": _rounded(profit_loss),
        "profit_percent": _rounded(profit_loss_percent),
        "market_value": _rounded(market_value),
        "stop_loss": _first_float(data, "sl", "stopLoss", "stop_loss"),
        "take_profit": _first_float(data, "tp", "takeProfit", "take_profit"),
        "swap": _first_float(data, "swap"),
        "commission": _first_float(data, "commission"),
        "margin": _first_float(data, "margin"),
        "order_id": _first_str(data, "positionId", "orderId", "order_id", "id"),
        "instrument_id": _int(_lookup_value(data, "idQuote")) or _int(_lookup_value(data, "instrument_id")),
        "symbol_key": _first_str(data, "symbolKey", "symbol_key", "key"),
        "open_time": _lookup_value(data, "openTime") or _lookup_value(data, "open_time"),
        "account_number": account.get("account_number"),
        "currency": account.get("currency"),
        "raw_keys": sorted(data.keys()),
    }


def _normalize_order(raw: Any, account: dict[str, Any]) -> dict[str, Any]:
    data = _to_dict(raw)
    return {
        "symbol": str(data.get("symbol") or "").upper(),
        "name": _first_str(data, "name", "description", "displayName", "display_name", "instrumentName"),
        "display_name": _first_str(data, "displayName", "display_name", "name", "description", "instrumentName"),
        "description": _first_str(data, "description", "fullDescription", "full_description", "name"),
        "side": _normalize_side(_lookup_value(data, "side")),
        "volume": _first_float(data, "volume", "size", "amount") or 0.0,
        "price": _first_float(data, "openPrice", "open_price", "price") or 0.0,
        "stop_loss": _first_float(data, "sl", "stopLoss", "stop_loss"),
        "take_profit": _first_float(data, "tp", "takeProfit", "take_profit"),
        "order_id": _first_str(data, "positionId", "orderId", "order_id", "id"),
        "order_type": _first_str(data, "orderType", "order_type", "type"),
        "instrument_id": _int(_lookup_value(data, "idQuote")) or _int(_lookup_value(data, "instrument_id")),
        "symbol_key": _first_str(data, "symbolKey", "symbol_key", "key"),
        "expiration": _lookup_value(data, "expiration"),
        "open_time": _lookup_value(data, "openTime") or _lookup_value(data, "open_time"),
        "account_number": account.get("account_number"),
        "currency": account.get("currency"),
    }


def _normalize_quote(symbol: str, raw: Any) -> dict[str, Any]:
    if raw is None:
        return {"symbol": symbol.upper(), "available": False}

    data = _to_dict(raw)
    bid = _first_float(data, "bid")
    ask = _first_float(data, "ask")
    mid = ((bid + ask) / 2) if bid is not None and ask is not None else None
    spread = _first_float(data, "spread")
    if spread is None and bid is not None and ask is not None:
        spread = ask - bid

    previous = _first_float(
        data,
        "previousClose",
        "previous_close",
        "prevClose",
        "prev_close",
        "previousDayClose",
        "previousClosePrice",
        "lastClose",
        "lastClosePrice",
        "yesterdayClose",
        "yesterdayClosePrice",
        "prevDayClose",
        "referencePrice",
        "close",
        "open",
    )
    daily_change = _first_float(
        data,
        "dailyChange",
        "daily_change",
        "dayChange",
        "day_change",
        "dailyPriceChange",
        "dayPriceChange",
        "todayChange",
        "priceChange",
        "change",
    )
    if daily_change is None and mid is not None and previous:
        daily_change = mid - previous

    daily_change_percent = _first_float(
        data,
        "dailyChangePercent",
        "daily_change_percent",
        "dayChangePercent",
        "changePercent",
        "changePercentage",
        "percentageChange",
        "percentChange",
        "dailyPercentageChange",
        "dayPercentageChange",
        "percentageDailyChange",
        "dailyChangePct",
        "dayChangePct",
        "changePct",
        "pctChange",
        "dailyReturn",
        "dailyReturnPercent",
        "dayReturn",
        "dayReturnPercent",
        "todayChangePercent",
        "todayChangePercentage",
    )
    if daily_change_percent is None and daily_change is not None and previous:
        daily_change_percent = (daily_change / previous) * 100

    return {
        "symbol": str(data.get("symbol") or symbol).upper(),
        "name": _first_str(data, "name", "description", "displayName", "display_name", "instrumentName"),
        "display_name": _first_str(data, "displayName", "display_name", "name", "description", "instrumentName"),
        "description": _first_str(data, "description", "fullDescription", "full_description", "name"),
        "available": True,
        "bid": bid,
        "ask": ask,
        "mid": _rounded(mid),
        "spread": _rounded(spread),
        "spread_percent": _rounded((spread / mid) * 100 if spread is not None and mid else None),
        "high": _first_float(data, "high"),
        "low": _first_float(data, "low"),
        "previous": previous,
        "daily_change": _rounded(daily_change),
        "daily_change_percent": _rounded(daily_change_percent),
        "time": _lookup_value(data, "timestamp") or _lookup_value(data, "time"),
        "raw_keys": sorted(data.keys()),
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
    cash_balance = _float(account.get("cash_balance"))
    cash_funds = cash_balance if cash_balance is not None else _float(account.get("balance"))
    equity = _float(account.get("equity"))
    free_margin = _float(account.get("free_margin"))
    total_equity = _float(account.get("total_equity"))
    asset_value = _float(account.get("asset_value"))

    used_margin = (
        max(equity - free_margin, 0.0)
        if equity is not None and free_margin is not None
        else None
    )
    position_profit_net = sum(
        value
        for position in positions
        if (value := _float(position.get("profit_loss"))) is not None
    )
    profit_net = _float(account.get("profit_net"))
    if profit_net is None and positions:
        profit_net = position_profit_net
    total_equity_with_profit = (
        total_equity + profit_net
        if total_equity is not None and profit_net is not None
        else None
    )
    portfolio_value = _first_present(
        total_equity_with_profit,
        _float(account.get("account_value")),
        _float(account.get("portfolio_value")),
    )
    value_source = account.get("value_source")
    if total_equity_with_profit is not None:
        value_source = "total_equity_plus_net_profit"
    if portfolio_value is None:
        portfolio_value = equity if equity is not None else cash_funds
    profit_percent = _float(account.get("profit_percent"))
    if profit_percent is None and profit_net is not None and portfolio_value:
        cost_basis = portfolio_value - profit_net
        profit_percent = (profit_net / cost_basis) * 100 if cost_basis else None

    quote_values = [quote for quote in quotes.values() if quote.get("available")]
    daily_changes = [
        quote["daily_change_percent"]
        for quote in quote_values
        if quote.get("daily_change_percent") is not None
    ]

    return {
        "account_number": account.get("account_number"),
        "account_numbers": account.get("account_numbers") or [account.get("account_number")],
        "account_count": account.get("account_count") or 1,
        "currency": account.get("currency") or "",
        "side_bar_account_value": _rounded(_float(account.get("side_bar_account_value"))),
        "portfolio_value": _rounded(portfolio_value),
        "account_value": _rounded(portfolio_value),
        "balance": _rounded(portfolio_value),
        "cash_balance": _rounded(cash_funds),
        "total_equity": _rounded(total_equity),
        "equity": _rounded(equity),
        "free_margin": _rounded(free_margin),
        "asset_value": _rounded(asset_value),
        "used_margin": _rounded(used_margin),
        "margin_level_percent": _rounded((equity / used_margin) * 100 if used_margin else None),
        "profit_net": _rounded(profit_net),
        "profit_percent": _rounded(profit_percent),
        "position_profit_net": _rounded(position_profit_net),
        "open_positions": len(positions),
        "pending_orders": len(orders),
        "quotes_available": len(quote_values),
        "average_daily_change_percent": _rounded(
            sum(daily_changes) / len(daily_changes) if daily_changes else None
        ),
        "value_source": value_source,
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


def _lookup_value(data: dict[str, Any], key: str) -> Any:
    if key in data:
        return data[key]

    normalized = _normalize_key(key)
    for existing_key, value in data.items():
        if _normalize_key(str(existing_key)) == normalized:
            return value
    return None


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip().replace("\xa0", " ")
        text = re.sub(r"[^0-9,.\-]", "", text)
        if not text or text in {"-", ".", ","}:
            return None
        if "," in text and "." in text:
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text:
            text = text.replace(",", ".")
        value = text
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
        value = _float(_lookup_value(data, key))
        if value is not None:
            return value
    return None


def _first_str(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _lookup_value(data, key)
        if value is not None and value != "":
            return str(value)
    return None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _normalize_side(value: Any) -> str | None:
    if isinstance(value, str) and not value.isdigit():
        lowered = value.lower()
        if "buy" in lowered or "kup" in lowered:
            return "buy"
        if "sell" in lowered or "sprzed" in lowered:
            return "sell"
        return value

    side = _int(value)
    if side == 0:
        return "buy"
    if side == 1:
        return "sell"
    return None


def _largest_balance_value(data: dict[str, Any]) -> tuple[float | None, str | None]:
    ignored = (
        "time",
        "timestamp",
        "level",
        "percent",
        "percentage",
        "rate",
        "id",
        "account",
        "number",
        "leverage",
    )
    best_value: float | None = None
    best_key: str | None = None
    for key, raw_value in data.items():
        normalized_key = _normalize_key(str(key))
        if any(part in normalized_key for part in ignored):
            continue
        value = _float(raw_value)
        if value is None or value <= 0 or value > 1_000_000_000_000:
            continue
        if best_value is None or value > best_value:
            best_value = value
            best_key = str(key)
    return best_value, best_key


def _numeric_debug_fields(data: dict[str, Any]) -> dict[str, float]:
    fields: dict[str, float] = {}
    for key, raw_value in data.items():
        value = _float(raw_value)
        if value is not None:
            fields[str(key)] = _rounded(value) or 0.0
    return fields


def _sum_values(items: list[dict[str, Any]], key: str) -> float | None:
    values = [_float(item.get(key)) for item in items]
    numbers = [value for value in values if value is not None]
    return _rounded(sum(numbers)) if numbers else None


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


def _client_key(email: str, password: str, account_number: int | None) -> str:
    return _cache_key(email, password, str(account_number or "auto"))


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
