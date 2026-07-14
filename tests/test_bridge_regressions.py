from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest


def _install_bridge_dependency_stubs() -> None:
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.web = types.SimpleNamespace()
    sys.modules.setdefault("aiohttp", aiohttp)

    xtb_api = types.ModuleType("xtb_api")

    class XTBClient:
        pass

    xtb_api.XTBClient = XTBClient
    sys.modules.setdefault("xtb_api", xtb_api)

    browser_auth = types.ModuleType("xtb_api.auth.browser_auth")
    browser_auth.BrowserCASAuth = type("BrowserCASAuth", (), {})
    browser_auth.LOGIN_URL = "https://example.invalid"
    browser_auth.OTP_WAIT_TIMEOUT = 1
    browser_auth._STEALTH_JS = ""
    sys.modules.setdefault("xtb_api.auth.browser_auth", browser_auth)

    cas_client = types.ModuleType("xtb_api.auth.cas_client")
    cas_client.CASClient = type("CASClient", (), {})
    cas_client.CASClientConfig = type("CASClientConfig", (), {})
    sys.modules.setdefault("xtb_api.auth.cas_client", cas_client)

    exceptions = types.ModuleType("xtb_api.exceptions")

    class CASError(Exception):
        def __init__(self, code="", message=""):
            super().__init__(message or code)
            self.code = code

    exceptions.CASError = CASError
    sys.modules.setdefault("xtb_api.exceptions", exceptions)

    enums = types.ModuleType("xtb_api.types.enums")
    enums.SubscriptionEid = type(
        "SubscriptionEid",
        (),
        {"TOTAL_BALANCE": 1, "POSITIONS": 2, "ORDERS": 3},
    )
    sys.modules.setdefault("xtb_api.types.enums", enums)

    websocket = types.ModuleType("xtb_api.types.websocket")
    websocket.CASLoginSuccess = type("CASLoginSuccess", (), {})
    websocket.CASLoginTwoFactorRequired = type("CASLoginTwoFactorRequired", (), {})
    sys.modules.setdefault("xtb_api.types.websocket", websocket)


