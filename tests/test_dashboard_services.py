from __future__ import annotations

import unittest
from unittest.mock import patch

from crypto_trader.dashboard_services import attach_previous_system_checklist_snapshot, system_checklist_payload


class SystemChecklistPayloadTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
