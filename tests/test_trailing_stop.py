from __future__ import annotations

import tempfile
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from crypto_trader.config import DEFAULT_CONFIG
from crypto_trader.reporting import format_partial_take_profit_message
from crypto_trader.storage import get_journal_state, insert_trade_execution_row, list_trade_execution_rows, update_trade_execution
from crypto_trader.trailing_stop import STATE_KEY, run_trailing_stop_cycle


class FakeTrailingExchange:
    def __init__(self, *, mark: float, current_sl: float, contracts: float = 1.0) -> None:
        self.mark = mark
        self.current_sl = current_sl
        self.contracts = contracts
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
                "contracts": self.contracts,
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


class FakeTrailingExchangePosSideRetry(FakeTrailingExchange):
    def __init__(self, *, mark: float, current_sl: float) -> None:
        super().__init__(mark=mark, current_sl=current_sl)
        self.fail_first = True

    def create_order(self, symbol: str, order_type: str, side: str, amount: str, price: float | None, params: dict) -> dict:
        if self.fail_first and "posSide" in params:
            self.fail_first = False
            raise RuntimeError("Order failed because you don't have any positions in this direction for this contract to reduce or close.")
        return super().create_order(symbol, order_type, side, amount, price, params)


class FakeTrailingExchangeNoAlgo(FakeTrailingExchange):
    def privateGetTradeOrdersAlgoPending(self, request: dict) -> dict:
        return {"data": []}


class FakeTrailingExchangeRequiresPosSide(FakeTrailingExchange):
    def create_order(self, symbol: str, order_type: str, side: str, amount: str, price: float | None, params: dict) -> dict:
        if "posSide" not in params:
            raise RuntimeError('okx {"code":"1","data":[{"sCode":"51000","sMsg":"Parameter posSide error"}]}')
        return super().create_order(symbol, order_type, side, amount, price, params)

class FakeTrailingExchangeRequiresNetPosSide(FakeTrailingExchange):
    def create_order(self, symbol: str, order_type: str, side: str, amount: str, price: float | None, params: dict) -> dict:
        if params.get("posSide") != "net":
            raise RuntimeError('okx {"code":"1","data":[{"sCode":"51000","sMsg":"Parameter posSide error"}]}')
        return super().create_order(symbol, order_type, side, amount, price, params)

class FakeTrailingExchangeSnapshotAlgo(FakeTrailingExchangeNoAlgo):
    def fetch_positions(self) -> list[dict]:
        rows = super().fetch_positions()
        rows[0]["info"] = {
            "closeOrderAlgo": [
                {
                    "algoId": "snapshot-sl-algo",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "slTriggerPx": str(self.current_sl),
                    "slOrdPx": "-1",
                }
            ]
        }
        return rows


class FakeTrailingExchangeGenericAlgo(FakeTrailingExchange):
    def privateGetTradeOrdersAlgoPending(self, request: dict) -> dict:
        if request.get("ordType"):
            return {"data": []}
        return super().privateGetTradeOrdersAlgoPending(request)

class FakeShortTrailingExchange(FakeTrailingExchange):
    def __init__(self, *, mark: float, current_sl: float, contracts: float = 1.0, margin_mode: str | None = None) -> None:
        super().__init__(mark=mark, current_sl=current_sl, contracts=contracts)
        self.margin_mode = margin_mode

    def fetch_positions(self) -> list[dict]:
        row = {
            "symbol": "BTC/USDT:USDT",
            "side": "short",
            "contracts": self.contracts,
            "entry_price": 100.0,
            "mark_price": self.mark,
            "info": {"posSide": "short"},
        }
        if self.margin_mode:
            row["marginMode"] = self.margin_mode
            row["info"]["mgnMode"] = self.margin_mode
        return [row]

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1m", limit: int | None = None) -> list[list[float]]:
        rows = []
        close = self.mark
        for index in range(limit or 15):
            rows.append([index, close, close + 1.0, close - 1.0, close, 1.0])
        return rows

    def privateGetTradeOrdersAlgoPending(self, request: dict) -> dict:
        return {
            "data": [
                {
                    "algoId": "short-sl-algo-1",
                    "instId": request.get("instId"),
                    "posSide": "short",
                    "slTriggerPx": str(self.current_sl),
                    "slOrdPx": "-1",
                }
            ]
        }

