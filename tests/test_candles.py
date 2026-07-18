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

    def test_detects_spinning_top_as_indecision(self) -> None:
        ohlcv = [
            [1, 100, 102, 99, 101, 10],
            [2, 101, 103, 100, 102, 11],
            [3, 102, 104, 101, 103, 12],
            [4, 103, 105, 102, 104, 13],
            [5, 104, 107, 101, 105.2, 14],
        ]

        result = detect_candlestick_patterns(ohlcv)

        self.assertIn("spinning_top", result["patterns"])
        self.assertIn("spinning_top", result["indecision_patterns"])
        self.assertEqual(result["direction"], "neutral")

    def test_detects_bullish_kicker(self) -> None:
        ohlcv = [
            [1, 110, 111, 108, 109, 10],
            [2, 109, 109.5, 106, 107, 11],
            [3, 107, 107.5, 104, 105, 12],
            [4, 105, 105.2, 100.5, 101, 13],
            [5, 106, 110, 105.8, 109, 18],
        ]

        result = detect_candlestick_patterns(ohlcv)

        self.assertIn("bullish_kicker", result["patterns"])
        self.assertIn("bullish_kicker", result["reversal_patterns"]["bullish"])
        self.assertGreater(result["bullish_score"], result["bearish_score"])

    def test_detects_three_inside_up(self) -> None:
        ohlcv = [
            [1, 112, 113, 109, 110, 10],
            [2, 110, 111, 106, 107, 11],
            [3, 107, 108, 101, 102, 12],
            [4, 102.5, 105, 102, 104, 13],
            [5, 104.5, 108.5, 104, 108.2, 15],
        ]

        result = detect_candlestick_patterns(ohlcv)

        self.assertIn("three_inside_up", result["patterns"])
        self.assertIn("three_inside_up", result["reversal_patterns"]["bullish"])

    def test_detects_rising_three_methods(self) -> None:
        ohlcv = [
            [1, 100, 111, 99, 110, 20],
            [2, 109, 110, 106, 107, 12],
            [3, 107, 108.5, 105.5, 106.5, 11],
            [4, 106.5, 108, 105.8, 107.2, 10],
            [5, 107.5, 113, 107, 112, 22],
        ]

        result = detect_candlestick_patterns(ohlcv)

        self.assertIn("rising_three_methods", result["patterns"])
        self.assertIn("rising_three_methods", result["continuation_patterns"])
        self.assertGreater(result["bullish_score"], result["bearish_score"])
        self.assertIn("reference_sources", result)
