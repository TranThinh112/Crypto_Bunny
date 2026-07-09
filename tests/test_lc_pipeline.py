from __future__ import annotations

import tempfile
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch

import crypto_trader.lc_pipeline as lc_pipeline_module
from crypto_trader.config import DEFAULT_CONFIG
from crypto_trader.lc_pipeline import (
    _four_hour_notification_text,
    format_internal_lc_view,
    format_internal_notifications_view,
    lc_pipeline_dashboard_payload,
    lc_pipeline_mini_pool,
    notify_mini_pool_summary,
    _recheck_rows_with_latest_market_data,
    _two_hour_notification_text,
    update_lc_internal_pipeline,
)
from crypto_trader.models import TradeCandidate
from crypto_trader.storage import get_journal_state, set_journal_state


def _candidate(
    symbol: str,
    win: float,
    confidence: float = 80.0,
    volume: float = 1.0,
    side: str = "long",
) -> TradeCandidate:
    return TradeCandidate(
        symbol=symbol,
        base=symbol.split("/")[0],
        side=side,
        confidence=confidence,
        win_probability_pct=win,
        entry=1.0,
        stop_loss=0.98,
        take_profit=1.03,
        risk_reward=1.5,
        order_usdt=20.0,
        quantity=1.0,
        spread_pct=0.01,
        news_score=0.0,
        news_count=0,
        indicator_summary={"volume_ratio": volume},
    )


def _saved_row(
    symbol: str,
    win: float,
    *,
    side: str = "long",
    state: str = "HOUR_1",
    confidence: float = 80.0,
    volume: float = 1.0,
) -> dict:
    return {
        "symbol": symbol,
        "base": symbol.split("/")[0],
        "side": side,
        "state": state,
        "first_seen_at": "2026-07-06T00:00:00+00:00",
        "last_seen_at": "2026-07-06T00:00:00+00:00",
        "entry": 1.0,
        "price": 1.0,
        "confidence": confidence,
        "win_probability_pct": win,
        "risk_reward": 1.5,
        "volume_ratio": volume,
        "payload": {"symbol": symbol, "side": side, "win_probability_pct": win},
    }