class FakeTrailingExchangeWithManualFill(FakeTrailingExchange):
    def fetch_my_trades(self, symbol: str, since: int | None = None, limit: int | None = None) -> list[dict]:
        return [
            {
                "symbol": symbol,
                "side": "sell",
                "amount": 0.3,
                "price": 64890.0,
                "timestamp": 1784512800000,
                "reduceOnly": True,
                "info": {"posSide": "long"},
            }
        ]


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
        config["loss_guard"] = {"enabled": False}
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
                "quantity": 1.0,
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

    def test_loss_guard_closes_twenty_five_percent_once_at_negative_r(self) -> None:
        config = self._config()
        config["loss_guard"] = {
            "enabled": True,
            "effective_from": "2026-07-18T00:00:00+00:00",
            "auto_close_enabled": True,
            "partial_close_r": -0.8,
            "partial_close_fraction": 0.25,
        }
        self._insert_open_execution(config)
        exchange = FakeTrailingExchange(mark=64432.0, current_sl=64407.0, contracts=1.0)

        with patch("crypto_trader.trailing_stop.create_exchange", return_value=exchange):
            result = run_trailing_stop_cycle(config)

        self.assertEqual(result["partial_closed"], 1)
        self.assertEqual(result["items"][0]["status"], "loss_guard_partial_closed")
        self.assertEqual(exchange.orders[0]["side"], "sell")
        self.assertEqual(exchange.orders[0]["amount"], "0.25")
        self.assertTrue(exchange.orders[0]["params"]["reduceOnly"])
        row = list_trade_execution_rows(config, statuses=["OPEN"])[0]
        self.assertTrue(row["loss_guard_partial_done"])
        self.assertAlmostEqual(row["loss_guard_partial_amount"], 0.25)

        exchange_again = FakeTrailingExchange(mark=64420.0, current_sl=64407.0, contracts=0.75)
        with patch("crypto_trader.trailing_stop.create_exchange", return_value=exchange_again):
            second = run_trailing_stop_cycle(config)

        self.assertEqual(second["partial_closed"], 0)
        self.assertEqual(exchange_again.orders, [])

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
        self.assertIn("+105.00 USDT", message)
        self.assertIn("+8.75 USDT", message)
        self.assertIn("+455.00 USDT", message)
        self.assertIn("PARTIAL TP + GỒNG LÃI", message)
        self.assertIn("BTC/USDT:USDT LONG", message)
        self.assertIn("ID lệnh: VT #1", message)
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

    def test_partial_take_profit_message_labels_manual_partial(self) -> None:
        message = format_partial_take_profit_message(
            self._config(),
            {
                "trade_execution_id": 10,
                "symbol": "GMX/USDT:USDT",
                "side": "short",
                "entry": 7.247036,
                "trigger_price": 6.828,
                "close_fraction": 0.3,
                "partial_amount": 18,
                "remaining_amount": 42,
                "contract_size": 0.1,
                "old_stop_loss": 7.682,
                "new_stop_loss": 7.203539,
                "old_take_profit": 6.724,
                "new_take_profit": 6.567089,
                "manual_partial_detected": True,
            },
        )

        self.assertIn("ID lệnh: VT #10", message)
        self.assertIn("Phát hiện vị thế đã giảm 30%", message)
        self.assertNotIn("Đã chốt 30% vị thế", message)
        self.assertIn("Nấc tiếp theo kích hoạt khi giá chạm", message)

    def test_partial_take_profit_retries_without_pos_side_when_okx_rejects_direction(self) -> None:
        config = self._config()
        config["exchange"]["position_side_mode"] = "long_short"
        config["trailing_stop"]["partial_take_profit"] = {
            "enabled": True,
            "trigger_tp_progress": 0.7,
            "close_fraction": 0.3,
            "remaining_sl_buffer_r": 0.1,
            "tp_extension_fraction": 0.3,
        }
        self._insert_open_execution(config)
        exchange = FakeTrailingExchangePosSideRetry(mark=64882.0, current_sl=64407.0)

        with patch("crypto_trader.trailing_stop.create_exchange", return_value=exchange):
            result = run_trailing_stop_cycle(config)

        self.assertEqual(result["partial_closed"], 1)
        self.assertEqual(len(exchange.orders), 1)
        self.assertNotIn("posSide", exchange.orders[0]["params"])
        self.assertTrue(exchange.orders[0]["params"]["reduceOnly"])

    def test_partial_take_profit_retries_with_pos_side_when_okx_requires_direction(self) -> None:
        config = self._config()
        config["exchange"]["position_side_mode"] = "net"
        config["trailing_stop"]["partial_take_profit"] = {
            "enabled": True,
            "trigger_tp_progress": 0.7,
            "close_fraction": 0.3,
            "remaining_sl_buffer_r": 0.1,
            "tp_extension_fraction": 0.3,
        }
        self._insert_open_execution(config)
        exchange = FakeTrailingExchangeRequiresPosSide(mark=64882.0, current_sl=64407.0)

        with patch("crypto_trader.trailing_stop.create_exchange", return_value=exchange):
            result = run_trailing_stop_cycle(config)

        self.assertEqual(result["partial_closed"], 1)
        self.assertEqual(len(exchange.orders), 1)
        self.assertEqual(exchange.orders[0]["params"]["posSide"], "long")
        self.assertTrue(exchange.orders[0]["params"]["reduceOnly"])

    def test_partial_take_profit_retries_with_net_pos_side_when_okx_requires_net(self) -> None:
        config = self._config()
        config["exchange"]["position_side_mode"] = "long_short"
        config["trailing_stop"]["partial_take_profit"] = {
            "enabled": True,
            "trigger_tp_progress": 0.7,
            "close_fraction": 0.3,
            "remaining_sl_buffer_r": 0.1,
            "tp_extension_fraction": 0.3,
        }
        self._insert_open_execution(config)
        exchange = FakeTrailingExchangeRequiresNetPosSide(mark=64882.0, current_sl=64407.0)

        with patch("crypto_trader.trailing_stop.create_exchange", return_value=exchange):
            result = run_trailing_stop_cycle(config)

        self.assertEqual(result["partial_closed"], 1)
        self.assertEqual(len(exchange.orders), 1)
        self.assertEqual(exchange.orders[0]["params"]["posSide"], "net")
        self.assertTrue(exchange.orders[0]["params"]["reduceOnly"])

    def test_partial_take_profit_uses_live_position_margin_mode_and_pos_side(self) -> None:
        config = self._config()
        config["exchange"]["td_mode"] = "isolated"
        config["exchange"]["position_side_mode"] = "net"
        config["trailing_stop"]["partial_take_profit"] = {
            "enabled": True,
            "trigger_tp_progress": 0.7,
            "close_fraction": 0.3,
            "remaining_sl_buffer_r": 0.1,
            "tp_extension_fraction": 0.3,
        }
        insert_trade_execution_row(
            config,
            {
                "created_at": "2026-07-19T00:00:00+00:00",
                "updated_at": "2026-07-19T00:00:00+00:00",
                "symbol": "BTC/USDT:USDT",
                "side": "SHORT",
                "status": "OPEN",
                "entry_price": 100.0,
                "stop_loss": 110.0,
                "take_profit": 80.0,
                "quantity": 1.0,
                "initial_entry_price": 100.0,
                "initial_stop_loss": 110.0,
            },
        )
        exchange = FakeShortTrailingExchange(mark=85.0, current_sl=110.0, margin_mode="cross")

        with patch("crypto_trader.trailing_stop.create_exchange", return_value=exchange):
            result = run_trailing_stop_cycle(config)

        self.assertEqual(result["partial_closed"], 1)
        self.assertEqual(exchange.orders[0]["params"]["tdMode"], "cross")
        self.assertEqual(exchange.orders[0]["params"]["posSide"], "short")

    def test_partial_take_profit_closes_even_when_okx_algo_is_missing(self) -> None:
        config = self._config()
        config["trailing_stop"]["partial_take_profit"] = {
            "enabled": True,
            "trigger_tp_progress": 0.7,
            "close_fraction": 0.3,
            "remaining_sl_buffer_r": 0.1,
            "tp_extension_fraction": 0.3,
        }
        self._insert_open_execution(config)
        exchange = FakeTrailingExchangeNoAlgo(mark=64882.0, current_sl=64407.0)

        with (
            patch("crypto_trader.trailing_stop.create_exchange", return_value=exchange),
            patch("crypto_trader.notifier.send_telegram_message", return_value=True) as send_message,
        ):
            result = run_trailing_stop_cycle(config)

        self.assertEqual(result["partial_closed"], 1)
        self.assertEqual(exchange.orders[0]["amount"], "0.3")
        self.assertEqual(exchange.amend_requests, [])
        self.assertEqual(result["items"][0]["protection_error"], "OKX SL/TP algo order not found")
        row = list_trade_execution_rows(config, statuses=["OPEN"])[0]
        self.assertTrue(row["partial_take_profit_done"])
        self.assertEqual(row["profit_extension_step"], 0)
        self.assertAlmostEqual(row["stop_loss"], 64407.0)
        self.assertAlmostEqual(row["take_profit"], 65032.0)
        message = send_message.call_args.args[1]
        self.assertIn("Chưa dời SL/TP", message)
        self.assertIn("OKX SL/TP algo order not found", message)

    def test_partial_take_profit_retries_step_one_protection_after_missing_algo(self) -> None:
        config = self._config()
        config["trailing_stop"]["partial_take_profit"] = {
            "enabled": True,
            "trigger_tp_progress": 0.7,
            "close_fraction": 0.3,
            "remaining_sl_buffer_r": 0.1,
            "tp_extension_fraction": 0.3,
        }
        self._insert_open_execution(config)

        with (
            patch("crypto_trader.trailing_stop.create_exchange", return_value=FakeTrailingExchangeNoAlgo(mark=64882.0, current_sl=64407.0)),
            patch("crypto_trader.notifier.send_telegram_message", return_value=True),
        ):
            first = run_trailing_stop_cycle(config)

        self.assertEqual(first["partial_closed"], 1)
        exchange = FakeTrailingExchange(mark=64882.0, current_sl=64407.0, contracts=0.7)
        with patch("crypto_trader.trailing_stop.create_exchange", return_value=exchange):
            second = run_trailing_stop_cycle(config)

        self.assertEqual(second["amended"], 1)
        self.assertEqual(second["items"][0]["status"], "profit_step_extended")
        self.assertEqual(second["items"][0]["new_stop_loss"], 64544.5)
        self.assertEqual(second["items"][0]["new_take_profit"], 65182.0)
        self.assertEqual(exchange.orders, [])
        self.assertEqual(exchange.amend_requests[0]["newSlTriggerPx"], "64544.5")
        self.assertEqual(exchange.amend_requests[0]["newTpTriggerPx"], "65182.0")
        row = list_trade_execution_rows(config, statuses=["OPEN"])[0]
        self.assertEqual(row["profit_extension_step"], 1)
        self.assertAlmostEqual(row["stop_loss"], 64544.5)
        self.assertAlmostEqual(row["take_profit"], 65182.0)

    def test_partial_take_profit_uses_position_snapshot_algo_when_pending_query_is_empty(self) -> None:
        config = self._config()
        config["trailing_stop"]["partial_take_profit"] = {
            "enabled": True,
            "trigger_tp_progress": 0.7,
            "close_fraction": 0.3,
            "remaining_sl_buffer_r": 0.1,
            "tp_extension_fraction": 0.3,
        }
        self._insert_open_execution(config)
        exchange = FakeTrailingExchangeSnapshotAlgo(mark=64882.0, current_sl=64407.0)

        with patch("crypto_trader.trailing_stop.create_exchange", return_value=exchange):
            result = run_trailing_stop_cycle(config)

        self.assertEqual(result["partial_closed"], 1)
        self.assertEqual(result["amended"], 1)
        self.assertEqual(exchange.amend_requests[0]["algoId"], "snapshot-sl-algo")
        self.assertIsNone(result["items"][0].get("protection_error"))

    def test_partial_take_profit_queries_pending_algos_without_ord_type_as_fallback(self) -> None:
        config = self._config()
        config["trailing_stop"]["partial_take_profit"] = {
            "enabled": True,
            "trigger_tp_progress": 0.7,
            "close_fraction": 0.3,
            "remaining_sl_buffer_r": 0.1,
            "tp_extension_fraction": 0.3,
        }
        self._insert_open_execution(config)
        exchange = FakeTrailingExchangeGenericAlgo(mark=64882.0, current_sl=64407.0)

        with patch("crypto_trader.trailing_stop.create_exchange", return_value=exchange):
            result = run_trailing_stop_cycle(config)

        self.assertEqual(result["partial_closed"], 1)
        self.assertEqual(result["amended"], 1)
        self.assertEqual(exchange.amend_requests[0]["algoId"], "sl-algo-1")
        self.assertIsNone(result["items"][0].get("protection_error"))

    def test_profit_step_waits_when_short_sl_would_be_below_mark(self) -> None:
        config = self._config()
        config["trailing_stop"]["partial_take_profit"] = {
            "enabled": True,
            "trigger_tp_progress": 0.7,
            "close_fraction": 0.3,
            "remaining_sl_buffer_r": 0.1,
            "tp_extension_fraction": 0.3,
            "sl_buffer_r_by_step": [0.1, 0.5, 1.0],
        }
        insert_trade_execution_row(
            config,
            {
                "created_at": "2026-07-19T00:00:00+00:00",
                "updated_at": "2026-07-19T00:00:00+00:00",
                "symbol": "BTC/USDT:USDT",
                "side": "SHORT",
                "status": "OPEN",
                "entry_price": 100.0,
                "stop_loss": 95.0,
                "take_profit": 87.0,
                "quantity": 1.0,
                "initial_entry_price": 100.0,
                "initial_stop_loss": 110.0,
                "partial_take_profit_done": True,
                "partial_take_profit_original_tp": 90.0,
                "partial_take_profit_extended_tp": 87.0,
                "profit_extension_step": 2,
            },
        )
        exchange = FakeShortTrailingExchange(mark=90.5, current_sl=95.0)

        with patch("crypto_trader.trailing_stop.create_exchange", return_value=exchange):
            result = run_trailing_stop_cycle(config)

        self.assertEqual(result["amended"], 0)
        self.assertEqual(exchange.amend_requests, [])
        self.assertEqual(result["items"][0]["status"], "waiting")
        self.assertEqual(result["items"][0]["reason"], "proposed SL trigger is invalid for current mark price")

    def test_partial_take_profit_marks_manual_reduction_and_only_amends_targets(self) -> None:
        config = self._config()
        config["trailing_stop"]["partial_take_profit"] = {
            "enabled": True,
            "trigger_tp_progress": 0.7,
            "close_fraction": 0.3,
            "remaining_sl_buffer_r": 0.1,
            "tp_extension_fraction": 0.3,
        }
        self._insert_open_execution(config)
        exchange = FakeTrailingExchange(mark=64882.0, current_sl=64407.0, contracts=0.7)

        with patch("crypto_trader.trailing_stop.create_exchange", return_value=exchange):
            result = run_trailing_stop_cycle(config)

        self.assertEqual(result["partial_closed"], 1)
        self.assertEqual(exchange.orders, [])
        self.assertEqual(result["items"][0]["manual_partial_detected"], True)
        self.assertEqual(exchange.amend_requests[0]["newSlTriggerPx"], "64544.5")
        self.assertEqual(exchange.amend_requests[0]["newTpTriggerPx"], "65182.0")
        row = list_trade_execution_rows(config, statuses=["OPEN"])[0]
        self.assertTrue(row["partial_take_profit_done"])
        self.assertAlmostEqual(row["partial_take_profit_amount"], 0.3)

    def test_partial_take_profit_ignores_loss_guard_reduction_and_closes_live_amount(self) -> None:
        config = self._config()
        config["trailing_stop"]["partial_take_profit"] = {
            "enabled": True,
            "trigger_tp_progress": 0.7,
            "close_fraction": 0.3,
            "remaining_sl_buffer_r": 0.1,
            "tp_extension_fraction": 0.3,
        }
        self._insert_open_execution(config)
        row = list_trade_execution_rows(config, statuses=["OPEN"])[0]
        update_trade_execution(
            config,
            int(row["id"]),
            {
                "loss_guard_partial_done": True,
                "loss_guard_partial_amount": 0.25,
                "loss_guard_partial_price": 64300.0,
            },
        )
        exchange = FakeTrailingExchange(mark=64900.0, current_sl=64407.0, contracts=0.75)

        with patch("crypto_trader.trailing_stop.create_exchange", return_value=exchange):
            result = run_trailing_stop_cycle(config)

        self.assertEqual(result["partial_closed"], 1)
        self.assertEqual(len(exchange.orders), 1)
        self.assertEqual(exchange.orders[0]["amount"], "0.225")
        self.assertFalse(result["items"][0].get("manual_partial_detected", False))
        row = list_trade_execution_rows(config, statuses=["OPEN"])[0]
        self.assertTrue(row["partial_take_profit_done"])
        self.assertAlmostEqual(row["partial_take_profit_amount"], 0.225)

    def test_manual_reduction_uses_okx_fill_history_for_price_time_and_pnl(self) -> None:
        config = self._config()
        config["trailing_stop"]["partial_take_profit"] = {
            "enabled": True,
            "trigger_tp_progress": 0.7,
            "close_fraction": 0.3,
            "remaining_sl_buffer_r": 0.1,
            "tp_extension_fraction": 0.3,
        }
        self._insert_open_execution(config)
        exchange = FakeTrailingExchangeWithManualFill(mark=64700.0, current_sl=64407.0, contracts=0.7)

        with (
            patch("crypto_trader.trailing_stop.create_exchange", return_value=exchange),
            patch("crypto_trader.notifier.send_telegram_message", return_value=True) as send_message,
        ):
            result = run_trailing_stop_cycle(config)

        self.assertEqual(result["partial_closed"], 1)
        self.assertEqual(result["items"][0]["manual_partial_source"], "okx_fills")
        self.assertEqual(result["items"][0]["partial_price"], 64890.0)
        row = list_trade_execution_rows(config, statuses=["OPEN"])[0]
        self.assertAlmostEqual(row["partial_take_profit_price"], 64890.0)
        message = send_message.call_args.args[1]
        self.assertIn("Giá chốt thật: 64890.000000", message)
        self.assertIn("Đã xác nhận bạn chốt 30% vị thế", message)
        self.assertIn("+107.40 USDT", message)

    def test_manual_reduction_still_protects_when_price_falls_back_below_trigger(self) -> None:
        config = self._config()
        config["trailing_stop"]["partial_take_profit"] = {
            "enabled": True,
            "trigger_tp_progress": 0.7,
            "close_fraction": 0.3,
            "remaining_sl_buffer_r": 0.1,
            "tp_extension_fraction": 0.3,
        }
        self._insert_open_execution(config)
        row = list_trade_execution_rows(config, statuses=["OPEN"])[0]
        from crypto_trader.storage import update_trade_execution

        update_trade_execution(config, int(row["id"]), {"initial_quantity": 1.0, "quantity": 0.7})
        exchange = FakeTrailingExchange(mark=64700.0, current_sl=64407.0, contracts=0.7)

        with patch("crypto_trader.trailing_stop.create_exchange", return_value=exchange):
            result = run_trailing_stop_cycle(config)

        self.assertEqual(result["partial_closed"], 1)
        self.assertEqual(exchange.orders, [])
        self.assertEqual(result["items"][0]["manual_partial_detected"], True)
        self.assertEqual(exchange.amend_requests[0]["newSlTriggerPx"], "64544.5")
        self.assertEqual(exchange.amend_requests[0]["newTpTriggerPx"], "65182.0")
