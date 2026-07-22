from __future__ import annotations

import tempfile
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from crypto_trader.config import DEFAULT_CONFIG
from crypto_trader.storage import get_journal_state, insert_trade_execution_row, list_trade_execution_rows
from crypto_trader.trailing_stop import STATE_KEY, run_trailing_stop_cycle


class FakeTrailingExchange:
    def __init__(self, *, mark: float, current_sl: float) -> None:
        self.mark = mark
        self.current_sl = current_sl
        self.amend_requests: list[dict] = []
        self.orders: list[dict] = []

    def load_markets(self) -> dict:
        return {}

    def market(self, symbol: str) -> dict:
        return {"id": "BTC-USDT-SWAP", "symbol": symbol}

    def fetch_positions(self) -> list[dict]:
        return [
            {
                "symbol": "BTC/USDT:USDT",
                "side": "long",
                "contracts": 1,
                "entry_price": 64532.0,
                "mark_price": self.mark,
            }
        ]

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1m", limit: int | None = None) -> list[list[float]]:
        rows = []
        close = 64600.0
        for index in range(limit or 15):
            rows.append([index, close, close + 7.5, close - 7.5, close, 1.0])
        return rows

    def privateGetTradeOrdersAlgoPending(self, request: dict) -> dict:
        return {
            "data": [
                {
                    "algoId": "sl-algo-1",
                    "instId": request.get("instId"),
                    "posSide": "long",
                    "slTriggerPx": str(self.current_sl),
                    "slOrdPx": "-1",
                }
            ]
        }

    def privatePostTradeAmendAlgos(self, request: dict) -> dict:
        self.amend_requests.append(dict(request))
        return {"code": "0", "data": [{"algoId": request.get("algoId"), "sCode": "0"}]}

    def price_to_precision(self, symbol: str, price: float) -> str:
        return f"{price:.1f}"

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return f"{float(amount):.3f}".rstrip("0").rstrip(".")

    def create_order(self, symbol: str, order_type: str, side: str, amount: str, price: float | None, params: dict) -> dict:
        order = {
            "symbol": symbol,
            "type": order_type,
            "side": side,
            "amount": amount,
            "price": price,
            "params": dict(params),
            "id": f"order-{len(self.orders) + 1}",
        }
        self.orders.append(order)
        return order


