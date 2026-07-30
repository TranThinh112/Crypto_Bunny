from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from crypto_trader.dashboard_services import (
    _build_system_checklist_payload,
    _persist_cached_payload,
    _persist_system_checklist_snapshot,
    _slim_market_regime_snapshot,
    _trade_execution_profit_protection_levels,
    _trade_execution_summary,
    attach_previous_system_checklist_snapshot,
    system_checklist_history,
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

    def test_system_checklist_history_returns_empty_when_storage_times_out(self) -> None:
        with patch("crypto_trader.dashboard_services.list_journal_state_prefix", side_effect=TimeoutError("mongo timeout")):
            self.assertEqual(system_checklist_history({}), [])

    def test_previous_snapshot_is_optional_when_storage_times_out(self) -> None:
        payload = {"date": "2026-07-22", "created_at": "2026-07-22T13:55:00+00:00", "modules": []}
        with patch("crypto_trader.dashboard_services.get_journal_state", side_effect=TimeoutError("mongo timeout")):
            enriched = attach_previous_system_checklist_snapshot({}, payload)

        self.assertEqual(enriched["date"], "2026-07-22")
        self.assertIsNone(enriched["previous_snapshot"])

    def test_bunny_minimize_refresh_keeps_chart_stats(self) -> None:
        snapshot = {
            "date": "2026-07-25",
            "created_at": "2026-07-25T12:00:00+00:00",
            "modules": [
                {
                    "number": 2,
                    "name": "Bunny Minimize Losses",
                    "stats": [{"label": "recoveryMode", "value": "NORMAL"}],
                }
            ],
        }
        risk_state = {
            "recoveryMode": "SOFT_RECOVERY",
            "isRecoveryMode": True,
            "isPaused": False,
            "globalLossStreak": 1,
            "globalLossStreakThreshold": 2,
            "pauseTradingLossStreak": 4,
            "openPositionsCount": 3,
            "maxConcurrentPositions": 5,
            "normalRiskPercent": 1.0,
            "softRecoveryRiskPercent": 0.75,
            "recoveryModeRiskPercent": 0.5,
            "currentNormalMinRuleScore": 78,
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
            "recoveryCyclePnlUsdt": -18.05,
            "updatedAt": "2026-07-25T12:01:00+00:00",
        }

        with patch("crypto_trader.dashboard_services._system_report_date", return_value="2026-07-25"), patch(
            "crypto_trader.dashboard_services._preferred_system_checklist_snapshot", return_value=snapshot
        ), patch(
            "crypto_trader.dashboard_services._latest_system_checklist_snapshot", return_value=None
        ), patch(
            "crypto_trader.dashboard_services.get_trading_system_state", return_value=risk_state
        ), patch(
            "crypto_trader.dashboard_services.attach_previous_system_checklist_snapshot", side_effect=lambda _config, payload: payload
        ):
            payload = system_checklist_payload({})

        module = payload["modules"][0]
        values = {row["label"]: row["value"] for row in module["stats"]}
        self.assertEqual(values["recoveryMode"], "SOFT_RECOVERY")
        self.assertEqual(values["recoveryCyclePnlUsdt"], -18.05)
        for key in [
            "openPositionsCount",
            "maxConcurrentPositions",
            "slotUtilizationPercent",
            "softRecoveryMinRuleScore",
            "normalMinRiskReward",
            "normalRiskPercent",
        ]:
            self.assertIn(key, values)

    def test_slim_market_regime_snapshot_keeps_chart_indicators(self) -> None:
        snapshot = {
            "created_at": "2026-07-25T12:00:00+00:00",
            "regime": "LOW_VOLATILITY",
            "indicators": {
                "scope": "aggregate",
                "symbol": "MARKET",
                "trend_score": 61.25,
                "price_above_ema20_pct": 75.0,
                "ema20_above_ema50_pct": 60.0,
                "price_above_ema200_pct": 55.0,
                "price_above_vwap_pct": 55.0,
                "fear_greed": 27.0,
                "news_score": -0.43,
                "median_atr_pct": 0.1336,
                "funding_rate": -0.000033,
                "median_volume_ratio": 0.0494,
                "open_interest": 8586805.7685,
            },
        }

        indicators = _slim_market_regime_snapshot(snapshot)["indicators"]

        for key in [
            "trend_score",
            "price_above_ema20_pct",
            "ema20_above_ema50_pct",
            "price_above_ema200_pct",
            "price_above_vwap_pct",
            "fear_greed",
            "news_score",
            "median_atr_pct",
            "funding_rate",
            "median_volume_ratio",
            "open_interest",
        ]:
            self.assertIn(key, indicators)

    def test_profit_protection_does_not_mark_loss_close_as_partial_tp(self) -> None:
        levels = _trade_execution_profit_protection_levels(
            {
                "symbol": "BOME/USDT:USDT",
                "side": "SHORT",
                "entry_price": 0.0004989028037384,
                "initial_entry_price": 0.0004989028037384,
                "initial_stop_loss": 0.00051886,
                "stop_loss": 0.0005362,
                "take_profit": 0.0004592,
                "quantity": 81.0,
                "initial_quantity": 107.0,
                "partial_take_profit_done": True,
                "partial_take_profit_price": 0.0005171,
                "partial_take_profit_amount": 26.0,
                "partial_take_profit_pnl": -0.473127,
            },
            {"trigger_tp_progress": 0.7, "close_fraction": 0.3, "tp_extension_fraction": 0.3},
            {},
        )

        self.assertFalse(levels["partial_30"]["executed"])
        self.assertTrue(levels["partial_30"]["misclassified_loss_close"])
        self.assertLess(levels["partial_30"]["price"], 0.0004989028037384)
        self.assertEqual(levels["current_amount"], 81.0)
        self.assertAlmostEqual(levels["remaining_amount"], 56.7)

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

    def test_recovery_module_warns_for_blocked_state_with_trade_records(self) -> None:
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
        self.assertEqual(recovery["status"], "warn")

    def test_trend_approved_hold_queue_module_summarizes_queue_and_rewatch(self) -> None:
        queue_state = {
            "updated_at": "2026-07-30T12:00:00+00:00",
            "items": {
                "CAP/USDT:USDT|long": {
                    "symbol": "CAP/USDT:USDT",
                    "side": "long",
                    "status": "approved_hold",
                    "block_type": "temporary_block",
                }
            },
            "checked": [{"action": "still_blocked"}],
        }
        watchlist_state = {
            "items": {
                "ALLO/USDT:USDT|short": {
                    "symbol": "ALLO/USDT:USDT",
                    "side": "short",
                    "status": "priority_rewatch",
                }
            }
        }

        def fake_get(_config, key):
            if key == "trend_approved_hold_queue_state":
                return json.dumps(queue_state)
            if key == "trend_watchlist_state":
                return json.dumps(watchlist_state)
            return None

        with patch("crypto_trader.dashboard_services.get_journal_state", side_effect=fake_get):
            modules = system_modules_payload(
                {},
                checked_date="2026-07-12",
                checked_at_iso="2026-07-12T09:00:00+00:00",
                ai_history=[],
                replay={},
                strategy={},
                regime={},
                health={},
                risk_state={},
                row_counts={},
            )

        module = next(item for item in modules if item["name"] == "Trend Approved Hold Queue")
        self.assertEqual(module["trend_approved_hold"]["queue_count"], 1)
        self.assertEqual(module["trend_approved_hold"]["priority_rewatch_count"], 1)
        self.assertEqual(module["stats"][0]["value"], 1)

    def test_health_monitor_critical_is_guard_warning_not_module_failure(self) -> None:
        health = {
            "isCritical": True,
            "isWarning": False,
            "isPaused": True,
            "totalTrades": 5,
            "minimumTradesForEvaluation": 5,
            "reason": "Critical health threshold breached",
        }

        modules = system_modules_payload(
            {},
            checked_date="2026-07-12",
            checked_at_iso="2026-07-12T09:00:00+00:00",
            ai_history=[],
            replay={},
            strategy={},
            regime={},
            health=health,
            risk_state={},
            row_counts={},
        )

        module = next(item for item in modules if item["name"] == "Bunny Health Monitor")
        self.assertEqual(module["status"], "warn")

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

    def test_trade_execution_summary_prefers_okx_runtime_pnl(self) -> None:
        open_rows = [
            {
                "id": 7,
                "symbol": "ETH/USDT:USDT",
                "side": "SHORT",
                "entry_price": 1957.78,
                "stop_loss": 2031.2,
                "take_profit": 1869.68,
                "quantity": 1.2,
                "contract_size": 0.1,
                "pnl": 1.0,
                "snapshot_json": json.dumps(
                    {
                        "position": {
                            "contracts": 1.2,
                            "entryPrice": 1957.78,
                            "markPrice": 1934.3,
                            "unrealizedPnl": 2.8176,
                            "info": {"upl": "2.8176"},
                        }
                    }
                ),
            }
        ]

        def fake_rows(_config, *, statuses=None, **_kwargs):
            return open_rows if statuses == ["OPEN"] else []

        with patch("crypto_trader.dashboard_services.list_trade_execution_rows", side_effect=fake_rows), patch(
            "crypto_trader.dashboard_services.count_pending_orders", return_value=0
        ):
            payload = _trade_execution_summary({})

        item = payload["open_items"][0]
        self.assertEqual(item["quantity"], 1.2)
        self.assertEqual(item["mark_price"], 1934.3)
        self.assertEqual(item["pnl"], 2.8176)

    def test_trade_execution_summary_sorts_recent_closed_by_okx_close_time(self) -> None:
        closed_rows = [
            {
                "id": 2,
                "symbol": "TAO/USDT:USDT",
                "status": "LOSS",
                "closed_at": "2026-07-24T13:13:03+00:00",
                "exchange_close_history_json": json.dumps({"timestamp": 1_774_343_220_000}),
            },
            {
                "id": 9,
                "symbol": "HYPE/USDT:USDT",
                "status": "WIN",
                "closed_at": "2026-07-24T13:08:40+00:00",
                "exchange_close_history_json": json.dumps({"timestamp": 1_774_362_380_000}),
            },
        ]

        def fake_rows(_config, *, statuses=None, **_kwargs):
            return [] if statuses == ["OPEN"] else closed_rows

        with patch("crypto_trader.dashboard_services.list_trade_execution_rows", side_effect=fake_rows), patch(
            "crypto_trader.dashboard_services.count_pending_orders", return_value=0
        ):
            payload = _trade_execution_summary({})

        self.assertEqual([row["symbol"] for row in payload["recent_closed"][:2]], ["HYPE/USDT:USDT", "TAO/USDT:USDT"])
        self.assertIsNotNone(payload["recent_closed"][0]["exchange_closed_at"])

    def test_trade_execution_summary_dedupes_same_okx_close_history(self) -> None:
        duplicate_history = {
            "info": {
                "instId": "BEAT-USDT-SWAP",
                "posId": "3772320754283175936",
                "uTime": "1785047740407",
                "closeTotalPos": "0.9",
                "closeAvgPx": "3.5961222222222222",
                "realizedPnl": "-2.7630083471369007",
            },
            "symbol": "BEAT/USDT:USDT",
            "side": "short",
        }
        closed_rows = [
            {
                "id": 16,
                "symbol": "BEAT/USDT:USDT",
                "side": "SHORT",
                "status": "LOSS",
                "closed_at": "2026-07-25T09:40:02.533000+00:00",
                "exchange_close_history_json": json.dumps(duplicate_history),
            },
            {
                "id": 11,
                "symbol": "BEAT/USDT:USDT",
                "side": "SHORT",
                "status": "LOSS",
                "closed_at": "2026-07-25T09:40:02.533000+00:00",
                "exchange_close_history_json": json.dumps(duplicate_history),
            },
            {
                "id": 9,
                "symbol": "HYPE/USDT:USDT",
                "side": "SHORT",
                "status": "WIN",
                "closed_at": "2026-07-24T13:08:40.717000+00:00",
                "exchange_close_history_json": json.dumps(
                    {
                        "info": {
                            "posId": "3771387989693947904",
                            "uTime": "1784928363323",
                            "closeTotalPos": "29",
                            "closeAvgPx": "57.4579",
                            "realizedPnl": "1.942538",
                        }
                    }
                ),
            },
        ]

        def fake_rows(_config, *, statuses=None, **_kwargs):
            return [] if statuses == ["OPEN"] else closed_rows

        with patch("crypto_trader.dashboard_services.list_trade_execution_rows", side_effect=fake_rows), patch(
            "crypto_trader.dashboard_services.count_pending_orders", return_value=0
        ):
            payload = _trade_execution_summary({})

        beat_items = [item for item in payload["recent_closed"] if item["symbol"] == "BEAT/USDT:USDT"]
        total = sum(float(item["pnl"] or 0) for item in payload["recent_closed"])
        self.assertEqual(len(beat_items), 1)
        self.assertEqual(beat_items[0]["id"], 16)
        self.assertAlmostEqual(total, -0.82047)

    def test_profit_protection_prefers_okx_live_sl_tp_over_stored_values(self) -> None:
        row = {
            "side": "long",
            "entry_price": 1.1369,
            "initial_stop_loss": 1.0539,
            "stop_loss": 1.0539,
            "take_profit": 1.28,
            "quantity": 100,
            "payload_json": json.dumps(
                {
                    "position": {
                        "info": {
                            "closeOrderAlgo": [
                                {"slTriggerPx": "1.045", "tpTriggerPx": "1.2800"}
                            ]
                        }
                    }
                }
            ),
        }

        levels = _trade_execution_profit_protection_levels(row)

        self.assertEqual(levels["sl_steps"][0]["price"], 1.045)
        self.assertEqual(levels["current_sl"]["price"], 1.045)
        self.assertEqual(levels["tp_steps"][0]["price"], 1.28)
        self.assertEqual(levels["current_tp"]["price"], 1.28)

    def test_profit_protection_uses_live_remaining_quantity_after_partial(self) -> None:
        row = {
            "side": "short",
            "entry_price": 0.1544,
            "initial_stop_loss": 0.159,
            "stop_loss": 0.1539,
            "take_profit": 0.1453,
            "partial_take_profit_done": True,
            "partial_take_profit_fraction": 0.3,
            "partial_take_profit_amount": 7.32,
            "partial_take_profit_price": 0.1486,
            "partial_take_profit_original_tp": 0.1474,
            "partial_take_profit_extended_tp": 0.1453,
            "quantity": 24.4,
            "contract_size": 10,
            "payload_json": json.dumps({"position": {"contracts": 17.1, "contractSize": 10}}),
        }

        levels = _trade_execution_profit_protection_levels(row)

        self.assertEqual(levels["current_amount"], 17.1)
        self.assertEqual(levels["remaining_amount"], 17.1)
        self.assertAlmostEqual(levels["tp2"]["pnl"], 1.5561)

    def test_loss_guard_uses_initial_sl_after_stop_has_moved_positive(self) -> None:
        row = {
            "side": "short",
            "created_at": "2026-07-26T00:24:02+00:00",
            "entry_price": 0.1544,
            "initial_stop_loss": 0.159,
            "stop_loss": 0.1498,
            "take_profit": 0.1411,
            "quantity": 17.1,
            "contract_size": 10,
        }
        config = {
            "loss_guard": {
                "enabled": True,
                "effective_from": "2026-07-25T12:13:57+00:00",
                "apply_to_existing_positions": False,
                "partial_close_r": -0.8,
                "partial_close_fraction": 0.25,
            }
        }

        levels = _trade_execution_profit_protection_levels(row, config=config)

        self.assertIsNotNone(levels["loss_guard"])
        self.assertAlmostEqual(levels["loss_guard"]["partial_close_price"], 0.15808)

    def test_market_pattern_dashboard_uses_app_atlas_database(self) -> None:
        class FakeRepository:
            def __init__(self, *, db, config) -> None:
                self.db = db
                self.config = config

            def latest(self, *, limit: int = 20) -> list[dict]:
                return [{"symbol": "BTC/USDT:USDT", "timeframe": "15m"}]

            def health(self) -> dict:
                return {"collections": {"market_analysis_snapshots": 1}}

        with patch("crypto_trader.dashboard_services.load_engine_config", return_value={"engine": True}), patch(
            "crypto_trader.dashboard_services.atlas_database", return_value="APP_DB"
        ) as atlas_db, patch("crypto_trader.dashboard_services.AnalysisRepository", FakeRepository):
            from crypto_trader.dashboard_services import _market_pattern_engine_dashboard

            payload = _market_pattern_engine_dashboard({"database": {"atlas": {"database": "Bunny_Runtime_Live"}}})

        atlas_db.assert_called_once()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["latest"]["symbol"], "BTC/USDT:USDT")

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
        history_reader.assert_called_once_with(config, limit=60)
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
