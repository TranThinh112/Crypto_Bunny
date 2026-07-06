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
    lc_pipeline_mini_pool,
    notify_mini_pool_summary,
    update_lc_internal_pipeline,
)
from crypto_trader.models import TradeCandidate
from crypto_trader.storage import get_journal_state


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


class LcPipelineTest(TestCase):
    def _config(self) -> dict:
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        config = deepcopy(DEFAULT_CONFIG)
        config["_config_dir"] = self.tmpdir.name
        config["state_db_path"] = "state.sqlite"
        config["ai"]["internal"]["lc_pipeline_notify_two_hour_summary"] = False
        config["ai"]["internal"]["lc_pipeline_promote_to_pending"] = False
        return config

    def tearDown(self) -> None:
        tmpdir = getattr(self, "tmpdir", None)
        if tmpdir:
            tmpdir.cleanup()

    @patch("crypto_trader.notifier.send_telegram_message")
    def test_two_hour_summary_keeps_top_three_and_notifies(self, send_message) -> None:
        config = self._config()
        config["ai"]["internal"]["lc_pipeline_notify_two_hour_summary"] = True
        start = datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc)
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

    def test_mini_pool_prefers_saved_internal_lc_pairs(self) -> None:
        config = self._config()
        start = datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc)
        update_lc_internal_pipeline(
            config,
            [_candidate("AAA/USDT:USDT", 61), _candidate("BBB/USDT:USDT", 60), _candidate("CCC/USDT:USDT", 59)],
            now=start,
        )
        update_lc_internal_pipeline(
            config,
            [_candidate("DDD/USDT:USDT", 64), _candidate("EEE/USDT:USDT", 63), _candidate("FFF/USDT:USDT", 62)],
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

    def test_two_hour_rejected_keeps_opposite_side_duplicate_setup(self) -> None:
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

    @patch("crypto_trader.notifier.send_telegram_message")
    def test_surviving_undecided_pair_promotes_to_internal_lc_and_notifies(self, send_message) -> None:
        config = self._config()
        config["ai"]["internal"]["lc_pipeline_notify_two_hour_summary"] = False
        config["ai"]["internal"]["lc_pipeline_promote_to_pending"] = True
        start = datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc)
        update_lc_internal_pipeline(
            config,
            [_candidate("AAA/USDT:USDT", 61), _candidate("BBB/USDT:USDT", 60), _candidate("CCC/USDT:USDT", 59)],
            now=start,
        )
        update_lc_internal_pipeline(
            config,
            [_candidate("DDD/USDT:USDT", 64), _candidate("EEE/USDT:USDT", 63), _candidate("FFF/USDT:USDT", 62)],
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

    @patch("crypto_trader.notifier.send_telegram_message")
    def test_internal_notifications_view_groups_frames_without_chat_push(self, send_message) -> None:
        config = self._config()
        config["ai"]["internal"]["lc_pipeline_notify_two_hour_summary"] = False
        config["ai"]["internal"]["lc_pipeline_notify_mini_pool_summary"] = False
        start = datetime(2026, 7, 6, 0, 5, tzinfo=timezone.utc)
        update_lc_internal_pipeline(
            config,
            [_candidate("AAA/USDT:USDT", 61), _candidate("BBB/USDT:USDT", 60), _candidate("CCC/USDT:USDT", 59)],
            now=start,
        )
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

        message = format_internal_notifications_view(config)

        self.assertIn("Khung 1h", message)
        self.assertIn("Khung 2h", message)
        self.assertIn("Khung 4h", message)
        self.assertIn("🕐", message)
        self.assertIn("🕑", message)
        self.assertIn("🕓", message)
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
