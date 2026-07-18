from __future__ import annotations

import json
from contextlib import ExitStack
import tempfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from crypto_trader.config import DEFAULT_CONFIG
from crypto_trader.engine import (
    MINI_SYSTEM_NOTIFICATION_HISTORY_KEY,
    _create_pending_from_internal_scan,
    _notify_mini_system_block,
    run_once,
)
from crypto_trader.models import ExecutionResult, RiskCheck, TradeCandidate
from crypto_trader.storage import (
    acquire_journal_lease,
    list_pending_orders,
    open_pending_symbols,
    release_journal_lease,
    save_pending_order,
    set_journal_state,
)


def _candidate(symbol: str = "BTC/USDT:USDT") -> TradeCandidate:
    return TradeCandidate(
        symbol=symbol,
        base=symbol.split("/")[0],
        side="long",
        confidence=86.0,
        win_probability_pct=82.0,
        entry=100.0,
        stop_loss=98.0,
        take_profit=103.0,
        risk_reward=1.5,
        order_usdt=20.0,
        quantity=1.0,
        spread_pct=0.01,
        news_score=0.0,
        news_count=1,
    )


class EngineMiniQueueTest(TestCase):
    def _config(self) -> dict:
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        config = deepcopy(DEFAULT_CONFIG)
        config["_config_dir"] = self.tmpdir.name
        config["_atlas_test_mode"] = True
        config["ledger_path"] = "ledger.jsonl"
        config["mode"] = "dry_run"
        config["news"]["require_symbol_news"] = False
        config["ai"]["internal"]["market_scan_to_pending"] = True
        config["ai"]["internal"]["market_scan_pending_limit"] = 1
        config["ai"]["internal"]["market_scan_require_ai_for_pending"] = True
        config["ai"]["okx"]["provider"] = "local_policy"
        return config

    def tearDown(self) -> None:
        tmpdir = getattr(self, "tmpdir", None)
        if tmpdir:
            tmpdir.cleanup()

    def test_mini_processing_lease_is_exclusive_and_owner_scoped(self) -> None:
        config = self._config()
        key = "mini_processing_lease_test"

        self.assertTrue(acquire_journal_lease(config, key, "worker-a", ttl_seconds=60))
        self.assertFalse(acquire_journal_lease(config, key, "worker-b", ttl_seconds=60))
        self.assertFalse(release_journal_lease(config, key, "worker-b"))
        self.assertTrue(release_journal_lease(config, key, "worker-a"))
        self.assertTrue(acquire_journal_lease(config, key, "worker-b", ttl_seconds=60))
        self.assertTrue(release_journal_lease(config, key, "worker-b"))

    @patch("crypto_trader.engine.acquire_journal_lease", return_value=False)
    def test_mini_setup_waits_without_calling_5_5_when_another_worker_holds_lease(self, _lease) -> None:
        config = self._config()
        config["mode"] = "demo"
        candidate = _candidate("BTC/USDT:USDT")
        scan = {
            "provider": "openai",
            "slot_id": "2026-07-18T08:00:00+07:00",
            "selected_symbols": [candidate.symbol],
            "ai_review": {"approved_symbols": [candidate.symbol]},
        }

        with patch("crypto_trader.engine.review_candidate_for_lc_okx") as review:
            result = _create_pending_from_internal_scan(config, [candidate], scan, (0, set(), []), set())

        review.assert_not_called()
        self.assertEqual(result["created"], 0)
        self.assertTrue(result["skipped"][0]["retryable"])
        self.assertIn("Another Railway worker", result["skipped"][0]["reason"])

    def test_mini_scan_creates_only_best_lc_after_ai_review(self) -> None:
        config = self._config()
        config["ai"]["internal"]["market_scan_pending_limit"] = 3
        candidates = [_candidate("BTC/USDT:USDT"), _candidate("ETH/USDT:USDT")]
        scan = {
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "selected_symbols": ["ETH/USDT:USDT", "BTC/USDT:USDT"],
            "approved_symbols": ["ETH/USDT:USDT", "BTC/USDT:USDT"],
            "ai_review": {"approved_symbols": ["ETH/USDT:USDT", "BTC/USDT:USDT"]},
        }

        result = _create_pending_from_internal_scan(config, candidates, scan, (5, set(), []), set())

        self.assertTrue(result["allowed"])
        self.assertEqual(result["configured_limit"], 3)
        self.assertEqual(result["limit"], 1)
        self.assertEqual(result["created"], 1)
        self.assertEqual(
            {order["symbol"] for order in list_pending_orders(config, status="OPEN")},
            {"ETH/USDT:USDT"},
        )

    def test_mini_scan_uses_pending_gate_before_final_entry_gate(self) -> None:
        config = self._config()
        config["news"]["require_symbol_news"] = True
        config["strategy"]["min_win_probability_pct"] = 80
        candidate = _candidate("LIT/USDT:USDT")
        candidate.win_probability_pct = 60.84
        candidate.news_count = 0
        scan = {
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "selected_symbols": ["LIT/USDT:USDT"],
            "approved_symbols": ["LIT/USDT:USDT"],
            "ai_review": {"approved_symbols": ["LIT/USDT:USDT"]},
        }

        result = _create_pending_from_internal_scan(config, [candidate], scan, (5, set(), []), set())

        self.assertTrue(result["allowed"])
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["skipped"], [])
        orders = list_pending_orders(config, status="OPEN")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["symbol"], "LIT/USDT:USDT")

    @patch("crypto_trader.engine.lc_pipeline_pool_rows", return_value=[])
    def test_mini_scan_uses_saved_snapshot_when_latest_lc_row_is_gone(self, _lc_pipeline_pool_rows) -> None:
        config = self._config()
        scan = {
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "selected_symbols": ["BTC/USDT:USDT"],
            "approved_symbols": ["BTC/USDT:USDT"],
            "pool_symbols": ["BTC/USDT:USDT"],
            "ai_review": {"approved_symbols": ["BTC/USDT:USDT"]},
            "candidates": [
                {
                    "symbol": "BTC/USDT:USDT",
                    "side": "long",
                    "confidence": 86.0,
                    "win_probability_pct": 82.0,
                    "entry": 100.0,
                    "stop_loss": 98.0,
                    "take_profit": 103.0,
                    "risk_reward": 1.5,
                    "spread_pct": 0.01,
                    "news_score": 0.0,
                    "news_count": 1,
                }
            ],
        }

        result = _create_pending_from_internal_scan(config, [], scan, (5, set(), []), set())

        self.assertTrue(result["allowed"])
        self.assertEqual(result["created"], 1)
        orders = list_pending_orders(config, status="OPEN")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["symbol"], "BTC/USDT:USDT")

    def test_mini_scan_submits_demo_lc_to_okx_as_lc_okx(self) -> None:
        config = self._config()
        config["mode"] = "demo"
        candidates = [_candidate("BTC/USDT:USDT")]
        scan = {
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "selected_symbols": ["BTC/USDT:USDT"],
            "approved_symbols": ["BTC/USDT:USDT"],
            "ai_review": {"approved_symbols": ["BTC/USDT:USDT"]},
        }

        with (
            patch(
                "crypto_trader.engine.execute_candidate",
                return_value=ExecutionResult(
                    mode="demo",
                    submitted=True,
                    order_id="limit-123",
                    message="demo: limit order submitted",
                    journal_type="LC",
                    journal_id=1,
                ),
            ) as execute,
            patch("crypto_trader.engine.review_candidate_for_lc_okx", side_effect=lambda *args, **kwargs: (args[1], {"approved": True, "decision": "approve"})) as review,
        ):
            result = _create_pending_from_internal_scan(config, candidates, scan, (5, set(), []), set())

        review.assert_called_once()
        execute.assert_called_once()
        self.assertEqual(execute.call_args.kwargs["order_type_override"], "limit")
        self.assertEqual(execute.call_args.kwargs["entry_type"], "mini_lc_okx")
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["created_orders"][0]["status"], "LC_OKX")
        self.assertEqual(result["created_orders"][0]["exchange_order_id"], "limit-123")
        orders = list_pending_orders(config, status="LC_OKX")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["exchange_order_id"], "limit-123")

    def test_mini_scan_closes_placeholder_when_okx_submit_fails(self) -> None:
        config = self._config()
        config["mode"] = "demo"
        candidate = _candidate("BTC/USDT:USDT")
        scan = {
            "provider": "openai",
            "slot_id": "2026-07-18T08:00:00+07:00",
            "selected_symbols": [candidate.symbol],
            "ai_review": {"approved_symbols": [candidate.symbol]},
        }

        with (
            patch(
                "crypto_trader.engine.review_candidate_for_lc_okx",
                side_effect=lambda *args, **kwargs: (
                    args[1],
                    {"approved": True, "decision": "KEEP_SETUP", "accepted_for_okx": True},
                ),
            ),
            patch(
                "crypto_trader.engine.execute_candidate",
                return_value=ExecutionResult("demo", False, None, "OKX timeout"),
            ),
        ):
            result = _create_pending_from_internal_scan(config, [candidate], scan, (0, set(), []), set())

        self.assertEqual(result["created"], 0)
        self.assertIn("OKX timeout", result["skipped"][0]["reason"])
        self.assertEqual(list_pending_orders(config, status="ACTIVE"), [])

    def test_mini_scan_rejects_lc_okx_when_gpt_55_blocks_setup(self) -> None:
        config = self._config()
        config["mode"] = "demo"
        scan = {
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "selected_symbols": ["BTC/USDT:USDT"],
            "approved_symbols": ["BTC/USDT:USDT"],
            "ai_review": {"approved_symbols": ["BTC/USDT:USDT"]},
        }

        with (
            patch("crypto_trader.engine.review_candidate_for_lc_okx", side_effect=lambda *args, **kwargs: (args[1], {"approved": False, "decision": "reject", "reason": "Setup khong du chat luong de vao Market"})),
            patch("crypto_trader.engine.execute_candidate") as execute,
            patch("crypto_trader.notifier.send_telegram_message") as send_message,
        ):
            result = _create_pending_from_internal_scan(config, [_candidate()], scan, (5, set(), []), set())

        execute.assert_not_called()
        self.assertEqual(result["created"], 0)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertIn("Setup khong du chat luong", result["skipped"][0]["reason"])
        self.assertEqual(len(result["system_notifications"]), 1)
        message = send_message.call_args.args[1]
        self.assertIn("Thông báo hệ thống", message)
        self.assertIn("Mini -> 5.5/LC_OKX", message)
        self.assertIn("Setup khong du chat luong", message)
        self.assertEqual(list_pending_orders(config, status="LC_OKX"), [])

    def test_mini_scan_suppresses_cached_permanent_gpt_55_reject_notification(self) -> None:
        config = self._config()
        config["mode"] = "demo"
        scan = {
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "slot_id": "2026-07-17T01:00:00+00:00",
            "selected_symbols": ["BTC/USDT:USDT"],
            "approved_symbols": ["BTC/USDT:USDT"],
            "ai_review": {"approved_symbols": ["BTC/USDT:USDT"]},
        }
        cached_reject = {
            "approved": False,
            "decision": "DELETE_SETUP",
            "reason": "Xoa: thieu bias 4h/15m",
            "cache_mode": "permanent",
            "cached": True,
            "cache_reason": "Reused permanent rejected 5.5 setup review",
            "rejection_policy": "hard_delete",
            "accepted_for_okx": False,
        }

        with (
            patch("crypto_trader.engine.review_candidate_for_lc_okx", side_effect=lambda *args, **kwargs: (args[1], cached_reject)),
            patch("crypto_trader.engine.execute_candidate") as execute,
            patch("crypto_trader.notifier.send_telegram_message") as send_message,
        ):
            result = _create_pending_from_internal_scan(config, [_candidate()], scan, (5, set(), []), set())

        execute.assert_not_called()
        send_message.assert_not_called()
        self.assertEqual(result["created"], 0)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertIn("Xoa: thieu bias", result["skipped"][0]["reason"])
        self.assertEqual(result["system_notifications"], [])

    def test_mini_system_block_notification_dedupes_by_stage_slot_pair(self) -> None:
        config = self._config()
        candidate = _candidate()
        scan = {
            "slot_id": "2026-07-17T01:00:00+00:00",
            "status": "done",
            "selected_symbols": ["BTC/USDT:USDT"],
        }

        with patch("crypto_trader.notifier.send_telegram_message") as send_message:
            first = _notify_mini_system_block(
                config,
                stage="Mini -> 5.5/LC_OKX",
                reason="Xoa: thieu bias 4h/15m",
                scan=scan,
                candidate=candidate,
            )
            second = _notify_mini_system_block(
                config,
                stage="Mini -> 5.5/LC_OKX",
                reason="Reused permanent rejected 5.5 setup review: Xoa: thieu bias 4h/15m",
                scan=scan,
                candidate=candidate,
            )

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        send_message.assert_called_once()

    def test_mini_system_block_notification_respects_legacy_reason_fingerprint(self) -> None:
        config = self._config()
        candidate = _candidate()
        scan = {
            "slot_id": "2026-07-17T01:00:00+00:00",
            "status": "done",
            "selected_symbols": ["BTC/USDT:USDT"],
        }
        legacy_fingerprint = "2026-07-17T01:00:00+00:00|Mini -> 5.5/LC_OKX|BTC/USDT:USDT|long|Xoa: thieu bias 4h/15m"
        set_journal_state(
            config,
            MINI_SYSTEM_NOTIFICATION_HISTORY_KEY,
            json.dumps([{"fingerprint": legacy_fingerprint}]),
        )

        with patch("crypto_trader.notifier.send_telegram_message") as send_message:
            notification = _notify_mini_system_block(
                config,
                stage="Mini -> 5.5/LC_OKX",
                reason="Xoa: thieu bias 4h/15m",
                scan=scan,
                candidate=candidate,
            )

        self.assertIsNone(notification)
        send_message.assert_not_called()

    def test_mini_scan_submits_gpt_55_keep_monitor_to_okx_and_blocks_duplicate_review(self) -> None:
        config = self._config()
        config["mode"] = "demo"
        candidate = _candidate("INJ/USDT:USDT")
        scan = {
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "selected_symbols": ["INJ/USDT:USDT"],
            "approved_symbols": ["INJ/USDT:USDT"],
            "ai_review": {"approved_symbols": ["INJ/USDT:USDT"]},
        }
        review_decision = {
            "approved": False,
            "decision": "REJECT",
            "reason": "Missing 4h/15m confirmation; keep watching",
            "rejection_policy": "keep_monitor",
            "review_state": "GPT55_KEEP_SETUP",
            "accepted_for_okx": True,
        }

        def soft_review(_config, reviewed_candidate, *_args, **_kwargs):
            stored = deepcopy(reviewed_candidate)
            stored.decision_metadata = {
                **(stored.decision_metadata or {}),
                "okx_review": {
                    "route": "lc_okx_setup_review",
                    **review_decision,
                },
            }
            return stored, dict(review_decision)

        with (
            patch("crypto_trader.engine.review_candidate_for_lc_okx", side_effect=soft_review) as review,
            patch(
                "crypto_trader.engine.execute_candidate",
                return_value=ExecutionResult(
                    mode="demo",
                    submitted=True,
                    order_id="limit-keep-1",
                    message="demo: limit order submitted",
                    journal_type="LC",
                    journal_id=1,
                ),
            ) as execute,
        ):
            first = _create_pending_from_internal_scan(config, [candidate], scan, (5, set(), []), set())
            candidate.entry = 101.0
            second = _create_pending_from_internal_scan(
                config,
                [candidate],
                scan,
                (5, set(), []),
                open_pending_symbols(config),
            )

        execute.assert_called_once()
        review.assert_called_once()
        self.assertEqual(first["created"], 1)
        self.assertEqual(first["wait_slot"], 0)
        self.assertEqual(first["skipped"], [])
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["wait_slot"], 0)
        self.assertEqual(second["skipped"][0]["reason"], "Mini setup already processed (pending_submitted)")
        orders = list_pending_orders(config, status="LC_OKX")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["symbol"], "INJ/USDT:USDT")
        self.assertEqual(orders[0]["status"], "LC_OKX")
        self.assertEqual(orders[0]["exchange_order_id"], "limit-keep-1")
        payload = json.loads(str(orders[0]["payload_json"]))
        self.assertEqual(payload["decision_metadata"]["okx_review"]["rejection_policy"], "keep_monitor")
        self.assertEqual(payload["decision_metadata"]["okx_review"]["review_state"], "GPT55_KEEP_SETUP")
        self.assertTrue(payload["decision_metadata"]["okx_review"]["accepted_for_okx"])

    @patch("crypto_trader.engine.lc_pipeline_pool_rows", return_value=[])
    def test_same_symbol_in_later_pool_gets_fresh_5_5_review_and_replaces_old_order(self, _pool_rows) -> None:
        config = self._config()
        config["mode"] = "demo"
        first_candidate = _candidate("BTC/USDT:USDT")
        second_candidate = _candidate("BTC/USDT:USDT")
        second_candidate.entry = 99.0
        first_scan = {
            "provider": "openai",
            "slot_id": "2026-07-18T04:00:00+07:00",
            "selected_symbols": [first_candidate.symbol],
            "ai_review": {"approved_symbols": [first_candidate.symbol]},
        }
        second_scan = {
            **first_scan,
            "slot_id": "2026-07-18T08:00:00+07:00",
        }

        with (
            patch(
                "crypto_trader.engine.review_candidate_for_lc_okx",
                side_effect=lambda *args, **kwargs: (
                    args[1],
                    {"approved": True, "decision": "KEEP_SETUP", "accepted_for_okx": True},
                ),
            ) as review,
            patch(
                "crypto_trader.engine.execute_candidate",
                side_effect=[
                    ExecutionResult("demo", True, "limit-04", "submitted"),
                    ExecutionResult("demo", True, "limit-08", "submitted"),
                ],
            ) as execute,
            patch("crypto_trader.pending.create_exchange") as create_exchange,
        ):
            create_exchange.return_value.load_markets.return_value = None
            first = _create_pending_from_internal_scan(config, [first_candidate], first_scan, (0, set(), []), set())
            second = _create_pending_from_internal_scan(
                config,
                [second_candidate],
                second_scan,
                (1, {first_candidate.symbol}, []),
                open_pending_symbols(config),
            )

        self.assertEqual(first["created"], 1)
        self.assertEqual(second["created"], 1)
        self.assertEqual(review.call_count, 2)
        self.assertTrue(all(call.kwargs["force"] for call in review.call_args_list))
        self.assertEqual(execute.call_count, 2)
        create_exchange.return_value.cancel_order.assert_called_once_with("limit-04", first_candidate.symbol)
        orders = list_pending_orders(config, status="LC_OKX")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["exchange_order_id"], "limit-08")
        self.assertEqual(orders[0]["entry"], 99.0)

    @patch("crypto_trader.engine.lc_pipeline_pool_rows", return_value=[])
    def test_enter_market_decision_executes_exact_setup_once(self, _pool_rows) -> None:
        config = self._config()
        config["mode"] = "demo"
        candidate = _candidate("SOL/USDT:USDT")
        scan = {
            "provider": "openai",
            "slot_id": "2026-07-18T08:00:00+07:00",
            "selected_symbols": [candidate.symbol],
            "ai_review": {"approved_symbols": [candidate.symbol]},
        }
        market_decision = {
            "approved": True,
            "setup_action": "enter_market",
            "decision": "ENTER_MARKET",
            "accepted_for_okx": False,
        }

        with (
            patch(
                "crypto_trader.engine.review_candidate_for_lc_okx",
                side_effect=lambda *args, **kwargs: (args[1], market_decision),
            ) as review,
            patch(
                "crypto_trader.engine.execute_candidate",
                return_value=ExecutionResult("demo", True, "market-08", "submitted"),
            ) as execute,
        ):
            first = _create_pending_from_internal_scan(config, [candidate], scan, (0, set(), []), set())
            second = _create_pending_from_internal_scan(config, [candidate], scan, (1, {candidate.symbol}, []), set())

        review.assert_called_once()
        execute.assert_called_once()
        self.assertEqual(first["market"], 1)
        self.assertEqual(first["market_orders"][0]["exchange_order_id"], "market-08")
        self.assertEqual(second["market"], 0)
        self.assertIn("already processed", second["skipped"][0]["reason"])

    @patch("crypto_trader.engine.lc_pipeline_pool_rows", return_value=[])
    def test_rejected_later_setup_keeps_locally_valid_older_pending_order(self, _pool_rows) -> None:
        config = self._config()
        config["mode"] = "demo"
        first_candidate = _candidate("BTC/USDT:USDT")
        second_candidate = _candidate("BTC/USDT:USDT")
        second_candidate.entry = 100.2
        second_candidate.risk_reward = 0.5
        first_scan = {
            "provider": "openai",
            "slot_id": "2026-07-18T04:00:00+07:00",
            "selected_symbols": [first_candidate.symbol],
            "ai_review": {"approved_symbols": [first_candidate.symbol]},
        }
        second_scan = {**first_scan, "slot_id": "2026-07-18T08:00:00+07:00"}
        decisions = [
            {"approved": True, "decision": "KEEP_SETUP", "accepted_for_okx": True},
            {
                "approved": False,
                "decision": "DELETE_SETUP",
                "reason": "New 08:00 setup is not good enough",
                "rejection_policy": "hard_delete",
                "accepted_for_okx": False,
            },
        ]

        with (
            patch(
                "crypto_trader.engine.review_candidate_for_lc_okx",
                side_effect=lambda *args, **kwargs: (args[1], decisions.pop(0)),
            ) as review,
            patch(
                "crypto_trader.engine.execute_candidate",
                return_value=ExecutionResult("demo", True, "limit-04", "submitted"),
            ) as execute,
        ):
            _create_pending_from_internal_scan(config, [first_candidate], first_scan, (0, set(), []), set())
            rejected = _create_pending_from_internal_scan(
                config,
                [second_candidate],
                second_scan,
                (1, {first_candidate.symbol}, []),
                open_pending_symbols(config),
            )

        self.assertEqual(review.call_count, 2)
        execute.assert_called_once()
        self.assertEqual(rejected["created"], 0)
        self.assertEqual(rejected["rechecks"][0]["checked"], 1)
        self.assertEqual(
            rejected["rechecks"][0]["method"],
            "local_old_setup_snapshot_with_latest_market_context",
        )
        self.assertFalse(rejected["rechecks"][0]["gpt55_called"])
        self.assertFalse(rejected["rechecks"][0]["market_guard_checked"])
        self.assertEqual(len(rejected["rechecks"][0]["kept"]), 1)
        self.assertEqual(
            rejected["rechecks"][0]["kept"][0]["validation_source"],
            "old_pending_snapshot_with_latest_market_context",
        )
        orders = list_pending_orders(config, status="LC_OKX")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["exchange_order_id"], "limit-04")

    @patch("crypto_trader.engine.lc_pipeline_pool_rows", return_value=[])
    def test_rejected_later_setup_cancels_older_order_only_when_local_recheck_fails(self, _pool_rows) -> None:
        config = self._config()
        config["mode"] = "demo"
        first_candidate = _candidate("BTC/USDT:USDT")
        invalid_candidate = _candidate("BTC/USDT:USDT")
        invalid_candidate.entry = 100.2
        invalid_candidate.indicator_summary = {"last": 90.0}
        first_scan = {
            "provider": "openai",
            "slot_id": "2026-07-18T04:00:00+07:00",
            "selected_symbols": [first_candidate.symbol],
            "ai_review": {"approved_symbols": [first_candidate.symbol]},
        }
        second_scan = {**first_scan, "slot_id": "2026-07-18T08:00:00+07:00"}
        decisions = [
            {"approved": True, "decision": "KEEP_SETUP", "accepted_for_okx": True},
            {
                "approved": False,
                "decision": "DELETE_SETUP",
                "reason": "Delete the new setup",
                "rejection_policy": "hard_delete",
                "accepted_for_okx": False,
            },
        ]

        with (
            patch(
                "crypto_trader.engine.review_candidate_for_lc_okx",
                side_effect=lambda *args, **kwargs: (args[1], decisions.pop(0)),
            ),
            patch(
                "crypto_trader.engine.execute_candidate",
                return_value=ExecutionResult("demo", True, "limit-04", "submitted"),
            ),
            patch("crypto_trader.pending.create_exchange") as create_exchange,
        ):
            create_exchange.return_value.load_markets.return_value = None
            _create_pending_from_internal_scan(config, [first_candidate], first_scan, (0, set(), []), set())
            rejected = _create_pending_from_internal_scan(
                config,
                [invalid_candidate],
                second_scan,
                (1, {first_candidate.symbol}, []),
                open_pending_symbols(config),
            )

        create_exchange.return_value.cancel_order.assert_called_once_with("limit-04", first_candidate.symbol)
        self.assertEqual(rejected["rechecks"][0]["current_market_price"], 90.0)
        self.assertEqual(len(rejected["rechecks"][0]["canceled"]), 1)
        self.assertEqual(list_pending_orders(config, status="LC_OKX"), [])

    @patch("crypto_trader.engine.lc_pipeline_pool_rows", return_value=[])
    def test_rejected_later_setup_rechecks_old_order_with_current_market_guard(self, _pool_rows) -> None:
        config = self._config()
        config["mode"] = "demo"
        first_candidate = _candidate("BTC/USDT:USDT")
        second_candidate = _candidate("BTC/USDT:USDT")
        second_candidate.entry = 100.2
        first_scan = {
            "provider": "openai",
            "slot_id": "2026-07-18T04:00:00+07:00",
            "selected_symbols": [first_candidate.symbol],
            "ai_review": {"approved_symbols": [first_candidate.symbol]},
        }
        second_scan = {**first_scan, "slot_id": "2026-07-18T08:00:00+07:00"}
        decisions = [
            {"approved": True, "decision": "KEEP_SETUP", "accepted_for_okx": True},
            {
                "approved": False,
                "decision": "DELETE_SETUP",
                "reason": "Delete the new setup",
                "rejection_policy": "hard_delete",
                "accepted_for_okx": False,
            },
        ]
        market_layers = {
            first_candidate.symbol: {
                "layer_5m": {
                    "sample_count": 5,
                    "action": "avoid_new_entry",
                    "risk_score": 9.0,
                    "direction": "down",
                    "latest_observed_at": datetime.now(timezone.utc).isoformat(),
                },
                "layer_20m": {
                    "sample_count": 20,
                    "action": "normal",
                    "risk_score": 1.0,
                    "direction": "neutral",
                    "latest_observed_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        }

        with (
            patch(
                "crypto_trader.engine.review_candidate_for_lc_okx",
                side_effect=lambda *args, **kwargs: (args[1], decisions.pop(0)),
            ) as review,
            patch(
                "crypto_trader.engine.execute_candidate",
                return_value=ExecutionResult("demo", True, "limit-04", "submitted"),
            ),
            patch("crypto_trader.pending.create_exchange") as create_exchange,
        ):
            create_exchange.return_value.load_markets.return_value = None
            _create_pending_from_internal_scan(config, [first_candidate], first_scan, (0, set(), []), set())
            rejected = _create_pending_from_internal_scan(
                config,
                [second_candidate],
                second_scan,
                (1, {first_candidate.symbol}, []),
                open_pending_symbols(config),
                market_layers=market_layers,
            )

        self.assertEqual(review.call_count, 2)
        self.assertTrue(rejected["rechecks"][0]["market_guard_checked"])
        self.assertFalse(rejected["rechecks"][0]["gpt55_called"])
        self.assertEqual(len(rejected["rechecks"][0]["canceled"]), 1)
        create_exchange.return_value.cancel_order.assert_called_once_with("limit-04", first_candidate.symbol)
        self.assertEqual(list_pending_orders(config, status="LC_OKX"), [])

    @patch("crypto_trader.engine.lc_pipeline_pool_rows", return_value=[])
    def test_rejected_later_setup_ignores_stale_market_guard(self, _pool_rows) -> None:
        config = self._config()
        config["mode"] = "demo"
        first_candidate = _candidate("BTC/USDT:USDT")
        second_candidate = _candidate("BTC/USDT:USDT")
        second_candidate.entry = 100.2
        first_scan = {
            "provider": "openai",
            "slot_id": "2026-07-18T04:00:00+07:00",
            "selected_symbols": [first_candidate.symbol],
            "ai_review": {"approved_symbols": [first_candidate.symbol]},
        }
        second_scan = {**first_scan, "slot_id": "2026-07-18T08:00:00+07:00"}
        decisions = [
            {"approved": True, "decision": "KEEP_SETUP", "accepted_for_okx": True},
            {
                "approved": False,
                "decision": "DELETE_SETUP",
                "reason": "Delete the new setup",
                "rejection_policy": "hard_delete",
                "accepted_for_okx": False,
            },
        ]
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        market_layers = {
            first_candidate.symbol: {
                "layer_5m": {
                    "sample_count": 5,
                    "action": "avoid_new_entry",
                    "risk_score": 9.0,
                    "direction": "down",
                    "latest_observed_at": stale_time,
                },
                "layer_20m": {
                    "sample_count": 20,
                    "action": "avoid_new_entry",
                    "risk_score": 9.0,
                    "direction": "down",
                    "latest_observed_at": stale_time,
                },
            }
        }

        with (
            patch(
                "crypto_trader.engine.review_candidate_for_lc_okx",
                side_effect=lambda *args, **kwargs: (args[1], decisions.pop(0)),
            ) as review,
            patch(
                "crypto_trader.engine.execute_candidate",
                return_value=ExecutionResult("demo", True, "limit-04", "submitted"),
            ),
        ):
            _create_pending_from_internal_scan(config, [first_candidate], first_scan, (0, set(), []), set())
            rejected = _create_pending_from_internal_scan(
                config,
                [second_candidate],
                second_scan,
                (1, {first_candidate.symbol}, []),
                open_pending_symbols(config),
                market_layers=market_layers,
            )

        self.assertEqual(review.call_count, 2)
        self.assertFalse(rejected["rechecks"][0]["market_guard_checked"])
        self.assertIn("ignored stale Market Guard", rejected["rechecks"][0]["warnings"][0])
        self.assertEqual(len(rejected["rechecks"][0]["kept"]), 1)
        self.assertEqual(len(list_pending_orders(config, status="LC_OKX")), 1)

    @patch("crypto_trader.engine.lc_pipeline_pool_rows", return_value=[])
    def test_rejected_later_setup_keeps_only_one_valid_old_pending_order(self, _pool_rows) -> None:
        config = self._config()
        config["mode"] = "demo"
        first_candidate = _candidate("BTC/USDT:USDT")
        second_candidate = _candidate("BTC/USDT:USDT")
        second_candidate.entry = 100.2
        first_scan = {
            "provider": "openai",
            "slot_id": "2026-07-18T04:00:00+07:00",
            "selected_symbols": [first_candidate.symbol],
            "ai_review": {"approved_symbols": [first_candidate.symbol]},
        }
        second_scan = {**first_scan, "slot_id": "2026-07-18T08:00:00+07:00"}
        decisions = [
            {"approved": True, "decision": "KEEP_SETUP", "accepted_for_okx": True},
            {
                "approved": False,
                "decision": "DELETE_SETUP",
                "reason": "Delete the new setup",
                "rejection_policy": "hard_delete",
                "accepted_for_okx": False,
            },
        ]

        with (
            patch(
                "crypto_trader.engine.review_candidate_for_lc_okx",
                side_effect=lambda *args, **kwargs: (args[1], decisions.pop(0)),
            ) as review,
            patch(
                "crypto_trader.engine.execute_candidate",
                return_value=ExecutionResult("demo", True, "limit-04", "submitted"),
            ),
            patch("crypto_trader.pending.create_exchange") as create_exchange,
        ):
            create_exchange.return_value.load_markets.return_value = None
            _create_pending_from_internal_scan(config, [first_candidate], first_scan, (0, set(), []), set())
            save_pending_order(
                config,
                _candidate(first_candidate.symbol),
                "limit-legacy-duplicate",
                status="LC_OKX",
                journal_id=99,
            )
            rejected = _create_pending_from_internal_scan(
                config,
                [second_candidate],
                second_scan,
                (2, {first_candidate.symbol}, []),
                open_pending_symbols(config),
            )

        self.assertEqual(review.call_count, 2)
        self.assertEqual(rejected["rechecks"][0]["checked"], 2)
        self.assertEqual(len(rejected["rechecks"][0]["kept"]), 1)
        self.assertEqual(len(rejected["rechecks"][0]["canceled"]), 1)
        remaining = list_pending_orders(config, status="LC_OKX")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["exchange_order_id"], "limit-04")
        create_exchange.return_value.cancel_order.assert_called_once_with(
            "limit-legacy-duplicate",
            first_candidate.symbol,
        )

    @patch("crypto_trader.engine.lc_pipeline_pool_rows", return_value=[])
    def test_rejected_later_setup_notifies_when_old_okx_cancel_fails(self, _pool_rows) -> None:
        config = self._config()
        config["mode"] = "demo"
        first_candidate = _candidate("BTC/USDT:USDT")
        invalid_candidate = _candidate("BTC/USDT:USDT")
        invalid_candidate.indicator_summary = {"last": 90.0}
        first_scan = {
            "provider": "openai",
            "slot_id": "2026-07-18T04:00:00+07:00",
            "selected_symbols": [first_candidate.symbol],
            "ai_review": {"approved_symbols": [first_candidate.symbol]},
        }
        second_scan = {**first_scan, "slot_id": "2026-07-18T08:00:00+07:00"}
        decisions = [
            {"approved": True, "decision": "KEEP_SETUP", "accepted_for_okx": True},
            {
                "approved": False,
                "decision": "DELETE_SETUP",
                "reason": "Delete the new setup",
                "rejection_policy": "hard_delete",
                "accepted_for_okx": False,
            },
        ]

        with (
            patch(
                "crypto_trader.engine.review_candidate_for_lc_okx",
                side_effect=lambda *args, **kwargs: (args[1], decisions.pop(0)),
            ),
            patch(
                "crypto_trader.engine.execute_candidate",
                return_value=ExecutionResult("demo", True, "limit-04", "submitted"),
            ),
            patch("crypto_trader.pending.create_exchange") as create_exchange,
            patch("crypto_trader.notifier.send_telegram_message") as send_message,
        ):
            create_exchange.return_value.load_markets.return_value = None
            create_exchange.return_value.cancel_order.side_effect = RuntimeError("OKX cancel timeout")
            _create_pending_from_internal_scan(config, [first_candidate], first_scan, (0, set(), []), set())
            rejected = _create_pending_from_internal_scan(
                config,
                [invalid_candidate],
                second_scan,
                (1, {first_candidate.symbol}, []),
                open_pending_symbols(config),
            )

        self.assertEqual(len(rejected["rechecks"][0]["canceled"]), 0)
        self.assertEqual(len(rejected["rechecks"][0]["kept"]), 1)
        self.assertIn("OKX cancel timeout", rejected["rechecks"][0]["warnings"][0])
        self.assertIn("OKX cancel timeout", rejected["rechecks"][0]["cancellation_warnings"][0])
        messages = [str(call.args[1]) for call in send_message.call_args_list]
        self.assertTrue(any("recheck LC_OKX cũ" in message for message in messages))
        self.assertEqual(len(list_pending_orders(config, status="LC_OKX")), 1)

    @patch("crypto_trader.notifier.send_telegram_message")
    @patch("crypto_trader.engine.lc_pipeline_pool_rows")
    def test_mini_scan_calls_5_5_instead_of_wait_slot_when_old_slot_gate_is_full(
        self,
        lc_pipeline_pool_rows,
        send_telegram_message,
    ) -> None:
        config = self._config()
        config["mode"] = "demo"
        config["ai"]["internal"]["market_scan_pending_limit"] = 3
        candidate = _candidate("LIT/USDT:USDT")
        lc_pipeline_pool_rows.return_value = [
            {
                "symbol": "LIT/USDT:USDT",
                "source_slot": "4h",
                "source_index": 2,
                "source_label": "10/07/26 16:00:00",
            }
        ]
        scan = {
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "approved_symbols": ["LIT/USDT:USDT", "BTC/USDT:USDT"],
            "selected_symbols": ["LIT/USDT:USDT", "BTC/USDT:USDT"],
            "pool_symbols": ["LIT/USDT:USDT", "BTC/USDT:USDT"],
            "slot_id": "2026-07-06T20:00:00+00:00",
            "ai_review": {"approved_symbols": ["LIT/USDT:USDT", "BTC/USDT:USDT"]},
        }

        with (
            patch(
                "crypto_trader.engine.evaluate_candidate",
                return_value=RiskCheck(False, ["Da het slot: 2/2"], []),
            ),
            patch(
                "crypto_trader.engine.review_candidate_for_lc_okx",
                side_effect=lambda *args, **kwargs: (
                    args[1],
                    {"approved": True, "decision": "KEEP_SETUP", "accepted_for_okx": True},
                ),
            ) as review,
            patch(
                "crypto_trader.engine.execute_candidate",
                return_value=ExecutionResult(
                    mode="demo",
                    submitted=True,
                    order_id="limit-slot-1",
                    message="demo: limit order submitted",
                ),
            ),
        ):
            result = _create_pending_from_internal_scan(
                config,
                [candidate, _candidate("BTC/USDT:USDT")],
                scan,
                (2, set(), []),
                set(),
            )

        self.assertTrue(result["allowed"])
        self.assertEqual(result["configured_limit"], 3)
        self.assertEqual(result["limit"], 1)
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["wait_slot"], 0)
        self.assertEqual(result["skipped"], [])
        review.assert_called_once()
        self.assertTrue(review.call_args.kwargs["force"])
        orders = list_pending_orders(config, status="LC_OKX")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["symbol"], "LIT/USDT:USDT")
        self.assertEqual(orders[0]["status"], "LC_OKX")
        self.assertEqual(orders[0]["exchange_order_id"], "limit-slot-1")
        send_telegram_message.assert_not_called()

    def test_mini_scan_fallback_does_not_create_local_lc(self) -> None:
        config = self._config()
        scan = {
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "selected_symbols": ["BTC/USDT:USDT"],
            "approved_symbols": ["BTC/USDT:USDT"],
            "fallback": "local_policy",
        }

        result = _create_pending_from_internal_scan(config, [_candidate()], scan, (5, set(), []), set())

        self.assertFalse(result["allowed"])
        self.assertEqual(result["created"], 0)
        self.assertEqual(list_pending_orders(config, status="OPEN"), [])

    @patch("crypto_trader.notifier.send_telegram_message")
    def test_mini_scan_no_selected_uses_scan_skip_reason(self, send_telegram_message) -> None:
        config = self._config()
        scan = {
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "selected_symbols": [],
            "approved_symbols": [],
            "ai_review": {"decision": "NO_TRADE", "approved_symbols": []},
            "skip_reason": "Mini AI returned NO_TRADE; no setup may continue to LC_OKX",
        }

        result = _create_pending_from_internal_scan(config, [], scan, (5, set(), []), set())

        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "Mini AI returned NO_TRADE; no setup may continue to LC_OKX")
        self.assertEqual(result["created"], 0)
        self.assertEqual(len(result["system_notifications"]), 1)
        self.assertIn("Mini -> LC_OKX", send_telegram_message.call_args.args[1])

    @patch("crypto_trader.notifier.send_telegram_message")
    def test_empty_four_hour_pool_skip_does_not_send_mini_notification(self, send_telegram_message) -> None:
        config = self._config()
        scan = {
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "status": "waiting_lc",
            "selected_symbols": [],
            "approved_symbols": [],
            "skip_reason": "LC 4h pool has no approved symbols; Mini scan skipped",
            "suppress_pending_notification": True,
        }

        result = _create_pending_from_internal_scan(config, [], scan, (5, set(), []), set())

        self.assertFalse(result["allowed"])
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["system_notifications"], [])
        send_telegram_message.assert_not_called()

    def test_run_once_updates_lc_pipeline_before_running_mini_scan(self) -> None:
        config = self._config()
        call_order: list[str] = []

        with ExitStack() as stack:
            stack.enter_context(patch("crypto_trader.engine.select_runtime_config", side_effect=lambda value: value))
            stack.enter_context(patch("crypto_trader.engine.latest_decision_payload", return_value=None))
            stack.enter_context(patch("crypto_trader.engine._resolve_strategy_symbols", return_value=([], {}, [])))
            stack.enter_context(patch("crypto_trader.engine.open_pending_symbols", return_value=set()))
            stack.enter_context(patch("crypto_trader.engine.collect_news", return_value=SimpleNamespace(items=[])))
            stack.enter_context(patch("crypto_trader.engine.fetch_market_snapshots", return_value=([], [])))
            stack.enter_context(patch("crypto_trader.engine.market_guard_symbol_layers", return_value={}))
            stack.enter_context(patch("crypto_trader.engine.build_candidates", return_value=[]))
            stack.enter_context(patch("crypto_trader.engine.apply_position_sizing", return_value=None))
            stack.enter_context(patch("crypto_trader.engine.enrich_quantities", return_value=[]))
            stack.enter_context(patch("crypto_trader.engine.detect_market_regime", return_value={}))
            stack.enter_context(patch("crypto_trader.engine.record_trade_candidates"))
            stack.enter_context(
                patch(
                    "crypto_trader.engine.update_lc_internal_pipeline",
                    side_effect=lambda *_args, **_kwargs: call_order.append("lc") or {},
                )
            )
            stack.enter_context(
                patch(
                    "crypto_trader.engine.run_internal_market_scan_if_due",
                    side_effect=lambda *_args, **_kwargs: call_order.append("mini") or {"approved_symbols": []},
                )
            )
            stack.enter_context(patch("crypto_trader.engine.save_market_scan_observations", return_value=[]))
            stack.enter_context(patch("crypto_trader.engine._merge_cycle_candidates", return_value=([], {})))
            stack.enter_context(patch("crypto_trader.engine.internal_lc_memory", return_value={}))
            stack.enter_context(patch("crypto_trader.engine.maintain_pending_orders", return_value={}))
            stack.enter_context(patch("crypto_trader.engine.should_defer_new_vt_to_internal_lc", return_value=False))
            stack.enter_context(patch("crypto_trader.engine.active_trades_summary", return_value=(0, set(), [])))
            stack.enter_context(patch("crypto_trader.engine.write_report", return_value=Path("report.json")))
            stack.enter_context(patch("crypto_trader.engine.save_decision"))
            stack.enter_context(patch("crypto_trader.engine.record_ai_trade_decision"))
            run_once(config, execute=False)

        self.assertEqual(call_order, ["lc", "mini"])

    def test_run_once_falls_back_when_retryable_storage_calls_timeout(self) -> None:
        config = self._config()
        report_path = Path(self.tmpdir.name) / "reports" / "latest_decision.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text('{"action":"hold","candidates":[]}', encoding="utf-8")

        timeout_error = RuntimeError(
            "ac-jjoayb4-shard-00-02.mongodb.net:27017: read operation timed out"
        )

        with ExitStack() as stack:
            stack.enter_context(patch("crypto_trader.engine.select_runtime_config", side_effect=lambda value: value))
            stack.enter_context(patch("crypto_trader.engine.latest_decision_payload", side_effect=timeout_error))
            stack.enter_context(patch("crypto_trader.engine.open_pending_symbols", return_value=set()))
            stack.enter_context(patch("crypto_trader.engine._resolve_strategy_symbols", return_value=([], {}, [])))
            stack.enter_context(patch("crypto_trader.engine.collect_news", return_value=SimpleNamespace(items=[])))
            stack.enter_context(patch("crypto_trader.engine.fetch_market_snapshots", return_value=([], [])))
            stack.enter_context(patch("crypto_trader.engine.market_guard_symbol_layers", return_value={}))
            stack.enter_context(patch("crypto_trader.engine.build_candidates", return_value=[]))
            stack.enter_context(patch("crypto_trader.engine.apply_position_sizing", side_effect=timeout_error))
            stack.enter_context(patch("crypto_trader.engine.enrich_quantities", return_value=[]))
            stack.enter_context(patch("crypto_trader.engine.detect_market_regime", return_value={}))
            stack.enter_context(patch("crypto_trader.engine.record_trade_candidates", side_effect=timeout_error))
            stack.enter_context(patch("crypto_trader.engine.update_lc_internal_pipeline", return_value={}))
            stack.enter_context(patch("crypto_trader.engine.run_internal_market_scan_if_due", return_value=None))
            stack.enter_context(patch("crypto_trader.engine.save_market_scan_observations", side_effect=timeout_error))
            stack.enter_context(patch("crypto_trader.engine._merge_cycle_candidates", return_value=([], {})))
            stack.enter_context(patch("crypto_trader.engine.internal_lc_memory", return_value={}))
            stack.enter_context(patch("crypto_trader.engine.maintain_pending_orders", side_effect=timeout_error))
            stack.enter_context(patch("crypto_trader.engine.should_defer_new_vt_to_internal_lc", return_value=False))
            stack.enter_context(patch("crypto_trader.engine.active_trades_summary", return_value=(0, set(), [])))
            stack.enter_context(patch("crypto_trader.engine.save_decision", side_effect=timeout_error))
            stack.enter_context(patch("crypto_trader.engine.record_ai_trade_decision", side_effect=timeout_error))

            decision = run_once(config, execute=False)

        warnings = decision.scan_comparison.get("storage_warnings") or []
        self.assertEqual(decision.action, "hold")
        self.assertTrue(any("Previous decision memory" in item for item in warnings))
        self.assertTrue(any("Trade candidate history" in item for item in warnings))
        self.assertTrue(any("Market scan memory" in item for item in warnings))
        self.assertTrue(any("Position sizing state" in item for item in warnings))
        self.assertTrue(any("Pending order maintenance" in item for item in warnings))
        self.assertTrue(any("Decision history" in item for item in warnings))
        self.assertTrue(any("AI trade history" in item for item in warnings))
