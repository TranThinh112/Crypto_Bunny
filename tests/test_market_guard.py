from __future__ import annotations

import tempfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest import TestCase

from crypto_trader.config import DEFAULT_CONFIG
from crypto_trader.market_guard import _candle_alert
from crypto_trader.market_guard import market_guard_symbol_layers
from crypto_trader.storage import save_market_guard_observation


def _row(close: float, volume: float = 100.0) -> list[float]:
    return [0, close, close * 1.001, close * 0.999, close, volume]


class MarketGuardTest(TestCase):
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

    def test_detects_strong_wick_and_volume_spike(self) -> None:
        rows = [_row(100.0) for _ in range(30)]
        rows.append([0, 100.0, 101.0, 97.0, 100.2, 420.0])

        alert = _candle_alert("BTC/USDT:USDT", rows, {})

        self.assertIsNotNone(alert)
        assert alert is not None
        self.assertEqual(alert["severity"], "critical")
        self.assertEqual(alert["symbol"], "BTC/USDT:USDT")
        self.assertTrue(any("râu" in reason for reason in alert["reasons"]))
        self.assertTrue(any("volume" in reason for reason in alert["reasons"]))

    def test_ignores_quiet_candles(self) -> None:
        rows = [_row(100.0) for _ in range(31)]

        alert = _candle_alert("BTC/USDT:USDT", rows, {})

        self.assertIsNone(alert)

    def test_builds_5m_and_20m_layers_from_saved_observations(self) -> None:
        config = self._config()
        now = datetime.now(timezone.utc)
        for index in range(20):
            save_market_guard_observation(
                config,
                {
                    "created_at": (now + timedelta(minutes=index)).isoformat(),
                    "observed_at": (now + timedelta(minutes=index)).isoformat(),
                    "symbol": "BTC/USDT:USDT",
                    "last": 100 + index,
                    "move_pct": 0.2 if index < 18 else 1.1,
                    "candle_range_pct": 0.4,
                    "wick_pct": 0.1 if index < 18 else 0.7,
                    "wick_body_ratio": 1.0 if index < 18 else 3.0,
                    "volume_ratio": 1.1 if index < 18 else 3.2,
                    "severity": "normal" if index < 18 else "critical",
                    "reasons": [] if index < 18 else ["volume 3.20x"],
                },
            )

        layers = market_guard_symbol_layers(config, ["BTC/USDT:USDT"])

        self.assertEqual(layers["BTC/USDT:USDT"]["layer_5m"]["sample_count"], 5)
        self.assertEqual(layers["BTC/USDT:USDT"]["layer_20m"]["sample_count"], 20)
        self.assertEqual(layers["BTC/USDT:USDT"]["layer_5m"]["action"], "avoid_new_entry")
