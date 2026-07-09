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
    def test_cfd_endpoint_type_is_treated_as_real_account(self) -> None:
        self.assertTrue(bridge._is_real_account({"endpoint_type": "CFD"}))
        self.assertTrue(bridge._is_real_account({"endpoint_type": "REAL"}))
        self.assertFalse(bridge._is_real_account({"endpoint_type": "DEMO"}))

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


if __name__ == "__main__":
    unittest.main()
