from __future__ import annotations

import json
import tempfile
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from crypto_trader.atlas_mirror import atlas_database_for_collection
from crypto_trader.codex_features import _slot_state
from crypto_trader.config import DEFAULT_CONFIG
from crypto_trader.executor import candidate_client_order_id
from crypto_trader.models import TradeCandidate
from crypto_trader.runtime_sync import _fetch_positions_history, sync_runtime_state
from crypto_trader.storage import (
    insert_trade_execution_row,
    list_pending_orders,
    list_trade_execution_rows,
    save_pending_order,
)


class RuntimeSyncExchange:
    def __init__(self) -> None:
        self.algo_orders: list[dict] = []

    def market(self, symbol: str) -> dict:
        base, rest = symbol.split("/", 1)
        quote = rest.split(":", 1)[0]
        return {"id": f"{base}-{quote}-SWAP", "symbol": symbol}

    def price_to_precision(self, symbol: str, price: float) -> str:
        return f"{price:.4f}".rstrip("0").rstrip(".")

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return f"{amount:.4f}".rstrip("0").rstrip(".")

    def privatePostTradeOrderAlgo(self, request: dict) -> dict:
        self.algo_orders.append(dict(request))
        return {"code": "0", "data": [{"algoId": f"algo-{len(self.algo_orders)}"}]}

    def fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int | None = None) -> list[list[float]]:
        rows = []
        close = 100.0
        for index in range(limit or 80):
            rows.append([index, close, close + 0.2, close - 0.2, close, 1.0])
        return rows


class RuntimeSyncHighAtrExchange(RuntimeSyncExchange):
    def fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int | None = None) -> list[list[float]]:
        rows = []
        close = 100.0
        for index in range(limit or 80):
            rows.append([index, close, close + 3.2, close - 3.2, close, 1.0])
        return rows

class RuntimeSyncBullTrendExchange(RuntimeSyncExchange):
    def fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int | None = None) -> list[list[float]]:
        rows = []
        close = 100.0
        for index in range(limit or 100):
            close += 0.1
            rows.append([index, close, close + 0.2, close - 0.2, close, 1.0])
        return rows


class RuntimeSyncRejectingAlgoExchange(RuntimeSyncExchange):
    def privatePostTradeOrderAlgo(self, request: dict) -> dict:
        self.algo_orders.append(dict(request))
        return {
            "code": "1",
            "data": [{"sCode": "51278", "sMsg": "SL trigger price cannot be lower than the last price"}],
            "msg": "",
        }

