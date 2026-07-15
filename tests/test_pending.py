from __future__ import annotations

import json
import tempfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch

from crypto_trader.atlas_mirror import atlas_database
from crypto_trader.config import DEFAULT_CONFIG
from crypto_trader.lc_pipeline import save_lc_pipeline_mini_scan
from crypto_trader.models import ExecutionResult, TradeCandidate
from crypto_trader.pending import maintain_pending_orders
from crypto_trader.storage import count_pending_orders, list_pending_orders, open_pending_symbols, save_pending_order


def _candidate(symbol: str = "BTC/USDT:USDT", side: str = "long") -> TradeCandidate:
    return TradeCandidate(
        symbol=symbol,
        base=symbol.split("/")[0],
        side=side,  # type: ignore[arg-type]
        confidence=82.0,
        entry=100.0,
        stop_loss=97.5,
        take_profit=103.75,
        risk_reward=1.5,
        order_usdt=20.0,
        quantity=1.0,
        spread_pct=0.01,
        news_score=0.0,
        news_count=1,
        take_profit_pct=75,
        stop_loss_pct=50,
    )


def _age_pending_order(config: dict, order_id: int, hours: float) -> None:
    now = datetime.now(timezone.utc)
    created_at = now - timedelta(hours=hours)
    expires_at = now - timedelta(minutes=1)
    database = atlas_database(config)
    for name in ("pending_orders", "internal_pending_orders"):
        result = database[name].update_one(
            {"id": order_id},
            {
                "$set": {
                    "created_at": created_at.isoformat(),
                    "updated_at": created_at.isoformat(),
                    "expires_at": expires_at.isoformat(),
                }
            },
        )
        if int(result.matched_count or 0) > 0:
            break


def _keep_monitor_review(minutes_ago: int = 5) -> dict:
    reviewed_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "route": "lc_okx_setup_review",
        "approved": False,
        "decision": "GIU_THEO_DOI",
        "reason": "5.5 giu setup de theo doi them",
        "provider": "openai",
        "model": "gpt-5.5",
        "rejection_policy": "keep_monitor",
        "review_state": "GPT55_KEEP_SETUP",
        "accepted_for_okx": True,
        "reviewed_at": reviewed_at.isoformat(),
    }


def _market_entry_review(minutes_ago: int = 5) -> dict:
    reviewed_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "route": "lc_okx_setup_review",
        "approved": True,
        "setup_action": "enter_market",
        "decision": "ENTER_MARKET",
        "reason": "5.5 chon vao lenh market ngay",
        "provider": "openai",
        "model": "gpt-5.5",
        "review_state": "GPT55_ENTER_MARKET",
        "accepted_for_okx": False,
        "reviewed_at": reviewed_at.isoformat(),
    }


