from __future__ import annotations

import json
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import patch

from crypto_trader.ai_coordinator import (
    _candidate_market_summary,
    _local_market_scan_result,
    _validated_ai_symbols,
    internal_market_scan_due,
    internal_lc_memory,
    okx_ai_approval,
    run_internal_market_scan,
)
from crypto_trader.config import DEFAULT_CONFIG
from crypto_trader.lc_pipeline import save_lc_pipeline_mini_scan
from crypto_trader.models import RiskCheck, TradeCandidate
from crypto_trader.storage import recent_market_scan_memory, save_market_scan_observations, save_pending_order, set_journal_state


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
        config["_atlas_test_mode"] = True
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

    def test_internal_market_scan_runs_once_per_fixed_slot(self) -> None:
        config = self._config()
        config["ai"]["internal"]["market_scan_fixed_schedule"] = True
        config["ai"]["internal"]["market_scan_interval_seconds"] = 14400
        now = datetime(2026, 7, 4, 13, 30, tzinfo=timezone.utc)
        slot_id = datetime(2026, 7, 4, 13, 0, tzinfo=timezone.utc).isoformat()
        save_lc_pipeline_mini_scan(
            config,
            {"created_at": now.isoformat(), "slot_id": slot_id, "status": "done", "approved_symbols": []},
        )

        self.assertFalse(internal_market_scan_due(config, now=now))

    @patch("crypto_trader.ai_coordinator.notify_mini_pool_summary")
    @patch("crypto_trader.ai_coordinator.recent_market_scan_memory")
    @patch("crypto_trader.ai_coordinator.enrich_quantities")
    @patch("crypto_trader.ai_coordinator.apply_position_sizing")
    @patch("crypto_trader.ai_coordinator.build_candidates")
    @patch("crypto_trader.ai_coordinator.market_guard_symbol_layers")
    @patch("crypto_trader.ai_coordinator.fetch_market_snapshots")
    @patch("crypto_trader.ai_coordinator.fetch_top_volume_symbols")
    @patch("crypto_trader.ai_coordinator.collect_news")
    def test_internal_market_scan_fetches_latest_four_hour_symbols_into_source_universe(
        self,
        collect_news,
        fetch_top_volume_symbols,
        fetch_market_snapshots,
        market_guard_symbol_layers,
        build_candidates,
        apply_position_sizing,
        enrich_quantities,
        recent_market_scan_memory_mock,
        notify_mini_pool_summary,
    ) -> None:
        config = self._config()
        set_journal_state(
            config,
            "lc_internal_pipeline_state",
            json.dumps(
                {
                    "state_version": 3,
                    "day_key": "2026-07-06",
                    "four_hour_history": [
                        {
                            "frame": "4h",
                            "slot": "2026-07-06T08:00:00+07:00",
                            "created_at": "2026-07-06T01:00:00+00:00",
                            "index": 1,
                            "approved": [{"symbol": "LIT/USDT:USDT"}],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        )
        collect_news.return_value = {}
        fetch_top_volume_symbols.return_value = (["BTC/USDT:USDT"], [])
        fetch_market_snapshots.return_value = ([], [])
        market_guard_symbol_layers.return_value = {}
        build_candidates.return_value = []
        apply_position_sizing.return_value = None
        enrich_quantities.return_value = []
        recent_market_scan_memory_mock.return_value = {}

        run_internal_market_scan(config, force=True)

        self.assertEqual(
            fetch_market_snapshots.call_args.args[1],
            ["BTC/USDT:USDT", "LIT/USDT:USDT"],
        )
        notify_mini_pool_summary.assert_called_once()

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

    def test_local_market_scan_always_keeps_at_least_one_top_candidate(self) -> None:
        config = self._config()
        config["ai"]["internal"]["market_scan_max_symbols"] = 3
        config["ai"]["internal"]["market_scan_min_approved_symbols"] = 1
        config["ai"]["internal"]["market_scan_min_win_probability_pct"] = 90

        result = _local_market_scan_result(
            config,
            [
                _candidate("BTC/USDT:USDT", win=64.0),
                _candidate("ETH/USDT:USDT", win=70.0),
                _candidate("SOL/USDT:USDT", win=68.0),
            ],
            [],
        )

        self.assertEqual(result["qualified_symbols"], [])
        self.assertEqual(result["approved_symbols"], ["ETH/USDT:USDT"])
        self.assertEqual(
            result["selection_checks"],
            ["win_rate", "setup_quality", "trend_alignment", "indicator_strength"],
        )

    def test_local_market_scan_ranks_by_setup_trend_and_indicators_not_only_win_rate(self) -> None:
        config = self._config()
        config["ai"]["internal"]["market_scan_max_symbols"] = 1
        config["ai"]["internal"]["market_scan_min_approved_symbols"] = 1
        config["ai"]["internal"]["market_scan_min_win_probability_pct"] = 60

        weak_high_win = _candidate("BTC/USDT:USDT", win=72.0)
        weak_high_win.rule_score = 70.0
        weak_high_win.indicator_summary = {"volume_ratio": 0.6, "rsi": 79.0, "spread_pct": 0.05, "trend": "down"}
        weak_high_win.higher_timeframes = {
            "1h": {"trend": "down", "candlestick_patterns": {"direction": "bearish", "patterns": ["shooting_star"]}},
            "4h": {"trend": "down", "candlestick_patterns": {"direction": "bearish", "patterns": ["engulfing"]}},
        }
        weak_high_win.candlestick_patterns = {"1m": {"direction": "bearish", "patterns": ["doji"]}}

        strong_lower_win = _candidate("ETH/USDT:USDT", win=69.0)
        strong_lower_win.rule_score = 96.0
        strong_lower_win.indicator_summary = {"volume_ratio": 2.4, "rsi": 58.0, "spread_pct": 0.01, "trend": "up"}
        strong_lower_win.higher_timeframes = {
            "1h": {"trend": "up", "candlestick_patterns": {"direction": "bullish", "patterns": ["morning_star"]}},
            "4h": {"trend": "up", "candlestick_patterns": {"direction": "bullish", "patterns": ["hammer"]}},
        }
        strong_lower_win.candlestick_patterns = {"1m": {"direction": "bullish", "patterns": ["bullish_engulfing"]}}

        result = _local_market_scan_result(config, [weak_high_win, strong_lower_win], [])

        self.assertEqual(result["approved_symbols"], ["ETH/USDT:USDT"])

    def test_validated_ai_symbols_respects_single_pending_limit(self) -> None:
        symbols = _validated_ai_symbols(
            {"approved_symbols": ["ETH/USDT:USDT", "BTC/USDT:USDT"]},
            {"ETH/USDT:USDT", "BTC/USDT:USDT"},
            ["BTC/USDT:USDT"],
            1,
        )

        self.assertEqual(symbols, ["ETH/USDT:USDT"])

    def test_compact_candidate_summary_limits_payload_noise(self) -> None:
        candidate = _candidate("BTC/USDT:USDT")
        candidate.indicator_summary = {
            "last": 100,
            "ema_fast": 99,
            "ema_slow": 98,
            "rsi": 58.123,
            "atr": 2.0,
            "atr_pct": 1.23456,
            "volume_ratio": 1.4567,
            "support": 96,
            "resistance": 104,
            "spread_pct": 0.01234,
            "candlestick_patterns": {
                "1m": {"patterns": ["doji"], "pattern_details": ["raw"] * 20},
                "4h": {
                    "patterns": ["morning_star", "hammer", "dragonfly_doji", "extra"],
                    "direction": "bullish",
                    "strongest_pattern": "morning_star",
                    "signal_summary": "4h bullish reversal",
                    "pattern_details": ["raw"] * 20,
                },
            },
            "higher_timeframes": {
                "1m": {"trend": "up", "candles": [1] * 100},
                "1h": {"trend": "up", "rsi": 55.4, "range_position": "mid", "candles": [1] * 100},
                "4h": {"trend": "up", "rsi": 61.2, "range_position": "low", "candles": [1] * 100},
            },
        }
        candidate.reasons = ["r1", "r2", "r3", "r4"]
        candidate.warnings = ["w1", "w2", "w3"]
        memory = {
            "BTC/USDT:USDT": {
                "1m": [{"timeframe": "1m"}],
                "5m": [{"timeframe": "5m"}, {"timeframe": "5m-old"}],
                "1h": [{"timeframe": "1h"}],
            }
        }

        summary = _candidate_market_summary(candidate, scan_memory_by_symbol=memory, compact=True)

        self.assertNotIn("candlestick_patterns", summary)
        self.assertNotIn("1m", summary["rolling_scan_memory"])
        self.assertEqual(len(summary["rolling_scan_memory"]["5m"]), 1)
        indicator = summary["indicator_summary"]
        self.assertEqual(indicator["rsi"], 58.12)
        self.assertEqual(indicator["volume_ratio"], 1.457)
        self.assertIn("resistance_distance_pct", indicator)
        self.assertNotIn("atr", indicator)
        self.assertNotIn("1m", indicator["higher_timeframes"])
        self.assertNotIn("candles", indicator["higher_timeframes"]["1h"])
        self.assertNotIn("1m", indicator["candlestick_patterns"])
        self.assertNotIn("pattern_details", indicator["candlestick_patterns"]["4h"])
        self.assertEqual(indicator["candlestick_patterns"]["4h"]["patterns"], ["morning_star", "hammer", "dragonfly_doji"])
        self.assertEqual(summary["reasons"], ["r1", "r2", "r3"])
        self.assertEqual(summary["warnings"], ["w1", "w2"])

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
