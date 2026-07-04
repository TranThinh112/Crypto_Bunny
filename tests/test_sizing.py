from __future__ import annotations

import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import patch

from crypto_trader.config import DEFAULT_CONFIG
from crypto_trader.models import TradeCandidate
from crypto_trader.sizing import apply_position_sizing


def _candidate(
    symbol: str = "BTC/USDT:USDT",
    side: str = "long",
    confidence: float = 92.0,
    win_probability_pct: float = 62.0,
) -> TradeCandidate:
    base = symbol.split("/", 1)[0]
    return TradeCandidate(
        symbol=symbol,
        base=base,
        side=side,  # type: ignore[arg-type]
        confidence=confidence,
        entry=100.0,
        stop_loss=98.0,
        take_profit=103.0,
        risk_reward=1.5,
        order_usdt=20.0,
        quantity=1.0,
        spread_pct=0.01,
        news_score=0.0,
        news_count=1,
        win_probability_pct=win_probability_pct,
    )


class FakeExchange:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def load_markets(self) -> None:
        return None

    def fetch_positions_history(self, *_args) -> list[dict]:
        return self.rows


class SizingTest(TestCase):
    def _config(self) -> dict:
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        config = deepcopy(DEFAULT_CONFIG)
        config["_config_dir"] = self.tmpdir.name
        config["mode"] = "demo"
        config["state_db_path"] = "state.sqlite"
        config["exchange"]["leverage"] = 25
        config["position_sizing"].update(
            {
                "enabled": True,
                "bootstrap_existing_history": True,
                "base_margin_usdt": 2.0,
                "target_profit_usdt": 0.30,
                "tp_roi": 0.75,
                "open_fee": 0.0005,
                "close_fee": 0.0005,
                "safety_buffer": 0.02,
                "max_recovery_step": 4,
                "max_margin_usdt": 20,
                "max_cycle_loss_usdt": 10,
                "min_recovery_confidence": 88,
                "min_recovery_win_probability_pct": 58,
                "block_recovery_on_market_guard": True,
                "block_recovery_same_symbol_side": True,
                "max_recovery_4h_rsi_long": 76,
                "min_recovery_4h_rsi_short": 24,
            }
        )
        return config

    def tearDown(self) -> None:
        tmpdir = getattr(self, "tmpdir", None)
        if tmpdir:
            tmpdir.cleanup()

    def test_recovery_cycle_sizes_next_order_from_realized_loss(self) -> None:
        config = self._config()
        row = {
            "symbol": "BTC/USDT:USDT",
            "id": "loss-1",
            "side": "short",
            "pnl": -2.0,
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
        candidates = [_candidate("ETH/USDT:USDT", "long")]

        with patch("crypto_trader.sizing.create_exchange", return_value=FakeExchange([row])):
            result = apply_position_sizing(config, candidates)

        expected_net_tp = 0.75 - 0.0005 * 25 - 0.0005 * 25 * (1 + 0.75 / 25) - 0.02
        expected_margin = (0.30 - (-2.0)) / expected_net_tp
        self.assertAlmostEqual(result["margin_usdt"], expected_margin, places=3)
        self.assertEqual(result["recovery_step"], 1)
        self.assertAlmostEqual(candidates[0].margin_usdt or 0, expected_margin, places=3)
        self.assertAlmostEqual(candidates[0].order_usdt, expected_margin * 25, places=2)
        self.assertTrue(result["recovery_guard_active"])
        self.assertFalse(result["blocked_candidates"])

    def test_recovery_cycle_follows_formula_after_twenty_usdt_loss_when_caps_allow_it(self) -> None:
        config = self._config()
        config["position_sizing"]["max_margin_usdt"] = 50
        config["position_sizing"]["max_cycle_loss_usdt"] = 50
        row = {
            "symbol": "BTC/USDT:USDT",
            "id": "loss-20",
            "side": "short",
            "pnl": -20.0,
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
        candidates = [_candidate("ETH/USDT:USDT", "long")]

        with patch("crypto_trader.sizing.create_exchange", return_value=FakeExchange([row])):
            result = apply_position_sizing(config, candidates)

        expected_net_tp = 0.75 - 0.0005 * 25 - 0.0005 * 25 * (1 + 0.75 / 25) - 0.02
        expected_margin = (0.30 - (-20.0)) / expected_net_tp
        self.assertFalse(result["blocked"])
        self.assertAlmostEqual(result["margin_usdt"], expected_margin, places=3)
        self.assertAlmostEqual(candidates[0].order_usdt, expected_margin * 25, places=2)

    def test_recovery_cycle_resets_after_target_profit(self) -> None:
        config = self._config()
        row = {
            "symbol": "BTC/USDT:USDT",
            "id": "win-1",
            "pnl": 0.5,
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
        candidates = [_candidate()]

        with patch("crypto_trader.sizing.create_exchange", return_value=FakeExchange([row])):
            result = apply_position_sizing(config, candidates)

        self.assertEqual(result["cycle_pnl_usdt"], 0.0)
        self.assertEqual(result["recovery_step"], 0)
        self.assertEqual(result["margin_usdt"], 2.0)

    def test_recovery_cycle_ignores_cycle_loss_and_max_margin_caps(self) -> None:
        config = self._config()
        row = {
            "symbol": "BTC/USDT:USDT",
            "id": "loss-big",
            "pnl": -10.5,
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
        candidates = [_candidate()]

        with patch("crypto_trader.sizing.create_exchange", return_value=FakeExchange([row])):
            result = apply_position_sizing(config, candidates)

        expected_net_tp = 0.75 - 0.0005 * 25 - 0.0005 * 25 * (1 + 0.75 / 25) - 0.02
        expected_margin = (0.30 - (-10.5)) / expected_net_tp
        self.assertFalse(result["blocked"])
        self.assertAlmostEqual(result["margin_usdt"], expected_margin, places=3)
        self.assertAlmostEqual(candidates[0].margin_usdt or 0, expected_margin, places=3)
        self.assertAlmostEqual(candidates[0].order_usdt, expected_margin * 25, places=2)

    def test_recovery_guard_blocks_same_symbol_and_side_after_loss(self) -> None:
        config = self._config()
        row = {
            "symbol": "BTC/USDT:USDT",
            "id": "loss-same-side",
            "side": "long",
            "pnl": -2.0,
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
        candidates = [_candidate("BTC/USDT:USDT", "long")]

        with patch("crypto_trader.sizing.create_exchange", return_value=FakeExchange([row])):
            result = apply_position_sizing(config, candidates)

        self.assertFalse(result["blocked"])
        self.assertEqual(candidates[0].order_usdt, 0.0)
        self.assertEqual(candidates[0].confidence, 0.0)
        self.assertEqual(result["blocked_candidates"][0]["symbol"], "BTC/USDT:USDT")
        self.assertTrue(any("Last loss" in reason for reason in result["blocked_candidates"][0]["reasons"]))

    def test_recovery_guard_blocks_market_guard_warning(self) -> None:
        config = self._config()
        row = {
            "symbol": "BTC/USDT:USDT",
            "id": "loss-market-guard",
            "side": "short",
            "pnl": -2.0,
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
        candidate = _candidate("ETH/USDT:USDT", "long")
        candidate.warnings.append("Market guard 5m: action=avoid_new_entry, risk=8.0")

        with patch("crypto_trader.sizing.create_exchange", return_value=FakeExchange([row])):
            result = apply_position_sizing(config, [candidate])

        self.assertEqual(candidate.order_usdt, 0.0)
        self.assertTrue(any("Market Guard" in reason for reason in result["blocked_candidates"][0]["reasons"]))

    def test_recovery_guard_blocks_hot_4h_rsi_for_long(self) -> None:
        config = self._config()
        row = {
            "symbol": "BTC/USDT:USDT",
            "id": "loss-hot-rsi",
            "side": "short",
            "pnl": -2.0,
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
        candidate = _candidate("ETH/USDT:USDT", "long")
        candidate.higher_timeframes = {"4h": {"rsi": 80.0}}

        with patch("crypto_trader.sizing.create_exchange", return_value=FakeExchange([row])):
            result = apply_position_sizing(config, [candidate])

        self.assertEqual(candidate.order_usdt, 0.0)
        self.assertTrue(any("4H RSI" in reason for reason in result["blocked_candidates"][0]["reasons"]))
