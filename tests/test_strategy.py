from __future__ import annotations

from datetime import datetime, timezone
from unittest import TestCase

from crypto_trader.models import MarketSnapshot, NewsDigest
from crypto_trader.strategy import build_candidates


class StrategyTest(TestCase):
    def test_builds_single_best_side_per_symbol(self) -> None:
        config = {
            "risk": {"order_usdt": 20},
            "strategy": {"min_risk_reward": 2.0},
        }
        snapshot = MarketSnapshot(
            symbol="BTC/USDT:USDT",
            timestamp=datetime.now(timezone.utc),
            last=100.0,
            bid=99.9,
            ask=100.1,
            spread_pct=0.2,
            ema_fast=102.0,
            ema_slow=101.0,
            rsi=58.0,
            atr=1.0,
            atr_pct=1.0,
            volume_ratio=1.3,
            support=96.0,
            resistance=100.2,
        )
        digest = NewsDigest(items=[], by_symbol_score={"BTC": 3.0}, by_symbol_count={"BTC": 2})

        candidates = build_candidates(config, [snapshot], digest)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].side, "long")
        self.assertGreaterEqual(candidates[0].risk_reward, 2.0)

    def test_market_guard_memory_reduces_candidate_score(self) -> None:
        config = {
            "risk": {"order_usdt": 20},
            "strategy": {"min_risk_reward": 2.0},
        }
        snapshot = MarketSnapshot(
            symbol="BTC/USDT:USDT",
            timestamp=datetime.now(timezone.utc),
            last=100.0,
            bid=99.9,
            ask=100.1,
            spread_pct=0.2,
            ema_fast=102.0,
            ema_slow=101.0,
            rsi=58.0,
            atr=1.0,
            atr_pct=1.0,
            volume_ratio=1.3,
            support=96.0,
            resistance=100.2,
        )
        digest = NewsDigest(items=[], by_symbol_score={"BTC": 3.0}, by_symbol_count={"BTC": 2})
        layers = {
            "BTC/USDT:USDT": {
                "layer_5m": {
                    "sample_count": 5,
                    "action": "avoid_new_entry",
                    "risk_score": 7.5,
                    "alert_count": 2,
                    "max_volume_ratio": 3.2,
                    "window_move_pct": -1.1,
                },
                "layer_20m": {
                    "sample_count": 20,
                    "action": "wait_confirmation",
                    "risk_score": 4.2,
                    "alert_count": 3,
                    "max_volume_ratio": 2.7,
                    "window_move_pct": -0.8,
                    "direction": "down",
                },
            }
        }

        baseline = build_candidates(config, [snapshot], digest)
        guarded = build_candidates(config, [snapshot], digest, market_layers=layers)

        self.assertLess(guarded[0].confidence, baseline[0].confidence)
        self.assertTrue(any("Market guard" in warning for warning in guarded[0].warnings))

    def test_higher_timeframe_context_boosts_aligned_long(self) -> None:
        config = {
            "risk": {"order_usdt": 20},
            "strategy": {
                "min_risk_reward": 2.0,
                "confirmation_timeframes": {
                    "enabled": True,
                    "weights": {"1h": 5, "4h": 9},
                },
            },
        }
        snapshot = MarketSnapshot(
            symbol="BTC/USDT:USDT",
            timestamp=datetime.now(timezone.utc),
            last=100.0,
            bid=99.9,
            ask=100.1,
            spread_pct=0.2,
            ema_fast=101.0,
            ema_slow=100.5,
            rsi=57.0,
            atr=1.0,
            atr_pct=1.0,
            volume_ratio=1.2,
            support=96.0,
            resistance=101.0,
        )
        confirmed = MarketSnapshot(
            **{
                **snapshot.__dict__,
                "higher_timeframes": {
                    "1h": {
                        "trend": "up",
                        "ema_gap_pct": 0.8,
                        "price_vs_ema_slow_pct": 1.4,
                        "rsi": 62.0,
                        "range_position": 0.65,
                    },
                    "4h": {
                        "trend": "up",
                        "ema_gap_pct": 1.1,
                        "price_vs_ema_slow_pct": 2.0,
                        "rsi": 66.0,
                        "range_position": 0.7,
                    },
                },
            }
        )
        digest = NewsDigest(items=[], by_symbol_score={"BTC": 2.0}, by_symbol_count={"BTC": 1})

        baseline = build_candidates(config, [snapshot], digest)
        with_frames = build_candidates(config, [confirmed], digest)

        self.assertEqual(with_frames[0].side, "long")
        self.assertGreater(with_frames[0].confidence, baseline[0].confidence)
        self.assertTrue(any("4H trend confirms long" in reason for reason in with_frames[0].reasons))

    def test_candlestick_patterns_boost_aligned_side(self) -> None:
        config = {
            "risk": {"order_usdt": 20},
            "strategy": {
                "min_risk_reward": 2.0,
                "candlestick_patterns": {
                    "enabled": True,
                    "weights": {"1m": 3, "15m": 6, "1h": 9},
                },
            },
        }
        snapshot = MarketSnapshot(
            symbol="BTC/USDT:USDT",
            timestamp=datetime.now(timezone.utc),
            last=100.0,
            bid=99.9,
            ask=100.1,
            spread_pct=0.2,
            ema_fast=101.0,
            ema_slow=100.5,
            rsi=57.0,
            atr=1.0,
            atr_pct=1.0,
            volume_ratio=1.2,
            support=96.0,
            resistance=101.0,
        )
        patterned = MarketSnapshot(
            **{
                **snapshot.__dict__,
                "candlestick_patterns": {
                    "1m": {
                        "patterns": ["bullish_engulfing"],
                        "bullish_score": 3.0,
                        "bearish_score": 0.0,
                        "direction": "bullish",
                    },
                },
                "higher_timeframes": {
                    "5m": {
                        "trend": "up",
                        "ema_gap_pct": 0.5,
                        "price_vs_ema_slow_pct": 1.0,
                        "rsi": 60.0,
                        "range_position": 0.6,
                        "candlestick_patterns": {
                            "patterns": ["morning_star"],
                            "bullish_score": 3.5,
                            "bearish_score": 0.0,
                            "direction": "bullish",
                        },
                    }
                },
            }
        )
        digest = NewsDigest(items=[], by_symbol_score={"BTC": 1.0}, by_symbol_count={"BTC": 1})

        baseline = build_candidates(config, [snapshot], digest)
        with_patterns = build_candidates(config, [patterned], digest)

        self.assertEqual(with_patterns[0].side, "long")
        self.assertGreater(with_patterns[0].confidence, baseline[0].confidence)
        self.assertTrue(any("candlestick supports LONG" in reason for reason in with_patterns[0].reasons))

    def test_15m_reversal_patterns_help_broader_frame_analysis(self) -> None:
        config = {
            "risk": {"order_usdt": 20},
            "strategy": {
                "min_risk_reward": 2.0,
                "confirmation_timeframes": {
                    "enabled": True,
                    "weights": {"15m": 4, "1h": 7},
                },
                "candlestick_patterns": {
                    "enabled": True,
                    "weights": {"15m": 6, "1h": 9},
                },
            },
        }
        snapshot = MarketSnapshot(
            symbol="BTC/USDT:USDT",
            timestamp=datetime.now(timezone.utc),
            last=100.0,
            bid=99.9,
            ask=100.1,
            spread_pct=0.2,
            ema_fast=100.8,
            ema_slow=100.3,
            rsi=55.0,
            atr=1.0,
            atr_pct=1.0,
            volume_ratio=1.15,
            support=96.0,
            resistance=101.0,
            higher_timeframes={
                "15m": {
                    "trend": "down",
                    "ema_gap_pct": -0.3,
                    "price_vs_ema_slow_pct": -0.4,
                    "rsi": 39.0,
                    "range_position": 0.28,
                    "candlestick_patterns": {
                        "patterns": ["hammer", "bullish_pin_bar"],
                        "bullish_score": 3.2,
                        "bearish_score": 0.0,
                        "direction": "bullish",
                        "trend_context": "down",
                        "strongest_pattern": "hammer",
                        "signal_summary": "hammer supports a bullish read against down trend context",
                    },
                },
                "1h": {
                    "trend": "up",
                    "ema_gap_pct": 0.8,
                    "price_vs_ema_slow_pct": 1.1,
                    "rsi": 60.0,
                    "range_position": 0.58,
                    "candlestick_patterns": {
                        "patterns": ["morning_star"],
                        "bullish_score": 3.5,
                        "bearish_score": 0.0,
                        "direction": "bullish",
                        "trend_context": "down",
                        "strongest_pattern": "morning_star",
                        "signal_summary": "morning star supports a bullish read against down trend context",
                    },
                },
            },
        )
        digest = NewsDigest(items=[], by_symbol_score={"BTC": 1.0}, by_symbol_count={"BTC": 1})

        candidates = build_candidates(config, [snapshot], digest)

        self.assertEqual(candidates[0].side, "long")
        self.assertTrue(any("15M candlestick supports LONG" in reason for reason in candidates[0].reasons))
        self.assertTrue(any("15M candlestick lesson" in reason for reason in candidates[0].reasons))
