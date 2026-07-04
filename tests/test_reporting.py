from __future__ import annotations

import tempfile
from copy import deepcopy
from unittest import TestCase

from crypto_trader.config import DEFAULT_CONFIG
from crypto_trader.reporting import format_scan_message


class ReportingTest(TestCase):
    def _config(self) -> dict:
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        config = deepcopy(DEFAULT_CONFIG)
        config["_config_dir"] = self.tmpdir.name
        config["state_db_path"] = "state.sqlite"
        return config

    def tearDown(self) -> None:
        tmpdir = getattr(self, "tmpdir", None)
        if tmpdir:
            tmpdir.cleanup()

    def test_scan_message_uses_daily_sequence_and_source_labels(self) -> None:
        config = self._config()
        payload = {
            "candidates": [
                {
                    "symbol": "BTC/USDT:USDT",
                    "side": "long",
                    "scan_source": "new_scan",
                    "win_probability_pct": 76.5,
                    "confidence": 80,
                    "risk_reward": 1.7,
                },
                {
                    "symbol": "ETH/USDT:USDT",
                    "side": "short",
                    "scan_source": "old_rescan",
                    "win_probability_pct": 74.1,
                    "confidence": 78,
                    "risk_reward": 1.6,
                },
            ]
        }
        status = {"last_result": "no_order", "mode": "demo", "action": "hold"}

        first = format_scan_message(config, payload, status)
        second = format_scan_message(config, payload, status)

        self.assertIn("SC #1", first)
        self.assertIn("SC #2", second)
        self.assertIn("[🆕 mới]", first)
        self.assertIn("[🔁 cũ]", first)
        self.assertIn("Kết quả", first)
