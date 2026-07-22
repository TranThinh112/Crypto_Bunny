from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from crypto_trader.dashboard_services import (
    _build_system_checklist_payload,
    _persist_cached_payload,
    _persist_system_checklist_snapshot,
    _trade_execution_summary,
    attach_previous_system_checklist_snapshot,
    system_modules_payload,
    system_checklist_payload,
)


class SystemChecklistPayloadTests(unittest.TestCase):
    def test_cached_payload_storage_converts_datetimes_to_iso_strings(self) -> None:
        payload = {
            "generated_at": datetime(2026, 7, 12, 3, 15, tzinfo=timezone.utc),
            "items": [{"expires_at": datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc)}],
        }

        with patch("crypto_trader.dashboard_services.set_journal_state") as set_state:
            _persist_cached_payload({}, "dashboard:test", payload)

        stored_body = set_state.call_args.args[2]
        self.assertIn("2026-07-12T03:15:00+00:00", stored_body)
        self.assertIn("2026-07-14T00:00:00+00:00", stored_body)

    def test_system_checklist_snapshot_storage_converts_datetimes_to_iso_strings(self) -> None:
        payload = {
            "date": "2026-07-12",
            "created_at": datetime(2026, 7, 12, 3, 30, tzinfo=timezone.utc),
            "modules": [
                {
                    "number": 1,
                    "name": "Storage",
                    "details": {"expires_at": datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc)},
                }
            ],
        }

        with patch("crypto_trader.dashboard_services._current_system_checklist_snapshot", return_value=None), patch(
            "crypto_trader.dashboard_services.set_journal_state"
        ) as set_state:
            _persist_system_checklist_snapshot({}, payload)

        stored_bodies = [call.args[2] for call in set_state.call_args_list]
        self.assertTrue(any("2026-07-12T03:30:00+00:00" in body for body in stored_bodies))
        self.assertTrue(any("2026-07-13T00:00:00+00:00" in body for body in stored_bodies))

    def test_returns_current_snapshot_for_today_without_rebuilding(self) -> None:
        snapshot = {
            "date": "2026-07-10",
            "created_at": "2026-07-10T13:05:00+00:00",
            "modules": [{"number": 1, "name": "AI Decision Memory"}],
        }
        enriched = {**snapshot, "previous_snapshot": None}

        with patch("crypto_trader.dashboard_services._system_report_date", return_value="2026-07-10"), patch(
            "crypto_trader.dashboard_services._preferred_system_checklist_snapshot", return_value=snapshot
        ), patch(
            "crypto_trader.dashboard_services._latest_system_checklist_snapshot", return_value=snapshot
        ), patch(
            "crypto_trader.dashboard_services.refresh_system_checklist_snapshot"
        ) as refresh_snapshot, patch(
            "crypto_trader.dashboard_services.attach_previous_system_checklist_snapshot", return_value=enriched
        ) as attach_previous:
            payload = system_checklist_payload({})

        self.assertEqual(payload, enriched)
        refresh_snapshot.assert_not_called()
        attach_previous.assert_called_once_with({}, snapshot)

    def test_refreshes_when_today_snapshot_missing(self) -> None:
        rebuilt = {
            "date": "2026-07-10",
            "created_at": "2026-07-10T13:10:00+00:00",
            "modules": [{"number": 3, "name": "Bunny Health Monitor"}],
        }

        with patch("crypto_trader.dashboard_services._system_report_date", return_value="2026-07-10"), patch(
            "crypto_trader.dashboard_services._preferred_system_checklist_snapshot", return_value=None
        ), patch(
            "crypto_trader.dashboard_services._latest_system_checklist_snapshot", return_value={"date": "2026-07-09"}
        ), patch(
            "crypto_trader.dashboard_services.refresh_system_checklist_snapshot", return_value=rebuilt
        ) as refresh_snapshot:
            payload = system_checklist_payload({})

        self.assertEqual(payload, rebuilt)
        refresh_snapshot.assert_called_once()

    def test_uses_latest_snapshot_when_it_is_already_today(self) -> None:
        snapshot = {
            "date": "2026-07-10",
            "created_at": "2026-07-10T13:05:00+00:00",
            "modules": [{"number": 7, "name": "Prompt Caching"}],
        }
        enriched = {**snapshot, "previous_snapshot": None}

        with patch("crypto_trader.dashboard_services._system_report_date", return_value="2026-07-10"), patch(
            "crypto_trader.dashboard_services._preferred_system_checklist_snapshot", return_value=None
        ), patch(
            "crypto_trader.dashboard_services._latest_system_checklist_snapshot", return_value=snapshot
        ), patch(
            "crypto_trader.dashboard_services.refresh_system_checklist_snapshot"
        ) as refresh_snapshot, patch(
            "crypto_trader.dashboard_services.attach_previous_system_checklist_snapshot", return_value=enriched
        ) as attach_previous:
            payload = system_checklist_payload({}, max_age_seconds=1)

        self.assertEqual(payload, enriched)
        refresh_snapshot.assert_not_called()
        attach_previous.assert_called_once_with({}, snapshot)

    def test_force_refresh_still_rebuilds_payload(self) -> None:
        rebuilt = {
            "date": "2026-07-10",
            "created_at": "2026-07-10T13:10:00+00:00",
            "modules": [],
        }

        with patch("crypto_trader.dashboard_services._latest_system_checklist_snapshot", return_value={"date": "2026-07-09"}), patch(
            "crypto_trader.dashboard_services.refresh_system_checklist_snapshot", return_value=rebuilt
        ) as refresh_snapshot:
            payload = system_checklist_payload({}, force_refresh=True)

        self.assertEqual(payload, rebuilt)
        refresh_snapshot.assert_called_once()

    def test_attaches_previous_snapshot_from_runtime_cache(self) -> None:
        current = {
            "date": "2026-07-10",
            "created_at": "2026-07-10T13:43:31+00:00",
            "modules": [{"number": 2, "name": "Bunny Minimize Losses"}],
        }
        previous = {
            "date": "2026-07-10",
            "created_at": "2026-07-10T13:12:00+00:00",
            "modules": [{"number": 2, "name": "Bunny Minimize Losses"}],
        }

        with patch("crypto_trader.dashboard_services._raw_previous_system_checklist_snapshot", return_value=previous), patch(
            "crypto_trader.dashboard_services._fallback_previous_system_checklist_snapshot", return_value=None
        ):
            payload = attach_previous_system_checklist_snapshot({}, current)

        self.assertEqual(payload["previous_snapshot"], previous)

    def test_falls_back_to_history_when_runtime_cache_missing(self) -> None:
        current = {
            "date": "2026-07-10",
            "created_at": "2026-07-10T13:43:31+00:00",
            "modules": [{"number": 7, "name": "Prompt Caching"}],
        }
        previous = {
            "date": "2026-07-09",
            "created_at": "2026-07-09T13:43:31+00:00",
            "modules": [{"number": 7, "name": "Prompt Caching"}],
        }

        with patch("crypto_trader.dashboard_services._raw_previous_system_checklist_snapshot", return_value=None), patch(
            "crypto_trader.dashboard_services._fallback_previous_system_checklist_snapshot", return_value=previous
        ):
            payload = attach_previous_system_checklist_snapshot({}, current)

        self.assertEqual(payload["previous_snapshot"], previous)

    def test_recovery_module_warns_for_orphaned_blocked_state(self) -> None:
        blocked_state = {
            "blocked": True,
            "block_reason": "Recovery step limit reached: 4/4",
            "recovery_step": 4,
            "cycle_pnl_usdt": -222.39,
            "next_margin_usdt": 0.0,
            "processed_keys": ["old"],
        }

        with patch("crypto_trader.dashboard_services.get_journal_state", return_value=json.dumps(blocked_state)):
            modules = system_modules_payload(
                {},
                checked_date="2026-07-12",
                checked_at_iso="2026-07-12T09:00:00+00:00",
                ai_history=[],
                replay={},
                strategy={},
                regime={},
                health={},
                risk_state={"openPositionsCount": 0, "maxConcurrentPositions": 5},
                row_counts={
                    "trade_executions": 0,
                    "pending_orders": 0,
                    "internal_pending_orders": 0,
                    "paper_trades": 0,
                    "trade_memory": 0,
                },
            )

        recovery = next(item for item in modules if item["name"] == "Recovery Chain Manager")
        self.assertEqual(recovery["status"], "warn")

    def test_recovery_module_fails_for_blocked_state_with_trade_records(self) -> None:
        blocked_state = {
            "blocked": True,
            "block_reason": "Recovery step limit reached: 4/4",
            "recovery_step": 4,
            "cycle_pnl_usdt": -222.39,
            "next_margin_usdt": 0.0,
            "processed_keys": ["old"],
        }

        with patch("crypto_trader.dashboard_services.get_journal_state", return_value=json.dumps(blocked_state)):
            modules = system_modules_payload(
                {},
                checked_date="2026-07-12",
                checked_at_iso="2026-07-12T09:00:00+00:00",
                ai_history=[],
                replay={},
                strategy={},
                regime={},
                health={},
                risk_state={"openPositionsCount": 0, "maxConcurrentPositions": 5},
                row_counts={"trade_executions": 1},
            )

        recovery = next(item for item in modules if item["name"] == "Recovery Chain Manager")
        self.assertEqual(recovery["status"], "fail")

    def test_bunny_minimize_losses_dashboard_exposes_threshold_and_slot_rows(self) -> None:
        risk_state = {
            "recoveryMode": "SOFT_RECOVERY",
            "isRecoveryMode": True,
            "isPaused": False,
            "globalLossStreak": 1,
            "globalLossStreakThreshold": 2,
            "pauseTradingLossStreak": 4,
            "openPositionsCount": 2,
            "maxConcurrentPositions": 5,
            "normalRiskPercent": 1.0,
            "softRecoveryRiskPercent": 0.75,
            "recoveryModeRiskPercent": 0.5,
            "currentNormalMinRuleScore": 75,
            "currentNormalMinGptConfidence": 80,
            "normalMinRiskReward": 1.5,
            "softRecoveryMinRuleScore": 87,
            "softRecoveryMinGptConfidence": 89,
            "softRecoveryMinRiskReward": 2.0,
            "recoveryMinRuleScore": 90,
            "recoveryMinGptConfidence": 92,
            "recoveryMinRiskReward": 2.5,
            "strongSetupRuleScore": 85,
            "strongSetupGptConfidence": 88,
            "strongSetupMinRiskReward": 2.0,
            "enableAdaptiveThreshold": True,
            "weeklyTargetMinTrades": 3,
            "weeklyTargetMaxTrades": 7,
            "adaptiveScoreStep": 3,
            "adaptiveConfidenceStep": 3,
            "updatedAt": "2026-07-12T09:00:00+00:00",
        }

        modules = system_modules_payload(
            {},
            checked_date="2026-07-12",
            checked_at_iso="2026-07-12T09:00:00+00:00",
            ai_history=[],
            replay={},
            strategy={},
            regime={},
            health={},
            risk_state=risk_state,
            row_counts={},
        )

        module = next(item for item in modules if item["name"] == "Bunny Minimize Losses")
        values = {row["label"]: row["value"] for row in module["stats"]}
        self.assertEqual(module["status"], "warn")
        self.assertEqual(values["recoveryMode"], "SOFT_RECOVERY")
        self.assertEqual(values["maxConcurrentPositions"], 5)
        self.assertEqual(values["slotUtilizationPercent"], 40.0)
        self.assertEqual(values["softRecoveryMinRuleScore"], 87)
        self.assertEqual(values["recoveryMinRuleScore"], 90)
        self.assertEqual(values["strongSetupMinRiskReward"], 2.0)

    def test_trade_execution_summary_exposes_pending_total(self) -> None:
        with patch("crypto_trader.dashboard_services.list_trade_execution_rows", return_value=[]), patch(
            "crypto_trader.dashboard_services.count_pending_orders", return_value=4
        ):
            payload = _trade_execution_summary({})

        self.assertEqual(payload["pending_total"], 4)

    def test_module_one_uses_local_calendar_day_for_ai_decision_stats(self) -> None:
        with patch("crypto_trader.dashboard_services.ai_trade_decision_stats", return_value={"totalRecords": 389}) as trade_stats, patch(
            "crypto_trader.dashboard_services.ai_call_decision_stats",
            return_value={
                "totalDecisions": 5,
                "totalRecords": 5,
                "miniCallCount": 4,
                "okxCallCount": 1,
                "miniNoTradeCount": 1,
                "longCount": 3,
                "shortCount": 0,
                "noTradeCount": 1,
            },
        ) as call_stats:
            modules = system_modules_payload(
                {"timezone": "Asia/Saigon"},
                checked_date="2026-07-17",
                checked_at_iso="2026-07-17T06:30:00+00:00",
                ai_history=[],
                replay={},
                strategy={},
                regime={},
                health={},
                risk_state={},
                row_counts={},
            )

        trade_stats.assert_called_once()
        call_stats.assert_called_once()
        self.assertEqual(call_stats.call_args.args[0]["timezone"], "Asia/Saigon")
        self.assertEqual(call_stats.call_args.kwargs["created_from"], "2026-07-16T17:00:00+00:00")
        self.assertEqual(call_stats.call_args.kwargs["created_to"], "2026-07-17T17:00:00+00:00")
        module_one = next(item for item in modules if item["number"] == 1)
        values = {row["label"]: row["value"] for row in module_one["stats"]}
        self.assertEqual(values["total_decisions"], 5)
        self.assertEqual(values["Tổng log gọi AI trong phạm vi"], 5)
        self.assertEqual(values["mini_no_trade_count"], 1)

    def test_module_one_all_range_uses_all_ai_call_history_without_day_bounds(self) -> None:
        config = {"timezone": "Asia/Saigon"}
        with patch("crypto_trader.dashboard_services.ai_trade_decision_stats", return_value={"totalRecords": 99}) as trade_stats, patch(
            "crypto_trader.dashboard_services.ai_call_decision_stats",
            return_value={
                "totalDecisions": 12,
                "totalRecords": 12,
                "miniCallCount": 8,
                "okxCallCount": 4,
                "miniNoTradeCount": 2,
                "longCount": 5,
                "shortCount": 3,
                "noTradeCount": 1,
            },
        ) as call_stats:
            modules = system_modules_payload(
                config,
                checked_date="2026-07-17",
                checked_at_iso="2026-07-17T06:30:00+00:00",
                ai_history=[],
                replay={},
                strategy={},
                regime={},
                health={},
                risk_state={},
                row_counts={},
                ai_range="all",
            )

        trade_stats.assert_called_once_with(config)
        call_stats.assert_called_once_with(config)
        module_one = next(item for item in modules if item["number"] == 1)
        self.assertEqual(module_one["ai_range"], "all")
        self.assertEqual(module_one["ai_range_label"], "Toàn bộ dữ liệu đang lưu")
        values = {row["label"]: row["value"] for row in module_one["stats"]}
        self.assertEqual(values["total_decisions"], 12)
        self.assertEqual(values["Phạm vi dữ liệu AI"], "Toàn bộ dữ liệu đang lưu")
        self.assertEqual(values["Tổng log gọi AI trong phạm vi"], 12)

    def test_system_checklist_payload_embeds_market_regime_history(self) -> None:
        config = {"mode": "dry_run"}
        btc_history = [
            {
                "created_at": "2026-07-18T05:00:00+00:00",
                "regime": "LOW_VOLATILITY",
                "indicators": {"symbol": "BTC/USDT:USDT", "ema_fast": 100.0, "ema_slow": 99.0, "rsi": 56.0},
            },
            {
                "created_at": "2026-07-18T05:01:00+00:00",
                "regime": "LOW_VOLATILITY",
                "indicators": {"symbol": "BTC/USDT:USDT", "ema_fast": 101.0, "ema_slow": 99.5, "rsi": 57.0},
            },
        ]
        aggregate_history = {
            "created_at": "2026-07-18T05:03:00+00:00",
            "regime": "LOW_VOLATILITY",
            "indicators": {
                "scope": "aggregate",
                "symbol": "MARKET",
                "coverage_count": 5,
                "target_count": 40,
                "covered_symbols": [
                    "BTC/USDT:USDT",
                    "SOL/USDT:USDT",
                    "ETH/USDT:USDT",
                    "XRP/USDT:USDT",
                    "BNB/USDT:USDT",
                ],
                "market_symbols": [
                    "BTC/USDT:USDT",
                    "SOL/USDT:USDT",
                    "ETH/USDT:USDT",
                    "XRP/USDT:USDT",
                    "BNB/USDT:USDT",
                ],
            },
        }
        regime_history = [
            aggregate_history,
            *btc_history,
            {
                "created_at": "2026-07-18T05:02:00+00:00",
                "regime": "LOW_VOLATILITY",
                "indicators": {"symbol": "ETH/USDT:USDT", "ema_fast": 1845.0, "ema_slow": 1844.0, "rsi": 55.0},
            },
        ]

        with patch(
            "crypto_trader.dashboard_services.storage_stats",
            return_value={"backend": "atlas", "disk": {}, "row_counts": {}, "payload_bytes": {}},
        ), patch(
            "crypto_trader.dashboard_services.recent_ai_call_history", return_value=[]
        ), patch(
            "crypto_trader.dashboard_services.replay_stats", return_value={}
        ), patch(
            "crypto_trader.dashboard_services.current_strategy_state", return_value={}
        ), patch(
            "crypto_trader.dashboard_services.current_market_regime",
            return_value={
                "regime": "LOW_VOLATILITY",
                "confidence": 76.0,
                "created_at": "2026-07-18T05:03:00+00:00",
                "indicators": aggregate_history["indicators"],
            },
        ), patch(
            "crypto_trader.dashboard_services.get_bunny_health_state", return_value={}
        ), patch(
            "crypto_trader.dashboard_services.get_trading_system_state", return_value={}
        ), patch(
            "crypto_trader.dashboard_services.market_guard_block_status", return_value={}
        ), patch(
            "crypto_trader.dashboard_services.list_paper_trades", return_value=[]
        ), patch(
            "crypto_trader.dashboard_services.system_modules_payload", return_value=[]
        ) as modules_payload, patch(
            "crypto_trader.dashboard_services.market_regime_history", return_value=regime_history
        ) as history_reader:
            payload = _build_system_checklist_payload(config, automation={"last_result": ""})

        history_payload = payload["market_regime_history"]
        self.assertEqual(history_payload["items"], [aggregate_history])
        self.assertEqual(history_payload["top_symbols"], ["BTC/USDT:USDT", "XAU/USDT:USDT", "ETH/USDT:USDT"])
        self.assertEqual(history_payload["detail_symbols"], ["BTC/USDT:USDT", "XAU/USDT:USDT", "ETH/USDT:USDT"])
        self.assertEqual(history_payload["aggregate_limit"], 40)
        self.assertEqual(history_payload["market_symbols"], aggregate_history["indicators"]["market_symbols"])
        self.assertEqual(history_payload["by_symbol"]["BTC/USDT:USDT"]["items"], btc_history)
        self.assertEqual(history_payload["by_symbol"]["XAU/USDT:USDT"]["items"], [])
        self.assertEqual(history_payload["by_symbol"]["ETH/USDT:USDT"]["items"], [regime_history[-1]])
        self.assertEqual(history_payload["coverage"]["coverage_count"], 5)
        self.assertEqual(history_payload["coverage"]["target_count"], 40)
        history_reader.assert_called_once_with(config, limit=200)
        modules_payload.assert_called_once()
        self.assertEqual(modules_payload.call_args.kwargs["regime_history_items"], [aggregate_history])
        self.assertEqual(modules_payload.call_args.kwargs["regime_history_payload"], history_payload)

    def test_system_checklist_all_range_reuses_snapshot_and_updates_ai_module(self) -> None:
        snapshot = {
            "date": "2026-07-10",
            "created_at": "2026-07-10T13:10:00+00:00",
            "ai_range": "current",
            "modules": [{"number": 1, "ai_range": "current", "stats": []}, {"number": 2, "stats": []}],
        }
        enriched = {**snapshot, "ai_range": "all", "previous_snapshot": None}

        with patch("crypto_trader.dashboard_services._current_system_checklist_snapshot", return_value=snapshot) as current_snapshot, patch(
            "crypto_trader.dashboard_services.refresh_system_checklist_snapshot"
        ) as refresh_snapshot, patch(
            "crypto_trader.dashboard_services._build_system_checklist_payload"
        ) as build_payload, patch(
            "crypto_trader.dashboard_services.attach_previous_system_checklist_snapshot", return_value=enriched
        ) as attach_previous:
            payload = system_checklist_payload({}, ai_range="all")

        self.assertEqual(payload, enriched)
        current_snapshot.assert_called_once()
        refresh_snapshot.assert_not_called()
        build_payload.assert_not_called()
        updated_payload = attach_previous.call_args.args[1]
        self.assertEqual(updated_payload["ai_range"], "all")
        self.assertEqual(updated_payload["modules"][0]["ai_range"], "all")


if __name__ == "__main__":
    unittest.main()