class TrailingStopTest(TestCase):
    def _config(self) -> dict:
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        config = deepcopy(DEFAULT_CONFIG)
        config["_config_dir"] = self.tmpdir.name
        config["_atlas_test_mode"] = True
        config["mode"] = "live"
        config["trailing_stop"] = {
            "enabled": True,
            "activation_r_multiple": 1.0,
            "atr_timeframe": "1m",
            "atr_period": 14,
            "atr_multiplier": 1.5,
            "min_improvement_price": 0.0,
            "trigger_price_type": "last",
            "symbol_overrides": {"BTC": {"min_improvement_points": 2000, "point_value": 0.01}},
        }
        return config

    def tearDown(self) -> None:
        tmpdir = getattr(self, "tmpdir", None)
        if tmpdir:
            tmpdir.cleanup()

    @staticmethod
    def _insert_open_execution(config: dict, *, stop_loss: float = 64407.0) -> None:
        insert_trade_execution_row(
            config,
            {
                "created_at": "2026-07-19T00:00:00+00:00",
                "updated_at": "2026-07-19T00:00:00+00:00",
                "symbol": "BTC/USDT:USDT",
                "side": "LONG",
                "status": "OPEN",
                "entry_price": 64532.0,
                "stop_loss": stop_loss,
                "take_profit": 65032.0,
                "initial_entry_price": 64532.0,
                "initial_stop_loss": 64407.0,
            },
        )

    def test_trails_btc_stop_after_one_r_and_minimum_improvement(self) -> None:
        config = self._config()
        self._insert_open_execution(config)
        exchange = FakeTrailingExchange(mark=64657.0, current_sl=64407.0)

        with patch("crypto_trader.trailing_stop.create_exchange", return_value=exchange):
            result = run_trailing_stop_cycle(config)

        self.assertEqual(result["amended"], 1)
        self.assertEqual(exchange.amend_requests[0]["newSlTriggerPx"], "64634.5")
        row = list_trade_execution_rows(config, statuses=["OPEN"])[0]
        self.assertEqual(row["initial_stop_loss"], 64407.0)
        self.assertAlmostEqual(row["stop_loss"], 64634.5)
        self.assertIsNotNone(get_journal_state(config, STATE_KEY))

    def test_waits_until_position_reaches_activation_r(self) -> None:
        config = self._config()
        self._insert_open_execution(config)
        exchange = FakeTrailingExchange(mark=64600.0, current_sl=64407.0)

        with patch("crypto_trader.trailing_stop.create_exchange", return_value=exchange):
            result = run_trailing_stop_cycle(config)

        self.assertEqual(result["amended"], 0)
        self.assertEqual(exchange.amend_requests, [])
        self.assertEqual(result["items"][0]["reason"], "activation R not reached")

    def test_waits_when_btc_improvement_is_below_twenty_usd(self) -> None:
        config = self._config()
        self._insert_open_execution(config, stop_loss=64620.0)
        exchange = FakeTrailingExchange(mark=64657.0, current_sl=64620.0)

        with patch("crypto_trader.trailing_stop.create_exchange", return_value=exchange):
            result = run_trailing_stop_cycle(config)

        self.assertEqual(result["amended"], 0)
        self.assertEqual(exchange.amend_requests, [])
        self.assertEqual(result["items"][0]["reason"], "minimum improvement not reached")

    def test_uses_okx_algo_sl_as_initial_stop_for_existing_rows(self) -> None:
        config = self._config()
        insert_trade_execution_row(
            config,
            {
                "created_at": "2026-07-19T00:00:00+00:00",
                "updated_at": "2026-07-19T00:00:00+00:00",
                "symbol": "BTC/USDT:USDT",
                "side": "LONG",
                "status": "OPEN",
                "entry_price": 64532.0,
                "stop_loss": None,
                "take_profit": 65032.0,
            },
        )
        exchange = FakeTrailingExchange(mark=64657.0, current_sl=64407.0)

        with patch("crypto_trader.trailing_stop.create_exchange", return_value=exchange):
            result = run_trailing_stop_cycle(config)

        self.assertEqual(result["amended"], 1)
        row = list_trade_execution_rows(config, statuses=["OPEN"])[0]
        self.assertEqual(row["initial_stop_loss"], 64407.0)
        self.assertAlmostEqual(row["stop_loss"], 64634.5)

    def test_partial_take_profit_closes_once_protects_sl_and_extends_tp(self) -> None:
        config = self._config()
        config["trailing_stop"]["partial_take_profit"] = {
            "enabled": True,
            "trigger_tp_progress": 0.7,
            "close_fraction": 0.3,
            "remaining_sl_buffer_r": 0.1,
            "tp_extension_fraction": 0.3,
        }
        self._insert_open_execution(config)
        exchange = FakeTrailingExchange(mark=64882.0, current_sl=64407.0)

        with (
            patch("crypto_trader.trailing_stop.create_exchange", return_value=exchange),
            patch("crypto_trader.notifier.send_telegram_message", return_value=True) as send_message,
        ):
            result = run_trailing_stop_cycle(config)

        self.assertEqual(result["partial_closed"], 1)
        send_message.assert_called_once()
        message = send_message.call_args.args[1]
        self.assertIn("PARTIAL TP + GỒNG LÃI", message)
        self.assertIn("BTC/USDT:USDT LONG", message)
        self.assertIn("Đã chốt 30% vị thế", message)
        self.assertIn("64407.000000 → 64544.500000", message)
        self.assertIn("65032.000000 → 65182.000000", message)
        self.assertFalse(send_message.call_args.kwargs["with_buttons"])
        self.assertFalse(send_message.call_args.kwargs["replace_previous"])
        self.assertEqual(exchange.orders[0]["side"], "sell")
        self.assertEqual(exchange.orders[0]["amount"], "0.3")
        self.assertTrue(exchange.orders[0]["params"]["reduceOnly"])
        self.assertEqual(exchange.amend_requests[0]["newSlTriggerPx"], "64544.5")
        self.assertEqual(exchange.amend_requests[0]["newTpTriggerPx"], "65182.0")
        row = list_trade_execution_rows(config, statuses=["OPEN"])[0]
        self.assertTrue(row["partial_take_profit_done"])
        self.assertAlmostEqual(row["stop_loss"], 64544.5)
        self.assertAlmostEqual(row["take_profit"], 65182.0)

        exchange_again = FakeTrailingExchange(mark=64920.0, current_sl=64544.5)
        with patch("crypto_trader.trailing_stop.create_exchange", return_value=exchange_again):
            second = run_trailing_stop_cycle(config)

        self.assertEqual(second["partial_closed"], 0)
        self.assertEqual(exchange_again.orders, [])
