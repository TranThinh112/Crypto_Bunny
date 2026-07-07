from __future__ import annotations

import tempfile
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from crypto_trader.config import DEFAULT_CONFIG
from crypto_trader.engine import _create_pending_from_internal_scan
from crypto_trader.models import ExecutionResult, RiskCheck, TradeCandidate
from crypto_trader.storage import list_pending_orders


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
        return config

    def tearDown(self) -> None:
        tmpdir = getattr(self, "tmpdir", None)
        if tmpdir:
            tmpdir.cleanup()

    def test_mini_scan_creates_only_best_lc_after_ai_review(self) -> None:
        config = self._config()
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

        with patch(
            "crypto_trader.engine.execute_candidate",
            return_value=ExecutionResult(
                mode="demo",
                submitted=True,
                order_id="limit-123",
                message="demo: limit order submitted",
                journal_type="LC",
                journal_id=1,
            ),
        ) as execute:
            result = _create_pending_from_internal_scan(config, candidates, scan, (5, set(), []), set())

        execute.assert_called_once()
        self.assertEqual(execute.call_args.kwargs["order_type_override"], "limit")
        self.assertEqual(execute.call_args.kwargs["entry_type"], "mini_lc_okx")
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["created_orders"][0]["status"], "LC_OKX")
        self.assertEqual(result["created_orders"][0]["exchange_order_id"], "limit-123")
        orders = list_pending_orders(config, status="LC_OKX")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["exchange_order_id"], "limit-123")

    def test_mini_scan_queues_wait_slot_for_recheck_when_only_slot_is_full(self) -> None:
        config = self._config()
        candidate = _candidate("LIT/USDT:USDT")
        scan = {
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "approved_symbols": ["LIT/USDT:USDT"],
            "selected_symbols": ["LIT/USDT:USDT"],
            "pool_symbols": ["LIT/USDT:USDT"],
            "slot_id": "2026-07-06T20:00:00+00:00",
            "ai_review": {"approved_symbols": ["LIT/USDT:USDT"]},
        }

        with patch(
            "crypto_trader.engine.evaluate_candidate",
            return_value=RiskCheck(False, ["Da het slot: 2/2"], []),
        ):
            result = _create_pending_from_internal_scan(config, [candidate], scan, (2, set(), []), set())

        self.assertTrue(result["allowed"])
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["wait_slot"], 1)
        self.assertEqual(result["skipped"], [])
        orders = list_pending_orders(config, status="WAIT_SLOT")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["symbol"], "LIT/USDT:USDT")
        self.assertEqual(orders[0]["status"], "WAIT_SLOT")

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
