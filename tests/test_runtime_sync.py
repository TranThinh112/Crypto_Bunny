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
from crypto_trader.runtime_sync import sync_runtime_state
from crypto_trader.storage import (
    insert_trade_execution_row,
    list_pending_orders,
    list_trade_execution_rows,
    save_pending_order,
)


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
        self.assertEqual(losses[0]["close_reason"], "stop_loss")
        self.assertIsNone(losses[0]["position_slot"])
        send_message.assert_called_once()
        self.assertIn("SOL/USDT:USDT", send_message.call_args.args[1])

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
        self.assertEqual(losses[0]["close_reason"], "stop_loss")
        send_message.assert_called_once()
        self.assertIn("ETC/USDT:USDT", send_message.call_args.args[1])