class PendingTest(TestCase):
    def _pending_record_total(self, config: dict) -> int:
        database = atlas_database(config)
        return database["pending_orders"].count_documents({}) + database["internal_pending_orders"].count_documents({})

    def _config(self, mode: str = "dry_run") -> dict:
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        config = deepcopy(DEFAULT_CONFIG)
        config["_config_dir"] = self.tmpdir.name
        config["mode"] = mode
        config["_atlas_test_mode"] = True
        config["ledger_path"] = "ledger.jsonl"
        config["news"]["require_symbol_news"] = False
        config["ai"]["internal"]["provider"] = "local_policy"
        config["ai"]["okx"]["provider"] = "local_policy"
        config["pending_orders"]["enabled"] = True
        return config

    def tearDown(self) -> None:
        tmpdir = getattr(self, "tmpdir", None)
        if tmpdir:
            tmpdir.cleanup()

    def test_cancels_pending_when_setup_is_no_longer_valid(self) -> None:
        config = self._config()
        save_pending_order(config, _candidate(), "order-1", journal_id=12)

        result = maintain_pending_orders(config, [])

        self.assertEqual(result["canceled"], 1)
        self.assertEqual(result["events"][0]["lc_id"], 12)
        self.assertEqual(list_pending_orders(config, status="OPEN"), [])
        self.assertEqual(len(list_pending_orders(config, status="CANCELED")), 0)
        self.assertEqual(self._pending_record_total(config), 0)

    def test_cancels_local_pending_when_scan_quality_degrades(self) -> None:
        config = self._config()
        config["strategy"]["min_confidence"] = 60
        config["pending_orders"]["review"]["max_confidence_drop"] = 10
        save_pending_order(config, _candidate(), None, journal_id=12)
        weaker = _candidate()
        weaker.confidence = 68.0

        result = maintain_pending_orders(config, [weaker])

        self.assertEqual(result["canceled"], 1)
        self.assertIn("Confidence", result["events"][0]["reason"])
        self.assertEqual(list_pending_orders(config, status="OPEN"), [])

    def test_cancels_local_pending_when_guard_memory_is_risky(self) -> None:
        config = self._config()
        config["strategy"]["min_confidence"] = 60
        save_pending_order(config, _candidate(), None, journal_id=12)
        layers = {
            "BTC/USDT:USDT": {
                "layer_5m": {
                    "sample_count": 5,
                    "action": "avoid_new_entry",
                    "risk_score": 7.2,
                    "direction": "down",
                },
                "layer_20m": {
                    "sample_count": 20,
                    "action": "wait_confirmation",
                    "risk_score": 4.4,
                    "direction": "down",
                },
            }
        }

        result = maintain_pending_orders(config, [_candidate()], market_layers=layers)

        self.assertEqual(result["canceled"], 1)
        self.assertIn("Market Guard", result["events"][0]["reason"])
        self.assertEqual(list_pending_orders(config, status="OPEN"), [])

    def test_converts_pending_to_position_when_exchange_order_is_filled(self) -> None:
        class FakeExchange:
            def load_markets(self) -> None:
                return None

            def fetch_open_orders(self) -> list[dict]:
                return []

            def fetch_positions(self) -> list[dict]:
                return [{"symbol": "BTC/USDT:USDT", "contracts": 1}]

        config = self._config(mode="demo")
        save_pending_order(config, _candidate(), "order-1", journal_id=12)

        with patch("crypto_trader.pending.create_exchange", return_value=FakeExchange()):
            result = maintain_pending_orders(config, [_candidate()])

        self.assertEqual(result["converted"], 1)
        self.assertEqual(result["events"][0]["lc_id"], 12)
        self.assertEqual(result["events"][0]["vt_id"], 1)
        self.assertEqual(result["events"][0]["source"], "lc_okx_filled")
        self.assertEqual(len(list_pending_orders(config, status="FILLED")), 0)
        self.assertEqual(self._pending_record_total(config), 0)

    def test_keeps_local_pending_when_active_limit_is_full(self) -> None:
        class FakeExchange:
            def load_markets(self) -> None:
                return None

            def fetch_open_orders(self) -> list[dict]:
                return []

            def fetch_positions(self) -> list[dict]:
                return [
                    {"symbol": f"COIN{index}/USDT:USDT", "contracts": 1}
                    for index in range(5)
                ]

        config = self._config(mode="demo")
        config["risk"]["max_active_trades"] = 5
        save_pending_order(config, _candidate(), None, journal_id=12)

        with (
            patch("crypto_trader.pending.create_exchange", return_value=FakeExchange()),
            patch("crypto_trader.pending.execute_candidate") as execute,
        ):
            result = maintain_pending_orders(config, [_candidate()])

        execute.assert_not_called()
        self.assertEqual(result["kept"], 1)
        self.assertEqual(result["converted"], 0)
        self.assertEqual(len(list_pending_orders(config, status="OPEN")), 1)

    def test_submits_old_local_pending_to_okx_when_active_limit_is_full(self) -> None:
        class FakeExchange:
            def load_markets(self) -> None:
                return None

            def fetch_open_orders(self) -> list[dict]:
                return []

            def fetch_positions(self) -> list[dict]:
                return [
                    {"symbol": f"COIN{index}/USDT:USDT", "contracts": 1}
                    for index in range(5)
                ]

        config = self._config(mode="demo")
        config["risk"]["max_active_trades"] = 5
        config["pending_orders"]["local_max_age_hours"] = 6
        config["pending_orders"]["exchange_max_age_days"] = 1.5
        candidate = _candidate()
        candidate.decision_metadata = {"okx_review": _keep_monitor_review(minutes_ago=5)}
        record = save_pending_order(config, candidate, None, journal_id=12, max_age_hours=6)
        _age_pending_order(config, int(record["id"]), 7)

        with (
            patch("crypto_trader.pending.create_exchange", return_value=FakeExchange()),
            patch(
                "crypto_trader.pending.execute_candidate",
                return_value=ExecutionResult(
                    mode="demo",
                    submitted=True,
                    order_id="limit-1",
                    message="demo: limit order submitted",
                    journal_type="LC",
                    journal_id=12,
                ),
            ) as execute,
        ):
            result = maintain_pending_orders(config, [_candidate()])

        execute.assert_called_once()
        self.assertEqual(execute.call_args.kwargs["order_type_override"], "limit")
        self.assertEqual(result["events"][0]["type"], "pending_submitted")
        self.assertEqual(result["events"][0]["status"], "LC_OKX")
        self.assertEqual(result["events"][0]["exchange_order_id"], "limit-1")
        open_order = list_pending_orders(config, status="OPEN")[0]
        self.assertEqual(open_order["status"], "LC_OKX")
        self.assertEqual(open_order["exchange_order_id"], "limit-1")
        self.assertEqual(len(list_pending_orders(config, status="LC_OKX")), 1)
        self.assertEqual(count_pending_orders(config), 1)
        self.assertEqual(open_pending_symbols(config), {"BTC/USDT:USDT"})
        expires_at = datetime.fromisoformat(str(open_order["expires_at"]))
        self.assertGreater(expires_at, datetime.now(timezone.utc) + timedelta(days=1))

    def test_old_local_pending_keep_monitor_review_submits_without_reasking_gpt(self) -> None:
        class FakeExchange:
            def load_markets(self) -> None:
                return None

            def fetch_open_orders(self) -> list[dict]:
                return []

            def fetch_positions(self) -> list[dict]:
                return [{"symbol": f"COIN{index}/USDT:USDT", "contracts": 1} for index in range(5)]

        config = self._config(mode="demo")
        config["risk"]["max_active_trades"] = 5
        config["pending_orders"]["local_max_age_hours"] = 6
        candidate = _candidate("INJ/USDT:USDT")
        review = _keep_monitor_review(minutes_ago=5)
        candidate.decision_metadata = {"okx_review": review}
        record = save_pending_order(config, candidate, None, journal_id=12, max_age_hours=6)
        _age_pending_order(config, int(record["id"]), 7)

        with (
            patch("crypto_trader.pending.create_exchange", return_value=FakeExchange()),
            patch("crypto_trader.ai_coordinator.okx_ai_approval") as approval,
            patch(
                "crypto_trader.pending.execute_candidate",
                return_value=ExecutionResult(
                    mode="demo",
                    submitted=True,
                    order_id="limit-keep-local-1",
                    message="demo: limit order submitted",
                    journal_type="LC",
                    journal_id=12,
                ),
            ) as execute,
        ):
            result = maintain_pending_orders(config, [_candidate("INJ/USDT:USDT")])

        approval.assert_not_called()
        execute.assert_called_once()
        self.assertEqual(result["submitted"], 1)
        self.assertEqual(result["kept"], 1)
        order = list_pending_orders(config, status="LC_OKX")[0]
        self.assertEqual(order["exchange_order_id"], "limit-keep-local-1")
        payload = json.loads(str(order["payload_json"]))
        self.assertEqual(payload["decision_metadata"]["okx_review"]["reviewed_at"], review["reviewed_at"])
        self.assertTrue(payload["decision_metadata"]["okx_review"]["accepted_for_okx"])

    def test_releases_local_pending_when_active_slot_is_available(self) -> None:
        class FakeExchange:
            def load_markets(self) -> None:
                return None

            def fetch_open_orders(self) -> list[dict]:
                return []

            def fetch_positions(self) -> list[dict]:
                return [
                    {"symbol": f"COIN{index}/USDT:USDT", "contracts": 1}
                    for index in range(4)
                ]

        config = self._config(mode="demo")
        config["risk"]["max_active_trades"] = 5
        candidate = _candidate()
        candidate.decision_metadata = {"okx_review": _keep_monitor_review(minutes_ago=5)}
        save_pending_order(config, candidate, None, journal_id=12)

        with (
            patch("crypto_trader.pending.create_exchange", return_value=FakeExchange()),
            patch(
                "crypto_trader.pending.execute_candidate",
                return_value=ExecutionResult(
                    mode="demo",
                    submitted=True,
                    order_id="limit-2",
                    message="demo: limit order submitted",
                    journal_type="LC",
                    journal_id=12,
                ),
            ) as execute,
        ):
            result = maintain_pending_orders(config, [_candidate()])

        execute.assert_called_once()
        self.assertEqual(execute.call_args.kwargs["order_type_override"], "limit")
        self.assertEqual(result["submitted"], 1)
        self.assertEqual(result["events"][0]["lc_id"], 12)
        self.assertEqual(result["events"][0]["source"], "local_pending_okx")
        self.assertEqual(result["events"][0]["exchange_order_id"], "limit-2")
        self.assertEqual(len(list_pending_orders(config, status="LC_OKX")), 1)

    def test_rechecks_wait_slot_and_submits_to_okx_when_slot_opens(self) -> None:
        class FakeExchange:
            def load_markets(self) -> None:
                return None

            def fetch_open_orders(self) -> list[dict]:
                return []

            def fetch_positions(self) -> list[dict]:
                return [
                    {"symbol": f"COIN{index}/USDT:USDT", "contracts": 1}
                    for index in range(4)
                ]

        config = self._config(mode="demo")
        config["risk"]["max_active_trades"] = 5
        queued = _candidate("LIT/USDT:USDT")
        queued.decision_metadata = {
            "wait_slot_queue": {"scan_slot_id": "slot-1"},
            "okx_review": _keep_monitor_review(minutes_ago=5),
        }
        save_pending_order(
            config,
            queued,
            None,
            status="WAIT_SLOT",
            max_age_hours=6,
            journal_id=12,
        )
        save_lc_pipeline_mini_scan(
            config,
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "slot_id": "slot-1",
                "status": "done",
                "pool_symbols": ["LIT/USDT:USDT"],
                "selected_symbols": ["LIT/USDT:USDT"],
                "approved_symbols": ["LIT/USDT:USDT"],
            },
        )

        refreshed = _candidate("LIT/USDT:USDT")
        refreshed.win_probability_pct = 84.0
        refreshed.confidence = 85.0
        refreshed.indicator_summary = {"volume_ratio": 1.8}

        with (
            patch("crypto_trader.pending.create_exchange", return_value=FakeExchange()),
            patch(
                "crypto_trader.pending.execute_candidate",
                return_value=ExecutionResult(
                    mode="demo",
                    submitted=True,
                    order_id="limit-wait-1",
                    message="demo: limit order submitted",
                    journal_type="LC",
                    journal_id=12,
                ),
            ) as execute,
        ):
            result = maintain_pending_orders(config, [refreshed])

        execute.assert_called_once()
        self.assertEqual(result["submitted"], 1)
        self.assertEqual(result["events"][0]["source"], "mini_wait_slot_release")
        self.assertEqual(result["events"][0]["status"], "LC_OKX")
        order = list_pending_orders(config, status="LC_OKX")[0]
        self.assertEqual(order["status"], "LC_OKX")
        self.assertEqual(order["exchange_order_id"], "limit-wait-1")

    def test_legacy_wait_slot_keep_monitor_submits_to_okx_without_reasking_gpt(self) -> None:
        class FakeExchange:
            def load_markets(self) -> None:
                return None

            def fetch_open_orders(self) -> list[dict]:
                return []

            def fetch_positions(self) -> list[dict]:
                return [{"symbol": f"COIN{index}/USDT:USDT", "contracts": 1} for index in range(4)]

        config = self._config(mode="demo")
        config["risk"]["max_active_trades"] = 5
        queued = _candidate("INJ/USDT:USDT")
        review = _keep_monitor_review(minutes_ago=5)
        queued.decision_metadata = {
            "wait_slot_queue": {"scan_slot_id": "slot-1"},
            "okx_review": review,
        }
        save_pending_order(config, queued, None, status="WAIT_SLOT", max_age_hours=6, journal_id=12)
        save_lc_pipeline_mini_scan(
            config,
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "slot_id": "slot-1",
                "status": "done",
                "pool_symbols": ["INJ/USDT:USDT"],
                "selected_symbols": ["INJ/USDT:USDT"],
                "approved_symbols": ["INJ/USDT:USDT"],
            },
        )

        refreshed = _candidate("INJ/USDT:USDT")
        with (
            patch("crypto_trader.pending.create_exchange", return_value=FakeExchange()),
            patch("crypto_trader.ai_coordinator.okx_ai_approval") as approval,
            patch(
                "crypto_trader.pending.execute_candidate",
                return_value=ExecutionResult(
                    mode="demo",
                    submitted=True,
                    order_id="limit-keep-wait-1",
                    message="demo: limit order submitted",
                    journal_type="LC",
                    journal_id=12,
                ),
            ) as execute,
        ):
            result = maintain_pending_orders(config, [refreshed])

        approval.assert_not_called()
        execute.assert_called_once()
        self.assertEqual(result["submitted"], 1)
        self.assertEqual(result["kept"], 1)
        self.assertEqual(result["events"][0]["source"], "mini_wait_slot_release")
        order = list_pending_orders(config, status="LC_OKX")[0]
        self.assertEqual(order["status"], "LC_OKX")
        self.assertEqual(order["exchange_order_id"], "limit-keep-wait-1")
        payload = json.loads(str(order["payload_json"]))
        self.assertEqual(payload["decision_metadata"]["okx_review"]["reviewed_at"], review["reviewed_at"])
        self.assertEqual(payload["decision_metadata"]["wait_slot_queue"]["scan_slot_id"], "slot-1")

    def test_legacy_watchlist_keep_monitor_submits_without_reasking_gpt(self) -> None:
        class FakeExchange:
            def load_markets(self) -> None:
                return None

            def fetch_open_orders(self) -> list[dict]:
                return []

            def fetch_positions(self) -> list[dict]:
                return [{"symbol": f"COIN{index}/USDT:USDT", "contracts": 1} for index in range(4)]

        config = self._config(mode="demo")
        config["risk"]["max_active_trades"] = 5
        queued = _candidate("INJ/USDT:USDT")
        review = _keep_monitor_review(minutes_ago=5)
        queued.decision_metadata = {
            "setup_watchlist": {"scan_slot_id": "slot-1"},
            "okx_review": review,
        }
        save_pending_order(config, queued, None, status="WATCHLIST", max_age_hours=6, journal_id=12)
        save_lc_pipeline_mini_scan(
            config,
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "slot_id": "slot-1",
                "status": "done",
                "pool_symbols": ["INJ/USDT:USDT"],
                "selected_symbols": ["INJ/USDT:USDT"],
                "approved_symbols": ["INJ/USDT:USDT"],
            },
        )

        with (
            patch("crypto_trader.pending.create_exchange", return_value=FakeExchange()),
            patch("crypto_trader.ai_coordinator.okx_ai_approval") as approval,
            patch(
                "crypto_trader.pending.execute_candidate",
                return_value=ExecutionResult(
                    mode="demo",
                    submitted=True,
                    order_id="limit-legacy-watch-1",
                    message="demo: limit order submitted",
                    journal_type="LC",
                    journal_id=12,
                ),
            ) as execute,
        ):
            result = maintain_pending_orders(config, [_candidate("INJ/USDT:USDT")])

        approval.assert_not_called()
        execute.assert_called_once()
        self.assertEqual(result["submitted"], 1)
        self.assertEqual(result["kept"], 1)
        self.assertEqual(result["events"][0]["source"], "legacy_keep_monitor_release")
        order = list_pending_orders(config, status="LC_OKX")[0]
        self.assertEqual(order["exchange_order_id"], "limit-legacy-watch-1")
        payload = json.loads(str(order["payload_json"]))
        self.assertEqual(payload["decision_metadata"]["okx_review"]["reviewed_at"], review["reviewed_at"])

    def test_old_legacy_watchlist_keep_monitor_also_submits_without_reasking_gpt(self) -> None:
        class FakeExchange:
            def load_markets(self) -> None:
                return None

            def fetch_open_orders(self) -> list[dict]:
                return []

            def fetch_positions(self) -> list[dict]:
                return [{"symbol": f"COIN{index}/USDT:USDT", "contracts": 1} for index in range(4)]

        config = self._config(mode="demo")
        config["risk"]["max_active_trades"] = 5
        queued = _candidate("INJ/USDT:USDT")
        queued.decision_metadata = {
            "setup_watchlist": {"scan_slot_id": "slot-1"},
            "okx_review": _keep_monitor_review(minutes_ago=45),
        }
        save_pending_order(config, queued, None, status="WATCHLIST", max_age_hours=6, journal_id=12)
        save_lc_pipeline_mini_scan(
            config,
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "slot_id": "slot-1",
                "status": "done",
                "pool_symbols": ["INJ/USDT:USDT"],
                "selected_symbols": ["INJ/USDT:USDT"],
                "approved_symbols": ["INJ/USDT:USDT"],
            },
        )

        with (
            patch("crypto_trader.pending.create_exchange", return_value=FakeExchange()),
            patch("crypto_trader.ai_coordinator.okx_ai_approval") as approval,
            patch(
                "crypto_trader.pending.execute_candidate",
                return_value=ExecutionResult(
                    mode="demo",
                    submitted=True,
                    order_id="limit-legacy-watch-old-1",
                    message="demo: limit order submitted",
                    journal_type="LC",
                    journal_id=12,
                ),
            ) as execute,
        ):
            result = maintain_pending_orders(config, [_candidate("INJ/USDT:USDT")])

        approval.assert_not_called()
        execute.assert_called_once()
        self.assertEqual(result["submitted"], 1)
        self.assertEqual(result["events"][0]["source"], "legacy_keep_monitor_release")
        self.assertEqual(list_pending_orders(config, status="LC_OKX")[0]["exchange_order_id"], "limit-legacy-watch-old-1")

    def test_keeps_lc_okx_without_reasking_gpt_when_setup_review_already_saved(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.canceled: list[tuple[str, str]] = []

            def load_markets(self) -> None:
                return None

            def fetch_open_orders(self) -> list[dict]:
                return [{"id": "limit-1", "symbol": "BTC/USDT:USDT"}]

            def fetch_positions(self) -> list[dict]:
                return [{"symbol": f"COIN{index}/USDT:USDT", "contracts": 1} for index in range(4)]

            def cancel_order(self, order_id: str, symbol: str) -> None:
                self.canceled.append((order_id, symbol))

        exchange = FakeExchange()
        config = self._config(mode="demo")
        config["risk"]["max_active_trades"] = 5
        candidate = _candidate()
        candidate.decision_metadata = {
            "okx_review": {
                "route": "lc_okx_setup_review",
                "approved": True,
                "setup_action": "keep_setup",
                "decision": "KEEP_SETUP",
                "reason": "Setup duoc giu lam lenh cho OKX",
            }
        }
        save_pending_order(config, candidate, "limit-1", journal_id=12)

        with (
            patch("crypto_trader.pending.create_exchange", return_value=exchange),
            patch(
                "crypto_trader.pending.execute_candidate",
                return_value=ExecutionResult(
                    mode="demo",
                    submitted=True,
                    order_id="market-1",
                    message="demo: market order submitted",
                    journal_type="VT",
                    journal_id=1,
                    linked_journal_id=12,
                ),
            ) as execute,
        ):
            result = maintain_pending_orders(config, [_candidate()])

        self.assertEqual(exchange.canceled, [])
        execute.assert_not_called()
        self.assertEqual(result["converted"], 0)
        self.assertEqual(result["kept"], 1)
        self.assertEqual(result["events"][0]["source"], "lc_okx_stored_setup")

    def test_cancels_okx_pending_and_enters_market_when_slot_is_available(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.canceled: list[tuple[str, str]] = []

            def load_markets(self) -> None:
                return None

            def fetch_open_orders(self) -> list[dict]:
                return [{"id": "limit-1", "symbol": "BTC/USDT:USDT"}]

            def fetch_positions(self) -> list[dict]:
                return [
                    {"symbol": f"COIN{index}/USDT:USDT", "contracts": 1}
                    for index in range(4)
                ]

            def cancel_order(self, order_id: str, symbol: str) -> None:
                self.canceled.append((order_id, symbol))

        exchange = FakeExchange()
        config = self._config(mode="demo")
        config["risk"]["max_active_trades"] = 5
        candidate = _candidate()
        candidate.decision_metadata = {"okx_review": _market_entry_review(minutes_ago=5)}
        save_pending_order(config, candidate, "limit-1", journal_id=12)

        with (
            patch("crypto_trader.pending.create_exchange", return_value=exchange),
            patch(
                "crypto_trader.pending.execute_candidate",
                return_value=ExecutionResult(
                    mode="demo",
                    submitted=True,
                    order_id="market-1",
                    message="demo: market order submitted",
                    journal_type="VT",
                    journal_id=1,
                    linked_journal_id=12,
                ),
            ) as execute,
        ):
            result = maintain_pending_orders(config, [_candidate()])

        self.assertEqual(exchange.canceled, [("limit-1", "BTC/USDT:USDT")])
        execute.assert_called_once()
        self.assertEqual(execute.call_args.kwargs["order_type_override"], "market")
        self.assertEqual(result["converted"], 1)
        self.assertEqual(result["events"][0]["source"], "lc_okx_released")
        self.assertEqual(result["events"][0]["exchange_order_id"], "market-1")
        self.assertEqual(len(list_pending_orders(config, status="FILLED")), 0)
        self.assertEqual(self._pending_record_total(config), 0)

    def test_prioritizes_lc_okx_before_local_lc_when_slot_is_available(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.canceled: list[tuple[str, str]] = []

            def load_markets(self) -> None:
                return None

            def fetch_open_orders(self) -> list[dict]:
                return [{"id": "limit-1", "symbol": "BTC/USDT:USDT"}]

            def fetch_positions(self) -> list[dict]:
                return []

            def cancel_order(self, order_id: str, symbol: str) -> None:
                self.canceled.append((order_id, symbol))

        exchange = FakeExchange()
        config = self._config(mode="demo")
        config["risk"]["max_active_trades"] = 1
        save_pending_order(config, _candidate("ETH/USDT:USDT"), None, journal_id=21)
        lc_okx_candidate = _candidate("BTC/USDT:USDT")
        lc_okx_candidate.decision_metadata = {"okx_review": _keep_monitor_review(minutes_ago=5)}
        save_pending_order(config, lc_okx_candidate, "limit-1", journal_id=12)

        with (
            patch("crypto_trader.pending.create_exchange", return_value=exchange),
            patch(
                "crypto_trader.pending.execute_candidate",
                return_value=ExecutionResult(
                    mode="demo",
                    submitted=True,
                    order_id="market-1",
                    message="demo: market order submitted",
                    journal_type="VT",
                    journal_id=1,
                    linked_journal_id=12,
                ),
            ) as execute,
        ):
            result = maintain_pending_orders(
                config,
                [_candidate("ETH/USDT:USDT"), _candidate("BTC/USDT:USDT")],
            )

        self.assertEqual(exchange.canceled, [])
        execute.assert_not_called()
        self.assertEqual(result["converted"], 0)
        self.assertEqual(result["events"][0]["source"], "lc_okx_stored_setup")
        open_orders = list_pending_orders(config, status="OPEN")
        self.assertEqual({order["symbol"] for order in open_orders}, {"ETH/USDT:USDT", "BTC/USDT:USDT"})