def _load_bridge():
    _install_bridge_dependency_stubs()
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("xtb_bridge_test_module", root / "addons/xtb_bridge/bridge.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge()


class BridgeRegressionTests(unittest.TestCase):
    def test_cfd_endpoint_type_is_treated_as_real_account(self) -> None:
        self.assertTrue(bridge._is_real_account({"endpoint_type": "CFD"}))
        self.assertTrue(bridge._is_real_account({"endpoint_type": "CFD", "server_code": "XS-real1"}))
        self.assertTrue(bridge._is_real_account({"endpoint_type": "REAL"}))
        self.assertFalse(bridge._is_real_account({"endpoint_type": "DEMO"}))
        self.assertFalse(bridge._is_real_account({"endpoint_type": "CFD", "server_code": "XS-demo1"}))

    def test_total_equity_is_account_value_not_profit_plus_equity(self) -> None:
        account = bridge._normalize_account(
            {
                "balance": 19905.67,
                "cashStockValue": 376268.41,
                "totalEquity": 396174.08,
                "netProfit": 131364.04,
            },
            {"account_number": 53600874, "currency": "PLN"},
        )

        self.assertEqual(account["asset_value"], 376268.41)
        self.assertEqual(account["account_value"], 396174.08)
        self.assertEqual(account["portfolio_value"], 396174.08)
        self.assertEqual(account["value_source"], "total_equity")

        summary = bridge._build_summary(account, [], [], {})

        self.assertEqual(summary["account_value"], 396174.08)
        self.assertEqual(summary["balance"], 396174.08)
        self.assertEqual(summary["value_source"], "total_equity")

    def test_retirement_account_value_is_added_to_main_account_value(self) -> None:
        account = bridge._normalize_account(
            {
                "balance": 2953.41,
                "cashStockValue": 7428.93,
                "totalEquity": 10382.34,
                "netProfit": 1674.61,
            },
            {"account_number": 53578037, "currency": "PLN"},
        )
        retirement = {
            "account_value": 1654.70,
            "cash_balance": 816.29,
            "asset_value": 838.41,
            "accounts": [{"type_name": "IKZE", "account_value": 1654.70}],
        }

        self.assertTrue(bridge._merge_retirement_account_data(account, retirement))

        self.assertEqual(account["main_account_value"], 10382.34)
        self.assertEqual(account["retirement_account_value"], 1654.70)
        self.assertEqual(account["account_value"], 12037.04)
        self.assertEqual(account["portfolio_value"], 12037.04)
        self.assertEqual(account["side_bar_account_value"], 12037.04)
        self.assertEqual(account["cash_balance"], 3769.70)
        self.assertEqual(account["asset_value"], 8267.34)
        self.assertEqual(account["profit_net"], 1674.61)
        self.assertEqual(account["value_source"], "main_account_plus_retirement")

        summary = bridge._build_summary(account, [], [], {})

        self.assertEqual(summary["account_value"], 12037.04)
        self.assertEqual(summary["main_account_value"], 10382.34)
        self.assertEqual(summary["retirement_account_value"], 1654.70)
        self.assertEqual(summary["profit_net"], 1674.61)

    def test_ikze_withdrawal_response_is_parsed_as_money_values(self) -> None:
        response = bytes.fromhex("0a1408ddfd0410818f0518de8c0a2203504c4e288827")

        balance = bridge._parse_retirement_balance_response(response)

        self.assertEqual(balance["cash_balance"], 816.29)
        self.assertEqual(balance["asset_value"], 838.41)
        self.assertEqual(balance["account_value"], 1654.70)
        self.assertEqual(balance["currency"], "PLN")

    def test_positions_are_summed_per_account_and_symbol(self) -> None:
        positions = [
            {
                "account_number": 1,
                "symbol": "SNDK.US",
                "volume": 1.0,
                "market_value": 100.0,
                "profit_loss": 10.0,
                "profit_net": 10.0,
                "order_id": "a",
                "raw_keys": ["first"],
            },
            {
                "account_number": 1,
                "symbol": "SNDK.US",
                "volume": 2.0,
                "market_value": 200.0,
                "profit_loss": 20.0,
                "profit_net": 20.0,
                "order_id": "b",
                "raw_keys": ["second"],
            },
            {
                "account_number": 2,
                "symbol": "SNDK.US",
                "volume": 4.0,
                "market_value": 400.0,
                "profit_loss": 40.0,
                "profit_net": 40.0,
                "order_id": "c",
            },
        ]

        aggregated = bridge._aggregate_positions(positions)

        self.assertEqual(len(aggregated), 2)
        account_one = next(position for position in aggregated if position["account_number"] == 1)
        account_two = next(position for position in aggregated if position["account_number"] == 2)

        self.assertEqual(account_one["volume"], 3.0)
        self.assertEqual(account_one["market_value"], 300.0)
        self.assertEqual(account_one["profit_loss"], 30.0)
        self.assertEqual(account_one["profit_net"], 30.0)
        self.assertTrue(account_one["aggregated"])
        self.assertFalse(account_one["deduplicated"])
        self.assertEqual(account_one["position_count"], 2)
        self.assertEqual(account_one["duplicate_count"], 2)
        self.assertEqual(account_one["order_ids"], ["a", "b"])
        self.assertEqual(account_one["raw_keys"], ["first", "second"])

        self.assertFalse(account_two["aggregated"])
        self.assertEqual(account_two["profit_loss"], 40.0)


class ConnectedClientFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_client_is_used_when_session_file_needs_refresh(self) -> None:
        class RawClient:
            is_connected = True
            is_authenticated = True

        class Managed:
            email = "user@example.com"
            password = "secret"
            account_number = 123
            client = RawClient()

            async def get_snapshot(self):
                return {
                    "account": {"account_number": 123, "currency": "PLN", "account_value": 10},
                    "summary": {"account_number": 123, "currency": "PLN", "account_value": 10},
                    "positions": [],
                    "orders": [],
                    "quotes": {},
                }

        previous_clients = dict(bridge.CLIENTS)
        try:
            bridge.CLIENTS.clear()
            bridge.CLIENTS["fake"] = Managed()

            snapshot = await bridge._snapshot_from_connected_clients_if_refresh_risky(
                "user@example.com",
                "secret",
                123,
            )
        finally:
            bridge.CLIENTS.clear()
            bridge.CLIENTS.update(previous_clients)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["account"]["account_number"], 123)
        self.assertEqual(snapshot["summary"]["account_value"], 10)


class SessionRefreshSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_session_check_does_not_start_login_when_tgt_is_expired(self) -> None:
        calls = 0

        async def fail_if_called(*args, **kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("background refresh must not start a password login")

        original_fresh = bridge._session_file_is_fresh
        original_login = bridge._login_with_fallback
        try:
            bridge._session_file_is_fresh = lambda path: False
            bridge._login_with_fallback = fail_if_called

            with self.assertRaises(bridge.CASError) as raised:
                await bridge._ensure_session_ready("user@example.com", "secret")
        finally:
            bridge._session_file_is_fresh = original_fresh
            bridge._login_with_fallback = original_login

        self.assertEqual(calls, 0)
        self.assertEqual(raised.exception.code, "AUTH_MANAGER_TGT_EXPIRED")

    def test_automatic_login_sources_do_not_bypass_otp_retry_block(self) -> None:
        self.assertFalse(bridge._login_source_allows_new_otp("reauth"))
        self.assertFalse(bridge._login_source_allows_new_otp("snapshot"))
        self.assertTrue(bridge._login_source_allows_new_otp("reauth_manual"))
        self.assertTrue(bridge._login_source_allows_new_otp("otp_retry"))

    def test_active_client_reconnects_once_before_tgt_enters_refresh_margin(self) -> None:
        now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
        expires_at = (now + timedelta(minutes=10)).isoformat()

        with tempfile.TemporaryDirectory() as tmpdir:
            session_file = Path(tmpdir) / "session.json"
            session_file.write_text(
                json.dumps({"tgt": "TGT-test", "expires_at": expires_at}),
                encoding="utf-8",
            )

            original_time = bridge.time.time
            try:
                bridge.time.time = lambda: now.timestamp()
                self.assertTrue(
                    bridge._should_reconnect_client_before_tgt_expiry(session_file, None)
                )
                self.assertFalse(
                    bridge._should_reconnect_client_before_tgt_expiry(session_file, expires_at)
                )

                bridge.time.time = lambda: (now + timedelta(minutes=6)).timestamp()
                self.assertFalse(
                    bridge._should_reconnect_client_before_tgt_expiry(session_file, None)
                )
            finally:
                bridge.time.time = original_time

    async def test_managed_client_disables_library_auto_reconnect(self) -> None:
        created: list[dict] = []

        class FakeXTBClient:
            is_connected = False
            is_authenticated = False

            def __init__(self, **kwargs):
                created.append(kwargs)

            async def connect(self):
                self.is_connected = True
                self.is_authenticated = True

            async def disconnect(self):
                self.is_connected = False
                self.is_authenticated = False

        async def noop_session_ready(*args, **kwargs):
            return None

        async def fake_snapshot(client):
            return {"session": {"connected": client.is_connected}}

        original_client = bridge.XTBClient
        original_ready = bridge._ensure_session_ready
        original_snapshot = bridge._snapshot
        try:
            bridge.XTBClient = FakeXTBClient
            bridge._ensure_session_ready = noop_session_ready
            bridge._snapshot = fake_snapshot

            client = bridge.ManagedClient(
                email="user@example.com",
                password="secret",
                account_number=123,
            )
            snapshot = await client.get_snapshot()
        finally:
            bridge.XTBClient = original_client
            bridge._ensure_session_ready = original_ready
            bridge._snapshot = original_snapshot

        self.assertEqual(snapshot["session"]["connected"], True)
        self.assertEqual(created[0]["account_number"], 123)
        self.assertFalse(created[0]["auto_reconnect"])

    async def test_closed_websocket_after_expired_tgt_requires_reauth(self) -> None:
        class RawClient:
            is_connected = True
            is_authenticated = True

            async def disconnect(self):
                self.is_connected = False
                self.is_authenticated = False

        async def closed_snapshot(_client):
            raise RuntimeError("keepalive ping timeout; no close frame received")

        original_snapshot = bridge._snapshot
        original_fresh = bridge._session_file_is_fresh
        try:
            bridge._snapshot = closed_snapshot
            bridge._session_file_is_fresh = lambda path: False
            client = bridge.ManagedClient(
                email="user@example.com",
                password="secret",
                account_number=123,
            )
            client.client = RawClient()

            with self.assertRaises(bridge.CASError) as raised:
                await client.get_snapshot()
        finally:
            bridge._snapshot = original_snapshot
            bridge._session_file_is_fresh = original_fresh

        self.assertEqual(raised.exception.code, "AUTH_MANAGER_TGT_EXPIRED")
        self.assertIsNone(client.client)

    def test_browser_profiles_are_scoped_by_account_when_available(self) -> None:
        email = "user@example.com"
        password = "secret"

        self.assertNotEqual(
            bridge._browser_profile_dir(email, 1),
            bridge._browser_profile_dir(email, 2),
        )
        self.assertNotEqual(
            bridge._cookies_file(email, password, 1),
            bridge._cookies_file(email, password, 2),
        )
        self.assertNotEqual(
            bridge._login_key(email, password, 1),
            bridge._login_key(email, password, 2),
        )


if __name__ == "__main__":
    unittest.main()