class RuntimeSyncTest(TestCase):
    def _config(self) -> dict:
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        config = deepcopy(DEFAULT_CONFIG)
        config["_config_dir"] = self.tmpdir.name
        config["_atlas_test_mode"] = True
        config["mode"] = "demo"
        return config

    def tearDown(self) -> None:
        tmpdir = getattr(self, "tmpdir", None)
        if tmpdir:
            tmpdir.cleanup()

    @staticmethod
    def _mini_candidate() -> TradeCandidate:
        return TradeCandidate(
            symbol="BTC/USDT:USDT",
            base="BTC",
            side="long",
            confidence=88.0,
            entry=62000.0,
            stop_loss=61000.0,
            take_profit=64000.0,
            risk_reward=2.0,
            order_usdt=20.0,
            quantity=1.25,
            spread_pct=0.01,
            news_score=0.0,
            news_count=1,
            win_probability_pct=82.0,
            decision_metadata={
                "mini_setup": {"setup_id": "mini-btc-08"},
                "okx_review": {
                    "route": "lc_okx_setup_review",
                    "decision": "KEEP_SETUP",
                    "accepted_for_okx": True,
                },
            },
        )

    @staticmethod
    def _open_order(order_id: str = "limit-123") -> dict:
        return {
            "id": order_id,
            "symbol": "BTC/USDT:USDT",
            "side": "buy",
            "amount": 1.25,
            "remaining": 1.25,
            "price": 62000,
        }

    def test_runtime_sync_preserves_mini_and_5_5_metadata_for_existing_okx_order(self) -> None:
        config = self._config()
        save_pending_order(config, self._mini_candidate(), "limit-123", status="LC_OKX", journal_id=12)

        sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-18T00:00:00+00:00",
                "positions": [],
                "open_orders": [self._open_order()],
            },
        )

        pending = list_pending_orders(config, status="LC_OKX")
        self.assertEqual(len(pending), 1)
        payload = json.loads(str(pending[0]["payload_json"]))
        self.assertEqual(payload["confidence"], 88.0)
        self.assertEqual(payload["decision_metadata"]["mini_setup"]["setup_id"], "mini-btc-08")
        self.assertTrue(payload["decision_metadata"]["okx_review"]["accepted_for_okx"])

    def test_runtime_sync_attaches_orphan_okx_order_to_reviewed_mini_placeholder(self) -> None:
        config = self._config()
        candidate = self._mini_candidate()
        placeholder = save_pending_order(
            config,
            candidate,
            None,
            status="OPEN",
            max_age_hours=6,
            journal_id=12,
        )

        sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-18T00:00:00+00:00",
                "positions": [],
                "open_orders": [
                    {
                        **self._open_order("limit-recovered"),
                        "clientOrderId": candidate_client_order_id(candidate, entry_type="mini_lc_okx"),
                    }
                ],
            },
        )

        pending = list_pending_orders(config, status="LC_OKX")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], placeholder["id"])
        self.assertEqual(pending[0]["exchange_order_id"], "limit-recovered")
        payload = json.loads(str(pending[0]["payload_json"]))
        self.assertEqual(payload["decision_metadata"]["mini_setup"]["setup_id"], "mini-btc-08")

    def test_runtime_sync_does_not_attach_same_symbol_manual_order_to_mini_placeholder(self) -> None:
        config = self._config()
        placeholder = save_pending_order(
            config,
            self._mini_candidate(),
            None,
            status="OPEN",
            max_age_hours=6,
            journal_id=12,
        )

        sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-18T00:00:00+00:00",
                "positions": [],
                "open_orders": [
                    {
                        **self._open_order("manual-limit"),
                        "clientOrderId": "manual-btc-order",
                    }
                ],
            },
        )

        active = list_pending_orders(config, status="ACTIVE")
        placeholder_row = next(row for row in active if row["id"] == placeholder["id"])
        self.assertFalse(placeholder_row.get("exchange_order_id"))
        self.assertTrue(any(row.get("exchange_order_id") == "manual-limit" for row in active))

    def test_sync_runtime_state_seeds_ai_metadata(self) -> None:
        config = self._config()

        result = sync_runtime_state(
            config,
            account_snapshot={"enabled": True, "mode": "demo", "created_at": "2026-07-08T00:00:00+00:00", "positions": [], "open_orders": []},
        )

        database = atlas_database_for_collection(config, "ai_model_versions")
        self.assertEqual(database["ai_model_versions"].count_documents({}), 2)
        metric = database["prompt_metrics"].find_one({"prompt_version": "prompt-v1"}, {"_id": 0})
        self.assertIsNotNone(metric)
        self.assertEqual(metric["total_requests"], 0)
        self.assertTrue(result["ai"]["seeded_prompt_metric"])

    def test_slot_state_counts_duplicate_symbol_and_side_once(self) -> None:
        open_count, free_slots = _slot_state(
            [
                {"id": 1, "symbol": "KAITO/USDT:USDT", "side": "LONG", "position_slot": 1},
                {"id": 2, "symbol": "KAITO/USDT:USDT", "side": "LONG", "position_slot": 2},
            ],
            5,
        )

        self.assertEqual(open_count, 1)
        self.assertEqual(free_slots, [2, 3, 4, 5])

    def test_sync_runtime_state_imports_positions_and_orders_without_duplicates(self) -> None:
        config = self._config()
        snapshot = {
            "enabled": True,
            "mode": "demo",
            "created_at": "2026-07-08T00:00:00+00:00",
            "positions": [
                {
                    "symbol": "SOL/USDT:USDT",
                    "side": "long",
                    "contracts": 0.36,
                    "entry_price": 81.57,
                    "mark_price": 80.96,
                    "unrealized_pnl": -0.22,
                    "stop_loss": None,
                    "take_profit": None,
                }
            ],
            "open_orders": [
                {
                    "id": "limit-123",
                    "symbol": "BTC/USDT:USDT",
                    "side": "buy",
                    "amount": 1.25,
                    "remaining": 1.25,
                    "price": 62000,
                    "raw": {
                        "attachAlgoOrds": [
                            {"slTriggerPx": "61000", "tpTriggerPx": "64000"},
                        ]
                    },
                }
            ],
        }

        sync_runtime_state(config, account_snapshot=snapshot)
        sync_runtime_state(config, account_snapshot=snapshot)

        pending = list_pending_orders(config, status="ACTIVE", limit=20)
        executions = list_trade_execution_rows(config, statuses=["OPEN"], limit=20)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["exchange_order_id"], "limit-123")
        self.assertEqual(len(executions), 1)
        self.assertEqual(executions[0]["symbol"], "SOL/USDT:USDT")
        self.assertEqual(executions[0]["side"], "LONG")
        self.assertAlmostEqual(executions[0]["stop_loss"], 79.1229)
        self.assertAlmostEqual(executions[0]["take_profit"], 85.24065)
        self.assertAlmostEqual(executions[0]["risk_reward"], 1.5)

    @patch("crypto_trader.notifier.send_telegram_message", return_value=True)
    def test_sync_runtime_state_sets_missing_targets_for_manual_position_on_okx(self, send_message) -> None:
        config = self._config()
        config["exchange"]["leverage"] = 10
        exchange = RuntimeSyncExchange()

        result = sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-08T00:00:00+00:00",
                "_exchange": exchange,
                "positions": [
                    {
                        "symbol": "HYPE/USDT:USDT",
                        "side": "short",
                        "contracts": 29,
                        "entry_price": 58.175,
                        "mark_price": 57.9,
                        "leverage": 10,
                    }
                ],
                "open_orders": [],
            },
        )

        executions = list_trade_execution_rows(config, statuses=["OPEN"], limit=20)
        self.assertEqual(result["exchange"]["manual_positions_imported"], 1)
        self.assertEqual(result["exchange"]["position_targets_submitted"], 1)
        self.assertEqual(executions[0]["import_source"], "manual_okx_position")
        self.assertAlmostEqual(executions[0]["stop_loss"], 59.92025)
        self.assertAlmostEqual(executions[0]["take_profit"], 55.557125)
        self.assertAlmostEqual(executions[0]["risk_reward"], 1.5)
        self.assertEqual(exchange.algo_orders[0]["instId"], "HYPE-USDT-SWAP")
        self.assertEqual(exchange.algo_orders[0]["side"], "buy")
        self.assertEqual(exchange.algo_orders[0]["ordType"], "oco")
        self.assertEqual(exchange.algo_orders[0]["slTriggerPx"], "59.9203")
        self.assertEqual(exchange.algo_orders[0]["tpTriggerPx"], "55.5571")
        send_message.assert_called_once()
        message = send_message.call_args.args[1]
        self.assertIn("BOT ĐÃ GẮN TP/SL", message)
        self.assertIn("HYPE/USDT:USDT SHORT", message)
        self.assertIn("ID lệnh: VT #", message)
        self.assertIn("Entry: 58.175", message)
        self.assertIn("Khối lượng: 29", message)
        self.assertIn("Đòn bẩy: 10x", message)
        self.assertIn("Bot đánh giá: setup theo RR 1.5R (cơ bản)", message)
        self.assertIn("TP: 55.557125", message)
        self.assertIn("SL: 59.92025", message)
        self.assertIn("Nấc chốt 30% đầu tiên: 56.342487", message)
        self.assertIn("Khối lượng dự kiến chốt: 8.7", message)

    @patch("crypto_trader.notifier.send_telegram_message", return_value=True)
    def test_sync_runtime_state_uses_position_close_algo_before_reattaching_targets(self, send_message) -> None:
        config = self._config()
        exchange = RuntimeSyncExchange()

        result = sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-08T00:00:00+00:00",
                "_exchange": exchange,
                "positions": [
                    {
                        "symbol": "BEAT/USDT:USDT",
                        "side": "short",
                        "contracts": 0.7,
                        "entry_price": 3.3013,
                        "leverage": 6,
                        "info": {
                            "closeOrderAlgo": [
                                {
                                    "slTriggerPx": "3.6314",
                                    "tpTriggerPx": "2.8061",
                                }
                            ]
                        },
                    }
                ],
                "open_orders": [],
                "position_targets": {},
            },
        )

        executions = list_trade_execution_rows(config, statuses=["OPEN"], limit=20)
        self.assertEqual(result["exchange"]["position_targets_submitted"], 0)
        self.assertEqual(exchange.algo_orders, [])
        self.assertEqual(send_message.call_count, 0)
        self.assertAlmostEqual(executions[0]["stop_loss"], 3.6314)
        self.assertAlmostEqual(executions[0]["take_profit"], 2.8061)

    def test_sync_imports_reopened_same_symbol_position_as_new_execution(self) -> None:
        config = self._config()
        insert_trade_execution_row(
            config,
            {
                "created_at": "2026-07-25T09:42:03+00:00",
                "updated_at": "2026-07-25T13:27:02+00:00",
                "symbol": "BEAT/USDT:USDT",
                "side": "SHORT",
                "status": "OPEN",
                "entry_price": 3.3013,
                "quantity": 0.7,
                "max_contracts_seen": 0.9,
                "stop_loss": 3.6314,
                "take_profit": 2.8061,
            },
        )

        result = sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-26T06:40:00+00:00",
                "positions": [
                    {
                        "symbol": "BEAT/USDT:USDT",
                        "side": "short",
                        "contracts": 2.4,
                        "entry_price": 3.6107875,
                        "unrealized_pnl": -0.06,
                        "info": {
                            "cTime": "1785047792995",
                            "pos": "2.4",
                            "avgPx": "3.6107875",
                            "closeOrderAlgo": [
                                {"slTriggerPx": "3.8816", "tpTriggerPx": "3.2046"}
                            ],
                        },
                    }
                ],
                "open_orders": [],
                "position_targets": {},
                "positions_history": [
                    {
                        "instId": "BEAT-USDT-SWAP",
                        "direction": "short",
                        "realizedPnl": "-2.7630083471369007",
                        "pnlRatio": "-0.5579637005900505",
                        "closeAvgPx": "3.5961222222222222",
                        "uTime": "1785047740407",
                    }
                ],
            },
        )

        self.assertEqual(result["exchange"]["manual_positions_imported"], 1)
        self.assertEqual(result["exchange"]["executions_closed"], 1)
        open_rows = list_trade_execution_rows(config, statuses=["OPEN"], limit=20)
        losses = list_trade_execution_rows(config, statuses=["LOSS"], limit=20)
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(len(losses), 1)
        self.assertAlmostEqual(open_rows[0]["entry_price"], 3.6107875)
        self.assertAlmostEqual(open_rows[0]["quantity"], 2.4)
        self.assertAlmostEqual(open_rows[0]["stop_loss"], 3.8816)
        self.assertAlmostEqual(open_rows[0]["take_profit"], 3.2046)
        self.assertAlmostEqual(losses[0]["pnl"], -2.7630083471369007)

    def test_sync_imports_new_execution_when_okx_reuses_position_id(self) -> None:
        config = self._config()
        insert_trade_execution_row(
            config,
            {
                "created_at": "2026-07-25T09:42:03+00:00",
                "updated_at": "2026-07-26T07:58:02+00:00",
                "symbol": "BEAT/USDT:USDT",
                "side": "SHORT",
                "status": "OPEN",
                "entry_price": 3.6107875,
                "initial_entry_price": 3.3013,
                "initial_stop_loss": 3.37632955,
                "quantity": 2.4,
                "initial_quantity": 0.9,
                "max_contracts_seen": 2.4,
                "stop_loss": 4.5,
                "take_profit": 3.2046,
                "partial_take_profit_done": True,
                "partial_take_profit_at": "2026-07-25T13:27:02+00:00",
                "partial_take_profit_price": 3.3955,
                "partial_take_profit_amount": 0.2,
                "partial_take_profit_original_tp": 3.0149,
                "profit_extension_step": 0,
                "trailing_stop_updated_at": "2026-07-25T13:27:02+00:00",
                "trade_event_history_json": json.dumps(
                    [
                        {
                            "type": "open",
                            "created_at": "2026-07-25T09:42:03+00:00",
                            "entry_price": 3.3013,
                            "stop_loss": 3.37632955,
                            "take_profit": 3.18875568,
                        },
                        {
                            "type": "partial_close",
                            "created_at": "2026-07-25T13:27:02+00:00",
                            "mark_price": 3.3955,
                            "partial_amount": 0.2,
                        },
                    ],
                    ensure_ascii=False,
                ),
            },
        )

        sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-26T08:05:00+00:00",
                "positions": [
                    {
                        "symbol": "BEAT/USDT:USDT",
                        "side": "short",
                        "contracts": 2.4,
                        "entry_price": 3.6107875,
                        "unrealized_pnl": 0.62,
                        "info": {
                            "cTime": "1785047792995",
                            "pos": "2.4",
                            "avgPx": "3.6107875",
                            "closeOrderAlgo": [
                                {"slTriggerPx": "4.5", "tpTriggerPx": "3.2046"}
                            ],
                        },
                    }
                ],
                "open_orders": [],
                "position_targets": {},
            },
        )

        open_rows = list_trade_execution_rows(config, statuses=["OPEN"], limit=20)
        closed_rows = list_trade_execution_rows(
            config,
            statuses=["WIN", "LOSS", "BREAKEVEN", "CLOSED", "RECONCILED"],
            limit=20,
        )
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(len(closed_rows), 1)
        self.assertNotEqual(open_rows[0]["id"], closed_rows[0]["id"])
        row = open_rows[0]
        self.assertAlmostEqual(row["entry_price"], 3.6107875)
        self.assertAlmostEqual(row["initial_entry_price"], 3.6107875)
        self.assertAlmostEqual(row["initial_stop_loss"], 4.5)
        self.assertAlmostEqual(row["quantity"], 2.4)
        self.assertAlmostEqual(row["initial_quantity"], 2.4)
        self.assertFalse(bool(row.get("partial_take_profit_done")))
        self.assertIsNone(row.get("partial_take_profit_at"))
        self.assertIsNone(row.get("partial_take_profit_price"))
        self.assertIsNone(row.get("partial_take_profit_original_tp"))
        events = json.loads(row["trade_event_history_json"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "open")
        self.assertAlmostEqual(events[0]["entry_price"], 3.6107875)
        self.assertEqual(closed_rows[0]["close_reason"], "exchange_position_no_longer_open")

    @patch("crypto_trader.notifier.send_telegram_message", return_value=True)
    def test_sync_runtime_state_uses_wider_targets_for_high_atr_manual_position(self, _send_message) -> None:
        config = self._config()
        config["exchange"]["leverage"] = 10
        exchange = RuntimeSyncHighAtrExchange()

        sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-08T00:00:00+00:00",
                "_exchange": exchange,
                "positions": [
                    {
                        "symbol": "HYPE/USDT:USDT",
                        "side": "short",
                        "contracts": 29,
                        "entry_price": 58.175,
                        "mark_price": 57.9,
                        "leverage": 10,
                    }
                ],
                "open_orders": [],
            },
        )

        executions = list_trade_execution_rows(config, statuses=["OPEN"], limit=20)
        self.assertAlmostEqual(executions[0]["stop_loss"], 61.08375)
        self.assertAlmostEqual(executions[0]["take_profit"], 53.811875)
        self.assertAlmostEqual(executions[0]["risk_reward"], 1.5)
        self.assertEqual(exchange.algo_orders[0]["slTriggerPx"], "61.0838")
        self.assertEqual(exchange.algo_orders[0]["tpTriggerPx"], "53.8119")

    @patch("crypto_trader.notifier.send_telegram_message", return_value=True)
    def test_sync_runtime_state_uses_extended_rr_when_manual_position_is_supported(self, _send_message) -> None:
        config = self._config()
        config["exchange"]["leverage"] = 10
        exchange = RuntimeSyncBullTrendExchange()

        sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-08T00:00:00+00:00",
                "_exchange": exchange,
                "positions": [
                    {
                        "symbol": "HYPE/USDT:USDT",
                        "side": "long",
                        "contracts": 29,
                        "entry_price": 58.175,
                        "mark_price": 58.5,
                        "unrealized_pnl": 9.425,
                        "leverage": 10,
                    }
                ],
                "open_orders": [],
            },
        )

        executions = list_trade_execution_rows(config, statuses=["OPEN"], limit=20)
        self.assertAlmostEqual(executions[0]["stop_loss"], 56.42975)
        self.assertAlmostEqual(executions[0]["take_profit"], 61.2291875)
        self.assertAlmostEqual(executions[0]["risk_reward"], 1.75)
        payload = json.loads(str(executions[0]["payload_json"]))
        self.assertEqual(payload["manual_target_plan"]["rr_mode"], "extended")
        self.assertEqual(exchange.algo_orders[0]["slTriggerPx"], "56.4297")
        self.assertEqual(exchange.algo_orders[0]["tpTriggerPx"], "61.2292")

    def test_sync_runtime_state_derives_missing_tp_from_existing_manual_sl(self) -> None:
        config = self._config()
        exchange = RuntimeSyncExchange()

        sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-08T00:00:00+00:00",
                "_exchange": exchange,
                "positions": [
                    {
                        "symbol": "XRP/USDT:USDT",
                        "side": "long",
                        "contracts": 100,
                        "entry_price": 2.0,
                        "mark_price": 2.01,
                        "stop_loss": 1.9,
                    }
                ],
                "open_orders": [],
            },
        )

        executions = list_trade_execution_rows(config, statuses=["OPEN"], limit=20)
        self.assertAlmostEqual(executions[0]["stop_loss"], 1.9)
        self.assertAlmostEqual(executions[0]["take_profit"], 2.15)
        self.assertEqual(exchange.algo_orders[0]["ordType"], "conditional")
        self.assertNotIn("slTriggerPx", exchange.algo_orders[0])
        self.assertEqual(exchange.algo_orders[0]["tpTriggerPx"], "2.15")

    def test_sync_runtime_state_reattaches_stored_targets_when_okx_algo_is_missing(self) -> None:
        config = self._config()
        exchange = RuntimeSyncExchange()
        insert_trade_execution_row(
            config,
            {
                "created_at": "2026-07-07T23:50:00+00:00",
                "updated_at": "2026-07-07T23:50:00+00:00",
                "symbol": "BTC/USDT:USDT",
                "side": "LONG",
                "status": "OPEN",
                "entry_price": 64532.0,
                "stop_loss": 64407.0,
                "take_profit": 65032.0,
            },
        )

        result = sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-08T00:00:00+00:00",
                "_exchange": exchange,
                "positions": [
                    {
                        "symbol": "BTC/USDT:USDT",
                        "side": "long",
                        "contracts": 1,
                        "entry_price": 64532.0,
                        "mark_price": 64600.0,
                        "stop_loss": None,
                        "take_profit": None,
                    },
                ],
                "open_orders": [],
            },
        )

        row = list_trade_execution_rows(config, statuses=["OPEN"])[0]
        self.assertEqual(row["stop_loss"], 64407.0)
        self.assertEqual(row["take_profit"], 65032.0)
        self.assertEqual(result["exchange"]["manual_positions_imported"], 0)
        self.assertEqual(result["exchange"]["position_targets_submitted"], 1)
        self.assertEqual(exchange.algo_orders[0]["slTriggerPx"], "64407")
        self.assertEqual(exchange.algo_orders[0]["tpTriggerPx"], "65032")

    def test_sync_attaches_targets_with_position_margin_mode(self) -> None:
        config = self._config()
        config["exchange"]["position_side_mode"] = "long_short"
        exchange = RuntimeSyncExchange()

        sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-08-09T13:19:31+00:00",
                "_exchange": exchange,
                "positions": [
                    {
                        "id": "3776902093758128128",
                        "symbol": "BOME/USDT:USDT",
                        "side": "short",
                        "contracts": 87,
                        "entry_price": 0.0007223,
                        "mark_price": 0.0007262,
                        "info": {
                            "posId": "3776902093758128128",
                            "posSide": "short",
                            "mgnMode": "cross",
                            "cTime": "1786281076094",
                            "pos": "87",
                        },
                    }
                ],
                "open_orders": [],
            },
        )

        self.assertEqual(exchange.algo_orders[0]["tdMode"], "cross")
        self.assertEqual(exchange.algo_orders[0]["posSide"], "short")
        row = list_trade_execution_rows(config, statuses=["OPEN"])[0]
        self.assertEqual(row["exchange_position_id"], "3776902093758128128")
        self.assertEqual(row["exchange_margin_mode"], "cross")

    def test_sync_does_not_mark_rejected_algo_response_as_submitted(self) -> None:
        config = self._config()
        exchange = RuntimeSyncRejectingAlgoExchange()

        result = sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-08-09T13:19:31+00:00",
                "_exchange": exchange,
                "positions": [
                    {
                        "symbol": "LIT/USDT:USDT",
                        "side": "short",
                        "contracts": 8,
                        "entry_price": 2.3,
                        "mark_price": 2.4,
                    }
                ],
                "open_orders": [],
            },
        )

        self.assertEqual(result["exchange"]["position_targets_submitted"], 0)
        self.assertEqual(result["exchange"]["position_target_errors"], 1)
        row = list_trade_execution_rows(config, statuses=["OPEN"])[0]
        payload = json.loads(str(row["payload_json"]))
        self.assertFalse(payload["target_attach_result"]["submitted"])
        self.assertIn("51278", payload["target_attach_result"]["error"])

    def test_sync_does_not_match_different_position_lifecycle_by_symbol_side_only(self) -> None:
        config = self._config()
        insert_trade_execution_row(
            config,
            {
                "created_at": "2026-08-09T10:00:00+00:00",
                "updated_at": "2026-08-09T10:00:00+00:00",
                "symbol": "BOME/USDT:USDT",
                "side": "SHORT",
                "status": "OPEN",
                "entry_price": 0.00052,
                "quantity": 81.0,
                "exchange_position_id": "old-pos",
                "exchange_position_opened_at": "2026-08-09T09:00:00+00:00",
            },
        )

        result = sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-08-09T13:19:31+00:00",
                "positions": [
                    {
                        "id": "new-pos",
                        "symbol": "BOME/USDT:USDT",
                        "side": "short",
                        "contracts": 87,
                        "entry_price": 0.0007223,
                        "info": {"posId": "new-pos", "posSide": "short", "cTime": "1786281076094"},
                    }
                ],
                "open_orders": [],
            },
        )

        open_rows = list_trade_execution_rows(config, statuses=["OPEN"])
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(open_rows[0]["exchange_position_id"], "new-pos")
        self.assertEqual(result["exchange"]["manual_positions_imported"], 1)
        self.assertEqual(result["exchange"]["executions_closed"], 1)

    @patch("crypto_trader.notifier.send_telegram_message")
    def test_sync_closes_open_execution_when_position_disappears(self, send_message) -> None:
        config = self._config()
        snapshot = {
            "enabled": True,
            "mode": "demo",
            "created_at": "2026-07-08T00:00:00+00:00",
            "positions": [
                {"symbol": "SOL/USDT:USDT", "side": "long", "contracts": 1, "entry_price": 80, "unrealized_pnl": -1.25},
            ],
            "open_orders": [],
        }
        sync_runtime_state(config, account_snapshot=snapshot)

        result = sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-08T00:03:00+00:00",
                "positions": [],
                "open_orders": [],
            },
        )

        self.assertEqual(result["exchange"]["executions_closed"], 1)
        self.assertEqual(list_trade_execution_rows(config, statuses=["OPEN"]), [])
        losses = list_trade_execution_rows(config, statuses=["LOSS"])
        self.assertEqual(losses[0]["close_reason"], "manual")
        self.assertIsNone(losses[0]["position_slot"])
        messages = [call.args[1] for call in send_message.call_args_list]
        self.assertTrue(any("SOL/USDT:USDT" in message for message in messages))

    @patch("crypto_trader.notifier.send_telegram_message")
    def test_sync_uses_okx_position_history_pnl_when_position_disappears(self, send_message) -> None:
        config = self._config()
        sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-08T00:00:00+00:00",
                "positions": [
                    {"symbol": "ETC/USDT:USDT", "side": "long", "contracts": 1.53, "entry_price": 7.024, "unrealized_pnl": -2.54},
                ],
                "open_orders": [],
            },
        )

        result = sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-08T00:03:00+00:00",
                "positions": [],
                "open_orders": [],
                "positions_history": [
                    {
                        "instId": "ETC-USDT-SWAP",
                        "direction": "long",
                        "pnl": -3.16,
                        "percentage": -64.77,
                        "uTime": "1784476980000",
                    }
                ],
            },
        )

        self.assertEqual(result["exchange"]["executions_closed"], 1)
        row = list_trade_execution_rows(config, statuses=["LOSS"])[0]
        self.assertEqual(row["pnl"], -3.16)
        self.assertEqual(row["pnl_pct"], -64.77)
        self.assertEqual(row["exchange_close_source"], "okx_positions_history")
        messages = [call.args[1] for call in send_message.call_args_list]
        self.assertTrue(any("-3.16" in message for message in messages))

    @patch("crypto_trader.notifier.send_telegram_message")
    def test_sync_uses_okx_net_pnl_from_history_fees_and_funding(self, send_message) -> None:
        config = self._config()
        sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-22T00:20:00+00:00",
                "positions": [
                    {
                        "symbol": "XAU/USDT:USDT",
                        "side": "long",
                        "contracts": 0.038,
                        "entry_price": 3999.9,
                        "unrealized_pnl": 3.77,
                    },
                ],
                "open_orders": [],
            },
        )

        result = sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-22T00:24:07+00:00",
                "positions": [],
                "open_orders": [],
                "positions_history": [
                    {
                        "instId": "XAU-USDT-SWAP",
                        "direction": "long",
                        "pnl": "3.77",
                        "fundingFee": "-0.32452",
                        "fee": "-0.15387955",
                        "pnlRatio": "0.5418",
                        "uTime": "1784679842000",
                    }
                ],
            },
        )

        self.assertEqual(result["exchange"]["executions_closed"], 1)
        row = list_trade_execution_rows(config, statuses=["WIN"])[0]
        self.assertAlmostEqual(row["pnl"], 3.2916, places=4)
        self.assertEqual(row["pnl_pct"], 54.18)
        self.assertEqual(row["exchange_close_source"], "okx_positions_history")
        messages = [call.args[1] for call in send_message.call_args_list]
        close_messages = [message for message in messages if "+3.29" in message]
        self.assertTrue(close_messages)
        self.assertNotIn("+3.77", close_messages[0])

    @patch("crypto_trader.notifier.send_telegram_message")
    def test_sync_matches_position_history_near_trade_closed_at_not_sync_time(self, send_message) -> None:
        config = self._config()
        insert_trade_execution_row(
            config,
            {
                "created_at": "2026-07-22T00:20:00+00:00",
                "updated_at": "2026-07-22T00:24:07+00:00",
                "closed_at": "2026-07-22T00:24:07+00:00",
                "symbol": "XAU/USDT:USDT",
                "side": "LONG",
                "status": "WIN",
                "pnl": 5.6071,
                "close_reason": "take_profit",
            },
        )

        result = sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-22T01:05:00+00:00",
                "positions": [],
                "open_orders": [],
                "positions_history": [
                    {
                        "instId": "XAU-USDT-SWAP",
                        "direction": "long",
                        "pnl": "6.00",
                        "fundingFee": "-0.20",
                        "fee": "-0.1929",
                        "pnlRatio": "0.9",
                        "uTime": "1784682240000",
                    },
                    {
                        "instId": "XAU-USDT-SWAP",
                        "direction": "long",
                        "pnl": "3.77",
                        "fundingFee": "-0.32452",
                        "fee": "-0.15387955",
                        "pnlRatio": "0.5418",
                        "uTime": "1784679842000",
                    },
                ],
            },
        )

        self.assertEqual(result["exchange"]["corrected_close_pnls"], 1)
        row = list_trade_execution_rows(config, statuses=["WIN"])[0]
        self.assertAlmostEqual(row["pnl"], 3.2916, places=4)
        self.assertEqual(row["pnl_pct"], 54.18)

    def test_sync_prefers_position_history_update_time_over_open_time(self) -> None:
        config = self._config()
        insert_trade_execution_row(
            config,
            {
                "created_at": "2026-07-25T16:33:09+00:00",
                "updated_at": "2026-07-27T04:22:09+00:00",
                "closed_at": "2026-07-27T04:22:09+00:00",
                "symbol": "ETH/USDT:USDT",
                "side": "SHORT",
                "status": "LOSS",
                "pnl": -3.135284,
                "close_reason": "manual",
            },
        )

        result = sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-27T04:52:33+00:00",
                "positions": [],
                "open_orders": [],
                "positions_history": [
                    {
                        "symbol": "ETH/USDT:USDT",
                        "side": "short",
                        "timestamp": 1784996777072,
                        "lastUpdateTimestamp": 1785125917130,
                        "info": {
                            "instId": "ETH-USDT-SWAP",
                            "posSide": "short",
                            "cTime": "1784996777072",
                            "uTime": "1785125917130",
                            "openAvgPx": "1873.7728985507246377",
                            "closeAvgPx": "1943.5931884057971014",
                            "realizedPnl": "-4.9321185217422643",
                            "pnlRatio": "-0.5722143166169811",
                        },
                        "realizedPnl": -4.9321185217422645,
                    }
                ],
            },
        )

        self.assertEqual(result["exchange"]["corrected_close_pnls"], 1)
        row = list_trade_execution_rows(config, statuses=["LOSS"])[0]
        self.assertAlmostEqual(row["pnl"], -4.9321185217422645)
        self.assertEqual(row["pnl_pct"], -57.221432)
        self.assertEqual(row["exchange_close_source"], "okx_positions_history")

    def test_sync_collapses_duplicate_open_executions_for_same_position(self) -> None:
        config = self._config()
        for row_id in range(2):
            insert_trade_execution_row(
                config,
                {
                    "created_at": f"2026-07-07T23:5{row_id}:00+00:00",
                    "updated_at": f"2026-07-07T23:5{row_id}:00+00:00",
                    "symbol": "KAITO/USDT:USDT",
                    "side": "LONG",
                    "status": "OPEN",
                    "position_slot": row_id + 1,
                },
            )

        result = sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-08T00:00:00+00:00",
                "positions": [
                    {"symbol": "KAITO/USDT:USDT", "side": "long", "contracts": 1, "entry_price": 0.67},
                ],
                "open_orders": [],
            },
        )

        open_rows = list_trade_execution_rows(config, statuses=["OPEN"])
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(result["exchange"]["duplicate_executions_closed"], 1)

    def test_sync_preserves_existing_targets_when_position_snapshot_omits_them(self) -> None:
        config = self._config()
        insert_trade_execution_row(
            config,
            {
                "created_at": "2026-07-07T23:50:00+00:00",
                "updated_at": "2026-07-07T23:50:00+00:00",
                "symbol": "BTC/USDT:USDT",
                "side": "LONG",
                "status": "OPEN",
                "entry_price": 64532.0,
                "stop_loss": 64407.0,
                "take_profit": 65032.0,
            },
        )

        sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-08T00:00:00+00:00",
                "positions": [
                    {
                        "symbol": "BTC/USDT:USDT",
                        "side": "long",
                        "contracts": 1,
                        "entry_price": 64532.0,
                        "mark_price": 64600.0,
                        "stop_loss": None,
                        "take_profit": None,
                    },
                ],
                "open_orders": [],
            },
        )

        row = list_trade_execution_rows(config, statuses=["OPEN"])[0]
        self.assertEqual(row["stop_loss"], 64407.0)
        self.assertEqual(row["take_profit"], 65032.0)
        self.assertEqual(row["initial_stop_loss"], 64407.0)

    def test_sync_uses_algo_targets_from_snapshot_when_position_snapshot_omits_them(self) -> None:
        config = self._config()
        sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-08T00:00:00+00:00",
                "positions": [
                    {
                        "symbol": "XAU/USDT:USDT",
                        "side": "long",
                        "contracts": 1,
                        "entry_price": 4000.0,
                        "stop_loss": None,
                        "take_profit": None,
                    },
                ],
                "open_orders": [],
                "position_targets": {
                    ("XAU/USDT:USDT", "LONG"): {"stop_loss": 3900.0, "take_profit": 4200.0},
                },
            },
        )

        row = list_trade_execution_rows(config, statuses=["OPEN"])[0]
        self.assertEqual(row["stop_loss"], 3900.0)
        self.assertEqual(row["take_profit"], 4200.0)
        self.assertEqual(row["initial_stop_loss"], 3900.0)

    @patch("crypto_trader.notifier.send_telegram_message")
    def test_sync_backfills_recent_reconciled_exchange_close_notification(self, send_message) -> None:
        config = self._config()
        insert_trade_execution_row(
            config,
            {
                "created_at": "2026-07-08T00:00:00+00:00",
                "updated_at": "2026-07-08T00:05:00+00:00",
                "closed_at": "2026-07-08T00:05:00+00:00",
                "symbol": "ETC/USDT:USDT",
                "side": "LONG",
                "status": "RECONCILED",
                "pnl": -2.5,
                "close_reason": "exchange_position_no_longer_open",
                "position_slot": None,
            },
        )

        result = sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-08T00:06:00+00:00",
                "positions": [],
                "open_orders": [],
            },
        )

        self.assertEqual(result["exchange"]["backfilled_close_notifications"], 1)
        losses = list_trade_execution_rows(config, statuses=["LOSS"])
        self.assertEqual(losses[0]["close_reason"], "manual")
        messages = [call.args[1] for call in send_message.call_args_list]
        self.assertTrue(any("ETC/USDT:USDT" in message for message in messages))

    @patch("crypto_trader.notifier.send_telegram_message")
    def test_manual_profitable_exchange_close_is_not_labeled_take_profit(self, send_message) -> None:
        config = self._config()
        insert_trade_execution_row(
            config,
            {
                "created_at": "2026-07-08T00:00:00+00:00",
                "updated_at": "2026-07-08T00:05:00+00:00",
                "symbol": "LAB/USDT:USDT",
                "side": "LONG",
                "status": "OPEN",
                "entry_price": 0.05,
                "take_profit": 0.07,
                "stop_loss": 0.04,
                "pnl": 1.33,
                "pnl_pct": 26.42,
                "position_slot": 5,
            },
        )

        result = sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-08T00:06:00+00:00",
                "positions": [],
                "open_orders": [],
                "positions_history": [
                    {
                        "instId": "LAB-USDT-SWAP",
                        "posSide": "long",
                        "realizedPnl": "1.33",
                        "pnlRatio": "0.2642",
                        "closeAvgPx": "0.056",
                        "uTime": "2026-07-08T00:05:30+00:00",
                    }
                ],
            },
        )

        self.assertEqual(result["exchange"]["executions_closed"], 1)
        wins = list_trade_execution_rows(config, statuses=["WIN"])
        self.assertEqual(wins[0]["close_reason"], "manual")
        messages = [call.args[1] for call in send_message.call_args_list]
        self.assertTrue(any("Tự đóng" in message for message in messages))

    @patch("crypto_trader.notifier.send_telegram_message")
    def test_sync_retries_unnotified_exchange_close(self, send_message) -> None:
        config = self._config()
        insert_trade_execution_row(
            config,
            {
                "created_at": "2026-07-08T00:00:00+00:00",
                "updated_at": "2026-07-08T00:05:00+00:00",
                "closed_at": "2026-07-08T00:05:00+00:00",
                "symbol": "ETC/USDT:USDT",
                "side": "LONG",
                "status": "LOSS",
                "pnl": -2.5,
                "close_reason": "stop_loss",
                "position_slot": None,
            },
        )

        result = sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-08T00:06:00+00:00",
                "positions": [],
                "open_orders": [],
            },
        )

        self.assertEqual(result["exchange"]["retried_close_notifications"], 1)
        messages = [call.args[1] for call in send_message.call_args_list]
        self.assertTrue(any("ETC/USDT:USDT" in message for message in messages))

    def test_sync_corrects_recent_exchange_close_pnl_from_history(self) -> None:
        config = self._config()
        insert_trade_execution_row(
            config,
            {
                "created_at": "2026-07-08T00:00:00+00:00",
                "updated_at": "2026-07-08T00:05:00+00:00",
                "closed_at": "2026-07-08T00:05:00+00:00",
                "symbol": "ETC/USDT:USDT",
                "side": "LONG",
                "status": "LOSS",
                "pnl": -2.54,
                "pnl_pct": -51.99,
                "close_reason": "stop_loss",
                "position_slot": None,
            },
        )

        result = sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-08T00:06:00+00:00",
                "positions": [],
                "open_orders": [],
                "positions_history": [
                    {
                        "instId": "ETC-USDT-SWAP",
                        "direction": "long",
                        "pnl": -3.16,
                        "percentage": -64.77,
                        "uTime": "1784476980000",
                    }
                ],
            },
        )

        self.assertEqual(result["exchange"]["corrected_close_pnls"], 1)
        row = list_trade_execution_rows(config, statuses=["LOSS"])[0]
        self.assertEqual(row["pnl"], -3.16)
        self.assertEqual(row["pnl_pct"], -64.77)

    def test_sync_estimates_closed_pnl_from_snapshot_when_history_is_missing(self) -> None:
        config = self._config()
        insert_trade_execution_row(
            config,
            {
                "created_at": "2026-07-08T00:00:00+00:00",
                "updated_at": "2026-07-08T00:05:00+00:00",
                "closed_at": "2026-07-08T00:05:00+00:00",
                "symbol": "ETC/USDT:USDT",
                "side": "LONG",
                "status": "LOSS",
                "entry_price": 7.024,
                "pnl": -2.5398,
                "pnl_pct": -51.99,
                "close_reason": "stop_loss",
                "position_slot": None,
                "snapshot_json": json.dumps(
                    {
                        "position": {
                            "contracts": 1.53,
                            "contractSize": 10,
                            "initialMargin": 4.765707119454546,
                            "realizedPnl": -0.0671642910642104,
                            "info": {
                                "avgPx": "7.024",
                                "posSide": "long",
                                "closeOrderAlgo": [{"slTriggerPx": "6.825", "tpTriggerPx": "7.295"}],
                            },
                        }
                    }
                ),
                "payload_json": json.dumps(
                    {
                        "position": {
                            "initialMargin": 4.88532978,
                            "info": {"margin": "4.9462664"},
                        }
                    }
                ),
            },
        )

        result = sync_runtime_state(
            config,
            account_snapshot={
                "enabled": True,
                "mode": "demo",
                "created_at": "2026-07-08T00:06:00+00:00",
                "positions": [],
                "open_orders": [],
                "positions_history": [],
            },
        )

        self.assertEqual(result["exchange"]["corrected_close_pnls"], 1)
        row = list_trade_execution_rows(config, statuses=["LOSS"])[0]
        self.assertAlmostEqual(row["pnl"], -3.164075, places=5)
        self.assertAlmostEqual(row["pnl_pct"], -64.7668, places=3)
        self.assertEqual(row["exchange_close_source"], "estimated_from_position_snapshot")

    def test_fetch_positions_history_uses_raw_okx_fallback(self) -> None:
        class RawHistoryExchange:
            def fetch_positions_history(self, *args, **kwargs) -> list[dict[str, object]]:
                return []

            def privateGetAccountPositionsHistory(self, params: dict[str, object]) -> dict[str, object]:
                self.params = params
                return {
                    "data": [
                        {
                            "instId": "ETC-USDT-SWAP",
                            "direction": "long",
                            "pnl": "-3.16",
                            "pnlRatio": "-0.6477",
                            "uTime": "1784476980000",
                        }
                    ]
                }

        exchange = RawHistoryExchange()

        rows = _fetch_positions_history(exchange, 100)

        self.assertEqual(exchange.params["instType"], "SWAP")
        self.assertEqual(rows[0]["pnl"], "-3.16")
