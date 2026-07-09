from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
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
    exceptions.CASError = type("CASError", (Exception,), {})
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


if __name__ == "__main__":
    unittest.main()
