from __future__ import annotations

import tempfile
from copy import deepcopy
from unittest import TestCase

from crypto_trader.config import DEFAULT_CONFIG
from crypto_trader.engine import _create_pending_from_internal_scan
from crypto_trader.models import TradeCandidate
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
        config["state_db_path"] = "state.sqlite"
        config["ledger_path"] = "ledger.jsonl"
        config["mode"] = "dry_run"
        config["news"]["require_symbol_news"] = False
        config["ai"]["internal"]["market_scan_to_pending"] = True
        config["ai"]["internal"]["market_scan_pending_limit"] = 3
        config["ai"]["internal"]["market_scan_require_ai_for_pending"] = True
        return config

    def tearDown(self) -> None:
        tmpdir = getattr(self, "tmpdir", None)
        if tmpdir:
            tmpdir.cleanup()

    def test_mini_scan_creates_local_lc_only_after_ai_review(self) -> None:
        config = self._config()
        candidates = [_candidate("BTC/USDT:USDT"), _candidate("ETH/USDT:USDT")]
        scan = {
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "approved_symbols": ["ETH/USDT:USDT", "BTC/USDT:USDT"],
            "ai_review": {"approved_symbols": ["ETH/USDT:USDT", "BTC/USDT:USDT"]},
        }

        result = _create_pending_from_internal_scan(config, candidates, scan, (5, set(), []), set())

        self.assertTrue(result["allowed"])
        self.assertEqual(result["created"], 2)
        self.assertEqual(
            {order["symbol"] for order in list_pending_orders(config, status="OPEN")},
            {"BTC/USDT:USDT", "ETH/USDT:USDT"},
        )

    def test_mini_scan_fallback_does_not_create_local_lc(self) -> None:
        config = self._config()
        scan = {
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "approved_symbols": ["BTC/USDT:USDT"],
            "fallback": "local_policy",
        }

        result = _create_pending_from_internal_scan(config, [_candidate()], scan, (5, set(), []), set())

        self.assertFalse(result["allowed"])
        self.assertEqual(result["created"], 0)
        self.assertEqual(list_pending_orders(config, status="OPEN"), [])
