from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

from crypto_trader.executor import candidate_client_order_id, execute_candidate
from crypto_trader.models import TradeCandidate


def _candidate() -> TradeCandidate:
    return TradeCandidate(
        symbol="BTC/USDT:USDT",
        base="BTC",
        side="long",
        confidence=88.0,
        entry=62000.0,
        stop_loss=61000.0,
        take_profit=64000.0,
        risk_reward=2.0,
        order_usdt=20.0,
        quantity=0.01,
        spread_pct=0.01,
        news_score=0.0,
        news_count=1,
        decision_metadata={"mini_setup": {"setup_id": "mini-btc-08"}},
    )


def _config() -> dict:
    return {
        "mode": "demo",
        "exchange": {
            "account_type": "spot",
            "td_mode": "isolated",
            "position_side_mode": "net",
            "leverage": 1,
        },
        "execution": {"order_type": "limit", "attach_tp_sl": False},
    }


class ExecutorTest(TestCase):
    @patch("crypto_trader.executor.record_trade_execution")
    @patch("crypto_trader.executor.append_event")
    @patch("crypto_trader.executor.create_exchange")
    def test_mini_order_uses_deterministic_okx_client_order_id(self, create_exchange, _append, _record) -> None:
        candidate = _candidate()
        exchange = create_exchange.return_value
        exchange.create_order.return_value = {"id": "okx-1"}

        first = execute_candidate(_config(), candidate, entry_type="mini_lc_okx")
        second_id = candidate_client_order_id(candidate, entry_type="mini_lc_okx")

        self.assertTrue(first.submitted)
        self.assertEqual(len(second_id or ""), 32)
        self.assertEqual(exchange.create_order.call_args.args[5]["clOrdId"], second_id)

    @patch("crypto_trader.executor.record_trade_execution")
    @patch("crypto_trader.executor.append_event")
    @patch("crypto_trader.executor.create_exchange")
    def test_timeout_recovers_order_by_client_order_id(self, create_exchange, _append, _record) -> None:
        candidate = _candidate()
        expected_client_id = candidate_client_order_id(candidate, entry_type="mini_lc_okx")
        exchange = create_exchange.return_value
        exchange.create_order.side_effect = TimeoutError("request timed out")
        exchange.fetch_order.return_value = {
            "id": "okx-recovered",
            "clientOrderId": expected_client_id,
        }

        result = execute_candidate(_config(), candidate, entry_type="mini_lc_okx")

        self.assertTrue(result.submitted)
        self.assertEqual(result.order_id, "okx-recovered")
        self.assertEqual((result.raw or {}).get("submission_status"), "recovered")

    @patch("crypto_trader.executor.create_exchange")
    def test_unresolved_timeout_is_marked_unknown_instead_of_hard_failed(self, create_exchange) -> None:
        exchange = create_exchange.return_value
        exchange.create_order.side_effect = TimeoutError("request timed out")
        exchange.fetch_order.side_effect = RuntimeError("not visible yet")
        exchange.fetch_open_orders.return_value = []
        exchange.fetch_closed_orders.return_value = []

        result = execute_candidate(_config(), _candidate(), entry_type="mini_lc_okx")

        self.assertFalse(result.submitted)
        self.assertEqual((result.raw or {}).get("submission_status"), "unknown")
        self.assertTrue((result.raw or {}).get("client_order_id"))
