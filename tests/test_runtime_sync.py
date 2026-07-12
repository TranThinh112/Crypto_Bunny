from __future__ import annotations

import tempfile
from copy import deepcopy
from unittest import TestCase

from crypto_trader.atlas_mirror import atlas_database_for_collection
from crypto_trader.codex_features import _slot_state
from crypto_trader.config import DEFAULT_CONFIG
from crypto_trader.runtime_sync import sync_runtime_state
from crypto_trader.storage import insert_trade_execution_row, list_pending_orders, list_trade_execution_rows


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

    def test_sync_closes_open_execution_when_position_disappears(self) -> None:
        config = self._config()
        snapshot = {
            "enabled": True,
            "mode": "demo",
            "created_at": "2026-07-08T00:00:00+00:00",
            "positions": [
                {"symbol": "SOL/USDT:USDT", "side": "long", "contracts": 1, "entry_price": 80},
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
        reconciled = list_trade_execution_rows(config, statuses=["RECONCILED"])
        self.assertEqual(reconciled[0]["close_reason"], "exchange_position_no_longer_open")
        self.assertIsNone(reconciled[0]["position_slot"])

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
