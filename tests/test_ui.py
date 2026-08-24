from __future__ import annotations

import tempfile
import json
import threading
import time
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
from crypto_trader.trend_scan import TREND_WATCHLIST_STATE_KEY
from crypto_trader.ui import (
    SCAN_TELEGRAM_SLOT_KEY,
    STARTUP_TELEGRAM_MESSAGE,
    _format_ai_call_history_view,
    _compact_system_checklist_payload,
    _compact_trend_scan_payload,
    _compact_trend_setup_review_payload,
    _handle_telegram_update,
    _market_guard_notification_status,
    _manual_target_fast_sync_interval,
    _manual_target_fast_sync_worker,
    _open_okx_positions,
    _notify_system_error,
    _periodic_scan_notification_due,
    _remember_periodic_scan_notification,
    _run_automation_cycle,
    _run_lc_pipeline_slot_cycle,
    _run_lc_pipeline_worker_cycle,
    _sync_runtime_state_for_automation,
    _telegram_action_response,
    create_app,
)


class StopAfterWait:
    def __init__(self) -> None:
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def wait(self, _seconds: float) -> bool:
        self._set = True
        return True


class UiTest(TestCase):
    def test_manual_target_fast_sync_interval_defaults_to_five_seconds(self) -> None:
        self.assertEqual(_manual_target_fast_sync_interval({}), 5)
        self.assertEqual(
            _manual_target_fast_sync_interval(
                {"runtime_sync": {"manual_target_fast_sync_interval_seconds": 1}}
            ),
            5,
        )

    def test_manual_target_fast_sync_reports_skipped_busy_when_lock_is_held(self) -> None:
        lock = threading.Lock()
        lock.acquire()
        fast_sync_lock = threading.Lock()
        app = SimpleNamespace(
            state=SimpleNamespace(
                config_path="config.test.yaml",
                automation_stop=StopAfterWait(),
                lock=lock,
                manual_target_fast_sync_lock=fast_sync_lock,
                manual_target_fast_sync_status={},
            )
        )
        config = {
            "runtime_sync": {
                "manual_target_fast_sync_enabled": True,
                "manual_target_fast_sync_interval_seconds": 5,
            },
            "manual_position_targets": {"enabled": True},
        }
        try:
            with patch("crypto_trader.ui.load_config", return_value=config), patch(
                "crypto_trader.ui._automation_enabled", return_value=True
            ):
                _manual_target_fast_sync_worker(app)
        finally:
            lock.release()

        self.assertEqual(app.state.manual_target_fast_sync_status["status"], "skipped_busy")
        self.assertEqual(app.state.manual_target_fast_sync_status["reason"], "automation_lock_held")
        self.assertEqual(app.state.manual_target_fast_sync_status["interval_seconds"], 5)

    def test_manual_target_fast_sync_does_not_hold_automation_lock(self) -> None:
        lock = threading.Lock()
        fast_sync_lock = threading.Lock()
        fast_sync_lock.acquire()
        app = SimpleNamespace(
            state=SimpleNamespace(
                config_path="config.test.yaml",
                automation_stop=StopAfterWait(),
                lock=lock,
                manual_target_fast_sync_lock=fast_sync_lock,
                manual_target_fast_sync_status={},
            )
        )
        config = {
            "runtime_sync": {
                "manual_target_fast_sync_enabled": True,
                "manual_target_fast_sync_interval_seconds": 5,
            },
            "manual_position_targets": {"enabled": True},
        }
        try:
            with patch("crypto_trader.ui.load_config", return_value=config), patch(
                "crypto_trader.ui._automation_enabled", return_value=True
            ):
                _manual_target_fast_sync_worker(app)
        finally:
            fast_sync_lock.release()

        self.assertFalse(lock.locked())
        self.assertEqual(app.state.manual_target_fast_sync_status["status"], "skipped_busy")
        self.assertEqual(app.state.manual_target_fast_sync_status["reason"], "fast_sync_already_running")

    def test_automation_runtime_sync_times_out(self) -> None:
        app = SimpleNamespace(state=SimpleNamespace(runtime_sync_lock=threading.Lock()))
        done = threading.Event()

        def slow_sync(_config: dict) -> dict:
            time.sleep(0.2)
            done.set()
            return {"ok": True}

        with patch("crypto_trader.ui.sync_runtime_state", side_effect=slow_sync), patch(
            "crypto_trader.ui._automation_runtime_sync_timeout", return_value=0.01
        ):
            result = _sync_runtime_state_for_automation(
                app,
                {"runtime_sync": {"automation_timeout_seconds": 0.01}},
            )

        self.assertTrue(result["timeout"])
        self.assertEqual(result["timeout_seconds"], 0.01)
        self.assertTrue(done.wait(1.0))
        self.assertFalse(app.state.runtime_sync_lock.locked())

    def test_automation_runtime_sync_skips_when_previous_sync_is_running(self) -> None:
        runtime_sync_lock = threading.Lock()
        runtime_sync_lock.acquire()
        app = SimpleNamespace(state=SimpleNamespace(runtime_sync_lock=runtime_sync_lock))
        try:
            result = _sync_runtime_state_for_automation(app, {"runtime_sync": {}})
        finally:
            runtime_sync_lock.release()

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "runtime_sync_already_running")

    def test_dashboard_compact_payload_keeps_selected_module_detail_only(self) -> None:
        payload = {
            "date": "2026-08-01",
            "ok": True,
            "ok_count": 1,
            "total": 1,
            "items": [
                {
                    "name": "Runtime",
                    "status": "ok",
                    "detail": "OK",
                    "evidence": [{"label": f"e{i}", "value": i, "extra": "drop"} for i in range(20)],
                }
            ],
            "modules": [
                {
                    "number": 1,
                    "name": "Light",
                    "status": "ok",
                    "stats": [{"label": "a", "value": 1, "meaning": "m"}],
                    "file": {"preview": ["heavy"] * 100},
                },
                {
                    "number": 2,
                    "name": "Open",
                    "status": "ok",
                    "stats": [],
                    "file": {"preview": ["keep"]},
                },
            ],
        }

        compact = _compact_system_checklist_payload(payload, selected_module_key="2::Open")

        self.assertNotIn("file", compact["modules"][0])
        self.assertEqual(compact["modules"][1]["file"], {"preview": ["keep"]})
        self.assertEqual(len(compact["items"][0]["evidence"]), 8)

    def test_dashboard_compact_payload_keeps_trade_execution_open_items(self) -> None:
        payload = {
            "date": "2026-08-02",
            "ok": True,
            "modules": [
                {
                    "number": 15,
                    "name": "Thực thi giao dịch & Quản lý vị thế",
                    "status": "ok",
                    "stats": [{"label": "open_positions", "value": 1}],
                    "trade_execution": {
                        "open_count": 1,
                        "partial_done_count": 0,
                        "waiting_partial_count": 1,
                        "trailing_config": {"partial_enabled": True},
                        "open_items": [
                            {
                                "symbol": "XRP/USDT:USDT",
                                "side": "LONG",
                                "profit_protection_levels": {"partial_30": {"price": 1.2}},
                            }
                        ],
                        "recent_closed": [{"symbol": "BTC/USDT:USDT", "status": "WIN"}],
                        "heavy_debug": ["drop"],
                    },
                }
            ],
        }

        compact = _compact_system_checklist_payload(payload)
        trade_execution = compact["modules"][0]["trade_execution"]

        self.assertEqual(trade_execution["open_count"], 1)
        self.assertEqual(trade_execution["open_items"][0]["symbol"], "XRP/USDT:USDT")
        self.assertEqual(trade_execution["recent_closed"][0]["symbol"], "BTC/USDT:USDT")
        self.assertEqual(trade_execution["trailing_config"], {"partial_enabled": True})
        self.assertNotIn("heavy_debug", trade_execution)

    def test_trend_log_compact_payload_strips_heavy_raw_fields(self) -> None:
        trend = _compact_trend_scan_payload(
            {
                "created_at": "2026-08-01T00:00:00+00:00",
                "strong_symbols": [
                    {
                        "symbol": "CAP/USDT:USDT",
                        "trend_score": 70,
                        "ai_ready": True,
                        "frames": [{"timeframe": "4h"}],
                    }
                ],
            }
        )
        review = _compact_trend_setup_review_payload(
            {
                "setup_proposal": {
                    "symbol": "CAP/USDT:USDT",
                    "risk_reward": 1.75,
                    "selected_setup_method": "structure_swing_to_previous_extreme",
                    "setup_quality_score": 77.6,
                    "setup_quality_grade": "B",
                    "setup_quality_components": {"entry_score": 82},
                    "setup_candidates": [
                        {"entry_type": "pullback", "quality_score": 77.6, "warnings": ["fib_pullback_zone_mismatch"], "raw": "drop"}
                    ],
                    "frames": [1],
                },
                "ai_review": {"decision": "REVIEW", "reason": "wait", "evidence": ["heavy"]},
            }
        )

        self.assertNotIn("frames", trend["strong_symbols"][0])
        self.assertNotIn("frames", review["setup_proposal"])
        self.assertNotIn("evidence", review["ai_review"])
        self.assertEqual(review["setup_proposal"]["selected_setup_method"], "structure_swing_to_previous_extreme")
        self.assertEqual(review["setup_proposal"]["setup_quality_grade"], "B")
        self.assertEqual(review["setup_quality_components"], {"entry_score": 82})
        self.assertEqual(review["setup_candidates"][0]["entry_type"], "pullback")
        self.assertNotIn("raw", review["setup_candidates"][0])

    @patch("crypto_trader.ui._is_railway_runtime", return_value=True)
    @patch("crypto_trader.ui.send_telegram_message", return_value=True)
    def test_system_error_notification_is_vietnamese_and_deduplicated(self, send_message, _railway_runtime) -> None:
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

    @patch("crypto_trader.ui.recent_ai_call_history")
    def test_ai_history_view_formats_created_at_in_vietnam_time(self, recent_history) -> None:
        recent_history.return_value = [
            {
                "created_at": "2026-07-23T17:02:46+00:00",
                "role": "mini",
                "model": "gpt-5.4-mini",
                "status": "NO_TRADE",
                "symbols": ["XRP/USDT:USDT"],
            }
        ]

        message = _format_ai_call_history_view({"timezone": "Asia/Ho_Chi_Minh"})

        self.assertIn("24/07/2026 00:02:46 VN", message)

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
        self.assertEqual(payload["universe"]["max_symbols"], 40)
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

    def test_okx_positions_uses_pending_algo_targets_when_open_orders_are_empty(self) -> None:
        class AlgoExchange:
            markets_by_id = {"ETC-USDT-SWAP": {"symbol": "ETC/USDT:USDT"}}

            def load_markets(self) -> None:
                return None

            def fetch_open_orders(self) -> list[dict[str, object]]:
                return []

            def fetch_positions(self) -> list[dict[str, object]]:
                return [
                    {
                        "symbol": "ETC/USDT:USDT",
                        "side": "long",
                        "contracts": 1,
                        "entryPrice": 20,
                        "info": {"pos": "1", "posSide": "long", "instId": "ETC-USDT-SWAP"},
                    }
                ]

            def privateGetTradeOrdersAlgoPending(self, params: dict[str, object]) -> dict[str, object]:
                if params.get("ordType") != "oco":
                    return {"data": []}
                return {
                    "data": [
                        {
                            "algoId": "algo-1",
                            "instId": "ETC-USDT-SWAP",
                            "posSide": "long",
                            "side": "sell",
                            "ordType": "oco",
                            "slTriggerPx": "18.5",
                            "tpTriggerPx": "23.0",
                        }
                    ]
                }

        with patch("crypto_trader.ui.create_exchange", return_value=AlgoExchange()):
            payload = _open_okx_positions({"mode": "live"})

        self.assertEqual(payload["positions"][0]["stop_loss"], 18.5)
        self.assertEqual(payload["positions"][0]["take_profit"], 23.0)
        self.assertEqual(payload["positions"][0]["tp_sl_status"], "ok")
        self.assertEqual(payload["algo_target_count"], 1)

    def test_okx_position_history_endpoint_uses_raw_okx_history(self) -> None:
        class HistoryExchange:
            def __init__(self) -> None:
                self.params: dict[str, object] | None = None

            def load_markets(self) -> None:
                return None

            def privateGetAccountPositionsHistory(self, params: dict[str, object]) -> dict[str, object]:
                self.params = params
                return {
                    "data": [
                        {
                            "instId": "TAO-USDT-SWAP",
                            "posId": "3740224303088656384",
                            "realizedPnl": "-13.74",
                            "openAvgPx": "197.2",
                            "closeAvgPx": "188.4",
                        }
                    ]
                }

        exchange = HistoryExchange()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("mode: live\n", encoding="utf-8")
            app = create_app(str(config_path))
            client = TestClient(app)

            with patch("crypto_trader.ui.create_exchange", return_value=exchange):
                response = client.get(
                    "/api/okx-position-history",
                    params={
                        "instId": "TAO-USDT-SWAP",
                        "posId": "3740224303088656384",
                        "limit": 50,
                    },
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(exchange.params, {"instType": "SWAP", "limit": "50", "instId": "TAO-USDT-SWAP", "posId": "3740224303088656384"})
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["realized_pnl"], "-13.74")

    def test_okx_position_history_endpoint_handles_missing_selector(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("mode: live\n", encoding="utf-8")
            app = create_app(str(config_path))
            client = TestClient(app)

            response = client.get("/api/okx-position-history", params={"limit": "bad"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 0)
        self.assertIn("Provide instId or symbol", payload["message"])

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
                "_atlas_test_mode: true\n"
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
        self.assertEqual(
            rows[2],
            [
                {"text": "🤖 AI", "callback_data": "view_ai"},
                {"text": "📈 Trend", "callback_data": "view_trend_watchlist"},
            ],
        )
        self.assertEqual(
            rows[3],
            [
                {"text": "🛡 Guard", "callback_data": "view_guard"},
                {"text": "🧠 Memory", "callback_data": "view_memory"},
                {"text": "🟡 Wait Slot", "callback_data": "view_wait_slot_notifications"},
            ],
        )

    def test_trend_view_splits_trend_and_post_move_watchlists(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text("mode: dry_run\n", encoding="utf-8")
            config = load_config(config_path)
            set_journal_state(
                config,
                TREND_WATCHLIST_STATE_KEY,
                json.dumps(
                    {
                        "updated_at": "2026-08-25T02:00:00+07:00",
                        "items": {
                            "INJ/USDT:USDT|long": {
                                "symbol": "INJ/USDT:USDT",
                                "side": "long",
                                "status": "watching",
                                "source": "trend_scan",
                                "watch_type": "trend",
                                "trend_score": 68,
                                "htf_trend_score": 70,
                                "entry_readiness_score": 62,
                            },
                            "ONDO/USDT:USDT|long": {
                                "symbol": "ONDO/USDT:USDT",
                                "side": "long",
                                "status": "watching",
                                "source": "post_move_watch",
                                "watch_type": "post_move",
                                "trend_score": 80,
                            },
                        },
                        "pending_confirmations": {
                            "SUI/USDT:USDT|long": {
                                "symbol": "SUI/USDT:USDT",
                                "side": "long",
                                "source": "trend_scan",
                                "confirmation_count": 1,
                                "required_confirmations": 2,
                                "htf_trend_score": 64,
                            },
                            "GRASS/USDT:USDT|long": {
                                "symbol": "GRASS/USDT:USDT",
                                "side": "long",
                                "source": "post_move_watch",
                                "confirmation_count": 1,
                                "required_confirmations": 2,
                            },
                        },
                    },
                    ensure_ascii=False,
                ),
            )

            _, message, _ = _telegram_action_response(config, "view_trend_watchlist", config_path)

        self.assertIn("Trend: 1 | Sau sóng: 1 | Chờ xác nhận: 1", message)
        self.assertIn("Trend:", message)
        self.assertIn("Sau sóng:", message)
        self.assertIn("INJ/USDT:USDT", message)
        self.assertIn("ONDO/USDT:USDT", message)
        self.assertIn("SUI/USDT:USDT", message)
        self.assertNotIn("GRASS/USDT:USDT", message)

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

    @patch("crypto_trader.ui._is_railway_runtime", return_value=True)
    @patch("crypto_trader.ui.send_telegram_message")
    @patch("crypto_trader.ui.sync_telegram_commands")
    def test_app_startup_syncs_native_telegram_commands(self, sync_commands, send_message, _railway_runtime) -> None:
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
        self.assertTrue(send_message.call_args.kwargs["allow_during_startup_quiet"])

    @patch("crypto_trader.notifier._telegram_api_request")
    def test_telegram_startup_quiet_blocks_background_messages(self, api_request) -> None:
        from crypto_trader.notifier import send_telegram_message, set_telegram_startup_quiet_until

        config = {"notifications": {"telegram": {"enabled": True}}}
        set_telegram_startup_quiet_until(datetime.now(timezone.utc) + timedelta(minutes=5))
        try:
            sent = send_telegram_message(
                config,
                "LC internal recheck replay",
                with_buttons=False,
                replace_previous=False,
            )
            self.assertFalse(sent)
            api_request.assert_not_called()

            api_request.return_value = {"ok": True}
            sent = send_telegram_message(
                config,
                STARTUP_TELEGRAM_MESSAGE,
                with_buttons=False,
                replace_previous=False,
                allow_during_startup_quiet=True,
            )
            self.assertTrue(sent)
            api_request.assert_called_once()
        finally:
            set_telegram_startup_quiet_until(None)

    @patch("crypto_trader.notifier.urllib.request.urlopen")
    def test_telegram_api_never_uses_network_in_test_mode(self, urlopen) -> None:
        from crypto_trader.notifier import _telegram_api_request

        with patch.dict(
            "os.environ",
            {"TELEGRAM_BOT_TOKEN": "real-looking-token", "TELEGRAM_CHAT_ID": "123"},
            clear=False,
        ):
            response = _telegram_api_request(
                {"_atlas_test_mode": True, "notifications": {"telegram": {"enabled": True}}},
                "sendMessage",
                {"chat_id": "123", "text": "sample notification"},
            )

        self.assertIsNone(response)
        urlopen.assert_not_called()

    @patch("crypto_trader.ui.update_lc_internal_pipeline")
    @patch("crypto_trader.ui.collect_lc_pipeline_candidates")
    def test_lc_pipeline_worker_keeps_telegram_config_during_startup_quiet(self, collect_candidates, update_pipeline) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                "runtime_config_overrides:\n"
                "  enabled: false\n"
                "automation:\n"
                "  enabled: true\n"
                "notifications:\n"
                "  telegram:\n"
                "    enabled: true\n"
                "ai:\n"
                "  internal:\n"
                "    lc_pipeline_enabled: true\n",
                encoding="utf-8",
            )
            collect_candidates.return_value = {"candidates": [], "candidate_count": 0, "source_symbol_count": 0}
            update_pipeline.return_value = {
                "created_hourly": False,
                "created_two_hour": False,
                "created_four_hour": False,
                "hourly_slot": "2026-07-16T02:00:00+07:00",
                "two_hour_slot": "2026-07-16T02:00:00+07:00",
                "four_hour_slot": "2026-07-16T00:00:00+07:00",
            }
            app = SimpleNamespace(
                state=SimpleNamespace(
                    config_path=config_path,
                    lc_pipeline_lock=threading.Lock(),
                    lc_pipeline_status={},
                    telegram_startup_quiet_until=datetime.now(timezone.utc) + timedelta(minutes=5),
                )
            )

            _run_lc_pipeline_worker_cycle(app)

        passed_config = update_pipeline.call_args.args[0]
        self.assertTrue(passed_config["notifications"]["telegram"]["enabled"])

    @patch("crypto_trader.ui.collect_lc_pipeline_candidates")
    def test_lc_pipeline_worker_cycle_skips_when_app_is_stopping(self, collect_candidates) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                "runtime_config_overrides:\n"
                "  enabled: false\n"
                "automation:\n"
                "  enabled: true\n"
                "ai:\n"
                "  internal:\n"
                "    lc_pipeline_enabled: true\n",
                encoding="utf-8",
            )
            stop_event = threading.Event()
            stop_event.set()
            app = SimpleNamespace(
                state=SimpleNamespace(
                    config_path=config_path,
                    automation_stop=stop_event,
                    shutdown_started=True,
                    lc_pipeline_lock=threading.Lock(),
                    lc_pipeline_status={},
                    telegram_startup_quiet_until=None,
                )
            )

            _run_lc_pipeline_worker_cycle(app)

        collect_candidates.assert_not_called()

    @patch("crypto_trader.ui._notify_system_error")
    @patch("crypto_trader.ui.collect_lc_pipeline_candidates")
    def test_lc_pipeline_worker_suppresses_interpreter_shutdown_error(self, collect_candidates, notify_error) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                "runtime_config_overrides:\n"
                "  enabled: false\n"
                "automation:\n"
                "  enabled: true\n"
                "ai:\n"
                "  internal:\n"
                "    lc_pipeline_enabled: true\n",
                encoding="utf-8",
            )
            collect_candidates.side_effect = RuntimeError("cannot schedule new futures after interpreter shutdown")
            app = SimpleNamespace(
                state=SimpleNamespace(
                    config_path=config_path,
                    automation_stop=threading.Event(),
                    shutdown_started=False,
                    lc_pipeline_lock=threading.Lock(),
                    lc_pipeline_status={},
                    telegram_startup_quiet_until=None,
                )
            )

            _run_lc_pipeline_worker_cycle(app)

        notify_error.assert_not_called()
        self.assertEqual(app.state.lc_pipeline_status["last_result"], "error")

    @patch("crypto_trader.ui.run_internal_market_scan_if_due")
    @patch("crypto_trader.ui.update_lc_internal_pipeline")
    def test_lc_pipeline_slot_cycle_updates_pool_and_calls_mini(self, update_pipeline, run_mini) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                "runtime_config_overrides:\n"
                "  enabled: false\n"
                "automation:\n"
                "  enabled: true\n"
                "ai:\n"
                "  internal:\n"
                "    lc_pipeline_enabled: true\n",
                encoding="utf-8",
            )
            update_pipeline.return_value = {
                "created_hourly": False,
                "created_two_hour": False,
                "created_four_hour": True,
                "hourly_slot": "2026-07-26T04:00:00+07:00",
                "two_hour_slot": "2026-07-26T04:00:00+07:00",
                "four_hour_slot": "2026-07-26T04:00:00+07:00",
            }
            run_mini.return_value = {
                "status": "done",
                "slot_id": "2026-07-26T04:00:00+07:00",
                "created_at": "2026-07-26T04:00:03+07:00",
                "selected_symbols": ["LAB/USDT:USDT"],
            }
            app = SimpleNamespace(
                state=SimpleNamespace(
                    config_path=config_path,
                    automation_stop=threading.Event(),
                    shutdown_started=False,
                    lc_pipeline_slot_lock=threading.Lock(),
                    lc_pipeline_candidate_cache={
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "candidates": [{"symbol": "LAB/USDT:USDT", "side": "short"}],
                    },
                    lc_pipeline_status={},
                )
            )

            _run_lc_pipeline_slot_cycle(app)

        update_pipeline.assert_called_once()
        run_mini.assert_called_once()
        self.assertEqual(app.state.lc_pipeline_status["last_result"], "pool_updated")
        self.assertEqual(app.state.lc_pipeline_status["mini_scan_status"], "done")
        self.assertEqual(app.state.lc_pipeline_status["mini_scan_selected_symbols"], ["LAB/USDT:USDT"])

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

    def test_lc_pipeline_endpoint_degrades_instead_of_500_on_storage_timeout(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                "_atlas_test_mode: true\n"
                "lc_pipeline:\n"
                "  enabled: true\n",
                encoding="utf-8",
            )
            client = TestClient(create_app(config_path))

            with patch("crypto_trader.ui.lc_pipeline_dashboard_payload", side_effect=TimeoutError("read operation timed out")):
                response = client.get("/api/lc-pipeline")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["degraded"])
        self.assertEqual(payload["counts"]["undecided"], 0)
        self.assertEqual(payload["undecided"], [])
        self.assertIn("read operation timed out", payload["error"])

    def test_lc_pipeline_endpoint_degrades_when_dashboard_payload_hangs(self) -> None:
        def slow_payload(_config):
            time.sleep(0.2)
            return {"enabled": True}

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                "_atlas_test_mode: true\n",
                encoding="utf-8",
            )
            client = TestClient(create_app(config_path))

            with patch("crypto_trader.ui.LC_PIPELINE_ENDPOINT_TIMEOUT_SECONDS", 0.01), patch(
                "crypto_trader.ui.lc_pipeline_dashboard_payload", side_effect=slow_payload
            ):
                response = client.get("/api/lc-pipeline")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["degraded"])
        self.assertEqual(payload["counts"]["internal_lc"], 0)

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
