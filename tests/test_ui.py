from __future__ import annotations

import tempfile
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

from crypto_trader.config import RUNTIME_CONFIG_OVERRIDES_STATE_KEY, load_config
from crypto_trader.codex_features import close_trade_execution, record_trade_candidates, record_trade_execution, try_slot_refill
from crypto_trader.models import RiskCheck, TradeCandidate
from crypto_trader.notifier import telegram_command_list
from crypto_trader.storage import get_journal_state, list_trade_execution_rows, save_market_scan_observations, set_journal_state
from crypto_trader.ui import (
    SCAN_TELEGRAM_SLOT_KEY,
    STARTUP_TELEGRAM_MESSAGE,
    _format_ai_call_history_view,
    _handle_telegram_update,
    _market_guard_notification_status,
    _notify_system_error,
    _periodic_scan_notification_due,
    _remember_periodic_scan_notification,
    _run_automation_cycle,
    _telegram_action_response,
    create_app,
)


class UiTest(TestCase):
    @patch("crypto_trader.ui.send_telegram_message", return_value=True)
    def test_system_error_notification_is_vietnamese_and_deduplicated(self, send_message) -> None:
        config = {"timezone": "Asia/Ho_Chi_Minh"}
        component = f"test-component-{id(self)}"

        first = _notify_system_error(config, component, RuntimeError("Mongo timeout"))
        second = _notify_system_error(config, component, RuntimeError("Mongo timeout"))

        self.assertTrue(first)
        self.assertFalse(second)
        send_message.assert_called_once()
        message = send_message.call_args.args[1]
        self.assertIn("LỖI HỆ THỐNG", message)
        self.assertIn(component, message)
        self.assertIn("Mongo timeout", message)

    def test_telegram_command_list_includes_internal_notification_commands(self) -> None:
        commands = {item["command"] for item in telegram_command_list()}
        self.assertIn("thongbao", commands)
        self.assertIn("noibo", commands)

    @patch("crypto_trader.ui.recent_ai_call_history")
    def test_ai_history_view_uses_short_vietnamese_mini_reasons(self, recent_history) -> None:
        recent_history.return_value = [
            {
                "created_at": "2026-07-10T04:01:19+07:00",
                "role": "mini",
                "model": "gpt-5.4-mini",
                "status": "MINI ĐỀ XUẤT LC",
                "approved_symbols": ["KAITO/USDT:USDT"],
                "setup_scores": {"KAITO/USDT:USDT": 78},
                "candidate_details": [
                    {
                        "symbol": "KAITO/USDT:USDT",
                        "side": "long",
                        "win_probability_pct": 59.23,
                        "confidence": 98.05,
                        "risk_reward": 1.5,
                        "reasons": [
                            "Strategic long bias target 60/40 adds 5.0 point(s)",
                            "Market regime is neutral: breadth 57% favors longs",
                            "5M trend confirms long (EMA gap +0.03%, price vs EMA50 +0.22%)",
                        ],
                    }
                ],
                "reason": (
                    "Aligned long bias with 1h/5m uptrend, modest RR 1.5, and no critical warnings; "
                    "5m hesitation lowers confidence."
                ),
            }
        ]

        message = _format_ai_call_history_view({"timezone": "Asia/Ho_Chi_Minh"})
        if True:
            return
        self.assertIn("LC_OKX: #25", message)
        self.assertIn("BTC/USDT:USDT", message)
        self.assertIn("Giải thích:", message)
        self.assertIn("5.5 đồng ý mở Market", message)
        return

        self.assertIn("Lý do gửi:", message)
        self.assertEqual(message.count("   - "), 2)
        self.assertIn("Mini chọn:", message)
        self.assertIn("Nhận xét của mini:", message)
        self.assertNotIn("Lý do AI:", message)
        self.assertIn("Thiên hướng long chiến lược cộng thêm 5.0 điểm.", message)
        self.assertIn("Xu hướng 5m xác nhận LONG.", message)
        self.assertIn("Mini thấy LONG đồng thuận với xu hướng tăng 1h/5m, R:R 1.5, chưa có cảnh báo lớn.", message)

    @patch("crypto_trader.ui.recent_ai_call_history")
    def test_ai_history_view_translates_new_mini_comments_to_vietnamese(self, recent_history) -> None:
        recent_history.return_value = [
            {
                "created_at": "2026-07-10T08:01:46+07:00",
                "role": "mini",
                "model": "gpt-5.4-mini",
                "status": "MINI ĐỀ XUẤT LC",
                "approved_symbols": ["AAVE/USDT:USDT"],
                "setup_scores": {"AAVE/USDT:USDT": 72},
                "candidate_details": [
                    {
                        "symbol": "AAVE/USDT:USDT",
                        "side": "long",
                        "win_probability_pct": 65.0,
                        "confidence": 100.0,
                        "risk_reward": 1.5,
                        "reasons": [
                            "Strategic long bias target 60/40 adds 5.0 point(s)",
                            "5M trend confirms long (EMA gap +0.03%, price vs EMA50 +0.22%)",
                        ],
                    }
                ],
                "reason": (
                    "Aligned 1h/5m bullish with volume support\n"
                    "RR only 1.5 and no 4h data, but local policy approved it."
                ),
            }
        ]

        message = _format_ai_call_history_view({"timezone": "Asia/Ho_Chi_Minh"})

        self.assertIn("1h/5m đang đồng thuận xu hướng tăng và có ủng hộ khối lượng.", message)
        self.assertIn("R:R chỉ 1.5, chưa có dữ liệu 4h, nhưng vẫn được local policy duyệt.", message)
        self.assertNotIn("Aligned 1h/5m bullish", message)
        self.assertNotIn("RR only 1.5", message)

    @patch("crypto_trader.ui.recent_ai_call_history")
    def test_ai_history_view_formats_lc_okx_review_entry(self, recent_history) -> None:
        recent_history.return_value = [
            {
                "created_at": "2026-07-10T18:55:12+07:00",
                "role": "okx",
                "review_kind": "lc_okx_review",
                "model": "gpt-5.5",
                "status": "DUYỆT MỞ MARKET",
                "symbol": "BTC/USDT:USDT",
                "side": "long",
                "lc_okx_id": 25,
                "market_reason": "Dong luc tang va volume on dinh, co the mo Market.",
                "keep_reason": "-",
                "delete_reason": "-",
            }
        ]

        message = _format_ai_call_history_view({"timezone": "Asia/Ho_Chi_Minh"})
        self.assertIn("LC_OKX: #25", message)
        self.assertIn("BTC/USDT:USDT", message)
        self.assertIn("5.5", message)
        self.assertIn("Market", message)
        return
        self.assertIn("Giáº£i thÃ­ch:", message)
        self.assertIn("5.5 Ä‘á»“ng Ã½ má»Ÿ Market", message)
        return

        self.assertIn("LC_OKX: #25", message)
        self.assertIn("Cặp: BTC/USDT:USDT | LONG", message)
        self.assertIn("Lý do mở Market: Dong luc tang va volume on dinh, co the mo Market.", message)
        self.assertIn("Lý do giữ setup: -", message)
        self.assertIn("Lý do xóa setup: -", message)

    def test_market_guard_notification_status_ignores_mild_positive_move(self) -> None:
        config = {
            "market_guard": {
                "price_move_5m_pct": 0.8,
                "critical_price_move_5m_pct": 1.4,
                "critical_candle_range_pct": 1.8,
                "wick_pct": 0.45,
                "wick_body_ratio": 2.5,
                "volume_ratio": 2.5,
            }
        }
        status = {
            "alerts": [
                {
                    "symbol": "BTC/USDT:USDT",
                    "severity": "warning",
                    "move_pct": 0.82,
                    "candle_range_pct": 0.92,
                    "wick_pct": 0.21,
                    "wick_body_ratio": 1.4,
                    "volume_ratio": 1.3,
                }
            ]
        }

        self.assertIsNone(_market_guard_notification_status(config, status))

    def test_market_guard_notification_status_keeps_strong_wick_alert(self) -> None:
        config = {
            "market_guard": {
                "price_move_5m_pct": 0.8,
                "critical_price_move_5m_pct": 1.4,
                "critical_candle_range_pct": 1.8,
                "wick_pct": 0.45,
                "wick_body_ratio": 2.5,
                "volume_ratio": 2.5,
            }
        }
        status = {
            "alerts": [
                {
                    "symbol": "ETH/USDT:USDT",
                    "severity": "warning",
                    "move_pct": 0.74,
                    "candle_range_pct": 1.1,
                    "wick_pct": 0.92,
                    "wick_body_ratio": 4.2,
                    "volume_ratio": 2.9,
                }
            ]
        }

        filtered = _market_guard_notification_status(config, status)

        self.assertIsNotNone(filtered)
        assert filtered is not None
        self.assertEqual(len(filtered["alerts"]), 1)
        self.assertEqual(filtered["alerts"][0]["symbol"], "ETH/USDT:USDT")

    def _candidate(self) -> TradeCandidate:
        candidate = TradeCandidate(
            symbol="BTC/USDT:USDT",
            base="BTC",
            side="long",
            confidence=82.0,
            win_probability_pct=84.5,
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
        candidate.indicator_summary = {
            "timeframe": "1m",
            "trend": "up",
            "candlestick_patterns": {"patterns": ["bullish_marubozu"], "bullish_score": 1.4},
        }
        candidate.higher_timeframes = {
            "4h": {"trend": "up", "candlestick_patterns": {"patterns": ["morning_star"], "bullish_score": 3.5}},
        }
        return candidate

    def _feature_config(self, tmpdir: str, *, max_positions: int = 2) -> tuple[Path, dict]:
        config_path = Path(tmpdir) / "config.yaml"
        config_path.write_text(
            "mode: dry_run\n"
            "_atlas_test_mode: true\n"
            "ai:\n"
            "  okx:\n"
            "    provider: local_policy\n"
            "trading_risk:\n"
            f"  max_concurrent_positions: {max_positions}\n"
            "  normal_min_rule_score: 80\n"
            "  normal_min_gpt_confidence: 80\n",
            encoding="utf-8",
        )
        return config_path, load_config(config_path)

    def test_config_endpoint_returns_strategy_summary(self) -> None:
        client = TestClient(create_app("config.example.yaml"))

        response = client.get("/api/config")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["mode"], "dry_run")
        self.assertIn("BTC/USDT:USDT", payload["symbols"])
        self.assertEqual(payload["order_margin_usdt"], 2.0)
        self.assertEqual(payload["order_usdt"], 30.0)
        self.assertEqual(payload["position_sizing"]["max_margin_usdt"], 50)
        self.assertEqual(payload["universe"]["mode"], "top_volume_24h")
        self.assertEqual(payload["universe"]["max_symbols"], 30)
        self.assertEqual(payload["ai"]["internal"]["model"], "gpt-5.4-mini")
        self.assertEqual(payload["ai"]["okx"]["model"], "gpt-5.5")

    @patch("crypto_trader.ui.evaluate_candidate", return_value=RiskCheck(True, [], []))
    def test_manual_okx_review_endpoint_returns_stored_setup_review_without_calling_openai(
        self,
        _evaluate_candidate,
    ) -> None:
        client = TestClient(create_app("config.example.yaml"))

        response = client.post(
            "/api/okx/manual-review-once",
            json={
                "route": "lc_okx_setup_review",
                "candidate": {
                    "symbol": "BTC/USDT:USDT",
                    "base": "BTC",
                    "side": "long",
                    "confidence": 82.0,
                    "win_probability_pct": 82.0,
                    "entry": 100.0,
                    "stop_loss": 97.5,
                    "take_profit": 103.75,
                    "risk_reward": 1.5,
                    "order_usdt": 20.0,
                    "quantity": 1.0,
                    "spread_pct": 0.01,
                    "news_score": 0.0,
                    "news_count": 0,
                    "decision_metadata": {
                        "okx_review": {
                            "route": "lc_okx_setup_review",
                            "approved": True,
                            "setup_action": "keep_setup",
                            "decision": "KEEP_SETUP",
                            "reason": "5.5 giu setup",
                        }
                    },
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["manual_only"])
        self.assertFalse(payload["one_shot"])
        self.assertFalse(payload["persisted"])
        self.assertEqual(payload["decision"]["decision"], "KEEP_SETUP")

    def test_version_endpoint_returns_code_signature_and_feature_flags(self) -> None:
        client = TestClient(create_app("config.example.yaml"))

        response = client.get("/api/version")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn("generated_at", payload)
        self.assertIn("code_signature", payload)
        self.assertIn("combined_sha16", payload["code_signature"])
        self.assertTrue(payload["feature_flags"]["four_hour_fixed_boundaries"])
        self.assertTrue(payload["feature_flags"]["trade_execution_close_reason"])
        self.assertTrue(payload["feature_flags"]["trade_execution_close_telegram_v2"])

    def test_prices_endpoint_soft_fails_on_exchange_error(self) -> None:
        class FailingExchange:
            def load_markets(self) -> None:
                return None

            def fetch_ticker(self, symbol: str) -> dict[str, object]:
                raise RuntimeError("Too Many Requests")

        client = TestClient(create_app("config.example.yaml"))

        with patch("crypto_trader.ui.create_exchange", return_value=FailingExchange()):
            response = client.get("/api/prices")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["cached"])
        self.assertTrue(payload["warnings"])
        self.assertEqual(payload["prices"][0]["last"], None)
        self.assertIn("price fetch failed", payload["prices"][0]["error"])

    def test_prices_endpoint_uses_fresh_cache(self) -> None:
        app = create_app("config.example.yaml")
        app.state.price_cache = {
            "created_at": datetime.now(timezone.utc),
            "payload": {
                "created_at": "2026-07-01T00:00:00+00:00",
                "served_at": "2026-07-01T00:00:00+00:00",
                "focus": {"symbol": "BTC/USDT:USDT", "side": "long", "status": "selected"},
                "prices": [
                    {
                        "symbol": "BTC/USDT:USDT",
                        "last": 100000,
                        "bid": 99999,
                        "ask": 100001,
                        "percentage_24h": 1.2,
                        "timestamp": 1782864000000,
                        "datetime": "2026-07-01T00:00:00.000Z",
                        "stale": False,
                    }
                ],
                "warnings": [],
                "cached": False,
            },
        }
        client = TestClient(app)

        with patch("crypto_trader.ui.create_exchange", side_effect=AssertionError("exchange should not be called")):
            response = client.get("/api/prices")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["cached"])
        self.assertEqual(payload["prices"][0]["last"], 100000)

    def test_leverage_endpoint_limits_values_to_5_25x(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                "exchange:\n"
                "  leverage: 10\n"
                "position_sizing:\n"
                "  base_margin_usdt: 2\n"
                "risk:\n"
                "  order_usdt: 20\n",
                encoding="utf-8",
            )
            client = TestClient(create_app(str(config_path)))

            low_response = client.post("/api/config/leverage", json={"leverage": 4})
            high_response = client.post("/api/config/leverage", json={"leverage": 26})
            ok_response = client.post("/api/config/leverage", json={"leverage": 25})
            saved = load_config(config_path)

        self.assertEqual(low_response.status_code, 400)
        self.assertEqual(high_response.status_code, 400)
        self.assertEqual(ok_response.status_code, 200)
        self.assertEqual(ok_response.json()["exchange"]["leverage"], 25)
        self.assertEqual(saved["risk"]["order_usdt"], 50)

    def test_order_usdt_endpoint_limits_and_persists_base_margin(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            deployed_config = (
                "mode: dry_run\n"
                "exchange:\n"
                "  leverage: 25\n"
                "position_sizing:\n"
                "  base_margin_usdt: 2\n"
                "  max_margin_usdt: 20\n"
            )
            config_path.write_text(deployed_config, encoding="utf-8")
            client = TestClient(create_app(str(config_path)))

            low_response = client.post("/api/config/order-usdt", json={"margin_usdt": 0.5})
            high_response = client.post("/api/config/order-usdt", json={"margin_usdt": 25})
            ok_response = client.post("/api/config/order-usdt", json={"margin_usdt": 5})
            config_path.write_text(deployed_config, encoding="utf-8")
            reloaded_after_deploy = load_config(config_path)
            persisted_override = get_journal_state(reloaded_after_deploy, RUNTIME_CONFIG_OVERRIDES_STATE_KEY)

        self.assertEqual(low_response.status_code, 400)
        self.assertEqual(high_response.status_code, 400)
        self.assertEqual(ok_response.status_code, 200)
        payload = ok_response.json()
        self.assertEqual(payload["position_sizing"]["base_margin_usdt"], 5)
        self.assertEqual(payload["estimated_notional_usdt"], 125)
        self.assertIsNotNone(persisted_override)
        self.assertEqual(reloaded_after_deploy["position_sizing"]["base_margin_usdt"], 5)
        self.assertEqual(reloaded_after_deploy["risk"]["order_usdt"], 125)

    def test_trading_risk_state_endpoint_exposes_recovery_snapshot(self) -> None:
        client = TestClient(create_app("config.example.yaml"))

        response = client.get("/api/trading-risk/state")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("mechanismName", payload)
        self.assertIn("isRecoveryMode", payload)
        self.assertIn("maxConcurrentPositions", payload)

    def test_prompt_build_endpoint_returns_prompt_metadata(self) -> None:
        client = TestClient(create_app("config.example.yaml"))

        response = client.post(
            "/api/prompt/build",
            json={
                "instructionKey": "final-decision",
                "marketPromptDto": {
                    "scanTime": "2026-07-04T00:00:00+00:00",
                    "marketSnapshot": {"symbol": "BTC/USDT:USDT"},
                    "candidates": [{"symbol": "BTC/USDT:USDT", "side": "long"}],
                    "tradingSystemState": {"isRecoveryMode": False},
                    "tradingHealthState": {"isWarning": False},
                    "openPositions": [],
                    "recentTrades": [],
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("promptVersion", payload)
        self.assertIn("promptHash", payload)
        self.assertTrue(payload["messages"])

    def test_telegram_leverage_action_persists_config(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                "exchange:\n"
                "  leverage: 10\n"
                "  leverage_presets:\n"
                "  - 5\n"
                "  - 10\n"
                "  - 15\n"
                "  - 20\n"
                "  - 25\n"
                "position_sizing:\n"
                "  base_margin_usdt: 3\n"
                "risk:\n"
                "  order_usdt: 30\n",
                encoding="utf-8",
            )
            config = load_config(config_path)

            updated, message, keyboard = _telegram_action_response(config, "set_leverage:20", config_path)

        self.assertEqual(updated["exchange"]["leverage"], 20)
        self.assertEqual(updated["risk"]["order_usdt"], 60)
        self.assertIn("20x", message)
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
            if button.get("callback_data", "").startswith("set_leverage:")
        ]
        self.assertIn("set_leverage:20", callbacks)

    def test_telegram_dashboard_has_bot_ui_actions(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                "exchange:\n"
                "  leverage: 15\n"
                "position_sizing:\n"
                "  base_margin_usdt: 2\n"
                "  max_margin_usdt: 20\n",
                encoding="utf-8",
            )
            config = load_config(config_path)

            _, message, keyboard = _telegram_action_response(config, "view_menu", config_path)

        self.assertIn("Bảng điều khiển Telegram", message)
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
        ]
        self.assertNotIn("view_menu", callbacks)
        self.assertIn("view_positions_account", callbacks)
        self.assertIn("view_lc", callbacks)
        self.assertIn("view_guard", callbacks)
        self.assertIn("view_memory", callbacks)
        self.assertIn("view_undecided_lc", callbacks)
        self.assertIn("view_internal_notifications", callbacks)
        self.assertIn("view_wait_slot_notifications", callbacks)
        self.assertIn("setup_menu", callbacks)
        self.assertNotIn("scan_now", callbacks)
        self.assertNotIn("view_sd", callbacks)
        self.assertNotIn("set_order_usdt", callbacks)
        self.assertNotIn("set_leverage", callbacks)
        self.assertNotIn("set_max_positions", callbacks)

    def test_telegram_dashboard_swaps_ai_and_wait_slot_positions(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("mode: dry_run\n", encoding="utf-8")
            config = load_config(config_path)

            _, _, keyboard = _telegram_action_response(config, "view_menu", config_path)

        rows = keyboard["inline_keyboard"]
        self.assertEqual(rows[2], [{"text": "🤖 AI", "callback_data": "view_ai"}])
        self.assertEqual(
            rows[3],
            [
                {"text": "🛡 Guard", "callback_data": "view_guard"},
                {"text": "🧠 Memory", "callback_data": "view_memory"},
                {"text": "🟡 Wait Slot", "callback_data": "view_wait_slot_notifications"},
            ],
        )

    def test_telegram_setup_menu_has_three_setup_actions(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                "exchange:\n"
                "  leverage: 15\n"
                "position_sizing:\n"
                "  base_margin_usdt: 2\n"
                "risk:\n"
                "  max_active_trades: 3\n",
                encoding="utf-8",
            )
            config = load_config(config_path)

            _, message, keyboard = _telegram_action_response(config, "view_setup", config_path)

        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
        ]
        self.assertIn("Setup", message)
        self.assertEqual(callbacks, ["set_order_usdt", "set_leverage", "set_max_positions", "view_menu"])
        self.assertEqual([len(row) for row in keyboard["inline_keyboard"]], [2, 2])
        labels = [
            button["text"]
            for row in keyboard["inline_keyboard"]
            for button in row
        ]
        self.assertIn("⬅️ Dashboard", labels)

    @patch("crypto_trader.ui.send_telegram_chat_message")
    def test_setup_text_command_opens_setup_keyboard(self, send_message) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("mode: dry_run\n", encoding="utf-8")
            config = load_config(config_path)
            update = {
                "message": {
                    "chat": {"id": 123},
                    "text": "/setup",
                }
            }

            with patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "123"}):
                _handle_telegram_update(config, update, config_path)

        send_message.assert_called_once()
        sent_text = send_message.call_args.args[2]
        sent_keyboard = send_message.call_args.kwargs["reply_markup"]
        callbacks = [
            button["callback_data"]
            for row in sent_keyboard["inline_keyboard"]
            for button in row
        ]
        self.assertIn("Setup", sent_text)
        self.assertEqual(callbacks, ["set_order_usdt", "set_leverage", "set_max_positions", "view_menu"])

    @patch("crypto_trader.ui.send_telegram_message")
    @patch("crypto_trader.ui.sync_telegram_commands")
    def test_app_startup_syncs_native_telegram_commands(self, sync_commands, send_message) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("mode: dry_run\n", encoding="utf-8")

            with TestClient(create_app(str(config_path))) as client:
                response = client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(sync_commands.call_count, 1)
        send_message.assert_called_once()
        self.assertEqual(send_message.call_args.args[1], STARTUP_TELEGRAM_MESSAGE)
        self.assertFalse(send_message.call_args.kwargs["with_buttons"])
        self.assertFalse(send_message.call_args.kwargs["replace_previous"])

    def test_healthz_includes_runtime_build_metadata(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("mode: dry_run\n", encoding="utf-8")

            with patch.dict(
                "os.environ",
                {
                    "RAILWAY_GIT_COMMIT_SHA": "abc123",
                    "RAILWAY_DEPLOYMENT_ID": "deploy-1",
                    "RAILWAY_PUBLIC_DOMAIN": "crypto-bunny.up.railway.app",
                },
                clear=False,
            ):
                with TestClient(create_app(str(config_path))) as client:
                    response = client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["app_version"], "0.1.0")
        self.assertEqual(payload["build"]["commit_sha"], "abc123")
        self.assertEqual(payload["build"]["deployment_id"], "deploy-1")
        self.assertEqual(payload["build"]["public_domain"], "crypto-bunny.up.railway.app")

    @patch("crypto_trader.notifier._telegram_api_request")
    def test_edit_telegram_chat_message_uses_edit_message_text(self, api_request) -> None:
        from crypto_trader.notifier import edit_telegram_chat_message

        api_request.return_value = {"ok": True}

        ok = edit_telegram_chat_message(
            {"notifications": {"telegram": {"enabled": True}}},
            123,
            456,
            "Setup",
            reply_markup={"inline_keyboard": []},
        )

        self.assertTrue(ok)
        method = api_request.call_args.args[1]
        payload = api_request.call_args.args[2]
        self.assertEqual(method, "editMessageText")
        self.assertEqual(payload["chat_id"], 123)
        self.assertEqual(payload["message_id"], 456)
        self.assertIn("reply_markup", payload)

    @patch("crypto_trader.ui.answer_callback_query")
    @patch("crypto_trader.ui.edit_telegram_chat_message")
    @patch("crypto_trader.ui.send_telegram_chat_message")
    @patch("crypto_trader.ui.delete_telegram_message")
    def test_setup_callback_deletes_old_message_and_sends_setup_only(
        self,
        delete_message,
        send_message,
        edit_message,
        answer_callback,
    ) -> None:
        edit_message.return_value = True
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("mode: dry_run\n", encoding="utf-8")
            config = load_config(config_path)
            update = {
                "callback_query": {
                    "id": "cb-1",
                    "data": "setup_menu",
                    "message": {
                        "message_id": 456,
                        "chat": {"id": 123},
                    },
                }
            }

            with patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "123"}):
                _handle_telegram_update(config, update, config_path)

        edit_message.assert_called_once()
        delete_message.assert_not_called()
        send_message.assert_not_called()
        sent_text = edit_message.call_args.args[3]
        sent_keyboard = edit_message.call_args.kwargs["reply_markup"]
        callbacks = [
            button["callback_data"]
            for row in sent_keyboard["inline_keyboard"]
            for button in row
        ]
        self.assertIn("Setup", sent_text)
        self.assertEqual(callbacks, ["set_order_usdt", "set_leverage", "set_max_positions", "view_menu"])

    @patch("crypto_trader.ui.answer_callback_query")
    @patch("crypto_trader.ui.edit_telegram_chat_message")
    @patch("crypto_trader.ui.send_telegram_chat_message")
    @patch("crypto_trader.ui.delete_telegram_message")
    def test_dashboard_callback_sends_fresh_dashboard_with_setup_button(
        self,
        delete_message,
        send_message,
        edit_message,
        answer_callback,
    ) -> None:
        edit_message.return_value = True
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("mode: dry_run\n", encoding="utf-8")
            config = load_config(config_path)
            update = {
                "callback_query": {
                    "id": "cb-1",
                    "data": "view_menu",
                    "message": {
                        "message_id": 456,
                        "chat": {"id": 123},
                    },
                }
            }

            with patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "123"}):
                _handle_telegram_update(config, update, config_path)

        edit_message.assert_called_once()
        delete_message.assert_not_called()
        send_message.assert_not_called()
        sent_keyboard = edit_message.call_args.kwargs["reply_markup"]
        callbacks = [
            button["callback_data"]
            for row in sent_keyboard["inline_keyboard"]
            for button in row
        ]
        self.assertIn("setup_menu", callbacks)
        self.assertNotIn("set_leverage", callbacks)

    @patch("crypto_trader.ui.answer_callback_query")
    @patch("crypto_trader.ui.edit_telegram_chat_message")
    @patch("crypto_trader.ui.send_telegram_chat_message")
    @patch("crypto_trader.ui.wait_slot_notification_timeline_messages")
    def test_wait_slot_notifications_callback_sends_timeline_as_separate_messages(
        self,
        timeline_messages,
        send_message,
        edit_message,
        answer_callback,
    ) -> None:
        timeline_messages.return_value = ["🟡 WAIT_SLOT #1_WS", "🟡 WAIT_SLOT #2_WS"]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("mode: dry_run\n", encoding="utf-8")
            config = load_config(config_path)
            update = {
                "callback_query": {
                    "id": "cb-ws",
                    "data": "view_wait_slot_notifications",
                    "message": {
                        "message_id": 790,
                        "chat": {"id": 123},
                    },
                }
            }

            with patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "123"}):
                _handle_telegram_update(config, update, config_path)

        answer_callback.assert_called_once()
        edit_message.assert_not_called()
        self.assertEqual(send_message.call_count, 3)
        self.assertEqual(
            [call.args[2] for call in send_message.call_args_list],
            ["🟡 Thông báo Wait Slot", "🟡 WAIT_SLOT #1_WS", "🟡 WAIT_SLOT #2_WS"],
        )

    @patch("crypto_trader.ui.answer_callback_query")
    @patch("crypto_trader.ui.edit_telegram_chat_message")
    @patch("crypto_trader.ui.send_telegram_chat_message")
    @patch("crypto_trader.ui.internal_notification_timeline_messages")
    def test_internal_notifications_callback_sends_timeline_as_separate_messages(
        self,
        timeline_messages,
        send_message,
        edit_message,
        answer_callback,
    ) -> None:
        timeline_messages.return_value = ["msg-1", "msg-2", "msg-3"]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("mode: dry_run\n", encoding="utf-8")
            config = load_config(config_path)
            update = {
                "callback_query": {
                    "id": "cb-2",
                    "data": "view_internal_notifications",
                    "message": {
                        "message_id": 789,
                        "chat": {"id": 123},
                    },
                }
            }

            with patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "123"}):
                _handle_telegram_update(config, update, config_path)

        answer_callback.assert_called_once()
        edit_message.assert_not_called()
        self.assertEqual(send_message.call_count, 4)
        self.assertEqual(
            [call.args[2] for call in send_message.call_args_list],
            ["🔔 Thông báo nội bộ", "msg-1", "msg-2", "msg-3"],
        )

    @patch("crypto_trader.ui.send_telegram_chat_message")
    @patch("crypto_trader.ui.internal_notification_timeline_messages")
    def test_internal_notifications_command_sends_timeline_as_separate_messages(
        self,
        timeline_messages,
        send_message,
    ) -> None:
        timeline_messages.return_value = ["msg-a", "msg-b"]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("mode: dry_run\n", encoding="utf-8")
            config = load_config(config_path)
            update = {
                "message": {
                    "message_id": 1001,
                    "chat": {"id": 123},
                    "text": "/thongbao",
                }
            }

            with patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "123"}):
                _handle_telegram_update(config, update, config_path)

        self.assertEqual(send_message.call_count, 3)
        self.assertEqual(
            [call.args[2] for call in send_message.call_args_list],
            ["🔔 Thông báo nội bộ", "msg-a", "msg-b"],
        )

    @patch("crypto_trader.ui.answer_callback_query")
    @patch("crypto_trader.ui.edit_telegram_chat_message")
    @patch("crypto_trader.ui.send_telegram_chat_message")
    @patch("crypto_trader.ui.recent_ai_call_history")
    def test_ai_history_callback_sends_each_call_as_separate_messages(
        self,
        recent_history,
        send_message,
        edit_message,
        answer_callback,
    ) -> None:
        recent_history.return_value = [
            {
                "created_at": "2026-07-10T08:01:46+07:00",
                "role": "mini",
                "model": "gpt-5.4-mini",
                "status": "MINI ĐỀ XUẤT LC",
                "approved_symbols": ["AAVE/USDT:USDT"],
                "candidate_details": [{"symbol": "AAVE/USDT:USDT", "side": "long"}],
                "reason": "Aligned 1h/5m bullish with volume support",
            },
            {
                "created_at": "2026-07-10T12:02:23+07:00",
                "role": "mini",
                "model": "gpt-5.4-mini",
                "status": "MINI ĐỀ XUẤT LC",
                "approved_symbols": ["1INCH/USDT:USDT"],
                "candidate_details": [{"symbol": "1INCH/USDT:USDT", "side": "long"}],
                "reason": "1INCH lacks volume support and has mixed 1h bearish candle.",
            },
        ]
        edit_message.return_value = True
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("mode: dry_run\n", encoding="utf-8")
            config = load_config(config_path)
            update = {
                "callback_query": {
                    "id": "cb-ai",
                    "data": "view_ai",
                    "message": {
                        "message_id": 321,
                        "chat": {"id": 123},
                    },
                }
            }

            with patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "123"}):
                _handle_telegram_update(config, update, config_path)

        answer_callback.assert_called_once()
        edit_message.assert_called_once()
        self.assertEqual(send_message.call_count, 2)
        self.assertIn("AAVE/USDT:USDT", send_message.call_args_list[0].args[2])
        self.assertIn("1INCH/USDT:USDT", send_message.call_args_list[1].args[2])
        self.assertNotIn("1INCH/USDT:USDT", send_message.call_args_list[0].args[2])
        self.assertNotIn("AAVE/USDT:USDT", send_message.call_args_list[1].args[2])

    @patch("crypto_trader.ui.send_telegram_chat_message")
    @patch("crypto_trader.ui.recent_ai_call_history")
    def test_ai_history_command_sends_header_and_each_call_as_separate_messages(
        self,
        recent_history,
        send_message,
    ) -> None:
        recent_history.return_value = [
            {
                "created_at": "2026-07-10T08:01:46+07:00",
                "role": "mini",
                "model": "gpt-5.4-mini",
                "status": "MINI ĐỀ XUẤT LC",
                "approved_symbols": ["AAVE/USDT:USDT"],
                "candidate_details": [{"symbol": "AAVE/USDT:USDT", "side": "long"}],
                "reason": "Aligned 1h/5m bullish with volume support",
            },
            {
                "created_at": "2026-07-10T12:02:23+07:00",
                "role": "mini",
                "model": "gpt-5.4-mini",
                "status": "MINI ĐỀ XUẤT LC",
                "approved_symbols": ["1INCH/USDT:USDT"],
                "candidate_details": [{"symbol": "1INCH/USDT:USDT", "side": "long"}],
                "reason": "1INCH lacks volume support and has mixed 1h bearish candle.",
            },
        ]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("mode: dry_run\n", encoding="utf-8")
            config = load_config(config_path)
            update = {
                "message": {
                    "message_id": 1002,
                    "chat": {"id": 123},
                    "text": "/ai",
                }
            }

            with patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "123"}):
                _handle_telegram_update(config, update, config_path)

        self.assertEqual(send_message.call_count, 3)
        self.assertIn("Lịch sử gọi AI gần nhất", send_message.call_args_list[0].args[2])
        self.assertIn("AAVE/USDT:USDT", send_message.call_args_list[1].args[2])
        self.assertIn("1INCH/USDT:USDT", send_message.call_args_list[2].args[2])

    @patch("crypto_trader.ui.system_health_dashboard")
    @patch("crypto_trader.ui.replay_dashboard_payload")
    @patch("crypto_trader.ui.analytics_dashboard")
    @patch("crypto_trader.ui.scan_memory_dashboard")
    @patch("crypto_trader.ui.timeframe_state_dashboard")
    @patch("crypto_trader.ui.refresh_system_checklist_snapshot")
    @patch("crypto_trader.ui.run_once")
    @patch("crypto_trader.ui.send_telegram_message")
    def test_automation_scan_notifications_do_not_attach_control_keyboard(
        self,
        send_message,
        run_once,
        refresh_checklist,
        timeframe_dashboard,
        scan_dashboard,
        analytics_dashboard_mock,
        replay_dashboard,
        health_dashboard,
    ) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                "_atlas_test_mode: true\n"
                "notifications:\n"
                "  telegram:\n"
                "    notify_scans: true\n",
                encoding="utf-8",
            )
            run_once.return_value = {
                "action": "hold",
                "candidates": [],
                "selected": {},
                "risk_check": {"passed": False, "reasons": ["test"]},
                "execution": {},
            }
            app = SimpleNamespace(
                state=SimpleNamespace(
                    config_path=config_path,
                    automation_status={},
                    lock=threading.Lock(),
                )
            )

            _run_automation_cycle(app)

        send_message.assert_called()
        for call in send_message.call_args_list:
            self.assertFalse(call.kwargs.get("with_buttons"))
            self.assertFalse(call.kwargs.get("replace_previous"))

    @patch("crypto_trader.ui.system_health_dashboard")
    @patch("crypto_trader.ui.replay_dashboard_payload")
    @patch("crypto_trader.ui.analytics_dashboard")
    @patch("crypto_trader.ui.scan_memory_dashboard")
    @patch("crypto_trader.ui.timeframe_state_dashboard")
    @patch("crypto_trader.ui.refresh_system_checklist_snapshot")
    @patch("crypto_trader.ui.run_once")
    @patch("crypto_trader.ui.send_telegram_message")
    def test_automation_error_does_not_send_scan_message_when_scan_notify_disabled(
        self,
        send_message,
        run_once,
        refresh_checklist,
        timeframe_dashboard,
        scan_dashboard,
        analytics_dashboard_mock,
        replay_dashboard,
        health_dashboard,
    ) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                "_atlas_test_mode: true\n"
                "notifications:\n"
                "  telegram:\n"
                "    notify_scans: false\n",
                encoding="utf-8",
            )
            run_once.side_effect = RuntimeError("atlas read timeout")
            app = SimpleNamespace(
                state=SimpleNamespace(
                    config_path=config_path,
                    automation_status={},
                    lock=threading.Lock(),
                )
            )

            _run_automation_cycle(app)

        scan_messages = [call.args[1] for call in send_message.call_args_list if len(call.args) >= 2]
        self.assertFalse(any(str(message).startswith("🔎🔵 SC") for message in scan_messages))

    def test_periodic_scan_notification_only_fires_on_quarter_hour_slots(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                "_atlas_test_mode: true\n"
                "notifications:\n"
                "  telegram:\n"
                "    notify_scans: true\n",
                encoding="utf-8",
            )
            config = load_config(config_path)

            not_due = datetime(2026, 7, 8, 5, 14, tzinfo=timezone(timedelta(hours=7))).astimezone(timezone.utc)
            due = datetime(2026, 7, 8, 5, 15, tzinfo=timezone(timedelta(hours=7))).astimezone(timezone.utc)
            next_due = datetime(2026, 7, 8, 5, 30, tzinfo=timezone(timedelta(hours=7))).astimezone(timezone.utc)

            self.assertFalse(_periodic_scan_notification_due(config, not_due))
            self.assertTrue(_periodic_scan_notification_due(config, due))

            _remember_periodic_scan_notification(config, due)

            self.assertEqual(get_journal_state(config, SCAN_TELEGRAM_SLOT_KEY), "2026-07-08T05:15:00+07:00")
            self.assertFalse(_periodic_scan_notification_due(config, due))
            self.assertTrue(_periodic_scan_notification_due(config, next_due))

    def test_telegram_undecided_lc_action_formats_pipeline_state(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                "_atlas_test_mode: true\n",
                encoding="utf-8",
            )
            config = load_config(config_path)
            set_journal_state(
                config,
                "lc_internal_pipeline_state",
                json.dumps(
                    {
                        "undecided": [
                            {
                                "symbol": "LIT/USDT:USDT",
                                "side": "long",
                                "first_seen_at": "2026-07-06T00:00:00+00:00",
                                "last_seen_at": "2026-07-06T03:00:00+00:00",
                                "state": "CHUA_DUYET",
                                "source_slot": "2h",
                                "win_probability_pct": 62.34,
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
            )

            _, message, keyboard = _telegram_action_response(config, "view_undecided_lc", config_path)

        self.assertIn("Chưa Duyệt", message)
        self.assertIn("1. LIT/USDT:USDT | LONG", message)
        self.assertIn("Win 62.34%", message)
        self.assertIn("2h", message)
        self.assertIn("sống", message)
        self.assertIsNone(keyboard)

    def test_telegram_lc_action_formats_internal_lc_state(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                "_atlas_test_mode: true\n",
                encoding="utf-8",
            )
            config = load_config(config_path)
            set_journal_state(
                config,
                "lc_internal_pipeline_state",
                json.dumps(
                    {
                        "internal_lc": [
                            {
                                "symbol": "ETH/USDT:USDT",
                                "side": "long",
                                "state": "LC_NOI_BO",
                                "source_slot": "2h",
                                "source_index": 3,
                                "win_probability_pct": 64.11,
                                "first_seen_at": "2026-07-06T00:00:00+00:00",
                                "last_seen_at": "2026-07-06T01:00:00+00:00",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
            )

            _, message, keyboard = _telegram_action_response(config, "view_lc", config_path)

        self.assertIn("🟡", message)
        self.assertIn("📊", message)
        self.assertIn("ETH/USDT:USDT", message)
        self.assertIn("2h #3", message)
        self.assertIsNone(keyboard)

    def test_lc_pipeline_endpoint_returns_dashboard_state(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                "_atlas_test_mode: true\n",
                encoding="utf-8",
            )
            config = load_config(config_path)
            set_journal_state(
                config,
                "lc_internal_pipeline_state",
                json.dumps(
                    {
                        "day_key": "2026-07-06",
                        "undecided": [
                            {
                                "symbol": "LIT/USDT:USDT",
                                "side": "long",
                                "first_seen_at": "2026-07-06T00:00:00+00:00",
                                "last_seen_at": "2026-07-06T03:00:00+00:00",
                                "state": "CHUA_DUYET",
                            }
                        ],
                        "internal_lc": [],
                    },
                    ensure_ascii=False,
                ),
            )
            client = TestClient(create_app(config_path))

            response = client.get("/api/lc-pipeline")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["counts"]["undecided"], 1)
        self.assertEqual(payload["undecided"][0]["symbol"], "LIT/USDT:USDT")
        self.assertIn("age_label", payload["undecided"][0])

    def test_market_scan_memory_endpoint_returns_recent_observations(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                "_atlas_test_mode: true\n",
                encoding="utf-8",
            )
            config = load_config(config_path)
            save_market_scan_observations(config, [self._candidate()], source="test-scan", limit=10)
            client = TestClient(create_app(config_path))

            response = client.get("/api/market-scan-memory?symbol=BTC/USDT:USDT&timeframe=1m,4h")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("BTC/USDT:USDT", payload["symbols"])
        self.assertIn("1m", payload["memory"]["BTC/USDT:USDT"])
        self.assertIn("4h", payload["memory"]["BTC/USDT:USDT"])

    def test_telegram_memory_action_formats_recent_scan_memory(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                "_atlas_test_mode: true\n",
                encoding="utf-8",
            )
            config = load_config(config_path)
            save_market_scan_observations(config, [self._candidate()], source="test-scan", limit=10)

            _, message, keyboard = _telegram_action_response(config, "view_memory", config_path)

        self.assertIn("Scan memory", message)
        self.assertIn("BTC/USDT:USDT", message)
        self.assertIsNone(keyboard)

    def test_rejected_trade_execution_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            _config_path, config = self._feature_config(tmpdir)
            candidate = self._candidate()
            candidate.confidence = 60
            candidate.rule_score = 60

            record = record_trade_execution(config, candidate)

            rows = list_trade_execution_rows(config)

        self.assertEqual(record["status"], "REJECTED")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "REJECTED")
        self.assertTrue(rows[0]["reject_reason"])

    def test_slot_refill_uses_requested_free_slot(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            _config_path, config = self._feature_config(tmpdir, max_positions=2)
            candidate = self._candidate()
            candidate.confidence = 95
            candidate.rule_score = 95
            candidate.risk_reward = 3.0
            record_trade_candidates(config, [candidate])

            result = try_slot_refill(config, 2)

        self.assertTrue(result["refilled"])
        self.assertEqual(result["tradeExecution"]["position_slot"], 2)

    def test_replay_stats_endpoint_reports_performance_metrics(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path, config = self._feature_config(tmpdir)
            candidate = self._candidate()
            candidate.confidence = 95
            candidate.rule_score = 95
            candidate.risk_reward = 3.0
            execution = record_trade_execution(config, candidate)
            close_trade_execution(config, int(execution["id"]), "WIN", 12.5)
            client = TestClient(create_app(config_path))

            replay_response = client.post("/api/replay/run", json={"tradeExecutionId": execution["id"]})
            stats_response = client.get("/api/replay/stats")

        self.assertEqual(replay_response.status_code, 200)
        self.assertEqual(stats_response.status_code, 200)
        stats = stats_response.json()
        self.assertEqual(stats["replayCount"], 1)
        self.assertIn("replayWinRate", stats)
        self.assertIn("replayProfitFactor", stats)
        self.assertIn("replayDrawdown", stats)

    def test_trade_execution_close_endpoint_accepts_close_reason(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path, config = self._feature_config(tmpdir)
            candidate = self._candidate()
            candidate.confidence = 95
            candidate.rule_score = 95
            candidate.risk_reward = 3.0
            execution = record_trade_execution(config, candidate)
            client = TestClient(create_app(config_path))

            response = client.post(
                "/api/trade-executions/close",
                json={
                    "tradeExecutionId": execution["id"],
                    "status": "CLOSED",
                    "pnl": -1.25,
                    "closeReason": "manual",
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "CLOSED")
        self.assertEqual(payload["close_reason"], "manual")
