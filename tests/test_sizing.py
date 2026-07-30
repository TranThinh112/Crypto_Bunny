from __future__ import annotations

import tempfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch

from crypto_trader.config import DEFAULT_CONFIG
from crypto_trader.models import TradeCandidate
from crypto_trader.sizing import apply_position_sizing, rebuild_recovery_cycle_state
from crypto_trader.storage import get_journal_state, set_journal_state


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

class RawHistoryExchange(FakeExchange):
    def __init__(self, rows: list[dict], raw_rows: list[dict]) -> None:
        super().__init__(rows)
        self.raw_rows = raw_rows

    def privateGetAccountPositionsHistory(self, params: dict[str, object]) -> dict[str, object]:
        return {"data": self.raw_rows}


class SizingTest(TestCase):
    def _config(self) -> dict:
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        config = deepcopy(DEFAULT_CONFIG)
        config["_config_dir"] = self.tmpdir.name
        config["mode"] = "demo"
        config["_atlas_test_mode"] = True
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
                "hard_loss_streak_threshold": 2,
                "hard_loss_usdt_threshold": 10,
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

    def test_recovery_cycle_enters_hard_after_ten_usdt_loss(self) -> None:
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

        self.assertTrue(result["blocked"])
        self.assertEqual(result["recovery_band"], "hard")
        self.assertAlmostEqual(result["cycle_pnl_usdt"], -20.0, places=6)
        self.assertAlmostEqual(result["hard_peak_loss_usdt"], -20.0, places=6)
        self.assertAlmostEqual(result["soft_return_pnl_usdt"], -10.0, places=6)
        self.assertEqual(result["margin_usdt"], 0.0)

    def test_recovery_cycle_enters_hard_after_two_consecutive_losses(self) -> None:
        config = self._config()
        config["position_sizing"]["max_margin_usdt"] = 50
        config["position_sizing"]["max_cycle_loss_usdt"] = 50
        rows = [
            {
                "symbol": "BTC/USDT:USDT",
                "id": "loss-1",
                "side": "short",
                "pnl": -3.0,
                "timestamp": int((datetime.now(timezone.utc) - timedelta(minutes=2)).timestamp() * 1000),
            },
            {
                "symbol": "ETH/USDT:USDT",
                "id": "loss-2",
                "side": "long",
                "pnl": -4.0,
                "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
            },
        ]
        candidates = [_candidate("SOL/USDT:USDT", "long")]

        with patch("crypto_trader.sizing.create_exchange", return_value=FakeExchange(rows)):
            result = apply_position_sizing(config, candidates)

        self.assertTrue(result["blocked"])
        self.assertEqual(result["recovery_band"], "hard")
        self.assertEqual(result["loss_streak"], 2)
        self.assertAlmostEqual(result["cycle_pnl_usdt"], -7.0, places=6)
        self.assertAlmostEqual(result["soft_return_pnl_usdt"], -3.5, places=6)

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

    def test_hard_recovery_returns_to_soft_after_recovering_half_peak_loss(self) -> None:
        config = self._config()
        config["position_sizing"]["reset_orphaned_blocked_state"] = False
        set_journal_state(
            config,
            "position_sizing:recovery_cycle",
            (
                '{"cycle_pnl_usdt": -20.0, "recovery_step": 4, '
                '"recovery_band": "hard", "next_margin_usdt": 0.0, '
                '"processed_keys": ["old"], "processed_pnl_by_key": {}, '
                '"blocked": true, "block_reason": "Recovery step limit reached: 4/4", '
                '"hard_start_pnl_usdt": -20.0, "hard_peak_loss_usdt": -20.0, '
                '"soft_return_pnl_usdt": -10.0}'
            ),
        )
        row = {
            "symbol": "ETH/USDT:USDT",
            "id": "half-recovered",
            "side": "long",
            "pnl": 11.0,
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
        candidates = [_candidate("SOL/USDT:USDT", "long")]

        with patch("crypto_trader.sizing.create_exchange", return_value=FakeExchange([row])):
            result = apply_position_sizing(config, candidates)

        self.assertFalse(result["blocked"])
        self.assertEqual(result["recovery_band"], "soft")
        self.assertEqual(result["recovery_step"], 3)
        self.assertAlmostEqual(result["cycle_pnl_usdt"], -9.0, places=6)
        self.assertAlmostEqual(result["soft_return_pnl_usdt"], -10.0, places=6)

    def test_hard_recovery_stays_hard_until_half_loss_is_recovered(self) -> None:
        config = self._config()
        config["position_sizing"]["reset_orphaned_blocked_state"] = False
        config["position_sizing"]["max_cycle_loss_usdt"] = 50
        set_journal_state(
            config,
            "position_sizing:recovery_cycle",
            (
                '{"cycle_pnl_usdt": -20.0, "recovery_step": 4, '
                '"recovery_band": "hard", "next_margin_usdt": 0.0, '
                '"processed_keys": ["old"], "processed_pnl_by_key": {}, '
                '"blocked": true, "block_reason": "Hard recovery triggered by loss streak", '
                '"hard_start_pnl_usdt": -20.0, "hard_peak_loss_usdt": -20.0, '
                '"soft_return_pnl_usdt": -10.0}'
            ),
        )
        row = {
            "symbol": "ETH/USDT:USDT",
            "id": "small-hard-win",
            "side": "long",
            "pnl": 5.0,
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
        candidates = [_candidate("SOL/USDT:USDT", "long")]

        with patch("crypto_trader.sizing.create_exchange", return_value=FakeExchange([row])):
            result = apply_position_sizing(config, candidates)

        self.assertTrue(result["blocked"])
        self.assertEqual(result["recovery_band"], "hard")
        self.assertAlmostEqual(result["cycle_pnl_usdt"], -15.0, places=6)
        self.assertAlmostEqual(result["soft_return_pnl_usdt"], -10.0, places=6)
        self.assertIn("50% of the hard loss", result["block_reason"])

    def test_hard_recovery_updates_peak_loss_when_drawdown_gets_deeper(self) -> None:
        config = self._config()
        config["position_sizing"]["reset_orphaned_blocked_state"] = False
        set_journal_state(
            config,
            "position_sizing:recovery_cycle",
            (
                '{"cycle_pnl_usdt": -20.0, "recovery_step": 4, '
                '"recovery_band": "hard", "next_margin_usdt": 0.0, '
                '"processed_keys": ["old"], "processed_pnl_by_key": {}, '
                '"blocked": true, "block_reason": "Recovery step limit reached: 4/4", '
                '"hard_start_pnl_usdt": -20.0, "hard_peak_loss_usdt": -20.0, '
                '"soft_return_pnl_usdt": -10.0}'
            ),
        )
        row = {
            "symbol": "ETH/USDT:USDT",
            "id": "deeper-loss",
            "side": "long",
            "pnl": -10.0,
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
        candidates = [_candidate("SOL/USDT:USDT", "long")]

        with patch("crypto_trader.sizing.create_exchange", return_value=FakeExchange([row])):
            result = apply_position_sizing(config, candidates)

        self.assertTrue(result["blocked"])
        self.assertEqual(result["recovery_band"], "hard")
        self.assertAlmostEqual(result["cycle_pnl_usdt"], -30.0, places=6)
        self.assertAlmostEqual(result["hard_peak_loss_usdt"], -30.0, places=6)
        self.assertAlmostEqual(result["soft_return_pnl_usdt"], -15.0, places=6)

    def test_soft_recovery_after_half_recovery_does_not_reblock_at_step_limit(self) -> None:
        config = self._config()
        config["position_sizing"]["reset_orphaned_blocked_state"] = False
        set_journal_state(
            config,
            "position_sizing:recovery_cycle",
            (
                '{"cycle_pnl_usdt": -9.0, "recovery_step": 4, '
                '"recovery_band": "soft", "next_margin_usdt": 0.0, '
                '"processed_keys": ["old"], "processed_pnl_by_key": {}, '
                '"blocked": false, "block_reason": null, '
                '"hard_start_pnl_usdt": -20.0, "hard_peak_loss_usdt": -20.0, '
                '"soft_return_pnl_usdt": -10.0, '
                '"hard_soft_recovered_at": "2026-07-20T01:00:00+00:00"}'
            ),
        )
        row = {
            "symbol": "ETH/USDT:USDT",
            "id": "soft-small-loss",
            "side": "long",
            "pnl": -0.2,
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
        candidates = [_candidate("SOL/USDT:USDT", "long")]

        with patch("crypto_trader.sizing.create_exchange", return_value=FakeExchange([row])):
            result = apply_position_sizing(config, candidates)

        self.assertFalse(result["blocked"])
        self.assertEqual(result["recovery_band"], "soft")
        self.assertAlmostEqual(result["cycle_pnl_usdt"], -9.2, places=6)
        self.assertAlmostEqual(result["hard_peak_loss_usdt"], -20.0, places=6)

    def test_configured_cycle_start_rebuilds_legacy_blocked_state_from_okx_history(self) -> None:
        config = self._config()
        start_at = datetime(2026, 7, 5, tzinfo=timezone.utc)
        config["position_sizing"]["cycle_start_at"] = start_at.isoformat()
        config["position_sizing"]["target_profit_usdt"] = 30.0
        config["position_sizing"]["max_margin_usdt"] = 100
        config["position_sizing"]["max_cycle_loss_usdt"] = 50
        set_journal_state(
            config,
            "position_sizing:recovery_cycle",
            (
                '{"cycle_pnl_usdt": -117.324218, "recovery_step": 4, '
                '"next_margin_usdt": 0.0, "processed_keys": ["legacy"], '
                '"processed_pnl_by_key": {}, "blocked": true, '
                '"block_reason": "Recovery step limit reached: 4/4"}'
            ),
        )
        old_row = {
            "symbol": "SOL/USDT:USDT",
            "id": "before-cycle",
            "pnl": -100.0,
            "timestamp": int((start_at - timedelta(days=1)).timestamp() * 1000),
        }
        cycle_row = {
            "symbol": "HYPE/USDT:USDT",
            "id": "inside-cycle",
            "side": "short",
            "realizedPnl": "-17.822675",
            "timestamp": int((start_at + timedelta(days=1)).timestamp() * 1000),
        }
        candidates = [_candidate("ETH/USDT:USDT", "long")]

        with patch("crypto_trader.sizing.create_exchange", return_value=FakeExchange([old_row, cycle_row])):
            result = apply_position_sizing(config, candidates)

        self.assertTrue(result["blocked"])
        self.assertEqual(result["recovery_band"], "hard")
        self.assertAlmostEqual(result["cycle_pnl_usdt"], -17.822675, places=6)
        self.assertAlmostEqual(result["soft_return_pnl_usdt"], -8.911338, places=6)
        self.assertEqual(result["cycle_start_at"], start_at.isoformat())
        state_raw = get_journal_state(config, "position_sizing:recovery_cycle") or "{}"
        self.assertIn(start_at.isoformat(), state_raw)
        self.assertNotIn("before-cycle", state_raw)
        self.assertNotIn("_bootstrap_configured_history", state_raw)

    def test_recovery_cycle_uses_okx_realized_pnl_after_fees_and_funding(self) -> None:
        config = self._config()
        config["position_sizing"]["target_profit_usdt"] = 10.0
        row = {
            "symbol": "XAU/USDT:USDT",
            "id": "xau-win-1",
            "side": "long",
            "pnl": "3.77",
            "realizedPnl": "3.29160045",
            "fundingFee": "-0.32452",
            "fee": "-0.15387955",
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
        candidates = [_candidate("ETH/USDT:USDT", "long")]

        with patch("crypto_trader.sizing.create_exchange", return_value=FakeExchange([row])):
            result = apply_position_sizing(config, candidates)

        self.assertAlmostEqual(result["cycle_pnl_usdt"], 3.29160045, places=6)
        self.assertAlmostEqual(result["last_realized_net_pnl"], 3.29160045, places=6)

    def test_rebuild_recovery_cycle_uses_okx_realized_pnl_for_tao(self) -> None:
        config = self._config()
        config["position_sizing"]["target_profit_usdt"] = 30.0
        raw_tao = {
            "instId": "TAO-USDT-SWAP",
            "posId": "3740224303088656384",
            "direction": "long",
            "realizedPnl": "-13.7361275492279702",
            "pnl": "-13.2400000000000003",
            "fee": "-0.281165",
            "fundingFee": "-0.2149625492279699",
            "uTime": int(datetime.now(timezone.utc).timestamp() * 1000),
        }

        with patch("crypto_trader.sizing.create_exchange", return_value=RawHistoryExchange([], [raw_tao])):
            result = rebuild_recovery_cycle_state(config)

        self.assertEqual(result["closed_count"], 1)
        self.assertAlmostEqual(result["state"]["cycle_pnl_usdt"], -13.7361275492279702, places=6)
        self.assertTrue(any(key.startswith("TAO/USDT:USDT:3740224303088656384:") for key in result["state"]["processed_keys"]))
        state = get_journal_state(config, "position_sizing:recovery_cycle")
        self.assertIn("TAO/USDT:USDT:3740224303088656384:", state or "")

    def test_rebuild_recovery_cycle_prefers_raw_okx_history_without_merging_ccxt_history(self) -> None:
        config = self._config()
        config["position_sizing"]["target_profit_usdt"] = 30.0
        ccxt_duplicate_or_stale = {
            "symbol": "ETH/USDT:USDT",
            "id": "ccxt-stale",
            "pnl": "-900.0",
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
        raw_current = {
            "instId": "ETH-USDT-SWAP",
            "posId": "raw-current",
            "direction": "long",
            "realizedPnl": "-20.84",
            "uTime": int(datetime.now(timezone.utc).timestamp() * 1000),
        }

        with patch(
            "crypto_trader.sizing.create_exchange",
            return_value=RawHistoryExchange([ccxt_duplicate_or_stale], [raw_current]),
        ):
            result = rebuild_recovery_cycle_state(config)

        self.assertEqual(result["closed_count"], 1)
        self.assertAlmostEqual(result["state"]["cycle_pnl_usdt"], -20.84, places=6)
        state = get_journal_state(config, "position_sizing:recovery_cycle") or ""
        self.assertNotIn("ccxt-stale", state)

    def test_rebuild_recovery_cycle_includes_partial_okx_position_closes_within_cycle_window(self) -> None:
        config = self._config()
        partial_row = {
            "instId": "ETH-USDT-SWAP",
            "posId": "partial-1",
            "direction": "long",
            "realizedPnl": "-4.9321185217422645",
            "openMaxPos": "0.69",
            "closeTotalPos": "0.56",
            "uTime": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
        full_row = {
            "instId": "LAB-USDT-SWAP",
            "posId": "full-1",
            "direction": "short",
            "realizedPnl": "0.2962974612276823",
            "openMaxPos": "12.9",
            "closeTotalPos": "12.9",
            "uTime": int(datetime.now(timezone.utc).timestamp() * 1000),
        }

        with patch("crypto_trader.sizing.create_exchange", return_value=RawHistoryExchange([], [partial_row, full_row])):
            result = rebuild_recovery_cycle_state(config)

        self.assertEqual(result["closed_count"], 2)
        self.assertAlmostEqual(result["state"]["cycle_pnl_usdt"], -4.635822, places=6)

    def test_recovery_cycle_blocks_when_cycle_loss_limit_is_reached(self) -> None:
        config = self._config()
        config["position_sizing"]["hard_loss_usdt_threshold"] = 0
        config["position_sizing"]["hard_loss_streak_threshold"] = 0
        row = {
            "symbol": "BTC/USDT:USDT",
            "id": "loss-big",
            "pnl": -10.5,
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
        candidates = [_candidate()]

        with patch("crypto_trader.sizing.create_exchange", return_value=FakeExchange([row])):
            result = apply_position_sizing(config, candidates)

        self.assertTrue(result["blocked"])
        self.assertEqual(result["margin_usdt"], 0.0)
        self.assertEqual(candidates[0].order_usdt, 0.0)
        self.assertIn("cycle loss limit", result["block_reason"])

    def test_recovery_cycle_blocks_when_required_margin_exceeds_cap(self) -> None:
        config = self._config()
        config["position_sizing"]["max_cycle_loss_usdt"] = 50
        config["position_sizing"]["max_margin_usdt"] = 5
        config["position_sizing"]["hard_loss_usdt_threshold"] = 0
        config["position_sizing"]["hard_loss_streak_threshold"] = 0
        row = {
            "symbol": "BTC/USDT:USDT",
            "id": "loss-needs-large-margin",
            "pnl": -10.5,
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
        candidates = [_candidate()]

        with patch("crypto_trader.sizing.create_exchange", return_value=FakeExchange([row])):
            result = apply_position_sizing(config, candidates)

        self.assertTrue(result["blocked"])
        self.assertEqual(result["margin_usdt"], 0.0)
        self.assertEqual(candidates[0].order_usdt, 0.0)
        self.assertIn("margin limit", result["block_reason"])

    def test_recovery_cycle_final_guard_blocks_stale_state_over_loss_limit(self) -> None:
        config = self._config()
        config["position_sizing"]["reset_orphaned_blocked_state"] = False
        set_journal_state(
            config,
            "position_sizing:recovery_cycle",
            (
                '{"cycle_pnl_usdt": -910.0, "recovery_step": 18, '
                '"recovery_band": "soft", "next_margin_usdt": 1292.0, '
                '"processed_keys": ["old"], "processed_pnl_by_key": {}, '
                '"blocked": false, "block_reason": null}'
            ),
        )
        candidates = [_candidate()]

        with patch("crypto_trader.sizing.create_exchange", return_value=FakeExchange([])):
            result = apply_position_sizing(config, candidates)

        self.assertTrue(result["blocked"])
        self.assertEqual(result["margin_usdt"], 0.0)
        self.assertEqual(candidates[0].order_usdt, 0.0)
        self.assertIn("cycle loss limit", result["block_reason"])

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

    def test_orphaned_blocked_state_resets_to_base_margin(self) -> None:
        config = self._config()
        config["position_sizing"]["bootstrap_existing_history"] = False
        set_journal_state(
            config,
            "position_sizing:recovery_cycle",
            (
                '{"cycle_pnl_usdt": -222.396962, "recovery_step": 4, '
                '"next_margin_usdt": 0.0, "processed_keys": ["old"], '
                '"blocked": true, "block_reason": "Recovery step limit reached: 4/4"}'
            ),
        )
        closed_history = [
            {
                "symbol": "OP/USDT:USDT",
                "id": "old-loss",
                "side": "long",
                "pnl": -1.73,
                "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
            }
        ]
        candidates = [_candidate("ETH/USDT:USDT", "long")]

        with patch("crypto_trader.sizing.storage_stats", return_value={"row_counts": {}}), patch(
            "crypto_trader.sizing.create_exchange", return_value=FakeExchange(closed_history)
        ):
            result = apply_position_sizing(config, candidates)

        self.assertFalse(result["blocked"])
        self.assertEqual(result["recovery_step"], 0)
        self.assertEqual(result["margin_usdt"], 2.0)
        self.assertEqual(candidates[0].order_usdt, 50.0)
