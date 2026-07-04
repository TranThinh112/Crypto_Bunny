from __future__ import annotations

import tempfile
from copy import deepcopy
from unittest import TestCase

from crypto_trader.ai_coordinator import _candidate_market_summary, internal_lc_memory, okx_ai_approval
from crypto_trader.config import DEFAULT_CONFIG
from crypto_trader.models import RiskCheck, TradeCandidate
from crypto_trader.storage import recent_market_scan_memory, save_market_scan_observations, save_pending_order


def _candidate(symbol: str = "BTC/USDT:USDT", side: str = "long", win: float = 82.0) -> TradeCandidate:
    return TradeCandidate(
        symbol=symbol,
        base=symbol.split("/")[0],
        side=side,  # type: ignore[arg-type]
        confidence=82.0,
        win_probability_pct=win,
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


class AiCoordinatorTest(TestCase):
    def _config(self) -> dict:
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        config = deepcopy(DEFAULT_CONFIG)
        config["_config_dir"] = self.tmpdir.name
        config["state_db_path"] = "state.sqlite"
        config["ledger_path"] = "ledger.jsonl"
        config["news"]["require_symbol_news"] = False
        config["ai"]["internal"]["provider"] = "local_policy"
        config["ai"]["okx"]["provider"] = "local_policy"
        return config

    def tearDown(self) -> None:
        tmpdir = getattr(self, "tmpdir", None)
        if tmpdir:
            tmpdir.cleanup()

    def test_internal_memory_prioritizes_lc_okx_before_local_lc(self) -> None:
        config = self._config()
        save_pending_order(config, _candidate("ETH/USDT:USDT", win=95.0), None, journal_id=21)
        save_pending_order(config, _candidate("BTC/USDT:USDT", win=80.0), "limit-1", journal_id=12)

        memory = internal_lc_memory(config)

        self.assertEqual(memory["pending_total"], 2)
        self.assertEqual(memory["lc_okx_count"], 1)
        self.assertEqual(memory["local_lc_count"], 1)
        self.assertEqual(memory["preferred"]["status"], "LC_OKX")
        self.assertEqual(memory["preferred"]["lc_id"], 12)

    def test_okx_ai_defers_new_vt_when_internal_lc_exists(self) -> None:
        config = self._config()
        save_pending_order(config, _candidate("BTC/USDT:USDT"), "limit-1", journal_id=12)
        candidate = _candidate("SOL/USDT:USDT")
        check = RiskCheck(True, [], [])

        new_vt = okx_ai_approval(config, candidate, check, context={"route": "new_vt"})
        pending_release = okx_ai_approval(config, candidate, check, context={"route": "lc_okx_release"})

        self.assertFalse(new_vt["approved"])
        self.assertEqual(new_vt["decision"], "defer_to_internal_lc")
        self.assertTrue(pending_release["approved"])

    def test_candidate_summary_separates_4h_context_from_code_timeframes(self) -> None:
        candidate = _candidate("BTC/USDT:USDT")
        candidate.higher_timeframes = {
            "5m": {
                "trend": "up",
                "candlestick_patterns": {
                    "patterns": ["bullish_marubozu"],
                    "bullish_score": 1.4,
                    "bearish_score": 0.0,
                    "direction": "bullish",
                    "strongest_pattern": "bullish_marubozu",
                    "signal_summary": "bullish_marubozu supports a bullish read against up trend context",
                },
            },
            "15m": {
                "trend": "up",
                "candlestick_patterns": {
                    "patterns": ["hammer"],
                    "bullish_score": 1.8,
                    "bearish_score": 0.0,
                    "direction": "bullish",
                    "strongest_pattern": "hammer",
                    "signal_summary": "hammer supports a bullish read against down trend context",
                },
            },
            "1h": {
                "trend": "up",
                "candlestick_patterns": {
                    "patterns": ["morning_star"],
                    "bullish_score": 3.5,
                    "bearish_score": 0.0,
                    "direction": "bullish",
                    "strongest_pattern": "morning_star",
                    "signal_summary": "morning_star supports a bullish read against down trend context",
                },
            },
            "4h": {
                "trend": "down",
                "rsi": 41.0,
                "candlestick_patterns": {
                    "patterns": ["inverted_hammer"],
                    "bullish_score": 1.7,
                    "bearish_score": 0.0,
                    "direction": "bullish",
                    "trend_context": "down",
                    "strongest_pattern": "inverted_hammer",
                    "signal_summary": "inverted_hammer supports a bullish read against down trend context",
                },
            },
        }

        summary = _candidate_market_summary(candidate)

        self.assertEqual(summary["mini_context_4h"]["timeframe"], "4h")
        self.assertEqual(summary["mini_context_4h"]["strongest_pattern"], "inverted_hammer")
        self.assertEqual({item["timeframe"] for item in summary["code_timeframe_analysis"]}, {"5m", "15m", "1h"})

    def test_recent_market_scan_memory_reuses_previous_timeframe_scans(self) -> None:
        config = self._config()
        candidate = _candidate("BTC/USDT:USDT")
        candidate.indicator_summary = {"timeframe": "1m", "trend": "up"}
        candidate.higher_timeframes = {
            "5m": {"trend": "up", "candlestick_patterns": {"patterns": ["bullish_marubozu"], "bullish_score": 1.4}},
            "1h": {"trend": "up", "candlestick_patterns": {"patterns": ["morning_star"], "bullish_score": 3.5}},
            "4h": {"trend": "down", "candlestick_patterns": {"patterns": ["hammer"], "bullish_score": 1.8}},
        }

        save_market_scan_observations(config, [candidate], source="scan-1", limit=10)
        save_market_scan_observations(config, [candidate], source="scan-2", limit=10)

        memory = recent_market_scan_memory(
            config,
            symbols=["BTC/USDT:USDT"],
            timeframes=["1m", "5m", "1h", "4h"],
            lookback_hours=12,
            per_symbol_timeframe_limit=2,
        )

        self.assertIn("BTC/USDT:USDT", memory)
        self.assertEqual(len(memory["BTC/USDT:USDT"]["1m"]), 2)
        self.assertEqual(len(memory["BTC/USDT:USDT"]["5m"]), 2)
        self.assertEqual(len(memory["BTC/USDT:USDT"]["1h"]), 2)
        self.assertEqual(len(memory["BTC/USDT:USDT"]["4h"]), 2)
