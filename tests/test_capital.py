from __future__ import annotations

from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from crypto_trader.capital import (
    analyze_configuration_change,
    calculate_capital_reserve_state,
    calculate_position_size,
    calculate_realized_capital,
    calculate_trade_intent_position_size,
    check_capital_allocation,
)
from crypto_trader.config import DEFAULT_CONFIG


class CapitalTest(TestCase):
    def _config(self) -> dict:
        config = deepcopy(DEFAULT_CONFIG)
        config["_atlas_test_mode"] = True
        config["exchange"]["leverage"] = 10
        config["capital_reserve"].update(
            {
                "base_reserve_percent": 20,
                "warning_reserve_percent": 25,
                "recovery_reserve_percent": 30,
                "critical_reserve_percent": 40,
                "min_trading_capital": 10,
            }
        )
        config["position_sizing"].update(
            {
                "normal_risk_percent": 2,
                "warning_risk_percent": 1,
                "recovery_risk_percent": 0.5,
                "critical_risk_percent": 0,
                "default_stop_loss_percent": 5,
                "default_take_profit_percent": 8,
                "max_order_size_percent_of_trading_capital": 50,
                "min_order_size": 1,
                "max_leverage": 25,
            }
        )
        return config

    def test_realized_capital_excludes_unrealized_pnl(self) -> None:
        self.assertEqual(calculate_realized_capital(125.5, 7.25), 118.25)
        self.assertEqual(calculate_realized_capital(125.5, -4.5), 130.0)

    def test_reserve_state_uses_mode_specific_reserve_and_used_capital(self) -> None:
        state = calculate_capital_reserve_state(
            self._config(),
            mode="RECOVERY",
            used_trading_capital=15,
            snapshot={"ok": True, "realized_capital": 100},
        )

        self.assertTrue(state["ok"])
        self.assertEqual(state["mode"], "RECOVERY")
        self.assertEqual(state["reserve_percent"], 30)
        self.assertEqual(state["reserve_amount"], 30)
        self.assertEqual(state["trading_capital"], 70)
        self.assertEqual(state["available_trading_capital"], 55)

    def test_allocation_blocks_margin_above_available_trading_capital(self) -> None:
        state = {
            "ok": True,
            "mode": "HEALTHY",
            "trading_capital": 80,
            "available_trading_capital": 12,
            "reserve_amount": 20,
            "realized_capital": 100,
        }

        with patch("crypto_trader.capital.latest_capital_reserve_state", return_value=state):
            result = check_capital_allocation(self._config(), 15)

        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "Insufficient trading capital after reserve protection")

    def test_position_size_caps_order_by_risk_before_capital(self) -> None:
        state = {
            "ok": True,
            "mode": "HEALTHY",
            "realized_capital": 125,
            "reserve_amount": 25,
            "trading_capital": 100,
            "used_trading_capital": 0,
            "available_trading_capital": 50,
        }

        with patch("crypto_trader.capital.latest_capital_reserve_state", return_value=state):
            result = calculate_position_size(
                self._config(),
                {"symbol": "BTC/USDT:USDT", "side": "LONG", "mode": "HEALTHY", "leverage": 10},
            )

        self.assertTrue(result["allowed"])
        self.assertEqual(result["risk_amount"], 2)
        self.assertEqual(result["max_order_size_by_risk"], 40)
        self.assertEqual(result["max_order_size_by_capital"], 50)
        self.assertEqual(result["suggested_order_size"], 40)
        self.assertEqual(result["required_margin"], 4)

    def test_quality_margin_targets_scale_with_account_and_grade(self) -> None:
        config = self._config()
        config["exchange"]["leverage"] = 25
        config["position_sizing"].update(
            {
                "quality_margin_enabled": True,
                "quality_margin_percent_by_grade": {"S": 10, "A": 7, "B": 5, "C": 2, "D": 0},
                "quality_margin_mode_multiplier": {"HEALTHY": 1, "WARNING": 0.75, "RECOVERY": 0.5, "CRITICAL": 0},
                "quality_margin_max_loss_percent_by_mode": {"HEALTHY": 5, "WARNING": 3.75, "RECOVERY": 2.5, "CRITICAL": 0},
                "max_leverage": 25,
            }
        )
        state = {
            "ok": True,
            "mode": "HEALTHY",
            "realized_capital": 20,
            "reserve_amount": 0,
            "trading_capital": 20,
            "used_trading_capital": 0,
            "available_trading_capital": 20,
        }

        with patch("crypto_trader.capital.latest_capital_reserve_state", return_value=state):
            result = calculate_position_size(
                config,
                {
                    "symbol": "CAP/USDT:USDT",
                    "side": "LONG",
                    "mode": "HEALTHY",
                    "leverage": 25,
                    "stop_loss_percent": 1.2,
                    "take_profit_percent": 3.6,
                    "setup_grade": "S",
                },
            )

        self.assertTrue(result["allowed"])
        self.assertEqual(result["quality_margin_target_margin"], 2)
        self.assertEqual(result["suggested_order_size"], 50)
        self.assertEqual(result["required_margin"], 2)
        self.assertEqual(result["estimated_loss"], 0.6)
        self.assertEqual(result["effective_risk_amount"], 0.6)
        self.assertEqual(result["effective_risk_source"], "quality_margin_loss_cap")

    def test_quality_margin_recovery_uses_lower_multiplier_and_loss_cap(self) -> None:
        config = self._config()
        config["exchange"]["leverage"] = 25
        config["position_sizing"].update(
            {
                "quality_margin_enabled": True,
                "quality_margin_percent_by_grade": {"S": 10, "A": 7, "B": 5, "C": 2, "D": 0},
                "quality_margin_mode_multiplier": {"HEALTHY": 1, "WARNING": 0.75, "RECOVERY": 0.5, "CRITICAL": 0},
                "quality_margin_max_loss_percent_by_mode": {"HEALTHY": 5, "WARNING": 3.75, "RECOVERY": 2.5, "CRITICAL": 0},
                "max_leverage": 25,
            }
        )
        state = {
            "ok": True,
            "mode": "RECOVERY",
            "realized_capital": 20,
            "reserve_amount": 0,
            "trading_capital": 20,
            "used_trading_capital": 0,
            "available_trading_capital": 20,
        }

        with patch("crypto_trader.capital.latest_capital_reserve_state", return_value=state):
            result = calculate_position_size(
                config,
                {
                    "symbol": "CAP/USDT:USDT",
                    "side": "LONG",
                    "mode": "RECOVERY",
                    "leverage": 25,
                    "stop_loss_percent": 2.0,
                    "take_profit_percent": 5.0,
                    "setup_grade": "S",
                },
            )

        self.assertTrue(result["allowed"])
        self.assertEqual(result["quality_margin_target_margin"], 1)
        self.assertEqual(result["quality_margin_max_loss_amount"], 0.5)
        self.assertEqual(result["suggested_order_size"], 25)
        self.assertEqual(result["required_margin"], 1)
        self.assertEqual(result["estimated_loss"], 0.5)
        self.assertEqual(result["effective_risk_amount"], 0.5)
        self.assertEqual(result["effective_risk_source"], "quality_margin_loss_cap")

    def test_trade_intent_position_size_passes_setup_grade_to_quality_margin(self) -> None:
        config = self._config()
        config["exchange"]["leverage"] = 25
        config["position_sizing"].update(
            {
                "quality_margin_enabled": True,
                "quality_margin_percent_by_grade": {"S": 10, "A": 7, "B": 5, "C": 2, "D": 0},
                "quality_margin_mode_multiplier": {"HEALTHY": 1, "WARNING": 0.75, "RECOVERY": 0.5, "CRITICAL": 0},
                "quality_margin_max_loss_percent_by_mode": {"HEALTHY": 5, "WARNING": 3.75, "RECOVERY": 2.5, "CRITICAL": 0},
                "max_leverage": 25,
            }
        )
        state = {
            "ok": True,
            "mode": "HEALTHY",
            "realized_capital": 20,
            "reserve_amount": 0,
            "trading_capital": 20,
            "used_trading_capital": 0,
            "available_trading_capital": 20,
        }

        with patch("crypto_trader.capital.latest_capital_reserve_state", return_value=state):
            result = calculate_trade_intent_position_size(
                config,
                {
                    "symbol": "CAP/USDT:USDT",
                    "side": "long",
                    "entry_price": 100,
                    "stop_loss": 98.8,
                    "take_profit": 103.6,
                    "risk_profile": "Normal",
                },
                {
                    "symbol": "CAP/USDT:USDT",
                    "side": "long",
                    "entry_price": 100,
                    "stop_loss": 98.8,
                    "take_profit": 103.6,
                    "risk_pct": 1.2,
                },
                {"decision": "APPROVE", "setup_grade": "S"},
            )

        self.assertTrue(result["allowed"])
        self.assertEqual(result["setup_grade"], "S")
        self.assertEqual(result["quality_margin_setup_grade"], "S")
        self.assertEqual(result["margin_usdt"], 2)
        self.assertEqual(result["notional_usdt"], 50)

    def test_configuration_impact_flags_unsafe_growth(self) -> None:
        with patch(
            "crypto_trader.capital.latest_capital_snapshot",
            return_value={"ok": True, "realized_capital": 100},
        ):
            report = analyze_configuration_change(
                self._config(),
                {"initial_order_size": 100, "max_concurrent_positions": 5, "reserve_percent": 20},
            )

        self.assertFalse(report["is_safe"])
        self.assertIn(report["risk_level"], {"HIGH", "CRITICAL"})
        self.assertTrue(report["warnings"])
