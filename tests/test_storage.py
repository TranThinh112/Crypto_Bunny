from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from unittest import TestCase

from crypto_trader.atlas_mirror import atlas_database
from crypto_trader.models import TradeCandidate
from crypto_trader.storage import (
    compact_market_scan_observations,
    prune_decision_history,
    prune_market_scan_observations,
    save_market_scan_observations,
)


class StorageTest(TestCase):
    def _config(self, tmpdir: str) -> dict:
        return {
            "_atlas_test_mode": True,
            "_atlas_test_database": f"storage_test_{abs(hash(tmpdir))}",
            "market_scan_memory": {
                "keep_hours": 72,
                "max_rows_per_symbol_timeframe": 2,
                "max_json_bytes": 2000,
            },
        }

    def _candidate(self) -> TradeCandidate:
        huge_patterns = [{"name": f"pattern-{index}", "raw": "x" * 1000} for index in range(50)]
        return TradeCandidate(
            symbol="BTC/USDT:USDT",
            base="BTC",
            side="long",
            confidence=91.2,
            entry=62000,
            stop_loss=61000,
            take_profit=64000,
            risk_reward=2.0,
            order_usdt=50,
            quantity=0.001,
            spread_pct=0.01,
            news_score=0.2,
            news_count=2,
            indicator_summary={
                "timeframe": "1m",
                "last": 62000,
                "rsi": 55,
                "trend": "up",
                "raw_candles": [["x" * 500] * 6 for _ in range(200)],
                "candlestick_patterns": {
                    "patterns": huge_patterns,
                    "direction": "bullish",
                    "signal_summary": "strong",
                },
            },
            higher_timeframes={
                "5m": {
                    "trend": "up",
                    "rsi": 56,
                    "raw_candles": [["y" * 500] * 6 for _ in range(200)],
                    "candlestick_patterns": {
                        "patterns": huge_patterns,
                        "bullish_score": 88,
                    },
                }
            },
            reasons=["reason " + ("a" * 1000) for _ in range(10)],
            warnings=["warning " + ("b" * 1000) for _ in range(10)],
            win_probability_pct=84.5,
        )

    def test_prune_market_scan_observations_keeps_recent_rows_per_symbol_timeframe(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config = self._config(tmpdir)
            now = datetime.now(timezone.utc)
            collection = atlas_database(config)["market_scan_observations"]
            for index in range(4):
                created_at = (now - timedelta(minutes=index)).isoformat()
                collection.replace_one(
                    {"id": index + 1},
                    {
                        "_id": index + 1,
                        "id": index + 1,
                        "created_at": created_at,
                        "source": "test",
                        "symbol": "BTC/USDT:USDT",
                        "side": "long",
                        "timeframe": "1m",
                        "confidence": 90,
                        "win_probability_pct": 80,
                        "risk_reward": 1.5,
                        "score": 80,
                        "indicator_json": "{}",
                        "payload_json": "{}",
                    },
                    upsert=True,
                )
            collection.replace_one(
                {"id": 5},
                {
                    "_id": 5,
                    "id": 5,
                    "created_at": (now - timedelta(hours=100)).isoformat(),
                    "source": "test",
                    "symbol": "ETH/USDT:USDT",
                    "side": "long",
                    "timeframe": "5m",
                    "confidence": 90,
                    "win_probability_pct": 80,
                    "risk_reward": 1.5,
                    "score": 80,
                    "indicator_json": "{}",
                    "payload_json": "{}",
                },
                upsert=True,
            )

            result = prune_market_scan_observations(config)
            rows = list(collection.find({}, {"_id": 0}).sort([("created_at", -1)]))

        self.assertEqual(result["deleted_old"], 1)
        self.assertEqual(result["deleted_over_limit"], 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual({(row["symbol"], row["timeframe"]) for row in rows}, {("BTC/USDT:USDT", "1m")})

    def test_save_market_scan_observations_stores_compact_payloads(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config = self._config(tmpdir)
            saved = save_market_scan_observations(config, [self._candidate()], source="scan", limit=10)
            rows = list(atlas_database(config)["market_scan_observations"].find({}, {"_id": 0}).sort([("timeframe", 1)]))

        self.assertEqual(saved, 2)
        self.assertEqual({row["timeframe"] for row in rows}, {"1m", "5m"})
        for row in rows:
            self.assertLessEqual(len(row["indicator_json"].encode("utf-8")), 2000)
            self.assertLessEqual(len(row["payload_json"].encode("utf-8")), 2000)
            self.assertNotIn("raw_candles", row["indicator_json"])
            self.assertNotIn("raw_candles", row["payload_json"])
            self.assertNotIn('"candidate"', row["payload_json"])

    def test_compact_market_scan_observations_rewrites_existing_large_rows(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config = self._config(tmpdir)
            now = datetime.now(timezone.utc).isoformat()
            huge_json = '{"symbol":"BTC/USDT:USDT","candidate":"' + ("x" * 10000) + '","raw_candles":"' + ("y" * 10000) + '"}'
            atlas_database(config)["market_scan_observations"].replace_one(
                {"id": 1},
                {
                    "_id": 1,
                    "id": 1,
                    "created_at": now,
                    "source": "legacy",
                    "symbol": "BTC/USDT:USDT",
                    "side": "long",
                    "timeframe": "1m",
                    "confidence": 90,
                    "win_probability_pct": 80,
                    "risk_reward": 2,
                    "score": 80,
                    "indicator_json": huge_json,
                    "payload_json": huge_json,
                },
                upsert=True,
            )

            result = compact_market_scan_observations(config)
            row = atlas_database(config)["market_scan_observations"].find_one({}, {"_id": 0})

        self.assertEqual(result["compacted"], 1)
        self.assertLessEqual(len(row["indicator_json"].encode("utf-8")), 2000)
        self.assertLessEqual(len(row["payload_json"].encode("utf-8")), 2000)
        self.assertNotIn("raw_candles", row["payload_json"])

    def test_prune_decision_history_keeps_latest_rows(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config = self._config(tmpdir)
            now = datetime.now(timezone.utc)
            collection = atlas_database(config)["decisions"]
            for index in range(6):
                created_at = (now - timedelta(minutes=index)).isoformat()
                collection.replace_one(
                    {"id": index + 1},
                    {
                        "_id": index + 1,
                        "id": index + 1,
                        "created_at": created_at,
                        "action": "HOLD",
                        "selected_symbol": None,
                        "selected_side": None,
                        "selected_win_probability_pct": None,
                        "payload_json": '{"blob":"' + ("x" * 1000) + '"}',
                    },
                    upsert=True,
                )

            result = prune_decision_history(config, keep_hours=72, max_rows=3)
            count = atlas_database(config)["decisions"].count_documents({})

        self.assertEqual(result["deleted_over_limit"], 3)
        self.assertEqual(count, 3)
