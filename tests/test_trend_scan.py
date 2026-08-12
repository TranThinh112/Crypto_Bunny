from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch

from crypto_trader.trend_scan import (
    build_entry_proposal,
    _review_prompt_package,
    _trend_watch_decision,
    _trend_setup_changed_enough,
    _trend_setup_fingerprint,
    _setup_to_candidate,
    normalize_ai_setup_review,
    process_trend_approved_hold_queue,
    recheck_trend_approved_hold_queue,
    run_trend_auto_shadow_reviews,
    upsert_trend_approved_hold_queue,
    _save_watchlist_ai_review_state,
    review_setup_with_mini,
)


class TrendScanTest(TestCase):
    def _config(self) -> dict:
        return {
            "ai": {
                "enabled": True,
                "allow_api_calls": True,
                "internal": {
                    "trend_setup_review_ai_enabled": True,
                    "trend_setup_review_ai_cooldown_seconds": 900,
                    "model": "gpt-5.4-mini",
                },
            }
        }

    def test_trend_setup_review_records_only_enriched_notification(self) -> None:
        setup = {
            "symbol": "CAP/USDT:USDT",
            "setup_state": "ready_for_ai_review",
            "risk_model": {"selected_method": "atr_volatility_rr"},
        }
        response = {
            "parsed": {
                "decision": "REVIEW",
                "setup_grade": "B",
                "reason": "wait for better confirmation",
            },
            "latency_ms": 1234.5,
        }

        with patch("crypto_trader.trend_scan.get_journal_state", return_value=None), patch(
            "crypto_trader.trend_scan.set_journal_state"
        ), patch("crypto_trader.codex_features.call_openai_json", return_value=response) as call_openai_json, patch(
            "crypto_trader.codex_features.record_ai_call_event"
        ) as record_ai_call_event:
            review_setup_with_mini(self._config(), setup, {"symbol": "CAP/USDT:USDT"}, notify_telegram=True)

        self.assertFalse(call_openai_json.call_args.kwargs["record_history"])
        self.assertFalse(call_openai_json.call_args.kwargs["notify_telegram"])
        record_ai_call_event.assert_called_once()
        event = record_ai_call_event.call_args.args[1]
        self.assertEqual(event["sl_tp_method"], "atr_volatility_rr")
        self.assertEqual(event["symbols"], ["CAP/USDT:USDT"])

    def test_trend_review_prompt_requires_gpt_confidence(self) -> None:
        package = _review_prompt_package({"symbol": "CAP/USDT:USDT"}, {"symbol": "CAP/USDT:USDT"})

        user = json.loads(package["messages"][1]["content"])

        self.assertIn("gpt_confidence", user["expected_json"])
        self.assertIn("required number 0-100", user["expected_json"]["gpt_confidence"])

    def test_trend_review_prompt_requires_wait_conditions(self) -> None:
        package = _review_prompt_package({"symbol": "CAP/USDT:USDT"}, {"symbol": "CAP/USDT:USDT"})

        user = json.loads(package["messages"][1]["content"])

        self.assertIn("APPROVE_NOW", user["allowed_review_statuses"])
        self.assertIn("next_approval_conditions", user["expected_json"])

    def test_adaptive_ai_ready_allows_strong_htf_near_miss_entry(self) -> None:
        decision = _trend_watch_decision(
            self._config(),
            htf={"side": "long", "score": 76.0},
            entry={"side": "long", "score": 60.0},
            legacy_side="long",
            legacy_score=76.0,
        )

        self.assertTrue(decision["entry_ready"])
        self.assertTrue(decision["ai_ready"])
        self.assertEqual(decision["ai_gate_mode"], "adaptive_strong_trend")
        self.assertEqual(decision["entry_action"], "READY_LONG")

    def test_adaptive_ai_ready_still_waits_when_htf_is_not_strong(self) -> None:
        decision = _trend_watch_decision(
            self._config(),
            htf={"side": "long", "score": 68.0},
            entry={"side": "long", "score": 60.0},
            legacy_side="long",
            legacy_score=68.0,
        )

        self.assertTrue(decision["entry_ready"])
        self.assertFalse(decision["ai_ready"])
        self.assertEqual(decision["ai_gate_mode"], "waiting")
        self.assertEqual(decision["entry_action"], "SETUP_LONG_REVIEW")

    def test_ai_review_uses_returned_gpt_confidence_without_calculating(self) -> None:
        setup = {"symbol": "CAP/USDT:USDT", "side": "long", "entry_price": 1, "stop_loss": 0.98, "take_profit": 1.05, "risk_reward": 2.5}
        normalized = normalize_ai_setup_review(
            {
                "decision": "APPROVE",
                "setup_grade": "A",
                "gpt_confidence": 93,
                "entry_quality": 50,
                "continuation_score": 50,
            },
            setup,
        )

        candidate = _setup_to_candidate(self._config(), setup, normalized)

        self.assertEqual(normalized["gpt_confidence"], 93)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.confidence, 93)

    def test_wait_review_status_maps_to_review_decision(self) -> None:
        normalized = normalize_ai_setup_review(
            {
                "review_status": "WAIT",
                "setup_grade": "B",
                "gpt_confidence": 71,
                "next_approval_conditions": ["pullback to EMA20", "volume ratio above 1.2"],
            },
            {"setup_state": "ready_for_ai_review"},
        )

        self.assertEqual(normalized["decision"], "REVIEW")
        self.assertEqual(normalized["review_status"], "WAIT")
        self.assertEqual(normalized["next_approval_conditions"], ["pullback to EMA20", "volume ratio above 1.2"])

    def test_missing_gpt_confidence_is_not_inferred_from_quality_scores(self) -> None:
        setup = {"symbol": "CAP/USDT:USDT", "side": "long", "entry_price": 1, "stop_loss": 0.98, "take_profit": 1.05, "risk_reward": 2.5}
        normalized = normalize_ai_setup_review(
            {
                "decision": "APPROVE",
                "setup_grade": "A",
                "entry_quality": 99,
                "continuation_score": 99,
            },
            setup,
        )

        candidate = _setup_to_candidate(self._config(), setup, normalized)

        self.assertIsNone(normalized["gpt_confidence"])
        self.assertIn("gpt_confidence_missing_or_invalid", normalized["warnings"])
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.confidence, 0)

    def test_weak_review_notification_does_not_show_extension(self) -> None:
        setup = {
            "symbol": "CAP/USDT:USDT",
            "setup_state": "ready_for_ai_review",
            "risk_model": {"selected_method": "atr_volatility_rr"},
        }
        response = {
            "parsed": {
                "decision": "REVIEW",
                "setup_grade": "C",
                "entry_quality": 55,
                "continuation_score": 50,
                "allow_recheck_if_setup_changes": False,
                "reason": "too weak to extend",
            },
            "latency_ms": 1234.5,
        }

        with patch("crypto_trader.trend_scan.get_journal_state", return_value=None), patch(
            "crypto_trader.trend_scan.set_journal_state"
        ), patch("crypto_trader.codex_features.call_openai_json", return_value=response), patch(
            "crypto_trader.codex_features.record_ai_call_event"
        ) as record_ai_call_event:
            review_setup_with_mini(
                self._config(),
                setup,
                {"symbol": "CAP/USDT:USDT"},
                notify_telegram=True,
                notification_context={"watchlist_remaining_minutes": 5, "watchlist_ai_review_extend_minutes": 30},
            )

        event = record_ai_call_event.call_args.args[1]
        self.assertEqual(event["watchlist_remaining_minutes"], 5)
        self.assertEqual(event["watchlist_ai_review_extend_minutes"], 0)

    def test_remove_pair_reject_notification_includes_cooldown(self) -> None:
        setup = {
            "symbol": "CAP/USDT:USDT",
            "setup_state": "ready_for_ai_review",
            "risk_model": {"selected_method": "atr_volatility_rr"},
        }
        response = {
            "parsed": {
                "decision": "REJECT",
                "setup_grade": "D",
                "reject_scope": "WATCHLIST_REMOVE",
                "reject_reason_type": "TREND_INVALID",
                "reason": "trend invalid",
            },
            "latency_ms": 1234.5,
        }

        with patch("crypto_trader.trend_scan.get_journal_state", return_value=None), patch(
            "crypto_trader.trend_scan.set_journal_state"
        ), patch("crypto_trader.codex_features.call_openai_json", return_value=response), patch(
            "crypto_trader.codex_features.record_ai_call_event"
        ) as record_ai_call_event:
            review_setup_with_mini(
                self._config(),
                setup,
                {"symbol": "CAP/USDT:USDT"},
                notify_telegram=True,
                notification_context={"watchlist_remaining_minutes": 5, "watchlist_ai_review_extend_minutes": 30},
            )

        event = record_ai_call_event.call_args.args[1]
        self.assertEqual(event["watchlist_ai_review_extend_minutes"], 0)
        self.assertEqual(event["watchlist_reject_cooldown_minutes"], 120)
        self.assertEqual(event["reject_scope"], "WATCHLIST_REMOVE")
        self.assertEqual(event["reject_reason_type"], "TREND_INVALID")

    def test_ai_review_extends_watchlist_when_expiring_soon(self) -> None:
        now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        state = {
            "items": {
                "CAP/USDT:USDT|long": {
                    "symbol": "CAP/USDT:USDT",
                    "side": "long",
                    "expires_at": (now + timedelta(minutes=5)).isoformat(),
                }
            }
        }
        saved: dict[str, str] = {}

        with patch("crypto_trader.trend_scan.get_journal_state", return_value=json.dumps(state)), patch(
            "crypto_trader.trend_scan.set_journal_state",
            side_effect=lambda _config, _key, value: saved.setdefault("value", value),
        ), patch("crypto_trader.trend_scan.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = now
            mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
            _save_watchlist_ai_review_state(
                self._config(),
                "CAP/USDT:USDT",
                "long",
                signature={"entry_price": 1},
                ai_review={"decision": "REVIEW", "setup_grade": "B"},
                setup={"entry_action": "WAIT_PULLBACK_LONG", "setup_state": "ready_for_ai_review"},
            )

        updated = json.loads(saved["value"])
        item = updated["items"]["CAP/USDT:USDT|long"]
        self.assertEqual(item["expires_at"], (now + timedelta(minutes=30)).isoformat())
        self.assertEqual(item["ai_review_extend_minutes"], 30)

    def test_weak_review_does_not_extend_watchlist(self) -> None:
        now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        original_expires_at = (now + timedelta(minutes=5)).isoformat()
        state = {
            "items": {
                "CAP/USDT:USDT|long": {
                    "symbol": "CAP/USDT:USDT",
                    "side": "long",
                    "expires_at": original_expires_at,
                }
            }
        }
        saved: dict[str, str] = {}

        with patch("crypto_trader.trend_scan.get_journal_state", return_value=json.dumps(state)), patch(
            "crypto_trader.trend_scan.set_journal_state",
            side_effect=lambda _config, _key, value: saved.setdefault("value", value),
        ), patch("crypto_trader.trend_scan.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = now
            mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
            _save_watchlist_ai_review_state(
                self._config(),
                "CAP/USDT:USDT",
                "long",
                signature={"entry_price": 1},
                ai_review={
                    "decision": "REVIEW",
                    "setup_grade": "C",
                    "entry_quality": 60,
                    "continuation_score": 50,
                    "allow_recheck_if_setup_changes": False,
                },
                setup={"entry_action": "WAIT_PULLBACK_LONG", "setup_state": "ready_for_ai_review"},
            )

        updated = json.loads(saved["value"])
        item = updated["items"]["CAP/USDT:USDT|long"]
        self.assertEqual(item["expires_at"], original_expires_at)
        self.assertNotIn("ai_review_extended_at", item)

    def test_same_fingerprint_blocks_repeat_trend_ai_review(self) -> None:
        setup = {
            "symbol": "CAP/USDT:USDT",
            "side": "long",
            "entry_action": "WAIT_PULLBACK_LONG",
            "setup_state": "ready_for_ai_review",
            "entry_type": "pullback",
            "entry_price": 1.0,
            "stop_loss": 0.98,
            "take_profit": 1.05,
            "risk_reward": 2.5,
            "pullback_quality": 62.0,
            "breakout_quality": 60.0,
            "rsi": 61.0,
            "price_vs_ema_slow_pct": 0.4,
            "volume_confirmation": True,
            "warnings": ["near_resistance"],
        }
        item = {
            "trend_score": 62.14,
            "entry_readiness_score": 9.88,
            "watch_type": "trend",
            "last_ai_verdict": "REVIEW",
            "last_ai_review_at": "2026-07-31T10:00:00+00:00",
            "last_ai_same_verdict_count": 2,
        }
        item["last_ai_setup_signature"] = {
            "symbol": "CAP/USDT:USDT",
            "side": "long",
            "entry_action": "WAIT_PULLBACK_LONG",
            "setup_state": "ready_for_ai_review",
            "entry_type": "pullback",
            "entry_price": 1.0,
            "stop_loss": 0.98,
            "take_profit": 1.05,
            "risk_reward": 2.5,
            "trend_score": 62.14,
            "entry_readiness_score": 9.88,
            "pullback_quality": 62.0,
            "breakout_quality": 60.0,
            "rsi": 61.0,
            "price_vs_ema_slow_pct": 0.4,
            "volume_confirmation": True,
            "watch_type": "trend",
            "warnings": ["near_resistance"],
        }
        item["last_ai_setup_fingerprint"] = _trend_setup_fingerprint(setup, item)

        changed = _trend_setup_changed_enough(self._config(), setup, item)

        self.assertFalse(changed["changed"])
        self.assertEqual(changed["reason"], "setup_fingerprint_unchanged")

    def test_price_drift_same_setup_class_does_not_trigger_ai_recheck(self) -> None:
        setup = {
            "symbol": "GIGGLE/USDT:USDT",
            "side": "long",
            "entry_action": "REVIEW_COUNTERTREND_SHORT",
            "setup_state": "review_only",
            "entry_type": "pullback",
            "entry_price": 48.6,
            "stop_loss": 47.9439,
            "take_profit": 49.748175,
            "risk_reward": 1.75,
            "pullback_quality": 100.0,
            "breakout_quality": 60.0,
            "volume_confirmation": True,
            "warnings": ["no_chase_entry"],
            "entry_action_reason": ["near_resistance"],
            "support_resistance": {"near_resistance": True},
        }
        item = {
            "symbol": "GIGGLE/USDT:USDT",
            "side": "long",
            "trend_score": 66.0,
            "entry_readiness_score": 58.0,
            "last_ai_verdict": "REVIEW",
            "last_ai_review_at": "2026-08-01T08:00:00+00:00",
            "last_ai_setup_class": "near_resistance_no_chase",
            "last_ai_setup_signature": {
                "symbol": "GIGGLE/USDT:USDT",
                "side": "long",
                "entry_action": "REVIEW_COUNTERTREND_SHORT",
                "setup_state": "review_only",
                "entry_type": "pullback",
                "entry_price": 45.0,
                "stop_loss": 44.3925,
                "take_profit": 46.063125,
                "risk_reward": 1.75,
                "trend_score": 65.0,
                "entry_readiness_score": 57.0,
                "pullback_quality": 100.0,
                "breakout_quality": 60.0,
                "volume_confirmation": True,
                "warnings": ["no_chase_entry"],
                "setup_class": "near_resistance_no_chase",
            },
        }

        with patch("crypto_trader.trend_scan.get_journal_state", return_value=None):
            changed = _trend_setup_changed_enough(self._config(), setup, item)

        self.assertFalse(changed["changed"])
        self.assertEqual(changed["reason"], "setup_unchanged")
        self.assertEqual(changed["setup_class"], "near_resistance_no_chase")

    def test_quality_improvement_overrides_same_class_cooldown(self) -> None:
        setup = {
            "symbol": "GIGGLE/USDT:USDT",
            "side": "long",
            "entry_action": "READY_PULLBACK_LONG",
            "setup_state": "ready",
            "entry_type": "pullback",
            "entry_price": 48.6,
            "stop_loss": 47.9439,
            "take_profit": 49.748175,
            "risk_reward": 1.75,
            "pullback_quality": 100.0,
            "breakout_quality": 60.0,
            "volume_confirmation": True,
            "warnings": [],
            "entry_action_reason": [],
            "risk_model": {"selected_method": "structure_swing_to_previous_extreme"},
        }
        item = {
            "symbol": "GIGGLE/USDT:USDT",
            "side": "long",
            "trend_score": 80.0,
            "entry_readiness_score": 78.0,
            "last_ai_verdict": "REVIEW",
            "last_ai_review_at": "2026-08-01T08:00:00+00:00",
            "last_ai_setup_class": "near_resistance_no_chase",
            "last_ai_setup_signature": {
                "symbol": "GIGGLE/USDT:USDT",
                "side": "long",
                "entry_action": "REVIEW_COUNTERTREND_SHORT",
                "setup_state": "review_only",
                "entry_type": "pullback",
                "entry_price": 48.6,
                "stop_loss": 47.9439,
                "take_profit": 49.748175,
                "risk_reward": 1.75,
                "trend_score": 66.0,
                "entry_readiness_score": 58.0,
                "pullback_quality": 100.0,
                "breakout_quality": 60.0,
                "volume_confirmation": True,
                "warnings": ["no_chase_entry"],
                "setup_class": "near_resistance_no_chase",
            },
        }

        with patch("crypto_trader.trend_scan.get_journal_state", return_value=None):
            changed = _trend_setup_changed_enough(self._config(), setup, item)

        self.assertTrue(changed["changed"])
        self.assertIn("entry_readiness_improved", changed["reason"])
        self.assertIn("setup_class_improved", changed["reason"])

    def test_entry_proposal_uses_structure_levels_when_support_resistance_available(self) -> None:
        config = self._config()
        row = {
            "symbol": "CAP/USDT:USDT",
            "side": "long",
            "payload": {
                "symbol": "CAP/USDT:USDT",
                "side": "long",
                "entry": 100.0,
                "indicator_summary": {
                    "last": 100.0,
                    "rsi": 52.0,
                    "atr_pct": 1.0,
                    "volume_ratio": 1.2,
                    "price_vs_ema_slow_pct": 0.5,
                    "support": 96.0,
                    "resistance": 112.0,
                    "range_position": 0.25,
                },
            },
        }

        setup = build_entry_proposal(config, row)

        self.assertEqual(setup["risk_model"]["selected_method"], "structure_swing_to_previous_extreme")
        self.assertTrue(setup["fibonacci_context"]["available"])
        self.assertGreater(setup["support_resistance"]["support"], 0)
        self.assertGreater(setup["support_resistance"]["resistance"], 0)
        self.assertGreaterEqual(len(setup["setup_candidates"]), 3)
        self.assertEqual(setup["selected_setup_method"], "structure_swing_to_previous_extreme")
        self.assertGreater(setup["setup_quality_score"], 0)
        self.assertIn(setup["setup_quality_grade"], {"A", "B", "C", "D"})

    def test_setup_quality_improvement_triggers_ai_recheck(self) -> None:
        setup = {
            "symbol": "CAP/USDT:USDT",
            "side": "long",
            "entry_action": "WAIT_PULLBACK_LONG",
            "setup_state": "ready_for_ai_review",
            "entry_type": "pullback",
            "entry_price": 1.0,
            "stop_loss": 0.98,
            "take_profit": 1.05,
            "risk_reward": 1.75,
            "trend_score": 63.0,
            "entry_readiness_score": 35.0,
            "setup_quality_score": 74.0,
            "setup_quality_grade": "C",
            "selected_setup_method": "structure_swing_to_previous_extreme",
            "pullback_quality": 72.0,
            "breakout_quality": 60.0,
            "volume_confirmation": True,
            "warnings": [],
        }
        item = {
            "symbol": "CAP/USDT:USDT",
            "side": "long",
            "trend_score": 63.0,
            "entry_readiness_score": 35.0,
            "last_ai_verdict": "REVIEW",
            "last_ai_review_at": "2026-08-01T08:00:00+00:00",
            "last_ai_setup_class": "ready_to_review",
            "last_ai_setup_signature": {
                "symbol": "CAP/USDT:USDT",
                "side": "long",
                "entry_action": "WAIT_PULLBACK_LONG",
                "setup_state": "ready_for_ai_review",
                "entry_type": "pullback",
                "entry_price": 1.0,
                "stop_loss": 0.98,
                "take_profit": 1.05,
                "risk_reward": 1.75,
                "trend_score": 63.0,
                "entry_readiness_score": 35.0,
                "setup_quality_score": 55.0,
                "setup_quality_grade": "D",
                "selected_setup_method": "atr_volatility_rr",
                "pullback_quality": 72.0,
                "breakout_quality": 60.0,
                "volume_confirmation": True,
                "warnings": [],
                "setup_class": "ready_to_review",
            },
        }

        with patch("crypto_trader.trend_scan.get_journal_state", return_value=None):
            changed = _trend_setup_changed_enough(self._config(), setup, item)

        self.assertTrue(changed["changed"])
        self.assertIn("setup_quality_improved", changed["reason"])

    def test_setup_only_reject_does_not_extend_or_remove_watchlist_first_time(self) -> None:
        now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        original_expires_at = (now + timedelta(minutes=5)).isoformat()
        state = {
            "items": {
                "CAP/USDT:USDT|long": {
                    "symbol": "CAP/USDT:USDT",
                    "side": "long",
                    "expires_at": original_expires_at,
                }
            }
        }
        saved: dict[str, str] = {}

        with patch("crypto_trader.trend_scan.get_journal_state", return_value=json.dumps(state)), patch(
            "crypto_trader.trend_scan.set_journal_state",
            side_effect=lambda _config, _key, value: saved.setdefault("value", value),
        ), patch("crypto_trader.trend_scan.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = now
            mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
            _save_watchlist_ai_review_state(
                self._config(),
                "CAP/USDT:USDT",
                "long",
                signature={"entry_price": 1},
                ai_review={"decision": "REJECT", "reject_scope": "SETUP_ONLY", "reject_reason_type": "BAD_ENTRY"},
                setup={"entry_action": "WAIT_PULLBACK_LONG", "setup_state": "ready_for_ai_review"},
            )

        updated = json.loads(saved["value"])
        item = updated["items"]["CAP/USDT:USDT|long"]
        self.assertEqual(item["expires_at"], original_expires_at)
        self.assertEqual(item["status"], "rejected_wait_new_setup")
        self.assertNotIn("ai_review_extended_at", item)

    def test_watchlist_remove_reject_removes_and_cooldowns(self) -> None:
        now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        state = {
            "items": {
                "CAP/USDT:USDT|long": {
                    "symbol": "CAP/USDT:USDT",
                    "side": "long",
                    "expires_at": (now + timedelta(minutes=5)).isoformat(),
                }
            }
        }
        saved: dict[str, str] = {}

        with patch("crypto_trader.trend_scan.get_journal_state", return_value=json.dumps(state)), patch(
            "crypto_trader.trend_scan.set_journal_state",
            side_effect=lambda _config, _key, value: saved.setdefault("value", value),
        ), patch("crypto_trader.trend_scan.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = now
            mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
            _save_watchlist_ai_review_state(
                self._config(),
                "CAP/USDT:USDT",
                "long",
                signature={"entry_price": 1},
                ai_review={"decision": "REJECT", "reject_scope": "WATCHLIST_REMOVE", "reject_reason_type": "TREND_INVALID"},
                setup={"entry_action": "WAIT_PULLBACK_LONG", "setup_state": "ready_for_ai_review"},
            )

        updated = json.loads(saved["value"])
        self.assertNotIn("CAP/USDT:USDT|long", updated["items"])
        self.assertIn("CAP/USDT:USDT|long", updated["rejected_until"])

    def test_approved_setup_with_temporary_block_enters_hold_queue(self) -> None:
        saved: dict[str, str] = {}
        setup = {"symbol": "CAP/USDT:USDT", "side": "long", "entry_price": 1.0}
        risk_capital = {
            "risk": {"approved": False, "reasons": ["Bunny Health Monitor dang pause"], "warnings": []},
            "capital": {"approved": False, "allowed": False, "reason": "Order size is not positive"},
        }

        with patch("crypto_trader.trend_scan.get_journal_state", return_value=None), patch(
            "crypto_trader.trend_scan.set_journal_state",
            side_effect=lambda _config, _key, value: saved.setdefault(_key, value),
        ), patch("crypto_trader.notifier.send_telegram_message") as send_message:
            state = upsert_trend_approved_hold_queue(
                self._config(),
                setup=setup,
                ai_review={"decision": "APPROVE", "setup_grade": "A"},
                activation={"status": "approved_for_risk", "trade_intent": {"symbol": "CAP/USDT:USDT", "side": "long"}},
                risk_capital=risk_capital,
            )

        item = state["items"]["CAP/USDT:USDT|long"]
        self.assertEqual(item["status"], "approved_hold")
        self.assertEqual(item["block_type"], "temporary_block")
        self.assertEqual(item["priority_rewatch_ttl_minutes"], 30)
        send_message.assert_called_once()
        message = send_message.call_args.args[1]
        self.assertIn("Trend APPROVED HOLD QUEUE", message)
        self.assertIn("ENTERED_QUEUE", message)
        self.assertIn("Lý do block:\n  - Health Monitor đang pause\n  - Vốn vào lệnh đang bằng 0/quá nhỏ", message)
        self.assertIn("Block đã gỡ:\n\nThời gian trong queue: 30p", message)
        self.assertIn("Thời gian trong queue: 30p", message)

    def test_approved_hold_update_notifies_resolved_block_reasons(self) -> None:
        existing_state = {
            "items": {
                "CAP/USDT:USDT|long": {
                    "created_at": "2026-07-30T11:30:00+00:00",
                    "symbol": "CAP/USDT:USDT",
                    "side": "long",
                    "block_type": "temporary_block",
                    "block_reasons": ["Bunny Health Monitor dang pause", "Order size is not positive"],
                }
            }
        }
        saved: dict[str, str] = {}
        setup = {"symbol": "CAP/USDT:USDT", "side": "long", "entry_price": 1.0}
        risk_capital = {
            "risk": {"approved": False, "reasons": ["Bunny Health Monitor dang pause"], "warnings": []},
            "capital": {"approved": False, "allowed": False, "reason": None},
        }

        with patch("crypto_trader.trend_scan.get_journal_state", return_value=json.dumps(existing_state)), patch(
            "crypto_trader.trend_scan.set_journal_state",
            side_effect=lambda _config, _key, value: saved.setdefault(_key, value),
        ), patch("crypto_trader.notifier.send_telegram_message") as send_message:
            upsert_trend_approved_hold_queue(
                self._config(),
                setup=setup,
                ai_review={"decision": "APPROVE", "setup_grade": "A"},
                activation={"status": "approved_for_risk", "trade_intent": {"symbol": "CAP/USDT:USDT", "side": "long"}},
                risk_capital=risk_capital,
            )

        send_message.assert_called_once()
        message = send_message.call_args.args[1]
        self.assertIn("Trend APPROVED HOLD UPDATE", message)
        self.assertIn("QUEUE_UPDATED", message)
        self.assertIn("Lý do block:\n  - Health Monitor đang pause", message)
        self.assertIn("Block đã gỡ:\n  - Vốn vào lệnh đang bằng 0/quá nhỏ", message)

    def test_expired_approved_hold_returns_to_priority_rewatch(self) -> None:
        now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        queue_state = {
            "items": {
                "CAP/USDT:USDT|long": {
                    "created_at": (now - timedelta(minutes=31)).isoformat(),
                    "updated_at": (now - timedelta(minutes=31)).isoformat(),
                    "expires_at": (now - timedelta(minutes=1)).isoformat(),
                    "symbol": "CAP/USDT:USDT",
                    "side": "long",
                    "block_type": "priority_rewatch",
                    "setup": {"entry_price": 1.0, "stop_loss": 0.99, "take_profit": 1.03, "risk_reward": 3.0},
                    "ai_review": {"decision": "APPROVE"},
                }
            }
        }
        saved: dict[str, str] = {}

        def fake_get(_config: dict, key: str) -> str | None:
            if key == "trend_approved_hold_queue_state":
                return json.dumps(queue_state)
            if key == "trend_watchlist_state":
                return json.dumps({"items": {}})
            return None

        with patch("crypto_trader.trend_scan.get_journal_state", side_effect=fake_get), patch(
            "crypto_trader.trend_scan.set_journal_state",
            side_effect=lambda _config, key, value: saved.__setitem__(key, value),
        ), patch("crypto_trader.notifier.send_telegram_message") as send_message:
            result = process_trend_approved_hold_queue(self._config(), now=now)

        self.assertEqual(result["processed"][0]["action"], "priority_rewatch")
        watchlist = json.loads(saved["trend_watchlist_state"])
        item = watchlist["items"]["CAP/USDT:USDT|long"]
        self.assertEqual(item["status"], "priority_rewatch")
        self.assertEqual(item["ttl_minutes"], 30)
        self.assertEqual(item["priority"], 100)
        send_message.assert_called_once()
        message = send_message.call_args.args[1]
        self.assertIn("Trend PRIORITY REWATCH", message)
        self.assertIn("BACK_TO_WATCHLIST", message)
        self.assertIn("TTL rewatch: 30p", message)

    def test_expired_remove_pair_hold_notifies_cooldown(self) -> None:
        now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        queue_state = {
            "items": {
                "CAP/USDT:USDT|long": {
                    "created_at": (now - timedelta(minutes=31)).isoformat(),
                    "updated_at": (now - timedelta(minutes=31)).isoformat(),
                    "expires_at": (now - timedelta(minutes=1)).isoformat(),
                    "symbol": "CAP/USDT:USDT",
                    "side": "long",
                    "block_type": "remove_pair",
                    "block_reasons": ["trend_invalid"],
                    "setup": {"entry_price": 1.0},
                    "ai_review": {"decision": "APPROVE"},
                }
            }
        }
        saved: dict[str, str] = {}

        def fake_get(_config: dict, key: str) -> str | None:
            if key == "trend_approved_hold_queue_state":
                return json.dumps(queue_state)
            if key == "trend_watchlist_state":
                return json.dumps({"items": {}})
            return None

        with patch("crypto_trader.trend_scan.get_journal_state", side_effect=fake_get), patch(
            "crypto_trader.trend_scan.set_journal_state",
            side_effect=lambda _config, key, value: saved.__setitem__(key, value),
        ), patch("crypto_trader.notifier.send_telegram_message") as send_message:
            result = process_trend_approved_hold_queue(self._config(), now=now)

        self.assertEqual(result["processed"][0]["action"], "remove_pair_cooldown")
        send_message.assert_called_once()
        message = send_message.call_args.args[1]
        self.assertIn("Trend REMOVE PAIR", message)
        self.assertIn("REMOVE_PAIR", message)
        self.assertIn("Lý do block:\n  - Trend đã gãy", message)
        self.assertIn("Block đã gỡ:\n\nCooldown: 120p", message)
        self.assertIn("Cooldown: 120p", message)

    def test_recheck_approved_hold_marks_ready_when_blocks_clear(self) -> None:
        now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        queue_state = {
            "items": {
                "CAP/USDT:USDT|long": {
                    "created_at": (now - timedelta(minutes=5)).isoformat(),
                    "updated_at": (now - timedelta(minutes=5)).isoformat(),
                    "next_recheck_at": (now - timedelta(seconds=1)).isoformat(),
                    "symbol": "CAP/USDT:USDT",
                    "side": "long",
                    "block_type": "temporary_block",
                    "block_reasons": ["Bunny Health Monitor dang pause", "Order size is not positive"],
                    "setup": {"symbol": "CAP/USDT:USDT", "side": "long"},
                    "ai_review": {"decision": "APPROVE"},
                    "activation": {"status": "approved_for_risk", "trade_intent": {"symbol": "CAP/USDT:USDT", "side": "long"}},
                }
            }
        }
        saved: dict[str, str] = {}

        with patch("crypto_trader.trend_scan.get_journal_state", return_value=json.dumps(queue_state)), patch(
            "crypto_trader.trend_scan.set_journal_state",
            side_effect=lambda _config, key, value: saved.__setitem__(key, value),
        ), patch(
            "crypto_trader.trend_scan.evaluate_trade_intent_risk_capital_shadow",
            return_value={"risk": {"approved": True, "reasons": [], "warnings": []}, "capital": {"approved": True, "allowed": True, "reason": "OK"}},
        ), patch("crypto_trader.notifier.send_telegram_message") as send_message:
            result = recheck_trend_approved_hold_queue(self._config(), now=now)

        self.assertEqual(result["checked"][0]["action"], "ready_for_order")
        updated = json.loads(saved["trend_approved_hold_queue_state"])
        self.assertEqual(updated["count"], 0)
        send_message.assert_called_once()
        message = send_message.call_args.args[1]
        self.assertIn("Trend QUEUE CLEARED", message)
        self.assertIn("READY_FOR_ORDER", message)
        self.assertIn("Block đã gỡ:\n  - Health Monitor đang pause\n  - Vốn vào lệnh đang bằng 0/quá nhỏ", message)

    def test_priority_rewatch_is_reviewed_before_regular_watchlist(self) -> None:
        config = self._config()
        config["ai"]["internal"]["trend_auto_shadow_review_limit"] = 1
        watchlist = {
            "items": {
                "REG/USDT:USDT|long": {"symbol": "REG/USDT:USDT", "side": "long", "status": "watching", "trend_score": 99},
                "CAP/USDT:USDT|long": {"symbol": "CAP/USDT:USDT", "side": "long", "status": "priority_rewatch", "trend_score": 10},
            }
        }

        with patch("crypto_trader.trend_scan._latest_market_scan_row_for_symbol_side", return_value=None):
            result = run_trend_auto_shadow_reviews(config, watchlist)

        self.assertEqual(result["items"][0]["symbol"], "CAP/USDT:USDT")

    def test_ai_review_does_not_shorten_existing_watchlist_ttl(self) -> None:
        now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        original_expires_at = (now + timedelta(minutes=90)).isoformat()
        state = {
            "items": {
                "CAP/USDT:USDT|long": {
                    "symbol": "CAP/USDT:USDT",
                    "side": "long",
                    "expires_at": original_expires_at,
                }
            }
        }
        saved: dict[str, str] = {}

        with patch("crypto_trader.trend_scan.get_journal_state", return_value=json.dumps(state)), patch(
            "crypto_trader.trend_scan.set_journal_state",
            side_effect=lambda _config, _key, value: saved.setdefault("value", value),
        ), patch("crypto_trader.trend_scan.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = now
            mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
            _save_watchlist_ai_review_state(
                self._config(),
                "CAP/USDT:USDT",
                "long",
                signature={"entry_price": 1},
                ai_review={"decision": "APPROVE"},
                setup={"entry_action": "READY", "setup_state": "ready_for_ai_review"},
            )

        updated = json.loads(saved["value"])
        item = updated["items"]["CAP/USDT:USDT|long"]
        self.assertEqual(item["expires_at"], original_expires_at)
        self.assertNotIn("ai_review_extended_at", item)
