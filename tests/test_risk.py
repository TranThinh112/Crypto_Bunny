from __future__ import annotations

import tempfile
from copy import deepcopy
from unittest import TestCase

from crypto_trader.codex_features import get_trading_system_state
from crypto_trader.config import DEFAULT_CONFIG
from crypto_trader.models import TradeCandidate
from crypto_trader.risk import evaluate_candidate


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


class RiskTest(TestCase):
    def _config(self) -> dict:
        config = deepcopy(DEFAULT_CONFIG)
        config["mode"] = "demo"
        config["strategy"]["min_confidence"] = 75
        config["strategy"]["min_risk_reward"] = 1.5
        config["news"]["require_symbol_news"] = False
        config["risk"]["max_active_trades"] = 5
        config["risk"]["max_daily_orders"] = 100
        config["risk"]["max_daily_planned_risk_usdt"] = 1000
        config["risk"]["cooldown_minutes"] = 0
        self.tmpdir = tempfile.TemporaryDirectory()
        config["_config_dir"] = self.tmpdir.name
        config["ledger_path"] = "empty-ledger.jsonl"
        return config

    def tearDown(self) -> None:
        tmpdir = getattr(self, "tmpdir", None)
        if tmpdir:
            tmpdir.cleanup()

    def test_blocks_any_existing_symbol_even_when_side_differs(self) -> None:
        check = evaluate_candidate(
            self._config(),
            _candidate(symbol="BTC/USDT:USDT", side="short"),
            active_summary=(1, {"BTC/USDT:USDT"}, []),
            enforce_active_limit=False,
        )

        self.assertFalse(check.passed)
        self.assertIn("Active OKX position/order already exists for BTC/USDT:USDT", check.reasons)

    def test_pending_can_ignore_only_active_limit_for_new_symbol(self) -> None:
        config = self._config()
        candidate = _candidate(symbol="ETH/USDT:USDT", side="long")

        market_check = evaluate_candidate(config, candidate, active_summary=(5, set(), []))
        pending_check = evaluate_candidate(
            config,
            candidate,
            active_summary=(5, set(), []),
            enforce_active_limit=False,
        )

        self.assertFalse(market_check.passed)
        self.assertIn("Active trade limit reached: 5/5", market_check.reasons)
        self.assertTrue(pending_check.passed)

    def test_blocks_candidate_below_min_win_probability(self) -> None:
        config = self._config()
        config["strategy"]["min_win_probability_pct"] = 80
        candidate = _candidate()
        candidate.win_probability_pct = 76.5

        blocked = evaluate_candidate(config, candidate, active_summary=(0, set(), []))

        candidate.win_probability_pct = 82.0
        passed = evaluate_candidate(config, candidate, active_summary=(0, set(), []))

        self.assertFalse(blocked.passed)
        self.assertIn("Win probability 76.50% is below minimum 80.00%", blocked.reasons)
        self.assertTrue(passed.passed)

    def test_trading_system_prefers_risk_max_active_when_trading_risk_not_overridden(self) -> None:
        state = get_trading_system_state(self._config())

        self.assertEqual(state["maxConcurrentPositions"], 5)
        self.assertEqual(state["globalLossStreakThreshold"], 2)
        self.assertEqual(state["normalRiskPercent"], 1.0)
        self.assertEqual(state["recoveryModeRiskPercent"], 0.5)
        self.assertEqual(state["recoveryMinRiskReward"], 2.5)
