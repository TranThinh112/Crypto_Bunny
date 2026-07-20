from __future__ import annotations

import io
import json
import os
import tempfile
import urllib.error
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from crypto_trader.codex_features import (
    _ai_call_message,
    _compact_candidate_storage_payload,
    ai_call_decision_stats,
    ai_trade_decision_stats,
    call_openai_json,
    detect_market_regime,
    okx_review_explanation_vi,
    record_ai_call_event,
    refresh_bunny_health_state,
    refresh_trading_system_state,
    select_runtime_config,
)
from crypto_trader.config import DEFAULT_CONFIG
from crypto_trader.models import TradeCandidate
from crypto_trader.storage import get_strategy_version, save_strategy_version


class _FakeOpenAIResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> "_FakeOpenAIResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class CodexFeaturesTest(TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _config(self) -> dict:
        return {
            "_atlas_test_mode": True,
            "_config_path": str(Path(self._tmpdir.name) / "config.yaml"),
            "_config_dir": self._tmpdir.name,
            "notifications": {
                "telegram": {
                    "enabled": True,
                    "notify_ai_api_calls": True,
                }
            },
            "ai": {"enabled": True, "manual_only": False, "allow_api_calls": True},
        }

    def _role_config(self) -> dict:
        return {"api_key_env": "OPENAI_API_KEY_TEST", "timeout_seconds": 1}

    def test_compact_candidate_storage_payload_preserves_market_pattern_context(self) -> None:
        candidate = TradeCandidate(
            symbol="BTC/USDT:USDT",
            base="BTC",
            side="long",
            confidence=86.0,
            win_probability_pct=82.0,
            entry=100.0,
            stop_loss=98.0,
            take_profit=103.0,
            risk_reward=1.5,
            order_usdt=20.0,
            quantity=1.0,
            spread_pct=0.01,
            news_score=0.0,
            news_count=1,
            indicator_summary={
                "trend": "up",
                "market_pattern": {
                    "snapshot_id": "mp-storage-1",
                    "timeframe": "4h",
                    "trend_regime": "bullish",
                    "confluence_bias": "bullish",
                    "confluence_score": 0.72,
                    "candlestick_count": 2,
                    "candlestick_patterns": [
                        {"pattern": "bullish_engulfing", "direction": "bullish", "confidence": 0.81},
                    ],
                },
            },
        )

        payload = _compact_candidate_storage_payload(candidate)

        market_pattern = payload["indicator_summary"]["market_pattern"]
        self.assertEqual(market_pattern["snapshot_id"], "mp-storage-1")
        self.assertEqual(market_pattern["candlestick_patterns"][0]["pattern"], "bullish_engulfing")

    def test_detect_market_regime_aggregates_top_volume_scope_separately_from_detail_tabs(self) -> None:
        config = {
            "market_regime": {
                "top_symbols": ["BTC/USDT:USDT", "SOL/USDT:USDT", "ETH/USDT:USDT"],
                "aggregate_limit": 5,
            },
            "strategy": {"universe": {"max_symbols": 5}},
        }
        snapshots = [
            SimpleNamespace(
                symbol=f"COIN{index}/USDT:USDT",
                last=100 + index,
                ema_fast=99 + index,
                ema_slow=98 + index,
                ema200=97 + index,
                vwap=99.5 + index,
                rsi=50 + index,
                adx=25 + index,
                atr_pct=2.0,
                volume_ratio=1.2,
                funding_rate=0.0001 * (index + 1),
                open_interest=1000 + index,
                fear_greed=60,
                news_score=1.5,
            )
            for index in range(5)
        ]
        snapshots[0].symbol = "BTC/USDT:USDT"
        snapshots[1].symbol = "SOL/USDT:USDT"
        snapshots[2].symbol = "ETH/USDT:USDT"

        with patch("crypto_trader.codex_features.insert_market_regime_history") as insert_history:
            result = detect_market_regime(config, snapshots)

        rows = [call.args[1] for call in insert_history.call_args_list]
        aggregate_rows = [
            row for row in rows if json.loads(row["indicators_json"]).get("scope") == "aggregate"
        ]
        symbol_rows = [
            row for row in rows if json.loads(row["indicators_json"]).get("scope") == "symbol"
        ]
        aggregate = json.loads(aggregate_rows[0]["indicators_json"])
        self.assertEqual(len(aggregate_rows), 1)
        self.assertEqual(len(symbol_rows), 3)
        self.assertEqual(result["indicators"]["scope"], "aggregate")
        self.assertEqual(aggregate["coverage_count"], 5)
        self.assertEqual(aggregate["target_count"], 5)
        self.assertEqual(aggregate["market_symbols"], [snapshot.symbol for snapshot in snapshots])
        self.assertEqual(aggregate["detail_symbols"], ["BTC/USDT:USDT", "SOL/USDT:USDT", "ETH/USDT:USDT"])
        self.assertIsNotNone(aggregate["adx"])
        self.assertIsNotNone(aggregate["funding_rate"])
        self.assertIsNotNone(aggregate["open_interest"])
        self.assertEqual(aggregate["fear_greed"], 60.0)
        self.assertEqual(aggregate["news_score"], 1.5)

    def _prompt_package(self) -> dict:
        return {
            "messages": [{"role": "system", "content": "Return JSON."}, {"role": "user", "content": "{}"}],
            "prompt_version": "prompt-v-test",
            "prompt_hash": "abcdef1234567890",
            "estimated_static_tokens": 12,
            "estimated_dynamic_tokens": 3,
            "estimated_cache_hit": 80,
        }

    @patch.dict(os.environ, {"OPENAI_API_KEY_TEST": "test-key"})
    @patch("crypto_trader.codex_features.register_model_version")
    @patch("crypto_trader.codex_features.register_prompt_metric")
    @patch("crypto_trader.notifier.send_telegram_message")
    @patch("crypto_trader.codex_features.urllib.request.urlopen")
    def test_openai_success_sends_telegram_notice(
        self,
        urlopen,
        send_telegram_message,
        _register_prompt_metric,
        _register_model_version,
    ) -> None:
        urlopen.return_value = _FakeOpenAIResponse(
            {
                "choices": [{"message": {"content": "{\"approved\": true}"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            }
        )

        result = call_openai_json(
            self._config(),
            self._role_config(),
            self._prompt_package(),
            model_name="gpt-test",
            purpose="mini_market_scan",
        )

        self.assertEqual(result["parsed"], {"approved": True})
        send_telegram_message.assert_called_once()
        message = send_telegram_message.call_args.args[1]
        self.assertIn("AI được gọi", message)
        self.assertIn("gpt-test", message)
        self.assertIn("Trạng thái: OK", message)
        self.assertFalse(send_telegram_message.call_args.kwargs["with_buttons"])
        self.assertFalse(send_telegram_message.call_args.kwargs["replace_previous"])

    @patch.dict(os.environ, {"OPENAI_API_KEY_TEST": "test-key"})
    @patch("crypto_trader.notifier.send_telegram_message")
    @patch("crypto_trader.codex_features.urllib.request.urlopen")
    def test_openai_http_error_sends_telegram_notice(self, urlopen, send_telegram_message) -> None:
        urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.openai.com/v1/chat/completions",
            code=429,
            msg="Too Many Requests",
            hdrs=None,
            fp=io.BytesIO(b'{"error":"rate limit"}'),
        )

        with self.assertRaises(RuntimeError):
            call_openai_json(
                self._config(),
                self._role_config(),
                self._prompt_package(),
                model_name="gpt-test",
                purpose="mini_market_scan",
            )

        send_telegram_message.assert_called_once()
        message = send_telegram_message.call_args.args[1]
        self.assertIn("GPT API lỗi", message)
        self.assertIn("OpenAI HTTP 429", message)

    @patch.dict(os.environ, {"OPENAI_API_KEY_TEST": "test-key"})
    @patch("crypto_trader.codex_features.urllib.request.urlopen")
    def test_openai_call_requires_policy_purpose(self, urlopen) -> None:
        with self.assertRaises(RuntimeError):
            call_openai_json(
                self._config(),
                self._role_config(),
                self._prompt_package(),
                model_name="gpt-test",
            )

        urlopen.assert_not_called()

    @patch.dict(os.environ, {"OPENAI_API_KEY_TEST": "test-key"})
    @patch("crypto_trader.codex_features.urllib.request.urlopen")
    def test_openai_okx_final_approval_is_blocked_when_auto_okx_openai_disabled(self, urlopen) -> None:
        config = self._config()
        config["ai"]["okx"] = {
            "auto_openai_enabled": False,
            "manual_openai_enabled": True,
            "approval_enabled": True,
        }

        with self.assertRaises(RuntimeError) as error:
            call_openai_json(
                config,
                self._role_config(),
                self._prompt_package(),
                model_name="gpt-5.5",
                purpose="okx_final_approval",
                route="new_vt",
            )

        self.assertIn("route=new_vt", str(error.exception))
        urlopen.assert_not_called()

    @patch.dict(os.environ, {"OPENAI_API_KEY_TEST": "test-key"})
    @patch("crypto_trader.notifier.send_telegram_message")
    @patch("crypto_trader.codex_features.urllib.request.urlopen")
    def test_openai_okx_final_approval_blocks_manual_one_shot_even_when_manual_enabled(self, urlopen, _send_telegram_message) -> None:
        config = self._config()
        config["ai"]["okx"] = {
            "auto_openai_enabled": False,
            "manual_openai_enabled": True,
            "approval_enabled": True,
        }
        urlopen.return_value = _FakeOpenAIResponse(
            {
                "choices": [{"message": {"content": "{\"approved\": true, \"decision\": \"APPROVE\"}"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            }
        )

        with self.assertRaises(RuntimeError) as error:
            call_openai_json(
                config,
                self._role_config(),
                self._prompt_package(),
                model_name="gpt-5.5",
                purpose="okx_final_approval",
                route="lc_okx_setup_review",
                manual_trigger=True,
            )

        self.assertIn("manual 5.5 calls are disabled", str(error.exception))
        urlopen.assert_not_called()

    @patch.dict(os.environ, {"OPENAI_API_KEY_TEST": "test-key"})
    @patch("crypto_trader.notifier.send_telegram_message")
    @patch("crypto_trader.codex_features.urllib.request.urlopen")
    def test_openai_okx_final_approval_allows_lc_okx_review_once_when_auto_is_disabled(
        self,
        urlopen,
        _send_telegram_message,
    ) -> None:
        config = self._config()
        config["ai"]["okx"] = {
            "auto_openai_enabled": False,
            "auto_lc_okx_review_once_enabled": True,
            "manual_openai_enabled": False,
            "approval_enabled": True,
        }
        urlopen.return_value = _FakeOpenAIResponse(
            {
                "choices": [{"message": {"content": "{\"approved\": true, \"decision\": \"APPROVE\"}"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            }
        )

        result = call_openai_json(
            config,
            self._role_config(),
            self._prompt_package(),
            model_name="gpt-5.5",
            purpose="okx_final_approval",
            route="lc_okx_setup_review",
            lc_okx_review_once=True,
        )

        self.assertTrue(result["parsed"]["approved"])
        urlopen.assert_called_once()

    def test_okx_review_explanation_treats_vao_market_as_market_entry(self) -> None:
        item = {
            "status": "VÀO MARKET",
            "market_reason": "risk sạch và setup đủ điều kiện",
            "keep_reason": "setup cần chờ xác nhận thêm",
        }

        message = okx_review_explanation_vi(item)

        self.assertIn("đồng ý mở Market", message)
        self.assertNotIn("giữ setup", message)

    def test_lc_okx_review_message_labels_market_entry(self) -> None:
        message = _ai_call_message(
            {
                "created_at": "2026-07-20T05:02:11+00:00",
                "role": "okx",
                "review_kind": "lc_okx_review",
                "model": "gpt-5.5",
                "status": "VÀO MARKET",
                "symbol": "BILL/USDT:USDT",
                "side": "short",
                "lc_okx_id": 6,
                "market_reason": "risk sạch và setup đủ điều kiện",
                "keep_reason": "setup cần chờ xác nhận thêm",
            }
        )

        self.assertIn("5.5 DUYỆT VÀO MARKET #6", message)
        self.assertIn("5.5 đồng ý mở Market", message)
        self.assertNotIn("5.5 chưa mở Market", message)

    @patch("crypto_trader.notifier.send_telegram_message")
    def test_lc_okx_review_message_is_single_vietnamese_explanation(self, send_telegram_message) -> None:
        record_ai_call_event(
            self._config(),
            {
                "created_at": "2026-07-12T05:23:21+07:00",
                "role": "okx",
                "review_kind": "lc_okx_review",
                "model": "gpt-5.5",
                "status": "XÓA SETUP",
                "symbol": "SUI/USDT:USDT",
                "side": "long",
                "lc_okx_id": 63,
                "market_reason": "-",
                "keep_reason": "-",
                "delete_reason": "Missing 4h bias and 15m confirmation; volume ratio 0.774 is weak",
            },
        )

        send_telegram_message.assert_called_once()
        message = send_telegram_message.call_args.args[1]
        self.assertIn("Giải thích:", message)
        self.assertIn("5.5 từ chối vì", message)
        self.assertIn("Thiếu bias 4h và xác nhận 15m", message)
        self.assertIn("volume ratio 0.774 còn yếu", message)
        self.assertNotIn("Lý do mở Market", message)
        self.assertNotIn("Lý do giữ setup", message)
        self.assertNotIn("Lý do xóa setup", message)

    @patch("crypto_trader.codex_features.list_ai_trade_decision_stat_rows")
    def test_ai_trade_decision_stats_uses_lightweight_rows_without_json_parsing(self, list_rows) -> None:
        list_rows.return_value = [
            {
                "decision": "ENTER_LONG",
                "trade_status": "WIN",
                "confidence": 91,
                "pnl": 1.25,
                "reason_json": "{not-valid-json",
                "snapshot_json": "{not-valid-json",
            },
            {
                "decision": "ENTER_SHORT",
                "trade_status": "LOSS",
                "confidence": 83,
                "pnl": -0.5,
                "reason_json": "{still-invalid",
                "snapshot_json": "{still-invalid",
            },
            {
                "decision": "NO_TRADE",
                "trade_status": None,
                "confidence": None,
                "pnl": 0,
            },
        ]

        stats = ai_trade_decision_stats(self._config())

        self.assertEqual(stats["totalDecisions"], 2)
        self.assertEqual(stats["totalRecords"], 3)
        self.assertEqual(stats["longCount"], 1)
        self.assertEqual(stats["shortCount"], 1)
        self.assertEqual(stats["noTradeCount"], 0)
        self.assertEqual(stats["longPercent"], 50.0)
        self.assertEqual(stats["shortPercent"], 50.0)
        self.assertEqual(stats["winrateLong"], 100.0)
        self.assertEqual(stats["winrateShort"], 0.0)
        self.assertEqual(stats["avgConfidenceLong"], 91.0)
        self.assertEqual(stats["avgConfidenceShort"], 83.0)
        self.assertEqual(stats["profitFactorLong"], 999.0)
        self.assertEqual(stats["profitFactorShort"], 0.0)

    @patch("crypto_trader.codex_features.list_ai_trade_decision_stat_rows")
    def test_ai_trade_decision_stats_counts_only_gpt55_delete_or_reject_as_no_trade(self, list_rows) -> None:
        config = self._config()
        list_rows.return_value = [
            {"decision": "ENTER_LONG", "trade_status": None, "confidence": 91, "pnl": 0},
            {"decision": "ENTER_SHORT", "trade_status": None, "confidence": 83, "pnl": 0},
            {"decision": "NO_TRADE", "trade_status": None, "confidence": None, "pnl": 0},
        ]

        record_ai_call_event(
            config,
            {"role": "okx", "review_kind": "lc_okx_review", "model": "gpt-5.5", "status": "GIỮ SETUP"},
            notify_telegram=False,
        )
        record_ai_call_event(
            config,
            {"role": "okx", "review_kind": "lc_okx_review", "model": "gpt-5.5", "status": "GIỮ THEO DÕI"},
            notify_telegram=False,
        )
        record_ai_call_event(
            config,
            {"role": "okx", "review_kind": "lc_okx_review", "model": "gpt-5.5", "status": "XÓA SETUP"},
            notify_telegram=False,
        )
        record_ai_call_event(
            config,
            {"role": "okx", "model": "gpt-5.5", "status": "KHÔNG VÀO LỆNH"},
            notify_telegram=False,
        )
        record_ai_call_event(
            config,
            {"role": "okx", "model": "gpt-5.5", "status": "NO_TRADE"},
            notify_telegram=False,
        )
        record_ai_call_event(
            config,
            {"role": "mini", "model": "gpt-5.4-mini", "status": "NO_TRADE"},
            notify_telegram=False,
        )

        stats = ai_trade_decision_stats(config)

        self.assertEqual(stats["totalDecisions"], 5)
        self.assertEqual(stats["totalRecords"], 3)
        self.assertEqual(stats["longCount"], 1)
        self.assertEqual(stats["shortCount"], 1)
        self.assertEqual(stats["noTradeCount"], 3)
        self.assertEqual(stats["longPercent"], 20.0)
        self.assertEqual(stats["shortPercent"], 20.0)

    @patch("crypto_trader.codex_features.list_ai_trade_decision_stat_rows_for_period")
    def test_ai_trade_decision_stats_filters_real_decisions_and_gpt55_rejects_by_period(self, list_rows) -> None:
        config = self._config()
        list_rows.return_value = [
            {"decision": "ENTER_LONG", "trade_status": None, "confidence": 91, "pnl": 0},
            {"decision": "ENTER_SHORT", "trade_status": None, "confidence": 83, "pnl": 0},
            {"decision": "ENTER_LONG", "trade_status": None, "confidence": 88, "pnl": 0},
        ]
        created_from = "2026-07-16T17:00:00+00:00"
        created_to = "2026-07-17T17:00:00+00:00"

        record_ai_call_event(
            config,
            {
                "created_at": "2026-07-16T16:59:59+00:00",
                "role": "okx",
                "review_kind": "lc_okx_review",
                "model": "gpt-5.5",
                "status": "XÓA SETUP",
            },
            notify_telegram=False,
        )
        record_ai_call_event(
            config,
            {
                "created_at": "2026-07-16T17:00:00+00:00",
                "role": "okx",
                "review_kind": "lc_okx_review",
                "model": "gpt-5.5",
                "status": "XÓA SETUP",
            },
            notify_telegram=False,
        )
        record_ai_call_event(
            config,
            {
                "created_at": "2026-07-17T17:00:00+00:00",
                "role": "okx",
                "review_kind": "lc_okx_review",
                "model": "gpt-5.5",
                "status": "XÓA SETUP",
            },
            notify_telegram=False,
        )

        stats = ai_trade_decision_stats(config, created_from=created_from, created_to=created_to)

        list_rows.assert_called_once_with(config, created_from=created_from, created_to=created_to, limit=5000)
        self.assertEqual(stats["longCount"], 2)
        self.assertEqual(stats["shortCount"], 1)
        self.assertEqual(stats["noTradeCount"], 1)
        self.assertEqual(stats["totalDecisions"], 4)

    def test_ai_call_decision_stats_counts_real_mini_and_gpt55_calls(self) -> None:
        config = self._config()
        created_from = "2026-07-16T17:00:00+00:00"
        created_to = "2026-07-17T17:00:00+00:00"
        record_ai_call_event(
            config,
            {
                "created_at": "2026-07-16T17:01:00+00:00",
                "role": "mini",
                "model": "gpt-5.4-mini",
                "approved_symbols": ["BTC/USDT:USDT"],
                "candidate_details": [{"symbol": "BTC/USDT:USDT", "side": "long", "confidence": 90}],
                "status": "MINI ĐỀ XUẤT LC",
            },
            notify_telegram=False,
        )
        record_ai_call_event(
            config,
            {
                "created_at": "2026-07-16T18:01:00+00:00",
                "role": "mini",
                "model": "gpt-5.4-mini",
                "approved_symbols": ["ETH/USDT:USDT"],
                "candidate_details": [{"symbol": "ETH/USDT:USDT", "side": "short", "confidence": 80}],
                "status": "MINI ĐỀ XUẤT LC",
            },
            notify_telegram=False,
        )
        record_ai_call_event(
            config,
            {
                "created_at": "2026-07-16T19:01:00+00:00",
                "role": "mini",
                "model": "gpt-5.4-mini",
                "approved_symbols": [],
                "candidate_details": [{"symbol": "SOL/USDT:USDT", "side": "long", "confidence": 70}],
                "status": "NO_TRADE",
            },
            notify_telegram=False,
        )
        record_ai_call_event(
            config,
            {
                "created_at": "2026-07-16T20:01:00+00:00",
                "role": "okx",
                "review_kind": "lc_okx_review",
                "model": "gpt-5.5",
                "status": "XÓA SETUP",
            },
            notify_telegram=False,
        )
        record_ai_call_event(
            config,
            {
                "created_at": "2026-07-16T21:01:00+00:00",
                "role": "okx",
                "review_kind": "lc_okx_review",
                "model": "gpt-5.5",
                "status": "GIỮ SETUP",
            },
            notify_telegram=False,
        )

        stats = ai_call_decision_stats(config, created_from=created_from, created_to=created_to)

        self.assertEqual(stats["totalDecisions"], 5)
        self.assertEqual(stats["miniCallCount"], 3)
        self.assertEqual(stats["okxCallCount"], 2)
        self.assertEqual(stats["longCount"], 1)
        self.assertEqual(stats["shortCount"], 1)
        self.assertEqual(stats["miniNoTradeCount"], 1)
        self.assertEqual(stats["noTradeCount"], 1)
        self.assertEqual(stats["avgConfidenceLong"], 90.0)
        self.assertEqual(stats["avgConfidenceShort"], 80.0)

    def test_ai_call_decision_stats_without_period_counts_all_stored_history(self) -> None:
        config = self._config()
        record_ai_call_event(
            config,
            {
                "created_at": "2026-07-15T01:00:00+00:00",
                "role": "mini",
                "model": "gpt-5.4-mini",
                "approved_symbols": ["BTC/USDT:USDT"],
                "candidate_details": [{"symbol": "BTC/USDT:USDT", "side": "long", "confidence": 88}],
                "status": "MINI ĐỀ XUẤT LC",
            },
            notify_telegram=False,
        )
        record_ai_call_event(
            config,
            {
                "created_at": "2026-07-17T01:00:00+00:00",
                "role": "okx",
                "review_kind": "lc_okx_review",
                "model": "gpt-5.5",
                "status": "XÓA SETUP",
            },
            notify_telegram=False,
        )

        stats = ai_call_decision_stats(config)

        self.assertEqual(stats["totalDecisions"], 2)
        self.assertEqual(stats["miniCallCount"], 1)
        self.assertEqual(stats["okxCallCount"], 1)
        self.assertEqual(stats["longCount"], 1)
        self.assertEqual(stats["noTradeCount"], 1)

    def test_default_strategy_syncs_but_cannot_override_deployed_risk_config(self) -> None:
        config = deepcopy(DEFAULT_CONFIG)
        config["_atlas_test_mode"] = True
        config["_config_path"] = str(Path(self._tmpdir.name) / "strategy-config.yaml")
        config["_config_dir"] = self._tmpdir.name
        config["risk"]["max_active_trades"] = 5
        config["risk"]["cooldown_minutes"] = 0
        config["trading_risk"]["max_concurrent_positions"] = 5
        stale_payload = {
            "risk": {**config["risk"], "max_active_trades": 1, "cooldown_minutes": 60},
            "trading_risk": {**config["trading_risk"], "max_concurrent_positions": 1},
        }
        save_strategy_version(
            config,
            {
                "version": "strategy-v1",
                "name": "STRATEGY-V1",
                "is_active": 1,
                "traffic_percent": 100,
                "payload_json": json.dumps(stale_payload),
            },
        )

        runtime = select_runtime_config(config)
        stored = get_strategy_version(config, "strategy-v1")
        stored_payload = json.loads(str((stored or {}).get("payload_json") or "{}"))

        self.assertEqual(runtime["risk"]["max_active_trades"], 5)
        self.assertEqual(runtime["risk"]["cooldown_minutes"], 0)
        self.assertEqual(runtime["trading_risk"]["max_concurrent_positions"], 5)
        self.assertEqual(stored_payload["risk"]["max_active_trades"], 5)
        self.assertEqual(stored_payload["risk"]["cooldown_minutes"], 0)

    def test_health_monitor_does_not_evaluate_a_single_loss(self) -> None:
        config = self._config()
        config["bunny_health_monitor"] = {
            "lookback_trades": 20,
            "min_trades_for_evaluation": 5,
        }
        rows = [
            {
                "id": 1,
                "status": "LOSS",
                "pnl": -3.164076,
                "closed_at": "2026-07-19T17:03:01+00:00",
            }
        ]

        with patch("crypto_trader.codex_features._closed_trade_executions", return_value=rows):
            health = refresh_bunny_health_state(config)

        self.assertTrue(health["isHealthy"])
        self.assertFalse(health["isWarning"])
        self.assertFalse(health["isCritical"])
        self.assertFalse(health["isPaused"])
        self.assertEqual(health["totalTrades"], 1)
        self.assertEqual(health["minimumTradesForEvaluation"], 5)
        self.assertEqual(health["totalPnl"], -3.164076)
        self.assertEqual(health["reason"], "Not enough trades (1/5)")

    @patch("crypto_trader.notifier.send_telegram_message")
    def test_recovery_mode_sends_telegram_when_enabled(self, send_telegram_message) -> None:
        config = self._config()
        config["trading_risk"] = {
            "global_loss_streak_threshold": 2,
            "recovery_min_rule_score": 90,
            "recovery_min_gpt_confidence": 92,
            "recovery_min_risk_reward": 2.5,
            "recovery_mode_risk_percent": 0.5,
        }
        rows = [
            {"id": 2, "status": "LOSS", "pnl": -1.0, "closed_at": "2026-07-20T12:00:00+00:00"},
            {"id": 1, "status": "LOSS", "pnl": -1.0, "closed_at": "2026-07-20T11:00:00+00:00"},
        ]

        with patch("crypto_trader.codex_features._closed_trade_executions", return_value=rows), patch(
            "crypto_trader.codex_features.get_trading_system_state_row",
            return_value={"is_recovery_mode": 0},
        ), patch("crypto_trader.codex_features.upsert_trading_system_state_row"), patch(
            "crypto_trader.codex_features._utcnow",
            return_value=datetime(2026, 7, 20, 12, 30, tzinfo=timezone.utc),
        ):
            state = refresh_trading_system_state(config)

        self.assertTrue(state["isRecoveryMode"])
        send_telegram_message.assert_called_once()
        message = send_telegram_message.call_args.args[1]
        self.assertIn("Bunny Recovery Mode đã BẬT", message)
        self.assertIn("19:30:00 20/07/26", message)
        self.assertIn("Chuỗi thua hệ thống: 2", message)

    @patch("crypto_trader.notifier.send_telegram_message")
    def test_recovery_mode_sends_telegram_when_disabled(self, send_telegram_message) -> None:
        config = self._config()
        config["trading_risk"] = {"global_loss_streak_threshold": 2, "normal_min_risk_reward": 1.5}
        rows = [
            {"id": 3, "status": "WIN", "pnl": 0.77, "closed_at": "2026-07-20T12:00:00+00:00"},
            {"id": 2, "status": "LOSS", "pnl": -1.0, "closed_at": "2026-07-20T11:00:00+00:00"},
            {"id": 1, "status": "LOSS", "pnl": -1.0, "closed_at": "2026-07-20T10:00:00+00:00"},
        ]

        with patch("crypto_trader.codex_features._closed_trade_executions", return_value=rows), patch(
            "crypto_trader.codex_features.get_trading_system_state_row",
            return_value={"is_recovery_mode": 1},
        ), patch("crypto_trader.codex_features.upsert_trading_system_state_row"), patch(
            "crypto_trader.codex_features._utcnow",
            return_value=datetime(2026, 7, 20, 12, 45, tzinfo=timezone.utc),
        ):
            state = refresh_trading_system_state(config)

        self.assertFalse(state["isRecoveryMode"])
        send_telegram_message.assert_called_once()
        message = send_telegram_message.call_args.args[1]
        self.assertIn("Bunny Recovery Mode đã TẮT", message)
        self.assertIn("19:45:00 20/07/26", message)
        self.assertIn("Chuỗi thua hệ thống: 0", message)

    @patch("crypto_trader.notifier.send_telegram_message")
    def test_recovery_mode_does_not_send_telegram_without_transition(self, send_telegram_message) -> None:
        config = self._config()
        config["trading_risk"] = {"global_loss_streak_threshold": 2}
        rows = [
            {"id": 2, "status": "LOSS", "pnl": -1.0, "closed_at": "2026-07-20T12:00:00+00:00"},
            {"id": 1, "status": "LOSS", "pnl": -1.0, "closed_at": "2026-07-20T11:00:00+00:00"},
        ]

        with patch("crypto_trader.codex_features._closed_trade_executions", return_value=rows), patch(
            "crypto_trader.codex_features.get_trading_system_state_row",
            return_value={"is_recovery_mode": 1},
        ), patch("crypto_trader.codex_features.upsert_trading_system_state_row"):
            state = refresh_trading_system_state(config)

        self.assertTrue(state["isRecoveryMode"])
        send_telegram_message.assert_not_called()

    def test_health_monitor_reuses_pause_deadline_for_same_trade_sample(self) -> None:
        config = self._config()
        config["bunny_health_monitor"] = {
            "lookback_trades": 20,
            "min_trades_for_evaluation": 5,
            "critical_pause_hours": 12,
        }
        rows = [
            {
                "id": index,
                "status": "LOSS",
                "pnl": -1.0,
                "closed_at": f"2026-07-19T17:0{index}:00+00:00",
            }
            for index in range(1, 6)
        ]

        with patch("crypto_trader.codex_features._closed_trade_executions", return_value=rows):
            first = refresh_bunny_health_state(config)
            second = refresh_bunny_health_state(config)

        self.assertTrue(first["isCritical"])
        self.assertTrue(first["isPaused"])
        self.assertEqual(second["pausedUntil"], first["pausedUntil"])
        self.assertEqual(second["evaluationKey"], first["evaluationKey"])

    def test_health_monitor_downgrades_same_sample_after_critical_cooldown(self) -> None:
        config = self._config()
        config["bunny_health_monitor"] = {
            "lookback_trades": 20,
            "min_trades_for_evaluation": 5,
            "risk_reduction_percent": 40,
            "score_increase_step": 4,
            "confidence_increase_step": 4,
        }
        rows = [
            {
                "id": index,
                "status": "LOSS",
                "pnl": -1.0,
                "closed_at": f"2026-07-19T17:0{index}:00+00:00",
            }
            for index in range(1, 6)
        ]
        with patch("crypto_trader.codex_features._closed_trade_executions", return_value=rows):
            initial = refresh_bunny_health_state(config)
        previous = dict(initial)
        previous["pausedUntil"] = "2026-07-19T18:00:00+00:00"

        with patch("crypto_trader.codex_features._closed_trade_executions", return_value=rows), patch(
            "crypto_trader.codex_features.get_trading_health_state_row",
            return_value={"payload_json": json.dumps(previous)},
        ), patch(
            "crypto_trader.codex_features._utcnow",
            return_value=datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc),
        ):
            health = refresh_bunny_health_state(config)

        self.assertFalse(health["isCritical"])
        self.assertTrue(health["isWarning"])
        self.assertFalse(health["isPaused"])
        self.assertTrue(health["criticalCooldownCompleted"])
        self.assertEqual(health["riskMultiplier"], 0.6)
        self.assertEqual(
            health["reason"],
            "Critical cooldown completed; waiting for a new closed trade",
        )
