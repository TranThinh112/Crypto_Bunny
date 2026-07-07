from __future__ import annotations

import tempfile
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch

from crypto_trader.config import DEFAULT_CONFIG
from crypto_trader.lc_pipeline import (
    format_internal_notifications_view,
    lc_pipeline_dashboard_payload,
    lc_pipeline_mini_pool,
    notify_mini_pool_summary,
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
        config["state_db_path"] = "state.sqlite"
        config["ai"]["internal"]["lc_pipeline_notify_two_hour_summary"] = False
        config["ai"]["internal"]["lc_pipeline_promote_to_pending"] = False
        config["ai"]["internal"]["lc_pipeline_min_win_probability_pct"] = 50
        return config

    def tearDown(self) -> None:
        tmpdir = getattr(self, "tmpdir", None)
        if tmpdir:
            tmpdir.cleanup()

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
                _saved_row("AAA/USDT:USDT", 61, volume=3),
                _saved_row("BBB/USDT:USDT", 60, volume=2),
                _saved_row("CCC/USDT:USDT", 59, volume=1),
                _saved_row("DDD/USDT:USDT", 64, volume=4),
                _saved_row("EEE/USDT:USDT", 63, volume=3),
                _saved_row("FFF/USDT:USDT", 62, volume=2),
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
        self.assertEqual([item["frame"] for item in internal_notifications], ["1h", "1h", "2h"])
        send_message.assert_called_once()
        self.assertIn("🕑 #1 LC nội bộ tổng hợp 2h", send_message.call_args.args[1])
        self.assertIn("DDD/USDT:USDT | LONG | Win 64.00%", send_message.call_args.args[1])
        self.assertFalse(send_message.call_args.kwargs["replace_previous"])

    @patch("crypto_trader.lc_pipeline._recheck_rows_with_latest_market_data")
    def test_mini_pool_prefers_saved_internal_lc_pairs(self, recheck_rows) -> None:
        config = self._config()
        start = datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc)
        update_lc_internal_pipeline(
            config,
            [_candidate("AAA/USDT:USDT", 64), _candidate("BBB/USDT:USDT", 63), _candidate("CCC/USDT:USDT", 62)],
            now=start,
        )
        recheck_rows.return_value = (
            [
                _saved_row("AAA/USDT:USDT", 64),
                _saved_row("BBB/USDT:USDT", 63),
                _saved_row("CCC/USDT:USDT", 62),
                _saved_row("DDD/USDT:USDT", 67),
                _saved_row("EEE/USDT:USDT", 66),
                _saved_row("FFF/USDT:USDT", 65),
            ],
            {"input_count": 6, "refreshed_count": 6, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 6},
        )
        update_lc_internal_pipeline(
            config,
            [_candidate("DDD/USDT:USDT", 67), _candidate("EEE/USDT:USDT", 66), _candidate("FFF/USDT:USDT", 65)],
            now=start + timedelta(hours=1),
        )
        current = [
            _candidate("AAA/USDT:USDT", 61),
            _candidate("BBB/USDT:USDT", 60),
            _candidate("CCC/USDT:USDT", 59),
            _candidate("DDD/USDT:USDT", 64),
            _candidate("EEE/USDT:USDT", 63),
            _candidate("FFF/USDT:USDT", 62),
        ]

        pool = lc_pipeline_mini_pool(config, current, limit=3)

        self.assertEqual([candidate.symbol for candidate in pool], ["DDD/USDT:USDT", "EEE/USDT:USDT", "FFF/USDT:USDT"])

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

    @patch("crypto_trader.lc_pipeline._recheck_rows_with_latest_market_data")
    def test_four_hour_uses_rechecked_win_rate_before_selecting_top_three(self, recheck_rows) -> None:
        config = self._config()
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
        self.assertEqual(state["four_hour_counter"], 1)
        self.assertEqual([event["daily_index"] for event in state["one_hour_history"]], [1, 2, 3, 4])
        self.assertEqual([event["daily_index"] for event in state["two_hour_history"]], [1, 2])
        self.assertEqual(state["four_hour_history"][0]["index"], 1)
        approved_four_hour = state["four_hour_history"][0]["approved"]
        self.assertEqual([row["symbol"] for row in approved_four_hour], ["JJJ/USDT:USDT", "KKK/USDT:USDT", "LLL/USDT:USDT"])
        self.assertEqual(approved_four_hour[0]["source_slot"], "4h")
        self.assertEqual(approved_four_hour[0]["source_index"], 1)
        self.assertEqual(approved_four_hour[0]["origin_source_slot"], "2h")
        self.assertEqual(approved_four_hour[0]["origin_source_index"], 2)

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
        start = datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc)
        update_lc_internal_pipeline(
            config,
            [_candidate("AAA/USDT:USDT", 64), _candidate("BBB/USDT:USDT", 63), _candidate("CCC/USDT:USDT", 62)],
            now=start,
        )
        recheck_rows.return_value = (
            [
                _saved_row("AAA/USDT:USDT", 64),
                _saved_row("BBB/USDT:USDT", 63),
                _saved_row("CCC/USDT:USDT", 62),
                _saved_row("DDD/USDT:USDT", 67),
                _saved_row("EEE/USDT:USDT", 66),
                _saved_row("FFF/USDT:USDT", 65),
            ],
            {"input_count": 6, "refreshed_count": 6, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 6},
        )
        update_lc_internal_pipeline(
            config,
            [_candidate("DDD/USDT:USDT", 67), _candidate("EEE/USDT:USDT", 66), _candidate("FFF/USDT:USDT", 65)],
            now=start + timedelta(hours=1),
        )

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

        notify_mini_pool_summary(config, rows, slot_id="slot-1")

        message = send_message.call_args.args[1]
        self.assertIn("Mini pool 4h", message)
        self.assertIn("AAA/USDT:USDT | LONG | 2h #3", message)
        self.assertIn("BBB/USDT:USDT | SHORT | HS 06/07/26 14:00:00", message)

    @patch("crypto_trader.lc_pipeline._recheck_rows_with_latest_market_data")
    @patch("crypto_trader.notifier.send_telegram_message")
    def test_internal_notifications_view_uses_one_line_timeline_without_chat_push(self, send_message, recheck_rows) -> None:
        config = self._config()
        config["ai"]["internal"]["lc_pipeline_notify_two_hour_summary"] = False
        config["ai"]["internal"]["lc_pipeline_notify_mini_pool_summary"] = False
        start = datetime(2026, 7, 7, 0, 5, tzinfo=timezone.utc)
        update_lc_internal_pipeline(
            config,
            [_candidate("AAA/USDT:USDT", 64), _candidate("BBB/USDT:USDT", 63), _candidate("CCC/USDT:USDT", 62)],
            now=start,
        )
        recheck_rows.return_value = (
            [
                _saved_row("AAA/USDT:USDT", 64),
                _saved_row("BBB/USDT:USDT", 63),
                _saved_row("CCC/USDT:USDT", 62),
                _saved_row("DDD/USDT:USDT", 67),
                _saved_row("EEE/USDT:USDT", 66),
                _saved_row("FFF/USDT:USDT", 65),
            ],
            {"input_count": 6, "refreshed_count": 6, "dropped": [], "warnings": [], "sync_complete": True, "synchronized_count": 6},
        )
        update_lc_internal_pipeline(
            config,
            [_candidate("DDD/USDT:USDT", 67), _candidate("EEE/USDT:USDT", 66), _candidate("FFF/USDT:USDT", 65)],
            now=start + timedelta(hours=1),
        )
        notify_mini_pool_summary(
            config,
            [{"symbol": "DDD/USDT:USDT", "side": "long", "source_slot": "2h", "source_index": 1}],
            slot_id="slot-1",
            now=start + timedelta(hours=1, minutes=10),
        )

        message = format_internal_notifications_view(config)

        lines = message.splitlines()
        self.assertIn("mỗi dòng là 1 thông báo", message)
        self.assertGreaterEqual(len(lines), 5)
        self.assertTrue(lines[2].startswith("🕐 1h top 3 setup"))
        self.assertTrue(lines[3].startswith("🕐 1h top 3 setup"))
        self.assertTrue(lines[4].startswith("🕑 #1 LC nội bộ tổng hợp 2h"))
        self.assertTrue(lines[5].startswith("🕓 Mini pool 4h"))
        self.assertIn("AAA/USDT:USDT", lines[2])
        self.assertIn("DDD/USDT:USDT", lines[4])
        send_message.assert_not_called()

    @patch("crypto_trader.notifier.send_telegram_message")
    def test_one_hour_stays_internal_but_two_hour_and_four_hour_push_when_enabled(self, send_message) -> None:
        config = self._config()
        config["ai"]["internal"]["lc_pipeline_notify_two_hour_summary"] = True
        config["ai"]["internal"]["lc_pipeline_notify_mini_pool_summary"] = True
        start = datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc)

        update_lc_internal_pipeline(
            config,
            [_candidate("AAA/USDT:USDT", 61), _candidate("BBB/USDT:USDT", 60), _candidate("CCC/USDT:USDT", 59)],
            now=start,
        )
        self.assertEqual(send_message.call_count, 0)

        update_lc_internal_pipeline(
            config,
            [_candidate("DDD/USDT:USDT", 64), _candidate("EEE/USDT:USDT", 63), _candidate("FFF/USDT:USDT", 62)],
            now=start + timedelta(hours=1),
        )
        notify_mini_pool_summary(
            config,
            [{"symbol": "DDD/USDT:USDT", "side": "long", "source_slot": "2h", "source_index": 1}],
            slot_id="slot-1",
        )

        messages = "\n".join(call.args[1] for call in send_message.call_args_list)
        self.assertEqual(send_message.call_count, 2)
        self.assertIn("LC nội bộ tổng hợp 2h", messages)
        self.assertIn("Mini pool 4h", messages)
