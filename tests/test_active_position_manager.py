from __future__ import annotations

import json
import tempfile
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from crypto_trader.active_position_manager import evaluate_open_positions
from crypto_trader.config import DEFAULT_CONFIG
from crypto_trader.storage import insert_trade_execution_row, list_trade_execution_rows


class ActivePositionFakeExchange:
    def __init__(self) -> None:
        self.orders: list[dict] = []

    def load_markets(self) -> None:
        return None

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return f"{amount:.8f}".rstrip("0").rstrip(".")

    def create_order(self, symbol: str, order_type: str, side: str, amount: str, price: float | None, params: dict) -> dict:
        self.orders.append(
            {
                "symbol": symbol,
                "type": order_type,
                "side": side,
                "amount": amount,
                "price": price,
                "params": dict(params),
            }
        )
        return {"id": f"order-{len(self.orders)}", "params": dict(params)}


class ActivePositionManagerTest(TestCase):
    def _config(self) -> dict:
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        config = deepcopy(DEFAULT_CONFIG)
        config["_config_dir"] = self.tmpdir.name
        config["_atlas_test_mode"] = True
        config["mode"] = "live"
        config["exchange"]["position_side_mode"] = "long_short"
        config["active_position_manager"] = {
            "enabled": True,
            "shadow_mode": False,
            "auto_execute_enabled": True,
            "apply_to_existing_positions": True,
            "execute_bad_cut": True,
            "bad_cut_r": -0.9,
            "partial_cut_fraction": 0.25,
            "notify_telegram": False,
        }
        return config

    def tearDown(self) -> None:
        tmpdir = getattr(self, "tmpdir", None)
        if tmpdir:
            tmpdir.cleanup()

    def test_reduce_only_close_uses_position_margin_mode(self) -> None:
        config = self._config()
        exchange = ActivePositionFakeExchange()
        row = insert_trade_execution_row(
            config,
            {
                "created_at": "2026-08-09T13:19:31+00:00",
                "updated_at": "2026-08-09T13:19:31+00:00",
                "symbol": "BOME/USDT:USDT",
                "side": "SHORT",
                "status": "OPEN",
                "entry_price": 0.0007223,
                "stop_loss": 0.000744,
                "take_profit": 0.0006898,
                "quantity": 66.0,
                "snapshot_json": json.dumps(
                    {
                        "position": {
                            "symbol": "BOME/USDT:USDT",
                            "side": "short",
                            "contracts": 66.0,
                            "contractSize": 1000.0,
                            "info": {
                                "posSide": "short",
                                "mgnMode": "cross",
                                "avgPx": "0.0007223",
                                "markPx": "0.00079",
                                "pos": "66",
                            },
                        }
                    }
                ),
            },
        )

        with patch("crypto_trader.active_position_manager.create_exchange", return_value=exchange):
            result = evaluate_open_positions(config, rows=[row], notify=False)

        self.assertEqual(result["items"][0]["action"], "BAD_CUT")
        self.assertTrue(result["items"][0]["execution"]["submitted"])
        self.assertEqual(exchange.orders[0]["params"]["tdMode"], "cross")
        self.assertEqual(exchange.orders[0]["params"]["posSide"], "short")

    def test_bad_cut_executes_only_once_per_trade_execution(self) -> None:
        config = self._config()
        exchange = ActivePositionFakeExchange()
        insert_trade_execution_row(
            config,
            {
                "created_at": "2026-08-20T07:00:00+00:00",
                "updated_at": "2026-08-20T07:00:00+00:00",
                "symbol": "BEAT/USDT:USDT",
                "side": "LONG",
                "status": "OPEN",
                "entry_price": 0.1474,
                "initial_entry_price": 0.1474,
                "initial_stop_loss": 0.14,
                "stop_loss": 0.14,
                "take_profit": 0.1739,
                "quantity": 7.8,
                "snapshot_json": json.dumps(
                    {
                        "position": {
                            "symbol": "BEAT/USDT:USDT",
                            "side": "long",
                            "contracts": 7.8,
                            "contractSize": 10.0,
                            "info": {
                                "posSide": "long",
                                "mgnMode": "cross",
                                "avgPx": "0.1474",
                                "markPx": "0.1346",
                                "pos": "7.8",
                            },
                        }
                    }
                ),
            },
        )

        with patch("crypto_trader.active_position_manager.create_exchange", return_value=exchange):
            first = evaluate_open_positions(config, notify=False)
            second = evaluate_open_positions(config, notify=False)

        self.assertEqual(first["items"][0]["action"], "BAD_CUT")
        self.assertTrue(first["items"][0]["execution"]["submitted"])
        self.assertEqual(second["items"][0]["action"], "HOLD_AFTER_BAD_CUT")
        self.assertFalse(second["items"][0]["execution"]["submitted"])
        self.assertEqual(second["items"][0]["execution"]["reason"], "execution_disabled_for_action")
        self.assertEqual(len(exchange.orders), 1)
        row = list_trade_execution_rows(config, statuses=["OPEN"])[0]
        self.assertTrue(row["bad_cut_done"])
        self.assertAlmostEqual(row["bad_cut_amount"], 1.95)
        self.assertAlmostEqual(row["bad_cut_trigger_r"], -0.9)
        self.assertAlmostEqual(row["bad_cut_price"], 0.1346)

    def test_after_partial_profit_tracks_unless_reversal_is_severe(self) -> None:
        config = self._config()
        config["active_position_manager"]["execute_good_exit"] = True
        exchange = ActivePositionFakeExchange()
        row = {
            "id": 77,
            "created_at": "2026-08-21T12:18:40+00:00",
            "updated_at": "2026-08-21T16:22:45+00:00",
            "symbol": "LAB/USDT:USDT",
            "side": "LONG",
            "status": "OPEN",
            "entry_price": 0.08175,
            "initial_entry_price": 0.08175,
            "initial_stop_loss": 0.07124,
            "stop_loss": 0.08393,
            "take_profit": 0.09576,
            "quantity": 55.7,
            "partial_take_profit_done": True,
            "snapshot_json": json.dumps(
                {
                    "position": {
                        "symbol": "LAB/USDT:USDT",
                        "side": "long",
                        "contracts": 55.7,
                        "contractSize": 10.0,
                        "info": {
                            "posSide": "long",
                            "mgnMode": "cross",
                            "avgPx": "0.08175",
                            "markPx": "0.09207",
                            "pos": "55.7",
                        },
                    }
                }
            ),
        }

        with patch("crypto_trader.active_position_manager.create_exchange", return_value=exchange):
            result = evaluate_open_positions(config, rows=[row], notify=False)

        item = result["items"][0]
        self.assertEqual(item["action"], "HOLD_AFTER_PARTIAL")
        self.assertFalse(item["execution"]["submitted"])
        self.assertEqual(exchange.orders, [])

    def test_after_partial_profit_good_exit_requires_severe_reversal(self) -> None:
        config = self._config()
        config["active_position_manager"]["execute_good_exit"] = True
        exchange = ActivePositionFakeExchange()
        row = {
            "id": 78,
            "created_at": "2026-08-21T12:18:40+00:00",
            "updated_at": "2026-08-21T16:22:45+00:00",
            "symbol": "LAB/USDT:USDT",
            "side": "LONG",
            "status": "OPEN",
            "entry_price": 0.08175,
            "initial_entry_price": 0.08175,
            "initial_stop_loss": 0.07124,
            "stop_loss": 0.08393,
            "take_profit": 0.09576,
            "quantity": 55.7,
            "partial_take_profit_done": True,
            "snapshot_json": json.dumps(
                {
                    "position": {
                        "symbol": "LAB/USDT:USDT",
                        "side": "long",
                        "contracts": 55.7,
                        "contractSize": 10.0,
                        "info": {
                            "posSide": "long",
                            "mgnMode": "cross",
                            "avgPx": "0.08175",
                            "markPx": "0.0805",
                            "pos": "55.7",
                        },
                    }
                }
            ),
        }

        with patch("crypto_trader.active_position_manager.create_exchange", return_value=exchange):
            result = evaluate_open_positions(config, rows=[row], notify=False)

        item = result["items"][0]
        self.assertEqual(item["action"], "GOOD_EXIT_REVIEW")
        self.assertTrue(item["execution"]["submitted"])
        self.assertEqual(exchange.orders[0]["side"], "sell")

    def test_profit_reversal_guard_closes_fraction_after_peak_drop(self) -> None:
        config = self._config()
        config["active_position_manager"].update(
            {
                "profit_reversal_guard_enabled": True,
                "execute_profit_reversal_guard": True,
                "profit_reversal_arm_r": 0.5,
                "profit_reversal_arm_tp_progress_pct": 35.0,
                "profit_reversal_drop_r": 0.35,
                "profit_reversal_drop_progress_pct": 25.0,
                "profit_reversal_close_fraction": 0.3,
            }
        )
        exchange = ActivePositionFakeExchange()
        row = {
            "id": 80,
            "created_at": "2026-08-22T04:29:58+00:00",
            "updated_at": "2026-08-22T05:10:01+00:00",
            "symbol": "BICO/USDT:USDT",
            "side": "LONG",
            "status": "OPEN",
            "entry_price": 0.02047,
            "initial_entry_price": 0.02047,
            "initial_stop_loss": 0.0198559,
            "stop_loss": 0.01855,
            "take_profit": 0.02277,
            "quantity": 1252.0,
            "trade_event_history_json": json.dumps(
                [
                    {
                        "type": "active_position_review",
                        "created_at": "2026-08-22T05:03:03+00:00",
                        "action": "SCALE_IN_REVIEW",
                        "r_multiple": 1.56,
                        "tp_progress_pct": 89.72,
                    }
                ]
            ),
            "snapshot_json": json.dumps(
                {
                    "position": {
                        "symbol": "BICO/USDT:USDT",
                        "side": "long",
                        "contracts": 1252.0,
                        "contractSize": 1.0,
                        "info": {
                            "posSide": "long",
                            "mgnMode": "cross",
                            "avgPx": "0.02047",
                            "markPx": "0.02116",
                            "pos": "1252",
                        },
                    }
                }
            ),
        }

        with patch("crypto_trader.active_position_manager.create_exchange", return_value=exchange):
            result = evaluate_open_positions(config, rows=[row], notify=False)

        item = result["items"][0]
        self.assertEqual(item["action"], "PROFIT_REVERSAL_GUARD")
        self.assertAlmostEqual(item["amount"], 375.6)
        self.assertTrue(item["execution"]["submitted"])
        self.assertEqual(exchange.orders[0]["side"], "sell")
        self.assertEqual(exchange.orders[0]["amount"], "375.6")

    def test_protected_current_position_does_not_execute_close(self) -> None:
        config = self._config()
        config["active_position_manager"]["execute_remainder_cut"] = True
        config["active_position_manager"]["protected_positions"] = [
            {
                "enabled": True,
                "symbol": "XRP/USDT:USDT",
                "side": "LONG",
                "trade_execution_id": 6,
                "exchange_position_id": "3765765730300190720",
            }
        ]
        exchange = ActivePositionFakeExchange()
        row = {
            "id": 6,
            "created_at": "2026-07-22T14:41:09+00:00",
            "updated_at": "2026-08-10T17:10:00+00:00",
            "symbol": "XRP/USDT:USDT",
            "side": "LONG",
            "status": "OPEN",
            "entry_price": 1.1183072164948453,
            "stop_loss": 1.0,
            "take_profit": 1.28,
            "quantity": 0.97,
            "loss_guard_partial_done": True,
            "exchange_position_id": "3765765730300190720",
            "snapshot_json": json.dumps(
                {
                    "position": {
                        "id": "3765765730300190720",
                        "symbol": "XRP/USDT:USDT",
                        "side": "long",
                        "contracts": 0.97,
                        "contractSize": 1.0,
                        "info": {
                            "posId": "3765765730300190720",
                            "posSide": "long",
                            "mgnMode": "cross",
                            "avgPx": "1.1183072164948453",
                            "markPx": "1.0",
                            "pos": "0.97",
                        },
                    }
                }
            ),
        }

        with patch("crypto_trader.active_position_manager.create_exchange", return_value=exchange):
            result = evaluate_open_positions(config, rows=[row], notify=False)

        item = result["items"][0]
        self.assertEqual(item["action"], "BAD_CUT_REMAINDER")
        self.assertTrue(item["protected_position"])
        self.assertEqual(item["execution"]["reason"], "protected_position")
        self.assertFalse(item["execution"]["submitted"])
        self.assertEqual(exchange.orders, [])
