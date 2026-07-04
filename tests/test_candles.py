from __future__ import annotations

from unittest import TestCase

from crypto_trader.candles import detect_candlestick_patterns


class CandlePatternTest(TestCase):
    def test_detects_dragonfly_doji_with_learning_context(self) -> None:
        ohlcv = [
            [1, 110, 111, 108, 109, 10],
            [2, 109, 109.5, 106, 107, 11],
            [3, 107, 107.5, 104, 105, 12],
            [4, 105, 105.3, 102, 103, 13],
            [5, 103.0, 103.2, 99.0, 103.02, 15],
        ]

        result = detect_candlestick_patterns(ohlcv)

        self.assertIn("doji", result["patterns"])
        self.assertIn("dragonfly_doji", result["patterns"])
        self.assertTrue(any(item["name"] == "dragonfly_doji" for item in result["pattern_details"]))
        self.assertIn("bullish", result["signal_summary"])

    def test_detects_hanging_man_after_uptrend(self) -> None:
        ohlcv = [
            [1, 100, 102, 99, 101, 10],
            [2, 101, 104, 100, 103, 11],
            [3, 103, 106, 102, 105, 12],
            [4, 105, 107, 104, 106, 13],
            [5, 106, 107, 101, 105.8, 14],
        ]

        result = detect_candlestick_patterns(ohlcv)

        self.assertIn("hanging_man", result["patterns"])
        self.assertEqual(result["trend_context"], "up")
        self.assertIn("hanging_man", result["reversal_patterns"]["bearish"])
        self.assertGreater(result["bearish_score"], result["bullish_score"])

    def test_detects_inverted_hammer_after_downtrend(self) -> None:
        ohlcv = [
            [1, 110, 111, 108, 109, 10],
            [2, 109, 109.5, 106, 107, 11],
            [3, 107, 107.5, 104, 105, 12],
            [4, 105, 105.3, 102, 103, 13],
            [5, 103, 107, 102.7, 103.2, 14],
        ]

        result = detect_candlestick_patterns(ohlcv)

        self.assertIn("inverted_hammer", result["patterns"])
        self.assertEqual(result["trend_context"], "down")
        self.assertIn("inverted_hammer", result["reversal_patterns"]["bullish"])
        self.assertGreater(result["bullish_score"], 0)

    def test_detects_piercing_line_and_strongest_pattern(self) -> None:
        ohlcv = [
            [1, 106, 107, 104, 105, 10],
            [2, 105, 105.5, 101, 102, 11],
            [3, 102, 102.2, 97, 98, 12],
            [4, 98.1, 102.5, 97.8, 101.2, 13],
        ]

        result = detect_candlestick_patterns(ohlcv)

        self.assertIn("piercing_line", result["patterns"])
        self.assertEqual(result["strongest_pattern"], "piercing_line")
        self.assertIn("piercing_line", result["reversal_patterns"]["bullish"])
