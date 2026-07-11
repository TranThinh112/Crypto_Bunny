from __future__ import annotations

import io
import json
import os
import tempfile
import urllib.error
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from crypto_trader.codex_features import ai_trade_decision_stats, call_openai_json, record_ai_call_event


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

        self.assertIn("auto_openai_enabled=false", str(error.exception))
        urlopen.assert_not_called()

    @patch.dict(os.environ, {"OPENAI_API_KEY_TEST": "test-key"})
    @patch("crypto_trader.notifier.send_telegram_message")
    @patch("crypto_trader.codex_features.urllib.request.urlopen")
    def test_openai_okx_final_approval_allows_manual_one_shot_when_manual_enabled(self, urlopen, _send_telegram_message) -> None:
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

        result = call_openai_json(
            config,
            self._role_config(),
            self._prompt_package(),
            model_name="gpt-5.5",
            purpose="okx_final_approval",
            route="lc_okx_setup_review",
            manual_trigger=True,
        )

        self.assertTrue(result["parsed"]["approved"])
        urlopen.assert_called_once()

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

        self.assertEqual(stats["totalDecisions"], 3)
        self.assertEqual(stats["longCount"], 1)
        self.assertEqual(stats["shortCount"], 1)
        self.assertEqual(stats["noTradeCount"], 1)
        self.assertEqual(stats["longPercent"], 33.33)
        self.assertEqual(stats["shortPercent"], 33.33)
        self.assertEqual(stats["winrateLong"], 100.0)
        self.assertEqual(stats["winrateShort"], 0.0)
        self.assertEqual(stats["avgConfidenceLong"], 91.0)
        self.assertEqual(stats["avgConfidenceShort"], 83.0)
        self.assertEqual(stats["profitFactorLong"], 999.0)
        self.assertEqual(stats["profitFactorShort"], 0.0)
