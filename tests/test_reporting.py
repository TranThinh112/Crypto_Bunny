from __future__ import annotations

import tempfile
from copy import deepcopy
from unittest import TestCase

from crypto_trader.config import DEFAULT_CONFIG
from crypto_trader.reporting import (
    _position_rows,
    format_pending_event_messages,
    format_scan_message,
    format_trade_execution_close_message,
)


class ReportingTest(TestCase):
    def _config(self) -> dict:
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        config = deepcopy(DEFAULT_CONFIG)
        config["_config_dir"] = self.tmpdir.name
        config["_atlas_test_mode"] = True
        return config

    def tearDown(self) -> None:
        tmpdir = getattr(self, "tmpdir", None)
        if tmpdir:
            tmpdir.cleanup()

    def test_scan_message_uses_daily_sequence_and_source_labels(self) -> None:
        config = self._config()
        payload = {
            "candidates": [
                {
                    "symbol": "BTC/USDT:USDT",
                    "side": "long",
                    "scan_source": "new_scan",
                    "win_probability_pct": 76.5,
                    "confidence": 80,
                    "risk_reward": 1.7,
                },
                {
                    "symbol": "ETH/USDT:USDT",
                    "side": "short",
                    "scan_source": "old_rescan",
                    "win_probability_pct": 74.1,
                    "confidence": 78,
                    "risk_reward": 1.6,
                },
            ]
        }
        status = {"last_result": "no_order", "mode": "demo", "action": "hold"}

        first = format_scan_message(config, payload, status)
        second = format_scan_message(config, payload, status)

        self.assertIn("SC #1", first)
        self.assertIn("SC #2", second)
        self.assertIn("[🆕 mới]", first)
        self.assertIn("[🔁 cũ]", first)
        self.assertIn("Kết quả", first)

    def test_pending_canceled_message_translates_all_reason_segments_to_vietnamese(self) -> None:
        payload = {
            "scan_comparison": {
                "pending_orders": {
                    "events": [
                        {
                            "type": "pending_canceled",
                            "lc_id": 5,
                            "symbol": "AAVE/USDT:USDT",
                            "side": "long",
                            "reason": (
                                "Win probability 63.50% is below minimum 80.00%; "
                                "No recent symbol-specific news confirmed the setup; "
                                "Giá entry mới lệch 1.63% so với LC cũ (ngưỡng 1.20%)"
                            ),
                        }
                    ]
                }
            }
        }

        messages = format_pending_event_messages(payload)

        self.assertEqual(len(messages), 1)
        self.assertIn("Tỉ lệ thắng 63.50% thấp hơn ngưỡng tối thiểu 80.00%", messages[0])
        self.assertIn("Chưa có tin tức gần đây xác nhận setup cho cặp này", messages[0])
        self.assertIn("Giá entry mới lệch 1.63% so với LC cũ (ngưỡng 1.20%)", messages[0])
        self.assertNotIn("Win probability 63.50%", messages[0])
        self.assertNotIn("No recent symbol-specific news confirmed the setup", messages[0])

    def test_pending_canceled_message_repairs_broken_vietnamese_entry_drift_text(self) -> None:
        payload = {
            "scan_comparison": {
                "pending_orders": {
                    "events": [
                        {
                            "type": "pending_canceled",
                            "lc_id": 5,
                            "symbol": "AAVE/USDT:USDT",
                            "side": "long",
                            "reason": "Gi? entry m?i l?ch 1.63% so v?i LC c? (ng?ng 1.20%)",
                        }
                    ]
                }
            }
        }

        messages = format_pending_event_messages(payload)

        self.assertEqual(len(messages), 1)
        self.assertIn("Giá entry mới lệch 1.63% so với LC cũ (ngưỡng 1.20%)", messages[0])
        self.assertNotIn("Gi? entry m?i l?ch", messages[0])

    def test_pending_converted_message_uses_vietnamese_with_accents(self) -> None:
        payload = {
            "scan_comparison": {
                "pending_orders": {
                    "events": [
                        {
                            "type": "pending_converted",
                            "source": "lc_okx_released",
                            "from_status": "LC_OKX",
                            "lc_id": 25,
                            "vt_id": 2,
                            "symbol": "OP/USDT:USDT",
                            "side": "long",
                            "exchange_order_id": "3730324469233868800",
                        }
                    ]
                }
            }
        }

        messages = format_pending_event_messages(payload)

        self.assertEqual(len(messages), 1)
        self.assertIn("LC_OKX #25", messages[0])
        self.assertIn("đã được chuyển thành VT #2", messages[0])
        self.assertIn("Cặp: OP/USDT:USDT LONG", messages[0])
        self.assertNotIn("da duoc chuyen thanh", messages[0])
        self.assertNotIn("Cap:", messages[0])

    def test_position_rows_use_pending_algo_targets_when_open_orders_are_empty(self) -> None:
        config = self._config()

        class AlgoExchange:
            markets_by_id = {"TAO-USDT-SWAP": {"symbol": "TAO/USDT:USDT"}}

            def fetch_open_orders(self) -> list[dict[str, object]]:
                return []

            def fetch_positions(self) -> list[dict[str, object]]:
                return [
                    {
                        "symbol": "TAO/USDT:USDT",
                        "side": "long",
                        "contracts": 2,
                        "entryPrice": 320,
                        "info": {"pos": "2", "posSide": "long", "instId": "TAO-USDT-SWAP"},
                    }
                ]

            def privateGetTradeOrdersAlgoPending(self, params: dict[str, object]) -> dict[str, object]:
                if params.get("ordType") != "conditional":
                    return {"data": []}
                return {
                    "data": [
                        {
                            "algoId": "algo-2",
                            "instId": "TAO-USDT-SWAP",
                            "posSide": "long",
                            "side": "sell",
                            "ordType": "conditional",
                            "slTriggerPx": "300",
                            "tpTriggerPx": "360",
                        }
                    ]
                }

        rows = _position_rows(config, AlgoExchange())

        self.assertEqual(rows[0]["stop_loss"], 300)
        self.assertEqual(rows[0]["take_profit"], 360)
        self.assertEqual(rows[0]["tp_sl_status"], "ok")

    def test_trade_execution_close_message_formats_take_profit_with_pnl(self) -> None:
        config = self._config()
        row = {
            "id": 25,
            "symbol": "OP/USDT:USDT",
            "side": "LONG",
            "status": "WIN",
            "pnl": 12.5,
            "close_reason": "tp",
            "closed_at": "2026-07-10T10:58:30+00:00",
            "payload_json": '{"order_usdt": 50.0, "margin_usdt": 2.0}',
        }

        message = format_trade_execution_close_message(config, row)

        self.assertIn("VT #25 | Chạm TP", message)
        self.assertIn("Cặp: OP/USDT:USDT LONG", message)
        self.assertIn("Lợi nhuận: +12.50 USDT (+25.00%)", message)
        self.assertIn("Đóng lúc: 10/07/2026 17:58:30", message)

    def test_trade_execution_close_message_formats_manual_close(self) -> None:
        config = self._config()
        row = {
            "id": 26,
            "symbol": "KAITO/USDT:USDT",
            "side": "LONG",
            "status": "CLOSED",
            "pnl": -3.2,
            "close_reason": "manual",
            "closed_at": "2026-07-10T11:05:00+00:00",
            "payload_json": '{"order_usdt": 50.0}',
        }

        message = format_trade_execution_close_message(config, row)

        self.assertIn("VT #26 | Tự đóng", message)
        self.assertIn("Lợi nhuận: -3.20 USDT (-6.40%)", message)
        self.assertIn("Đóng lúc: 10/07/2026 18:05:00", message)