class LcPipelineTest(TestCase):
    def _config(self) -> dict:
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        config = deepcopy(DEFAULT_CONFIG)
        config["_config_dir"] = self.tmpdir.name
        config["_atlas_test_mode"] = True
        config["ai"]["internal"]["lc_pipeline_notify_two_hour_summary"] = False
        config["ai"]["internal"]["lc_pipeline_promote_to_pending"] = False
        config["ai"]["internal"]["lc_pipeline_min_win_probability_pct"] = 50
        config["ai"]["internal"]["lc_pipeline_one_hour_min_win_probability_pct"] = 50
        config["ai"]["internal"]["lc_pipeline_two_hour_min_win_probability_pct"] = 50
        config["ai"]["internal"]["lc_pipeline_four_hour_min_win_probability_pct"] = 50
        return config

    def tearDown(self) -> None:
        tmpdir = getattr(self, "tmpdir", None)
        if tmpdir:
            tmpdir.cleanup()

    def test_two_hour_notification_infers_source_windows_from_one_hour_history(self) -> None:
        config = self._config()
        state = {
            "state_version": 3,
            "day_key": "2026-07-06",
            "one_hour_history": [
                {
                    "frame": "1h",
                    "slot": "2026-07-06T07:00:00+07:00",
                    "created_at": "2026-07-06T00:05:00+00:00",
                    "index": 1,
                    "daily_index": 1,
                    "approved": [],
                    "rejected": [],
                },
                {
                    "frame": "1h",
                    "slot": "2026-07-06T08:00:00+07:00",
                    "created_at": "2026-07-06T01:05:00+00:00",
                    "index": 2,
                    "daily_index": 2,
                    "approved": [],
                    "rejected": [],
                },
            ],
            "two_hour_history": [
                {
                    "frame": "2h",
                    "slot": "2026-07-06T08:00:00+07:00",
                    "created_at": "2026-07-06T01:05:00+00:00",
                    "index": 1,
                    "daily_index": 1,
                    "approved": [{**_saved_row("AAA/USDT:USDT", 64), "origin_source_slot": "1h", "origin_source_index": 2}],
                    "rejected": [],
                }
            ],
        }
        set_journal_state(config, "lc_internal_pipeline_state", json.dumps(state, ensure_ascii=False))

        message = _two_hour_notification_text(config, state["two_hour_history"][0])

        self.assertIn("Khung 🟡 2h: #1 (08:05)", message)
        self.assertIn("Gộp từ: 🔵 1h #1 (07:05), 🔵 1h #2 (08:05)", message)

    def test_dashboard_payload_sanitizes_sample_symbols_when_filter_enabled(self) -> None:
        config = self._config()
        config["ai"]["internal"]["lc_pipeline_drop_sample_symbols"] = True
        state = {
            "state_version": 3,
            "day_key": "2026-07-06",
            "undecided": [
                _saved_row("AAA/USDT:USDT", 61, state="CHUA_DUYET"),
                _saved_row("NEAR/USDT:USDT", 63, state="CHUA_DUYET", side="short"),
            ],
            "internal_lc": [
                _saved_row("BBB/USDT:USDT", 64, state="LC_NOI_BO"),
                _saved_row("ETH/USDT:USDT", 65, state="LC_NOI_BO"),
            ],
            "internal_notifications": [
                {
                    "title": "RC #1",
                    "lines": ["1. AAA/USDT:USDT | LONG", "2. ETH/USDT:USDT | LONG"],
                    "created_at": "2026-07-06T00:00:00+00:00",
                }
            ],
        }
        set_journal_state(config, "lc_internal_pipeline_state", json.dumps(state, ensure_ascii=False))

        payload = lc_pipeline_dashboard_payload(config)
        saved_state = json.loads(get_journal_state(config, "lc_internal_pipeline_state") or "{}")

        self.assertEqual([row["symbol"] for row in payload["undecided"]], ["NEAR/USDT:USDT"])
        self.assertEqual([row["symbol"] for row in payload["internal_lc"]], ["ETH/USDT:USDT"])
        self.assertNotIn("AAA/USDT:USDT", json.dumps(saved_state, ensure_ascii=False))
        self.assertNotIn("BBB/USDT:USDT", json.dumps(saved_state, ensure_ascii=False))

    def test_four_hour_notification_infers_source_windows_from_two_hour_history(self) -> None:
        config = self._config()
        state = {
            "state_version": 3,
            "day_key": "2026-07-06",
            "two_hour_history": [
                {
                    "frame": "2h",
                    "slot": "2026-07-06T08:00:00+07:00",
                    "created_at": "2026-07-06T01:05:00+00:00",
                    "index": 1,
                    "daily_index": 1,
                    "approved": [],
                    "rejected": [],
                },
                {
                    "frame": "2h",
                    "slot": "2026-07-06T10:00:00+07:00",
                    "created_at": "2026-07-06T03:05:00+00:00",
                    "index": 2,
                    "daily_index": 2,
                    "approved": [],
                    "rejected": [],
                },
            ],
            "four_hour_history": [
                {
                    "frame": "4h",
                    "slot": "2026-07-06T10:00:00+07:00",
                    "created_at": "2026-07-06T03:05:00+00:00",
                    "index": 1,
                    "daily_index": 1,
                    "approved": [{**_saved_row("BBB/USDT:USDT", 65), "origin_source_slot": "1h", "origin_source_index": 4}],
                    "rejected": [],
                }
            ],
        }
        set_journal_state(config, "lc_internal_pipeline_state", json.dumps(state, ensure_ascii=False))

        message = _four_hour_notification_text(config, state["four_hour_history"][0])

        self.assertIn("Khung 🔴 4h: #1 (10:05)", message)
        self.assertIn("Gộp từ: 🟡 2h #1 (08:05), 🟡 2h #2 (10:05)", message)

    @patch("crypto_trader.lc_pipeline._recheck_rows_with_latest_market_data")
    @patch("crypto_trader.notifier.send_telegram_message")
    def test_two_hour_summary_keeps_top_three_and_notifies(self, send_message, recheck_rows) -> None:
        config = self._config()
        config["ai"]["internal"]["lc_pipeline_notify_two_hour_summary"] = True
        start = datetime(2026, 7, 7, 0, 5, tzinfo=timezone.utc)
        first = [
            _candidate("AAA/USDT:USDT", 61, volume=3),
            _candidate("BBB/USDT:USDT", 60, volume=2),
            _candidate("CCC/USDT:USDT", 59, volume=1),
        ]
        second = [
            _candidate("DDD/USDT:USDT", 64, volume=4),
            _candidate("EEE/USDT:USDT", 63, volume=3),
            _candidate("FFF/USDT:USDT", 62, volume=2),
        ]
        recheck_rows.return_value = (
            [
                {**_saved_row("AAA/USDT:USDT", 61, volume=3), "source_slot": "1h", "source_index": 1, "source_time": start.isoformat()},
                {**_saved_row("BBB/USDT:USDT", 60, volume=2), "source_slot": "1h", "source_index": 1, "source_time": start.isoformat()},
                {**_saved_row("CCC/USDT:USDT", 59, volume=1), "source_slot": "1h", "source_index": 1, "source_time": start.isoformat()},
                {**_saved_row("DDD/USDT:USDT", 64, volume=4), "source_slot": "1h", "source_index": 2, "source_time": (start + timedelta(hours=1)).isoformat()},
                {**_saved_row("EEE/USDT:USDT", 63, volume=3), "source_slot": "1h", "source_index": 2, "source_time": (start + timedelta(hours=1)).isoformat()},
                {**_saved_row("FFF/USDT:USDT", 62, volume=2), "source_slot": "1h", "source_index": 2, "source_time": (start + timedelta(hours=1)).isoformat()},
            ],
            {"input_count": 6, "refreshed_count": 6, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 6},
        )

        update_lc_internal_pipeline(config, first, now=start)
        result = update_lc_internal_pipeline(config, second, now=start + timedelta(hours=1))

        self.assertTrue(result["created_two_hour"])
        approved = [row["symbol"] for row in result["two_hour_event"]["approved"]]
        rejected = [row["symbol"] for row in result["two_hour_event"]["rejected"]]
        self.assertEqual(approved, ["DDD/USDT:USDT", "EEE/USDT:USDT", "FFF/USDT:USDT"])
        self.assertEqual(rejected[:3], ["AAA/USDT:USDT", "BBB/USDT:USDT", "CCC/USDT:USDT"])
        raw_state = get_journal_state(config, "lc_internal_pipeline_state")
        state = json.loads(raw_state or "{}")
        internal_symbols = [row["symbol"] for row in state.get("internal_lc", [])]
        self.assertEqual(internal_symbols, ["DDD/USDT:USDT", "EEE/USDT:USDT", "FFF/USDT:USDT"])
        self.assertEqual(state["internal_lc"][0]["source_slot"], "2h")
        self.assertEqual(state["internal_lc"][0]["source_index"], 1)
        internal_notifications = state.get("internal_notifications") or []
        self.assertEqual([item["frame"] for item in internal_notifications], ["1h", "1h", "2h", "4h"])
        self.assertEqual(send_message.call_count, 2)
        first_message = send_message.call_args_list[0].args[1]
        second_message = send_message.call_args_list[1].args[1]
        self.assertIn("🟡 #1 LC nội bộ tổng hợp 2h", first_message)
        self.assertIn("Khung 🟡 2h: #1 (08:05)", first_message)
        self.assertIn("Gộp từ: 🔵 1h #1 (07:05), 🔵 1h #2 (08:05)", first_message)
        self.assertIn("DDD/USDT:USDT | LONG | Win 64.00%", first_message)
        self.assertIn("Gốc 🔵 1h #2 (08:05)", first_message)
        self.assertIn("🔴 #1 LC nội bộ tổng hợp 4h", second_message)
        self.assertFalse(send_message.call_args_list[0].kwargs["replace_previous"])
        self.assertFalse(send_message.call_args_list[1].kwargs["replace_previous"])

    @patch("crypto_trader.lc_pipeline._recheck_rows_with_latest_market_data")
    def test_two_hour_summary_runs_with_single_one_hour_pool_in_demo_mode(self, recheck_rows) -> None:
        config = self._config()
        start = datetime(2026, 7, 7, 0, 5, tzinfo=timezone.utc)
        one_hour_candidates = [
            _candidate("AAA/USDT:USDT", 58, volume=3),
            _candidate("BBB/USDT:USDT", 57, volume=2),
        ]
        recheck_rows.return_value = (
            [
                {**_saved_row("AAA/USDT:USDT", 58, volume=3), "source_slot": "1h", "source_index": 1, "source_time": start.isoformat()},
                {**_saved_row("BBB/USDT:USDT", 57, volume=2), "source_slot": "1h", "source_index": 1, "source_time": start.isoformat()},
            ],
            {"input_count": 2, "refreshed_count": 2, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 2},
        )

        first = update_lc_internal_pipeline(config, one_hour_candidates, now=start)
        result = update_lc_internal_pipeline(config, [], now=start + timedelta(hours=1))

        self.assertTrue(first["created_hourly"])
        self.assertTrue(result["created_two_hour"])
        self.assertTrue(result["created_four_hour"])
        self.assertEqual([row["symbol"] for row in result["two_hour_event"]["approved"]], ["AAA/USDT:USDT", "BBB/USDT:USDT"])

    @patch("crypto_trader.notifier.send_telegram_message")
    def test_one_hour_summary_is_sent_only_after_state_save_succeeds(self, send_message) -> None:
        config = self._config()
        start = datetime(2026, 7, 7, 12, 0, 40, tzinfo=timezone.utc)
        original_save_state = lc_pipeline_module._save_state
        save_attempts = {"count": 0}

        def flaky_save_state(config_arg, state_arg):
            save_attempts["count"] += 1
            if save_attempts["count"] == 1:
                raise RuntimeError("atlas timeout while saving state")
            return original_save_state(config_arg, state_arg)

        with patch("crypto_trader.lc_pipeline._save_state", side_effect=flaky_save_state):
            with self.assertRaisesRegex(RuntimeError, "atlas timeout while saving state"):
                update_lc_internal_pipeline(config, [_candidate("ARB/USDT:USDT", 64.03)], now=start)

            self.assertEqual(send_message.call_count, 0)

            result = update_lc_internal_pipeline(config, [_candidate("ARB/USDT:USDT", 64.03)], now=start)

        self.assertTrue(result["created_hourly"])
        self.assertEqual(send_message.call_count, 1)
        self.assertIn("1h top 1 setup", send_message.call_args.args[1])
        raw_state = get_journal_state(config, "lc_internal_pipeline_state")
        state = json.loads(raw_state or "{}")
        self.assertEqual(state.get("last_hourly_slot"), result["hourly_slot"])

    @patch("crypto_trader.lc_pipeline._recheck_rows_with_latest_market_data")
    def test_mini_pool_uses_latest_four_hour_pairs(self, recheck_rows) -> None:
        config = self._config()
        config["ai"]["internal"]["lc_pipeline_recheck_interval_minutes"] = 999
        start = datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc)
        update_lc_internal_pipeline(
            config,
            [_candidate("AAA/USDT:USDT", 64), _candidate("BBB/USDT:USDT", 63), _candidate("CCC/USDT:USDT", 62)],
            now=start,
        )
        recheck_rows.side_effect = [
            (
                [
                    _saved_row("AAA/USDT:USDT", 64),
                    _saved_row("BBB/USDT:USDT", 63),
                    _saved_row("CCC/USDT:USDT", 62),
                    _saved_row("DDD/USDT:USDT", 67),
                    _saved_row("EEE/USDT:USDT", 66),
                    _saved_row("FFF/USDT:USDT", 65),
                ],
                {
                    "input_count": 6,
                    "refreshed_count": 6,
                    "dropped": [],
                    "warnings": [],
                    "sync_complete": True,
                    "synchronized_count": 6,
                },
            ),
            (
                [
                    {**_saved_row("DDD/USDT:USDT", 67, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1},
                    {**_saved_row("EEE/USDT:USDT", 66, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1},
                    {**_saved_row("FFF/USDT:USDT", 65, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1},
                ],
                {
                    "input_count": 3,
                    "refreshed_count": 3,
                    "dropped": [],
                    "warnings": [],
                    "sync_complete": True,
                    "synchronized_count": 3,
                },
            ),
            (
                [
                    _saved_row("GGG/USDT:USDT", 69),
                    _saved_row("HHH/USDT:USDT", 68),
                    _saved_row("III/USDT:USDT", 67),
                    _saved_row("JJJ/USDT:USDT", 72),
                    _saved_row("KKK/USDT:USDT", 71),
                    _saved_row("LLL/USDT:USDT", 70),
                ],
                {
                    "input_count": 6,
                    "refreshed_count": 6,
                    "dropped": [],
                    "warnings": [],
                    "sync_complete": True,
                    "synchronized_count": 6,
                },
            ),
            (
                [
                    {**_saved_row("DDD/USDT:USDT", 67, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1},
                    {**_saved_row("EEE/USDT:USDT", 66, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1},
                    {**_saved_row("FFF/USDT:USDT", 65, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1},
                    {**_saved_row("JJJ/USDT:USDT", 72, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 2},
                    {**_saved_row("KKK/USDT:USDT", 71, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 2},
                    {**_saved_row("LLL/USDT:USDT", 70, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 2},
                ],
                {
                    "input_count": 6,
                    "refreshed_count": 6,
                    "dropped": [],
                    "warnings": [],
                    "sync_complete": True,
                    "synchronized_count": 6,
                },
            ),
        ]
        update_lc_internal_pipeline(
            config,
            [_candidate("DDD/USDT:USDT", 67), _candidate("EEE/USDT:USDT", 66), _candidate("FFF/USDT:USDT", 65)],
            now=start + timedelta(hours=1),
        )
        update_lc_internal_pipeline(
            config,
            [_candidate("GGG/USDT:USDT", 69), _candidate("HHH/USDT:USDT", 68), _candidate("III/USDT:USDT", 67)],
            now=start + timedelta(hours=2),
        )
        current = [
            _candidate("DDD/USDT:USDT", 64),
            _candidate("EEE/USDT:USDT", 63),
            _candidate("FFF/USDT:USDT", 62),
            _candidate("JJJ/USDT:USDT", 72),
            _candidate("KKK/USDT:USDT", 71),
            _candidate("LLL/USDT:USDT", 70),
        ]
        update_lc_internal_pipeline(
            config,
            [_candidate("JJJ/USDT:USDT", 72), _candidate("KKK/USDT:USDT", 71), _candidate("LLL/USDT:USDT", 70)],
            now=start + timedelta(hours=3),
        )

        pool = lc_pipeline_mini_pool(config, current, limit=3)

        self.assertEqual([candidate.symbol for candidate in pool], ["JJJ/USDT:USDT", "KKK/USDT:USDT", "LLL/USDT:USDT"])

    @patch("crypto_trader.lc_pipeline._recheck_rows_with_latest_market_data")
    def test_two_hour_uses_rechecked_win_rate_before_selecting_top_three(self, recheck_rows) -> None:
        config = self._config()
        start = datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc)
        update_lc_internal_pipeline(
            config,
            [_candidate("AAA/USDT:USDT", 90), _candidate("BBB/USDT:USDT", 89), _candidate("CCC/USDT:USDT", 88)],
            now=start,
        )
        recheck_rows.return_value = (
            [
                _saved_row("AAA/USDT:USDT", 55),
                _saved_row("BBB/USDT:USDT", 54),
                _saved_row("CCC/USDT:USDT", 53),
                _saved_row("DDD/USDT:USDT", 95),
                _saved_row("EEE/USDT:USDT", 94),
                _saved_row("FFF/USDT:USDT", 93),
            ],
            {"refreshed_count": 6, "dropped": [], "warnings": []},
        )

        result = update_lc_internal_pipeline(
            config,
            [_candidate("DDD/USDT:USDT", 60), _candidate("EEE/USDT:USDT", 59), _candidate("FFF/USDT:USDT", 58)],
            now=start + timedelta(hours=1),
        )

        self.assertTrue(result["created_two_hour"])
        self.assertEqual(
            [row["symbol"] for row in result["two_hour_event"]["approved"]],
            ["DDD/USDT:USDT", "EEE/USDT:USDT", "FFF/USDT:USDT"],
        )
        self.assertEqual(result["two_hour_recheck"]["refreshed_count"], 6)

    def test_one_hour_assigns_source_index_and_time_to_internal_lc(self) -> None:
        config = self._config()
        start = datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc)

        result = update_lc_internal_pipeline(
            config,
            [
                _candidate("AAA/USDT:USDT", 61.5),
                _candidate("BBB/USDT:USDT", 61.1),
            ],
            now=start,
        )

        approved = result["one_hour_event"]["approved"]
        self.assertEqual(approved[0]["source_slot"], "1h")
        self.assertEqual(approved[0]["source_index"], 1)
        self.assertTrue(approved[0]["source_time"])
        self.assertTrue(approved[0]["source_label"])

        raw_state = get_journal_state(config, "lc_internal_pipeline_state")
        state = json.loads(raw_state or "{}")
        self.assertEqual(state["internal_lc"][0]["source_slot"], "1h")
        self.assertEqual(state["internal_lc"][0]["source_index"], 1)
        self.assertTrue(state["internal_lc"][0]["source_time"])
        self.assertTrue(state["internal_lc"][0]["source_label"])

        message = format_internal_lc_view(config)
        self.assertIn("1h #1 (07:05:00)", message)

    @patch("crypto_trader.lc_pipeline._recheck_rows_with_latest_market_data")
    def test_pipeline_uses_stage_specific_thresholds(self, recheck_rows) -> None:
        config = self._config()
        config["ai"]["internal"]["lc_pipeline_one_hour_min_win_probability_pct"] = 61
        config["ai"]["internal"]["lc_pipeline_two_hour_min_win_probability_pct"] = 62
        config["ai"]["internal"]["lc_pipeline_four_hour_min_win_probability_pct"] = 63
        config["ai"]["internal"]["lc_pipeline_recheck_interval_minutes"] = 999
        start = datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc)

        hour_one = update_lc_internal_pipeline(
            config,
            [
                _candidate("AAA/USDT:USDT", 61.2),
                _candidate("BBB/USDT:USDT", 61.0),
                _candidate("CCC/USDT:USDT", 60.9),
            ],
            now=start,
        )
        self.assertEqual([row["symbol"] for row in hour_one["one_hour_event"]["approved"]], ["AAA/USDT:USDT", "BBB/USDT:USDT"])

        recheck_rows.side_effect = [
            (
                [
                    _saved_row("AAA/USDT:USDT", 61.9),
                    _saved_row("BBB/USDT:USDT", 61.8),
                    _saved_row("DDD/USDT:USDT", 64.0),
                    _saved_row("EEE/USDT:USDT", 63.0),
                    _saved_row("FFF/USDT:USDT", 62.0),
                ],
                {"refreshed_count": 5, "dropped": [], "warnings": []},
            ),
            (
                [
                    _saved_row("DDD/USDT:USDT", 62.8, state="LC_NOI_BO"),
                    _saved_row("EEE/USDT:USDT", 62.4, state="LC_NOI_BO"),
                    _saved_row("FFF/USDT:USDT", 62.1, state="LC_NOI_BO"),
                ],
                {"refreshed_count": 3, "dropped": [], "warnings": []},
            ),
            (
                [
                    _saved_row("DDD/USDT:USDT", 62.8, state="LC_NOI_BO"),
                    _saved_row("EEE/USDT:USDT", 62.4, state="LC_NOI_BO"),
                    _saved_row("FFF/USDT:USDT", 62.1, state="LC_NOI_BO"),
                    _saved_row("GGG/USDT:USDT", 64.5, state="LC_NOI_BO"),
                    _saved_row("HHH/USDT:USDT", 63.0, state="LC_NOI_BO"),
                    _saved_row("III/USDT:USDT", 62.9, state="LC_NOI_BO"),
                ],
                {"refreshed_count": 6, "dropped": [], "warnings": []},
            ),
            (
                [
                    _saved_row("DDD/USDT:USDT", 62.8, state="LC_NOI_BO"),
                    _saved_row("EEE/USDT:USDT", 62.4, state="LC_NOI_BO"),
                    _saved_row("FFF/USDT:USDT", 62.1, state="LC_NOI_BO"),
                    _saved_row("GGG/USDT:USDT", 64.5, state="LC_NOI_BO"),
                    _saved_row("HHH/USDT:USDT", 63.0, state="LC_NOI_BO"),
                    _saved_row("III/USDT:USDT", 62.9, state="LC_NOI_BO"),
                ],
                {"refreshed_count": 6, "dropped": [], "warnings": []},
            ),
        ]

        two_hour = update_lc_internal_pipeline(
            config,
            [
                _candidate("DDD/USDT:USDT", 64.0),
                _candidate("EEE/USDT:USDT", 63.0),
                _candidate("FFF/USDT:USDT", 62.0),
            ],
            now=start + timedelta(hours=1),
        )
        self.assertEqual(
            [row["symbol"] for row in two_hour["two_hour_event"]["approved"]],
            ["DDD/USDT:USDT", "EEE/USDT:USDT", "FFF/USDT:USDT"],
        )

        update_lc_internal_pipeline(
            config,
            [
                _candidate("GGG/USDT:USDT", 64.5),
                _candidate("HHH/USDT:USDT", 63.0),
                _candidate("III/USDT:USDT", 62.9),
            ],
            now=start + timedelta(hours=2),
        )
        four_hour = update_lc_internal_pipeline(
            config,
            [
                _candidate("JJJ/USDT:USDT", 64.2),
                _candidate("KKK/USDT:USDT", 63.4),
                _candidate("LLL/USDT:USDT", 63.0),
            ],
            now=start + timedelta(hours=3),
        )
        self.assertEqual(
            [row["symbol"] for row in four_hour["four_hour_event"]["approved"]],
            ["GGG/USDT:USDT", "HHH/USDT:USDT", "III/USDT:USDT"],
        )

    @patch("crypto_trader.lc_pipeline._recheck_rows_with_latest_market_data")
    def test_two_hour_recheck_keeps_old_win_rate_as_reference(self, recheck_rows) -> None:
        config = self._config()
        start = datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc)
        update_lc_internal_pipeline(
            config,
            [_candidate("AAA/USDT:USDT", 90), _candidate("BBB/USDT:USDT", 89), _candidate("CCC/USDT:USDT", 88)],
            now=start,
        )
        recheck_rows.return_value = (
            [
                _saved_row("AAA/USDT:USDT", 55),
                _saved_row("BBB/USDT:USDT", 54),
                _saved_row("CCC/USDT:USDT", 53),
                _saved_row("DDD/USDT:USDT", 95),
                _saved_row("EEE/USDT:USDT", 94),
                _saved_row("FFF/USDT:USDT", 93),
            ],
            {"refreshed_count": 6, "dropped": [], "warnings": []},
        )

        update_lc_internal_pipeline(
            config,
            [_candidate("DDD/USDT:USDT", 60), _candidate("EEE/USDT:USDT", 59), _candidate("FFF/USDT:USDT", 58)],
            now=start + timedelta(hours=1),
        )

        raw_state = get_journal_state(config, "lc_internal_pipeline_state")
        state = json.loads(raw_state or "{}")
        first_window = state["hourly_windows"][0]["top"]
        self.assertEqual(first_window[0]["symbol"], "AAA/USDT:USDT")
        self.assertEqual(first_window[0]["win_probability_pct"], 55)
        self.assertEqual(first_window[0]["previous_scan_win_probability_pct"], 90)
        self.assertEqual(first_window[0]["current_win_probability_pct"], 55)
        self.assertEqual(first_window[0]["peak_win_probability_pct"], 90)
        self.assertEqual(first_window[0]["win_rate_trend"], "down")
        self.assertEqual(first_window[0]["recheck_state"], "weaker")
        self.assertIn("last_recheck_at", first_window[0])

    @patch("crypto_trader.lc_pipeline._recheck_rows_with_latest_market_data")
    def test_four_hour_uses_rechecked_win_rate_before_selecting_top_three(self, recheck_rows) -> None:
        config = self._config()
        config["ai"]["internal"]["lc_pipeline_recheck_interval_minutes"] = 999
        start = datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc)
        update_lc_internal_pipeline(
            config,
            [_candidate("AAA/USDT:USDT", 90), _candidate("BBB/USDT:USDT", 89), _candidate("CCC/USDT:USDT", 88)],
            now=start,
        )
        recheck_rows.side_effect = [
            (
                [
                    _saved_row("AAA/USDT:USDT", 90),
                    _saved_row("BBB/USDT:USDT", 89),
                    _saved_row("CCC/USDT:USDT", 88),
                    _saved_row("DDD/USDT:USDT", 87),
                    _saved_row("EEE/USDT:USDT", 86),
                    _saved_row("FFF/USDT:USDT", 85),
                ],
                {"refreshed_count": 6, "dropped": [], "warnings": []},
            ),
            (
                [
                    _saved_row("AAA/USDT:USDT", 99, state="LC_NOI_BO"),
                    _saved_row("BBB/USDT:USDT", 98, state="LC_NOI_BO"),
                    _saved_row("CCC/USDT:USDT", 97, state="LC_NOI_BO"),
                ],
                {"refreshed_count": 3, "dropped": [], "warnings": []},
            ),
            (
                [
                    _saved_row("GGG/USDT:USDT", 70),
                    _saved_row("HHH/USDT:USDT", 69),
                    _saved_row("III/USDT:USDT", 68),
                    _saved_row("JJJ/USDT:USDT", 95),
                    _saved_row("KKK/USDT:USDT", 94),
                    _saved_row("LLL/USDT:USDT", 93),
                ],
                {"refreshed_count": 6, "dropped": [], "warnings": []},
            ),
            (
                [
                    _saved_row("AAA/USDT:USDT", 99, state="LC_NOI_BO"),
                    _saved_row("BBB/USDT:USDT", 98, state="LC_NOI_BO"),
                    _saved_row("CCC/USDT:USDT", 97, state="LC_NOI_BO"),
                    _saved_row("JJJ/USDT:USDT", 60, state="LC_NOI_BO"),
                    _saved_row("KKK/USDT:USDT", 59, state="LC_NOI_BO"),
                    _saved_row("LLL/USDT:USDT", 58, state="LC_NOI_BO"),
                ],
                {"refreshed_count": 6, "dropped": [], "warnings": []},
            ),
        ]
        update_lc_internal_pipeline(
            config,
            [_candidate("DDD/USDT:USDT", 87), _candidate("EEE/USDT:USDT", 86), _candidate("FFF/USDT:USDT", 85)],
            now=start + timedelta(hours=1),
        )
        update_lc_internal_pipeline(
            config,
            [_candidate("GGG/USDT:USDT", 84), _candidate("HHH/USDT:USDT", 83), _candidate("III/USDT:USDT", 82)],
            now=start + timedelta(hours=2),
        )

        result = update_lc_internal_pipeline(
            config,
            [_candidate("JJJ/USDT:USDT", 81), _candidate("KKK/USDT:USDT", 80), _candidate("LLL/USDT:USDT", 79)],
            now=start + timedelta(hours=3),
        )

        self.assertTrue(result["created_four_hour"])
        self.assertEqual(
            [row["symbol"] for row in result["four_hour_event"]["approved"]],
            ["AAA/USDT:USDT", "BBB/USDT:USDT", "CCC/USDT:USDT"],
        )
        self.assertEqual(result["four_hour_recheck"]["refreshed_count"], 6)

    @patch("crypto_trader.lc_pipeline._recheck_rows_with_latest_market_data")
    def test_two_hour_recheck_syncs_latest_hourly_state(self, recheck_rows) -> None:
        config = self._config()
        start = datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc)
        update_lc_internal_pipeline(
            config,
            [_candidate("AAA/USDT:USDT", 61), _candidate("BBB/USDT:USDT", 60), _candidate("CCC/USDT:USDT", 59)],
            now=start,
        )
        recheck_rows.return_value = (
            [
                _saved_row("AAA/USDT:USDT", 71),
                _saved_row("BBB/USDT:USDT", 70),
                _saved_row("CCC/USDT:USDT", 69),
                _saved_row("DDD/USDT:USDT", 68),
                _saved_row("EEE/USDT:USDT", 67),
                _saved_row("FFF/USDT:USDT", 66),
            ],
            {"input_count": 6, "refreshed_count": 6, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 6},
        )

        update_lc_internal_pipeline(
            config,
            [_candidate("DDD/USDT:USDT", 64), _candidate("EEE/USDT:USDT", 63), _candidate("FFF/USDT:USDT", 62)],
            now=start + timedelta(hours=1),
        )

        raw_state = get_journal_state(config, "lc_internal_pipeline_state")
        state = json.loads(raw_state or "{}")
        self.assertEqual(state["hourly_windows"][0]["top"][0]["win_probability_pct"], 71)
        self.assertEqual(state["hourly_windows"][1]["top"][0]["win_probability_pct"], 68)
        self.assertEqual(state["one_hour_history"][0]["approved"][0]["win_probability_pct"], 71)
        self.assertEqual(state["one_hour_history"][1]["approved"][0]["win_probability_pct"], 68)

    @patch("crypto_trader.lc_pipeline._recheck_rows_with_latest_market_data")
    def test_two_hour_does_not_fallback_to_stale_rows_when_recheck_finds_no_valid_setup(self, recheck_rows) -> None:
        config = self._config()
        start = datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc)
        update_lc_internal_pipeline(
            config,
            [_candidate("AAA/USDT:USDT", 61), _candidate("BBB/USDT:USDT", 60), _candidate("CCC/USDT:USDT", 59)],
            now=start,
        )
        recheck_rows.return_value = (
            [],
            {
                "input_count": 6,
                "refreshed_count": 0,
                "dropped": [
                    {"symbol": "AAA/USDT:USDT", "old_side": "long"},
                    {"symbol": "BBB/USDT:USDT", "old_side": "long"},
                    {"symbol": "CCC/USDT:USDT", "old_side": "long"},
                    {"symbol": "DDD/USDT:USDT", "old_side": "long"},
                    {"symbol": "EEE/USDT:USDT", "old_side": "long"},
                    {"symbol": "FFF/USDT:USDT", "old_side": "long"},
                ],
                "warnings": [],
                "sync_complete": True,
                "synchronized_count": 6,
            },
        )

        result = update_lc_internal_pipeline(
            config,
            [_candidate("DDD/USDT:USDT", 64), _candidate("EEE/USDT:USDT", 63), _candidate("FFF/USDT:USDT", 62)],
            now=start + timedelta(hours=1),
        )

        raw_state = get_journal_state(config, "lc_internal_pipeline_state")
        state = json.loads(raw_state or "{}")
        self.assertTrue(result["created_two_hour"])
        self.assertEqual(result["two_hour_event"]["approved"], [])
        self.assertEqual(state["internal_lc"], [])
        self.assertEqual(state["hourly_windows"][0]["top"], [])
        self.assertEqual(state["hourly_windows"][1]["top"], [])

    @patch("crypto_trader.lc_pipeline._recheck_rows_with_latest_market_data")
    def test_pipeline_stores_hourly_two_hour_four_hour_history_with_lineage(self, recheck_rows) -> None:
        config = self._config()
        config["ai"]["internal"]["lc_pipeline_recheck_interval_minutes"] = 999
        start = datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc)
        recheck_rows.side_effect = [
            (
                [
                    _saved_row("AAA/USDT:USDT", 63),
                    _saved_row("BBB/USDT:USDT", 62),
                    _saved_row("CCC/USDT:USDT", 61),
                    _saved_row("DDD/USDT:USDT", 66),
                    _saved_row("EEE/USDT:USDT", 65),
                    _saved_row("FFF/USDT:USDT", 64),
                ],
                {"input_count": 6, "refreshed_count": 6, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 6},
            ),
            (
                [
                    {**_saved_row("DDD/USDT:USDT", 66, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1},
                    {**_saved_row("EEE/USDT:USDT", 65, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1},
                    {**_saved_row("FFF/USDT:USDT", 64, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1},
                ],
                {"input_count": 3, "refreshed_count": 3, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 3},
            ),
            (
                [
                    _saved_row("GGG/USDT:USDT", 69),
                    _saved_row("HHH/USDT:USDT", 68),
                    _saved_row("III/USDT:USDT", 67),
                    _saved_row("JJJ/USDT:USDT", 72),
                    _saved_row("KKK/USDT:USDT", 71),
                    _saved_row("LLL/USDT:USDT", 70),
                ],
                {"input_count": 6, "refreshed_count": 6, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 6},
            ),
            (
                [
                    {**_saved_row("DDD/USDT:USDT", 66, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1},
                    {**_saved_row("EEE/USDT:USDT", 65, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1},
                    {**_saved_row("FFF/USDT:USDT", 64, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1},
                    {**_saved_row("JJJ/USDT:USDT", 72, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 2},
                    {**_saved_row("KKK/USDT:USDT", 71, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 2},
                    {**_saved_row("LLL/USDT:USDT", 70, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 2},
                ],
                {"input_count": 6, "refreshed_count": 6, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 6},
            ),
        ]

        update_lc_internal_pipeline(
            config,
            [_candidate("AAA/USDT:USDT", 63), _candidate("BBB/USDT:USDT", 62), _candidate("CCC/USDT:USDT", 61)],
            now=start,
        )
        update_lc_internal_pipeline(
            config,
            [_candidate("DDD/USDT:USDT", 66), _candidate("EEE/USDT:USDT", 65), _candidate("FFF/USDT:USDT", 64)],
            now=start + timedelta(hours=1),
        )
        update_lc_internal_pipeline(
            config,
            [_candidate("GGG/USDT:USDT", 69), _candidate("HHH/USDT:USDT", 68), _candidate("III/USDT:USDT", 67)],
            now=start + timedelta(hours=2),
        )
        result = update_lc_internal_pipeline(
            config,
            [_candidate("JJJ/USDT:USDT", 72), _candidate("KKK/USDT:USDT", 71), _candidate("LLL/USDT:USDT", 70)],
            now=start + timedelta(hours=3),
        )

        self.assertTrue(result["created_two_hour"])
        self.assertTrue(result["created_four_hour"])
        raw_state = get_journal_state(config, "lc_internal_pipeline_state")
        state = json.loads(raw_state or "{}")
        self.assertEqual(state["daily_one_hour_counter"], 4)
        self.assertEqual(state["daily_two_hour_counter"], 2)
        self.assertEqual(state["four_hour_counter"], 2)
        self.assertEqual([event["daily_index"] for event in state["one_hour_history"]], [1, 2, 3, 4])
        self.assertEqual([event["daily_index"] for event in state["two_hour_history"]], [1, 2])
        self.assertEqual([event["index"] for event in state["four_hour_history"]], [1, 2])
        approved_four_hour = state["four_hour_history"][-1]["approved"]
        self.assertEqual([row["symbol"] for row in approved_four_hour], ["JJJ/USDT:USDT", "KKK/USDT:USDT", "LLL/USDT:USDT"])
        self.assertEqual(approved_four_hour[0]["source_slot"], "4h")
        self.assertEqual(approved_four_hour[0]["source_index"], 2)
        self.assertEqual(approved_four_hour[0]["origin_source_slot"], "2h")
        self.assertEqual(approved_four_hour[0]["origin_source_index"], 2)
        undecided_by_symbol = {row["symbol"]: row for row in state["undecided"]}
        self.assertEqual(undecided_by_symbol["DDD/USDT:USDT"]["source_slot"], "4h")
        self.assertEqual(undecided_by_symbol["EEE/USDT:USDT"]["source_slot"], "4h")
        self.assertEqual(undecided_by_symbol["FFF/USDT:USDT"]["source_slot"], "4h")
        self.assertEqual(undecided_by_symbol["DDD/USDT:USDT"]["source_index"], 2)

    def test_history_cleanup_keeps_active_internal_state(self) -> None:
        config = self._config()
        stale_time = (datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc) - timedelta(days=8)).isoformat()
        recent_time = datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc).isoformat()
        set_state = {
            "state_version": 3,
            "day_key": "2026-07-06",
            "one_hour_history": [
                {"frame": "1h", "created_at": stale_time, "daily_index": 1, "approved": []},
                {"frame": "1h", "created_at": recent_time, "daily_index": 2, "approved": []},
            ],
            "two_hour_history": [
                {"frame": "2h", "created_at": stale_time, "daily_index": 1, "approved": []},
            ],
            "four_hour_history": [
                {"frame": "4h", "created_at": stale_time, "index": 1, "approved": []},
            ],
            "internal_lc": [
                {
                    "symbol": "ETH/USDT:USDT",
                    "side": "long",
                    "state": "LC_NOI_BO",
                    "first_seen_at": stale_time,
                    "last_seen_at": stale_time,
                    "win_probability_pct": 64.11,
                }
            ],
            "undecided": [
                {
                    "symbol": "LIT/USDT:USDT",
                    "side": "long",
                    "state": "CHUA_DUYET",
                    "first_seen_at": stale_time,
                    "last_seen_at": stale_time,
                    "win_probability_pct": 62.34,
                }
            ],
        }
        set_journal_state(config, "lc_internal_pipeline_state", json.dumps(set_state, ensure_ascii=False))

        payload = lc_pipeline_dashboard_payload(config)

        self.assertEqual(payload["counts"]["one_hour_history"], 1)
        self.assertEqual(payload["counts"]["two_hour_history"], 0)
        self.assertEqual(payload["counts"]["four_hour_history"], 0)
        self.assertEqual(payload["internal_lc"][0]["symbol"], "ETH/USDT:USDT")
        self.assertEqual(payload["undecided"][0]["symbol"], "LIT/USDT:USDT")

    @patch("crypto_trader.lc_pipeline._recheck_rows_with_latest_market_data")
    def test_two_hour_rejected_keeps_opposite_side_duplicate_setup(self, recheck_rows) -> None:
        config = self._config()
        start = datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc)
        update_lc_internal_pipeline(
            config,
            [
                _candidate("NEAR/USDT:USDT", 63.5, side="long"),
                _candidate("XLM/USDT:USDT", 61.08, side="long"),
                _candidate("OP/USDT:USDT", 60.56, side="long"),
            ],
            now=start,
        )
        recheck_rows.return_value = (
            [
                _saved_row("NEAR/USDT:USDT", 63.5, side="long"),
                _saved_row("XLM/USDT:USDT", 61.08, side="long"),
                _saved_row("OP/USDT:USDT", 60.56, side="long"),
                _saved_row("HOME/USDT:USDT", 60, side="short"),
                _saved_row("NOT/USDT:USDT", 59.11, side="short"),
                _saved_row("XLM/USDT:USDT", 56.2, side="short"),
            ],
            {"refreshed_count": 6, "dropped": [], "warnings": []},
        )
        result = update_lc_internal_pipeline(
            config,
            [
                _candidate("HOME/USDT:USDT", 60, side="short"),
                _candidate("NOT/USDT:USDT", 59.11, side="short"),
                _candidate("XLM/USDT:USDT", 56.2, side="short"),
            ],
            now=start + timedelta(hours=1),
        )

        rejected = [(row["symbol"], row["side"]) for row in result["two_hour_event"]["rejected"]]
        self.assertEqual(
            rejected,
            [
                ("HOME/USDT:USDT", "short"),
                ("NOT/USDT:USDT", "short"),
                ("XLM/USDT:USDT", "short"),
            ],
        )
        self.assertEqual(len(result["undecided"]), 3)

    @patch("crypto_trader.lc_pipeline._recheck_rows_with_latest_market_data")
    def test_two_hour_sends_soft_valid_below_threshold_rows_to_undecided_only(self, recheck_rows) -> None:
        config = self._config()
        config["ai"]["internal"]["lc_pipeline_two_hour_min_win_probability_pct"] = 62
        start = datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc)
        update_lc_internal_pipeline(
            config,
            [
                _candidate("AAA/USDT:USDT", 60, confidence=85, volume=2),
                _candidate("BBB/USDT:USDT", 59, confidence=82, volume=1),
            ],
            now=start,
        )
        recheck_rows.return_value = (
            [
                _saved_row("AAA/USDT:USDT", 60, confidence=85, volume=2),
                _saved_row("CCC/USDT:USDT", 58, confidence=81, volume=1),
                _saved_row("DDD/USDT:USDT", 55, confidence=80, volume=0.5),
            ],
            {
                "refreshed_count": 3,
                "dropped": [
                    {
                        "symbol": "BBB/USDT:USDT",
                        "old_side": "long",
                        "reason": "setup goc LONG khong con hop le trong du lieu moi nhat",
                    }
                ],
                "warnings": [],
            },
        )

        result = update_lc_internal_pipeline(
            config,
            [
                _candidate("CCC/USDT:USDT", 58, confidence=81, volume=1),
                _candidate("DDD/USDT:USDT", 55, confidence=80, volume=0.5),
            ],
            now=start + timedelta(hours=1),
        )

        self.assertEqual([row["symbol"] for row in result["two_hour_event"]["approved"]], ["AAA/USDT:USDT", "CCC/USDT:USDT"])
        rejected = [(row["symbol"], row["side"], row["source_slot"]) for row in result["two_hour_event"]["rejected"]]
        self.assertEqual(
            rejected,
            [
                ("DDD/USDT:USDT", "long", "2h"),
            ],
        )
        self.assertEqual([row["symbol"] for row in result["undecided"]], ["DDD/USDT:USDT"])
        self.assertNotIn("BBB/USDT:USDT", [row["symbol"] for row in result["undecided"]])

    @patch("crypto_trader.lc_pipeline.enrich_quantities")
    @patch("crypto_trader.lc_pipeline.apply_position_sizing")
    @patch("crypto_trader.lc_pipeline.build_candidates")
    @patch("crypto_trader.lc_pipeline.market_guard_symbol_layers")
    @patch("crypto_trader.lc_pipeline.fetch_market_snapshots")
    @patch("crypto_trader.lc_pipeline.collect_news")
    def test_recheck_keeps_original_side_and_does_not_flip_setup(
        self,
        collect_news,
        fetch_market_snapshots,
        market_guard_symbol_layers,
        build_candidates,
        apply_position_sizing,
        enrich_quantities,
    ) -> None:
        config = self._config()
        collect_news.return_value = {}
        fetch_market_snapshots.return_value = ([], [])
        market_guard_symbol_layers.return_value = {}
        apply_position_sizing.return_value = None
        enrich_quantities.return_value = []
        build_candidates.return_value = [_candidate("LIT/USDT:USDT", 42.94, side="short")]

        refreshed, meta = _recheck_rows_with_latest_market_data(
            config,
            [{**_saved_row("LIT/USDT:USDT", 65.0, side="long"), "source_slot": "1h", "source_index": 1}],
            now=datetime(2026, 7, 6, 1, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(refreshed, [])
        self.assertEqual(len(meta["dropped"]), 1)
        self.assertEqual(meta["dropped"][0]["symbol"], "LIT/USDT:USDT")
        self.assertEqual(meta["dropped"][0]["old_side"], "long")
        self.assertNotIn("current_side", meta["dropped"][0])
        self.assertNotIn("current_win_probability_pct", meta["dropped"][0])
        self.assertIn("khong doi chieu", meta["dropped"][0]["reason"])

    @patch("crypto_trader.lc_pipeline._recheck_rows_with_latest_market_data")
    def test_undecided_recheck_updates_all_rows_without_deleting_when_below_six(self, recheck_rows) -> None:
        config = self._config()
        state = {
            "state_version": 3,
            "day_key": "2026-07-06",
            "undecided": [
                {**_saved_row("AAA/USDT:USDT", 61, state="CHUA_DUYET"), "source_slot": "2h", "source_index": 1},
                {**_saved_row("BBB/USDT:USDT", 60, state="CHUA_DUYET"), "source_slot": "2h", "source_index": 1},
                {**_saved_row("CCC/USDT:USDT", 59, state="CHUA_DUYET"), "source_slot": "4h", "source_index": 1},
            ],
        }
        set_journal_state(config, "lc_internal_pipeline_state", json.dumps(state, ensure_ascii=False))
        recheck_rows.return_value = (
            [
                _saved_row("AAA/USDT:USDT", 55, state="CHUA_DUYET"),
                _saved_row("BBB/USDT:USDT", 49, state="CHUA_DUYET"),
            ],
            {
                "refreshed_count": 2,
                "dropped": [
                    {
                        "symbol": "CCC/USDT:USDT",
                        "old_side": "long",
                        "reason": "khong con setup hop le trong du lieu moi nhat",
                    }
                ],
                "warnings": [],
            },
        )

        result = update_lc_internal_pipeline(config, [], now=datetime(2026, 7, 6, 2, 0, tzinfo=timezone.utc))

        self.assertEqual(result["undecided_recheck"]["input_count"], 3)
        raw_state = get_journal_state(config, "lc_internal_pipeline_state")
        saved_state = json.loads(raw_state or "{}")
        undecided_by_symbol = {row["symbol"]: row for row in saved_state["undecided"]}
        self.assertEqual(len(undecided_by_symbol), 3)
        self.assertEqual(undecided_by_symbol["AAA/USDT:USDT"]["win_probability_pct"], 55)
        self.assertEqual(undecided_by_symbol["AAA/USDT:USDT"]["undecided_status"], "soft_valid")
        self.assertEqual(undecided_by_symbol["AAA/USDT:USDT"]["win_rate_trend"], "down")
        self.assertEqual(undecided_by_symbol["AAA/USDT:USDT"]["recheck_state"], "weaker")
        self.assertEqual(undecided_by_symbol["BBB/USDT:USDT"]["win_probability_pct"], 49)
        self.assertEqual(undecided_by_symbol["BBB/USDT:USDT"]["undecided_status"], "soft_invalid")
        self.assertEqual(undecided_by_symbol["BBB/USDT:USDT"]["recheck_state"], "weaker")
        self.assertEqual(undecided_by_symbol["CCC/USDT:USDT"]["win_probability_pct"], 0.0)
        self.assertEqual(undecided_by_symbol["CCC/USDT:USDT"]["undecided_status"], "missing_setup")
        self.assertEqual(undecided_by_symbol["CCC/USDT:USDT"]["recheck_state"], "invalid")
        self.assertEqual(undecided_by_symbol["CCC/USDT:USDT"]["peak_win_probability_pct"], 59)

    @patch("crypto_trader.lc_pipeline._recheck_rows_with_latest_market_data")
    def test_undecided_recheck_keeps_top_six_by_latest_win_rate(self, recheck_rows) -> None:
        config = self._config()
        state = {
            "state_version": 3,
            "day_key": "2026-07-06",
            "undecided": [
                {**_saved_row("AAA/USDT:USDT", 61, state="CHUA_DUYET"), "source_slot": "2h", "source_index": 1},
                {**_saved_row("BBB/USDT:USDT", 60, state="CHUA_DUYET"), "source_slot": "2h", "source_index": 1},
                {**_saved_row("CCC/USDT:USDT", 59, state="CHUA_DUYET"), "source_slot": "2h", "source_index": 1},
                {**_saved_row("DDD/USDT:USDT", 58, state="CHUA_DUYET"), "source_slot": "2h", "source_index": 1},
                {**_saved_row("EEE/USDT:USDT", 57, state="CHUA_DUYET"), "source_slot": "4h", "source_index": 1},
                {**_saved_row("FFF/USDT:USDT", 56, state="CHUA_DUYET"), "source_slot": "4h", "source_index": 1},
                {**_saved_row("GGG/USDT:USDT", 55, state="CHUA_DUYET"), "source_slot": "4h", "source_index": 1},
            ],
        }
        set_journal_state(config, "lc_internal_pipeline_state", json.dumps(state, ensure_ascii=False))
        recheck_rows.return_value = (
            [
                _saved_row("AAA/USDT:USDT", 70, state="CHUA_DUYET"),
                _saved_row("BBB/USDT:USDT", 69, state="CHUA_DUYET"),
                _saved_row("CCC/USDT:USDT", 68, state="CHUA_DUYET"),
                _saved_row("DDD/USDT:USDT", 67, state="CHUA_DUYET"),
                _saved_row("EEE/USDT:USDT", 66, state="CHUA_DUYET"),
                _saved_row("FFF/USDT:USDT", 65, state="CHUA_DUYET"),
                _saved_row("GGG/USDT:USDT", 40, state="CHUA_DUYET"),
            ],
            {"refreshed_count": 7, "dropped": [], "warnings": []},
        )

        result = update_lc_internal_pipeline(config, [], now=datetime(2026, 7, 6, 2, 0, tzinfo=timezone.utc))

        self.assertTrue(result["undecided_recheck"]["trimmed"])
        raw_state = get_journal_state(config, "lc_internal_pipeline_state")
        saved_state = json.loads(raw_state or "{}")
        kept_symbols = [row["symbol"] for row in saved_state["undecided"]]
        self.assertEqual(len(kept_symbols), 6)
        self.assertNotIn("GGG/USDT:USDT", kept_symbols)

    @patch("crypto_trader.lc_pipeline._recheck_rows_with_latest_market_data")
    def test_undecided_recheck_keeps_all_rows_when_exactly_six(self, recheck_rows) -> None:
        config = self._config()
        state = {
            "state_version": 3,
            "day_key": "2026-07-06",
            "undecided": [
                {**_saved_row("AAA/USDT:USDT", 61, state="CHUA_DUYET"), "source_slot": "2h", "source_index": 1},
                {**_saved_row("BBB/USDT:USDT", 60, state="CHUA_DUYET"), "source_slot": "2h", "source_index": 1},
                {**_saved_row("CCC/USDT:USDT", 59, state="CHUA_DUYET"), "source_slot": "2h", "source_index": 1},
                {**_saved_row("DDD/USDT:USDT", 58, state="CHUA_DUYET"), "source_slot": "4h", "source_index": 1},
                {**_saved_row("EEE/USDT:USDT", 57, state="CHUA_DUYET"), "source_slot": "4h", "source_index": 1},
                {**_saved_row("FFF/USDT:USDT", 56, state="CHUA_DUYET"), "source_slot": "4h", "source_index": 1},
            ],
        }
        set_journal_state(config, "lc_internal_pipeline_state", json.dumps(state, ensure_ascii=False))
        recheck_rows.return_value = (
            [
                _saved_row("AAA/USDT:USDT", 70, state="CHUA_DUYET"),
                _saved_row("BBB/USDT:USDT", 69, state="CHUA_DUYET"),
                _saved_row("CCC/USDT:USDT", 68, state="CHUA_DUYET"),
                _saved_row("DDD/USDT:USDT", 67, state="CHUA_DUYET"),
                _saved_row("EEE/USDT:USDT", 66, state="CHUA_DUYET"),
                _saved_row("FFF/USDT:USDT", 65, state="CHUA_DUYET"),
            ],
            {"refreshed_count": 6, "dropped": [], "warnings": []},
        )

        result = update_lc_internal_pipeline(config, [], now=datetime(2026, 7, 6, 2, 0, tzinfo=timezone.utc))

        self.assertFalse(result["undecided_recheck"]["trimmed"])
        raw_state = get_journal_state(config, "lc_internal_pipeline_state")
        saved_state = json.loads(raw_state or "{}")
        self.assertEqual(len(saved_state["undecided"]), 6)

    def test_mini_pool_does_not_fallback_without_internal_lc(self) -> None:
        config = self._config()
        current = [
            _candidate("AAA/USDT:USDT", 61),
            _candidate("BBB/USDT:USDT", 60),
            _candidate("CCC/USDT:USDT", 59),
        ]

        pool = lc_pipeline_mini_pool(config, current, limit=3)

        self.assertEqual(pool, [])

    @patch("crypto_trader.lc_pipeline._recheck_rows_with_latest_market_data")
    @patch("crypto_trader.notifier.send_telegram_message")
    def test_surviving_undecided_pair_promotes_to_internal_lc_and_notifies(self, send_message, recheck_rows) -> None:
        config = self._config()
        config["ai"]["internal"]["lc_pipeline_notify_two_hour_summary"] = False
        config["ai"]["internal"]["lc_pipeline_promote_to_pending"] = True
        config["ai"]["internal"]["lc_pipeline_recheck_interval_minutes"] = 60
        start = datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc)
        update_lc_internal_pipeline(
            config,
            [_candidate("AAA/USDT:USDT", 64), _candidate("BBB/USDT:USDT", 63), _candidate("CCC/USDT:USDT", 62)],
            now=start,
        )
        recheck_rows.side_effect = [
            (
                [
                    _saved_row("AAA/USDT:USDT", 64),
                    _saved_row("BBB/USDT:USDT", 63),
                    _saved_row("CCC/USDT:USDT", 62),
                    _saved_row("DDD/USDT:USDT", 67),
                    _saved_row("EEE/USDT:USDT", 66),
                    _saved_row("FFF/USDT:USDT", 65),
                ],
                {"input_count": 6, "refreshed_count": 6, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 6},
            ),
            (
                [
                    _saved_row("AAA/USDT:USDT", 64, state="CHUA_DUYET"),
                    _saved_row("BBB/USDT:USDT", 63, state="CHUA_DUYET"),
                    _saved_row("CCC/USDT:USDT", 62, state="CHUA_DUYET"),
                ],
                {"input_count": 3, "refreshed_count": 3, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 3},
            ),
            (
                [
                    _saved_row("AAA/USDT:USDT", 70, state="CHUA_DUYET"),
                    _saved_row("BBB/USDT:USDT", 60, state="CHUA_DUYET"),
                    _saved_row("CCC/USDT:USDT", 59, state="CHUA_DUYET"),
                ],
                {"input_count": 3, "refreshed_count": 3, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 3},
            ),
            (
                [
                    _saved_row("AAA/USDT:USDT", 70, state="CHUA_DUYET"),
                    _saved_row("BBB/USDT:USDT", 60, state="CHUA_DUYET"),
                    _saved_row("CCC/USDT:USDT", 59, state="CHUA_DUYET"),
                ],
                {"input_count": 3, "refreshed_count": 3, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 3},
            ),
        ]
        update_lc_internal_pipeline(
            config,
            [_candidate("DDD/USDT:USDT", 67), _candidate("EEE/USDT:USDT", 66), _candidate("FFF/USDT:USDT", 65)],
            now=start + timedelta(hours=1),
        )
        config["ai"]["internal"]["lc_pipeline_one_hour_min_win_probability_pct"] = 80

        result = update_lc_internal_pipeline(
            config,
            [_candidate("AAA/USDT:USDT", 70), _candidate("BBB/USDT:USDT", 60), _candidate("CCC/USDT:USDT", 59)],
            now=start + timedelta(hours=7),
        )

        promoted_symbols = [row["symbol"] for row in result["promoted"]]
        self.assertIn("AAA/USDT:USDT", promoted_symbols)
        raw_state = get_journal_state(config, "lc_internal_pipeline_state")
        self.assertIsNotNone(raw_state)
        state = json.loads(raw_state or "{}")
        undecided_symbols = [row["symbol"] for row in state.get("undecided", [])]
        self.assertNotIn("AAA/USDT:USDT", undecided_symbols)
        messages = "\n".join(call.args[1] for call in send_message.call_args_list)
        self.assertIn("hồi sinh thành LC nội bộ", messages)
        self.assertIn("Chưa Duyệt còn", messages)

    @patch("crypto_trader.notifier.send_telegram_message")
    def test_mini_pool_summary_notes_source_and_hs(self, send_message) -> None:
        config = self._config()
        config["ai"]["internal"]["lc_pipeline_notify_mini_pool_summary"] = True
        rows = [
            {"symbol": "AAA/USDT:USDT", "side": "long", "source_slot": "2h", "source_index": 3},
            {
                "symbol": "BBB/USDT:USDT",
                "side": "short",
                "source_slot": "HS",
                "revived_at": "2026-07-06T07:00:00+00:00",
                "revived_label": "06/07/26 14:00:00",
            },
        ]

        notify_mini_pool_summary(
            config,
            rows,
            scan={
                "mini_index": 1,
                "selected_symbols": ["AAA/USDT:USDT", "BBB/USDT:USDT"],
                "decision_reason_vi": "Mini đã chọn 2 cặp tốt nhất trong nhóm LC 4h.",
            },
            slot_id="slot-1",
        )

        message = send_message.call_args.args[1]
        self.assertIn("Mini #1", message)
        self.assertIn("Mini chọn: 2/3 cặp", message)
        self.assertIn("AAA/USDT:USDT | LONG | 2h #3", message)
        self.assertIn("BBB/USDT:USDT | SHORT | HS 06/07/26 14:00:00", message)
        self.assertIn("Lý do: Mini đã chọn 2 cặp tốt nhất trong nhóm LC 4h.", message)

    @patch("crypto_trader.notifier.send_telegram_message")
    def test_mini_pool_summary_includes_four_hour_lineage_when_available(self, send_message) -> None:
        config = self._config()
        config["ai"]["internal"]["lc_pipeline_notify_mini_pool_summary"] = True
        set_journal_state(
            config,
            "lc_internal_pipeline_state",
            json.dumps(
                {
                    "state_version": 3,
                    "day_key": "2026-07-06",
                    "four_hour_history": [
                        {
                            "frame": "4h",
                            "slot": "2026-07-06T10:00:00+07:00",
                            "created_at": "2026-07-06T03:05:00+00:00",
                            "index": 1,
                            "daily_index": 1,
                            "approved": [],
                            "rejected": [],
                            "source_windows": [
                                {"frame": "2h", "index": 1, "time": "08:05"},
                                {"frame": "2h", "index": 2, "time": "10:05"},
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        )

        notify_mini_pool_summary(
            config,
            [{"symbol": "AAA/USDT:USDT", "side": "long", "source_slot": "4h", "source_index": 1}],
            scan={
                "mini_index": 2,
                "selected_symbols": ["AAA/USDT:USDT"],
                "decision_reason_vi": "Mini chọn lại cặp mạnh nhất từ nhóm LC 4h.",
            },
            slot_id="slot-1",
            now=datetime(2026, 7, 6, 3, 10, tzinfo=timezone.utc),
        )

        message = send_message.call_args.args[1]
        self.assertIn("Mini #2", message)
        self.assertIn("Khung 🔴 4h: #1 (10:05)", message)
        self.assertIn("Gộp từ: 🟡 2h #1 (08:05), 🟡 2h #2 (10:05)", message)
        self.assertIn("Lý do: Mini chọn lại cặp mạnh nhất từ nhóm LC 4h.", message)

    @patch("crypto_trader.notifier.send_telegram_message")
    def test_mini_pool_summary_skips_sample_symbols_when_filter_enabled(self, send_message) -> None:
        config = self._config()
        config["ai"]["internal"]["lc_pipeline_drop_sample_symbols"] = True
        config["ai"]["internal"]["lc_pipeline_notify_mini_pool_summary"] = True

        notify_mini_pool_summary(
            config,
            [{"symbol": "AAA/USDT:USDT", "side": "long", "source_slot": "4h", "source_index": 1}],
            scan={
                "mini_index": 1,
                "selected_symbols": ["AAA/USDT:USDT"],
                "decision_reason_vi": "sample only",
            },
            now=datetime(2026, 7, 6, 1, 0, tzinfo=timezone.utc),
        )

        send_message.assert_not_called()

    @patch("crypto_trader.lc_pipeline._recheck_rows_with_latest_market_data")
    def test_four_hour_summary_runs_with_single_two_hour_pool_in_demo_mode(self, recheck_rows) -> None:
        config = self._config()
        first_start = datetime(2026, 7, 7, 0, 5, tzinfo=timezone.utc)
        first_candidates = [
            _candidate("AAA/USDT:USDT", 58, volume=3),
            _candidate("BBB/USDT:USDT", 57, volume=2),
        ]
        recheck_rows.side_effect = [
            (
                [
                    {**_saved_row("AAA/USDT:USDT", 58, volume=3), "source_slot": "1h", "source_index": 1, "source_time": first_start.isoformat()},
                    {**_saved_row("BBB/USDT:USDT", 57, volume=2), "source_slot": "1h", "source_index": 1, "source_time": first_start.isoformat()},
                ],
                {"input_count": 2, "refreshed_count": 2, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 2},
            ),
            (
                [
                    {
                        **_saved_row("AAA/USDT:USDT", 58, volume=3, state="LC_NOI_BO"),
                        "source_slot": "2h",
                        "source_index": 1,
                        "source_time": (first_start + timedelta(hours=1)).isoformat(),
                    },
                    {
                        **_saved_row("BBB/USDT:USDT", 57, volume=2, state="LC_NOI_BO"),
                        "source_slot": "2h",
                        "source_index": 1,
                        "source_time": (first_start + timedelta(hours=1)).isoformat(),
                    },
                ],
                {"input_count": 2, "refreshed_count": 2, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 2},
            ),
        ]

        update_lc_internal_pipeline(config, first_candidates, now=first_start)
        result = update_lc_internal_pipeline(config, [], now=first_start + timedelta(hours=1))

        self.assertTrue(result["created_two_hour"])
        self.assertTrue(result["created_four_hour"])
        self.assertEqual([row["symbol"] for row in result["four_hour_event"]["approved"]], ["AAA/USDT:USDT", "BBB/USDT:USDT"])

    @patch("crypto_trader.lc_pipeline._recheck_rows_with_latest_market_data")
    @patch("crypto_trader.notifier.send_telegram_message")
    def test_internal_notifications_view_shows_full_messages_in_timeline_without_chat_push(self, send_message, recheck_rows) -> None:
        config = self._config()
        config["ai"]["internal"]["lc_pipeline_notify_two_hour_summary"] = False
        config["ai"]["internal"]["lc_pipeline_notify_mini_pool_summary"] = False
        config["ai"]["internal"]["lc_pipeline_recheck_interval_minutes"] = 999
        start = datetime(2026, 7, 7, 0, 5, tzinfo=timezone.utc)
        update_lc_internal_pipeline(
            config,
            [_candidate("AAA/USDT:USDT", 64), _candidate("BBB/USDT:USDT", 63), _candidate("CCC/USDT:USDT", 62)],
            now=start,
        )
        recheck_rows.side_effect = [
            (
                [
                    {**_saved_row("AAA/USDT:USDT", 64), "source_slot": "1h", "source_index": 1, "source_time": start.isoformat()},
                    {**_saved_row("BBB/USDT:USDT", 63), "source_slot": "1h", "source_index": 1, "source_time": start.isoformat()},
                    {**_saved_row("CCC/USDT:USDT", 62), "source_slot": "1h", "source_index": 1, "source_time": start.isoformat()},
                    {**_saved_row("DDD/USDT:USDT", 67), "source_slot": "1h", "source_index": 2, "source_time": (start + timedelta(hours=1)).isoformat()},
                    {**_saved_row("EEE/USDT:USDT", 66), "source_slot": "1h", "source_index": 2, "source_time": (start + timedelta(hours=1)).isoformat()},
                    {**_saved_row("FFF/USDT:USDT", 65), "source_slot": "1h", "source_index": 2, "source_time": (start + timedelta(hours=1)).isoformat()},
                ],
                {"input_count": 6, "refreshed_count": 6, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 6},
            ),
            (
                [
                    {**_saved_row("DDD/USDT:USDT", 67, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1, "origin_source_slot": "1h", "origin_source_index": 2, "origin_source_time": (start + timedelta(hours=1)).isoformat()},
                    {**_saved_row("EEE/USDT:USDT", 66, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1, "origin_source_slot": "1h", "origin_source_index": 2, "origin_source_time": (start + timedelta(hours=1)).isoformat()},
                    {**_saved_row("FFF/USDT:USDT", 65, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1, "origin_source_slot": "1h", "origin_source_index": 2, "origin_source_time": (start + timedelta(hours=1)).isoformat()},
                ],
                {"input_count": 3, "refreshed_count": 3, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 3},
            ),
            (
                [
                    {**_saved_row("GGG/USDT:USDT", 69), "source_slot": "1h", "source_index": 3, "source_time": (start + timedelta(hours=2)).isoformat()},
                    {**_saved_row("HHH/USDT:USDT", 68), "source_slot": "1h", "source_index": 3, "source_time": (start + timedelta(hours=2)).isoformat()},
                    {**_saved_row("III/USDT:USDT", 67), "source_slot": "1h", "source_index": 3, "source_time": (start + timedelta(hours=2)).isoformat()},
                    {**_saved_row("JJJ/USDT:USDT", 72), "source_slot": "1h", "source_index": 4, "source_time": (start + timedelta(hours=3)).isoformat()},
                    {**_saved_row("KKK/USDT:USDT", 71), "source_slot": "1h", "source_index": 4, "source_time": (start + timedelta(hours=3)).isoformat()},
                    {**_saved_row("LLL/USDT:USDT", 70), "source_slot": "1h", "source_index": 4, "source_time": (start + timedelta(hours=3)).isoformat()},
                ],
                {"input_count": 6, "refreshed_count": 6, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 6},
            ),
            (
                [
                    {**_saved_row("DDD/USDT:USDT", 67, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1, "origin_source_slot": "1h", "origin_source_index": 2, "origin_source_time": (start + timedelta(hours=1)).isoformat()},
                    {**_saved_row("EEE/USDT:USDT", 66, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1, "origin_source_slot": "1h", "origin_source_index": 2, "origin_source_time": (start + timedelta(hours=1)).isoformat()},
                    {**_saved_row("FFF/USDT:USDT", 65, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1, "origin_source_slot": "1h", "origin_source_index": 2, "origin_source_time": (start + timedelta(hours=1)).isoformat()},
                    {**_saved_row("JJJ/USDT:USDT", 72, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 2, "origin_source_slot": "1h", "origin_source_index": 4, "origin_source_time": (start + timedelta(hours=3)).isoformat()},
                    {**_saved_row("KKK/USDT:USDT", 71, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 2, "origin_source_slot": "1h", "origin_source_index": 4, "origin_source_time": (start + timedelta(hours=3)).isoformat()},
                    {**_saved_row("LLL/USDT:USDT", 70, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 2, "origin_source_slot": "1h", "origin_source_index": 4, "origin_source_time": (start + timedelta(hours=3)).isoformat()},
                ],
                {"input_count": 6, "refreshed_count": 6, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 6},
            ),
        ]
        update_lc_internal_pipeline(
            config,
            [_candidate("DDD/USDT:USDT", 67), _candidate("EEE/USDT:USDT", 66), _candidate("FFF/USDT:USDT", 65)],
            now=start + timedelta(hours=1),
        )
        update_lc_internal_pipeline(
            config,
            [_candidate("GGG/USDT:USDT", 69), _candidate("HHH/USDT:USDT", 68), _candidate("III/USDT:USDT", 67)],
            now=start + timedelta(hours=2),
        )
        four_hour = update_lc_internal_pipeline(
            config,
            [_candidate("JJJ/USDT:USDT", 72), _candidate("KKK/USDT:USDT", 71), _candidate("LLL/USDT:USDT", 70)],
            now=start + timedelta(hours=3),
        )
        notify_mini_pool_summary(
            config,
            four_hour["four_hour_event"]["approved"][:2],
            scan={
                "mini_index": 1,
                "selected_symbols": ["JJJ/USDT:USDT", "KKK/USDT:USDT"],
                "decision_reason_vi": "Mini đã chọn 2 cặp mạnh nhất trong nhóm LC 4h.",
            },
            slot_id="slot-1",
            now=start + timedelta(hours=3, minutes=10),
        )

        message = format_internal_notifications_view(config)

        self.assertIn("mỗi khối là 1 thông báo đầy đủ", message)
        blocks = message.split("\n\n")
        self.assertGreaterEqual(len(blocks), 11)
        self.assertEqual(blocks[0], "🔔 Thông báo nội bộ")
        self.assertTrue(blocks[2].startswith("🔵 1h top 3 setup\n07/07/26 07:05:00"))
        self.assertTrue(blocks[3].startswith("🔵 1h top 3 setup\n07/07/26 08:05:00"))
        self.assertTrue(blocks[4].startswith("🟡 #1 LC nội bộ tổng hợp 2h\n07/07/26 08:05:00"))
        self.assertTrue(blocks[5].startswith("🔴 #1 LC nội bộ tổng hợp 4h\n07/07/26 08:05:00"))
        self.assertTrue(blocks[6].startswith("🔵 1h top 3 setup\n07/07/26 09:05:00"))
        self.assertTrue(blocks[7].startswith("🔵 1h top 3 setup\n07/07/26 10:05:00"))
        self.assertTrue(blocks[8].startswith("🟡 #2 LC nội bộ tổng hợp 2h\n07/07/26 10:05:00"))
        self.assertTrue(blocks[9].startswith("🔴 #2 LC nội bộ tổng hợp 4h\n07/07/26 10:05:00"))
        self.assertTrue(blocks[10].startswith("🟣 Mini #1\n07/07/26 10:15:00"))
        self.assertIn("Khung 🔵 1h: #1 (07:05)", blocks[2])
        self.assertIn("Khung 🟡 2h: #1 (08:05)", blocks[4])
        self.assertIn("Gộp từ: 🔵 1h #1 (07:05), 🔵 1h #2 (08:05)", blocks[4])
        self.assertIn("Gốc 🔵 1h #2 (08:05)", blocks[4])
        self.assertIn("Khung 🔴 4h: #1 (08:05)", blocks[5])
        self.assertIn("Gộp từ: 🟡 2h #1 (08:05)", blocks[5])
        self.assertIn("Khung 🔴 4h: #2 (10:05)", blocks[9])
        self.assertIn("Gộp từ: 🟡 2h #1 (08:05), 🟡 2h #2 (10:05)", blocks[9])
        self.assertIn("Gốc 🔵 1h #4 (10:05)", blocks[9])
        self.assertIn("Mini chọn: 2/3 cặp", blocks[10])
        self.assertIn("AAA/USDT:USDT", blocks[2])
        self.assertIn("DDD/USDT:USDT", blocks[4])
        self.assertIn("JJJ/USDT:USDT", blocks[9])
        send_message.assert_not_called()

    @patch("crypto_trader.lc_pipeline._recheck_rows_with_latest_market_data")
    @patch("crypto_trader.notifier.send_telegram_message")
    def test_one_hour_stays_internal_but_two_hour_and_four_hour_push_when_enabled(self, send_message, recheck_rows) -> None:
        config = self._config()
        config["ai"]["internal"]["lc_pipeline_notify_two_hour_summary"] = True
        config["ai"]["internal"]["lc_pipeline_notify_mini_pool_summary"] = True
        config["ai"]["internal"]["lc_pipeline_recheck_interval_minutes"] = 999
        start = datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc)

        update_lc_internal_pipeline(
            config,
            [_candidate("AAA/USDT:USDT", 61), _candidate("BBB/USDT:USDT", 60), _candidate("CCC/USDT:USDT", 59)],
            now=start,
        )
        self.assertEqual(send_message.call_count, 0)

        recheck_rows.side_effect = [
            (
                [
                    {**_saved_row("AAA/USDT:USDT", 61), "source_slot": "1h", "source_index": 1, "source_time": start.isoformat()},
                    {**_saved_row("BBB/USDT:USDT", 60), "source_slot": "1h", "source_index": 1, "source_time": start.isoformat()},
                    {**_saved_row("CCC/USDT:USDT", 59), "source_slot": "1h", "source_index": 1, "source_time": start.isoformat()},
                    {**_saved_row("DDD/USDT:USDT", 64), "source_slot": "1h", "source_index": 2, "source_time": (start + timedelta(hours=1)).isoformat()},
                    {**_saved_row("EEE/USDT:USDT", 63), "source_slot": "1h", "source_index": 2, "source_time": (start + timedelta(hours=1)).isoformat()},
                    {**_saved_row("FFF/USDT:USDT", 62), "source_slot": "1h", "source_index": 2, "source_time": (start + timedelta(hours=1)).isoformat()},
                ],
                {"input_count": 6, "refreshed_count": 6, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 6},
            ),
            (
                [
                    {**_saved_row("DDD/USDT:USDT", 64, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1, "origin_source_slot": "1h", "origin_source_index": 2, "origin_source_time": (start + timedelta(hours=1)).isoformat()},
                    {**_saved_row("EEE/USDT:USDT", 63, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1, "origin_source_slot": "1h", "origin_source_index": 2, "origin_source_time": (start + timedelta(hours=1)).isoformat()},
                    {**_saved_row("FFF/USDT:USDT", 62, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1, "origin_source_slot": "1h", "origin_source_index": 2, "origin_source_time": (start + timedelta(hours=1)).isoformat()},
                ],
                {"input_count": 3, "refreshed_count": 3, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 3},
            ),
            (
                [
                    {**_saved_row("GGG/USDT:USDT", 69), "source_slot": "1h", "source_index": 3, "source_time": (start + timedelta(hours=2)).isoformat()},
                    {**_saved_row("HHH/USDT:USDT", 68), "source_slot": "1h", "source_index": 3, "source_time": (start + timedelta(hours=2)).isoformat()},
                    {**_saved_row("III/USDT:USDT", 67), "source_slot": "1h", "source_index": 3, "source_time": (start + timedelta(hours=2)).isoformat()},
                    {**_saved_row("JJJ/USDT:USDT", 72), "source_slot": "1h", "source_index": 4, "source_time": (start + timedelta(hours=3)).isoformat()},
                    {**_saved_row("KKK/USDT:USDT", 71), "source_slot": "1h", "source_index": 4, "source_time": (start + timedelta(hours=3)).isoformat()},
                    {**_saved_row("LLL/USDT:USDT", 70), "source_slot": "1h", "source_index": 4, "source_time": (start + timedelta(hours=3)).isoformat()},
                ],
                {"input_count": 6, "refreshed_count": 6, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 6},
            ),
            (
                [
                    {**_saved_row("DDD/USDT:USDT", 64, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1, "origin_source_slot": "1h", "origin_source_index": 2, "origin_source_time": (start + timedelta(hours=1)).isoformat()},
                    {**_saved_row("EEE/USDT:USDT", 63, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1, "origin_source_slot": "1h", "origin_source_index": 2, "origin_source_time": (start + timedelta(hours=1)).isoformat()},
                    {**_saved_row("FFF/USDT:USDT", 62, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 1, "origin_source_slot": "1h", "origin_source_index": 2, "origin_source_time": (start + timedelta(hours=1)).isoformat()},
                    {**_saved_row("JJJ/USDT:USDT", 72, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 2, "origin_source_slot": "1h", "origin_source_index": 4, "origin_source_time": (start + timedelta(hours=3)).isoformat()},
                    {**_saved_row("KKK/USDT:USDT", 71, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 2, "origin_source_slot": "1h", "origin_source_index": 4, "origin_source_time": (start + timedelta(hours=3)).isoformat()},
                    {**_saved_row("LLL/USDT:USDT", 70, state="LC_NOI_BO"), "source_slot": "2h", "source_index": 2, "origin_source_slot": "1h", "origin_source_index": 4, "origin_source_time": (start + timedelta(hours=3)).isoformat()},
                ],
                {"input_count": 6, "refreshed_count": 6, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 6},
            ),
        ]
        update_lc_internal_pipeline(
            config,
            [_candidate("DDD/USDT:USDT", 64), _candidate("EEE/USDT:USDT", 63), _candidate("FFF/USDT:USDT", 62)],
            now=start + timedelta(hours=1),
        )
        update_lc_internal_pipeline(
            config,
            [_candidate("GGG/USDT:USDT", 69), _candidate("HHH/USDT:USDT", 68), _candidate("III/USDT:USDT", 67)],
            now=start + timedelta(hours=2),
        )
        four_hour = update_lc_internal_pipeline(
            config,
            [_candidate("JJJ/USDT:USDT", 72), _candidate("KKK/USDT:USDT", 71), _candidate("LLL/USDT:USDT", 70)],
            now=start + timedelta(hours=3),
        )
        notify_mini_pool_summary(
            config,
            four_hour["four_hour_event"]["approved"][:1],
            scan={
                "mini_index": 1,
                "selected_symbols": ["JJJ/USDT:USDT"],
                "decision_reason_vi": "Mini đã chọn cặp mạnh nhất từ nhóm LC 4h.",
            },
            slot_id="slot-1",
        )

        messages = "\n".join(call.args[1] for call in send_message.call_args_list)
        self.assertEqual(send_message.call_count, 5)
        self.assertIn("LC nội bộ tổng hợp 2h", messages)
        self.assertIn("LC nội bộ tổng hợp 4h", messages)
        self.assertIn("Gốc 🔵 1h #2 (08:05)", messages)
        self.assertIn("Gốc 🔵 1h #4 (10:05)", messages)
        self.assertIn("Mini #1", messages)
