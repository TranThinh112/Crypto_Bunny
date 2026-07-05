from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

from crypto_trader.models import TradeCandidate
from crypto_trader.storage import (
    compact_market_scan_observations,
    connect_state_db,
    prune_decision_history,
    prune_market_scan_observations,
    save_market_scan_observations,
)


class StorageTest(TestCase):
    def _config(self, tmpdir: str) -> dict:
        return {
            "state_db_path": str(Path(tmpdir) / "state.sqlite"),
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
            with connect_state_db(config) as connection:
                for index in range(4):
                    created_at = (now - timedelta(minutes=index)).isoformat()
                    connection.execute(
                        """
                        INSERT INTO market_scan_observations (
                            created_at, source, symbol, side, timeframe,
                            confidence, win_probability_pct, risk_reward, score,
                            indicator_json, payload_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            created_at,
                            "test",
                            "BTC/USDT:USDT",
                            "long",
                            "1m",
                            90,
                            80,
                            1.5,
                            80,
                            "{}",
                            "{}",
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO market_scan_observations (
                        created_at, source, symbol, side, timeframe,
                        confidence, win_probability_pct, risk_reward, score,
                        indicator_json, payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (now - timedelta(hours=100)).isoformat(),
                        "test",
                        "ETH/USDT:USDT",
                        "long",
                        "5m",
                        90,
                        80,
                        1.5,
                        80,
                        "{}",
                        "{}",
                    ),
                )
                connection.commit()

            result = prune_market_scan_observations(config)

            with connect_state_db(config) as connection:
                rows = connection.execute(
                    "SELECT symbol, timeframe FROM market_scan_observations ORDER BY created_at DESC"
                ).fetchall()

        self.assertEqual(result["deleted_old"], 1)
        self.assertEqual(result["deleted_over_limit"], 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual({(row["symbol"], row["timeframe"]) for row in rows}, {("BTC/USDT:USDT", "1m")})

    def test_save_market_scan_observations_stores_compact_payloads(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config = self._config(tmpdir)
            saved = save_market_scan_observations(config, [self._candidate()], source="scan", limit=10)

            with connect_state_db(config) as connection:
                rows = connection.execute(
                    "SELECT timeframe, indicator_json, payload_json FROM market_scan_observations ORDER BY timeframe"
                ).fetchall()

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
            with connect_state_db(config) as connection:
                connection.execute(
                    """
                    INSERT INTO market_scan_observations (
                        created_at, source, symbol, side, timeframe,
                        confidence, win_probability_pct, risk_reward, score,
                        indicator_json, payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (now, "legacy", "BTC/USDT:USDT", "long", "1m", 90, 80, 2, 80, huge_json, huge_json),
                )
                connection.commit()

            result = compact_market_scan_observations(config)
            with connect_state_db(config) as connection:
                row = connection.execute(
                    "SELECT indicator_json, payload_json FROM market_scan_observations"
                ).fetchone()

        self.assertEqual(result["compacted"], 1)
        self.assertLessEqual(len(row["indicator_json"].encode("utf-8")), 2000)
        self.assertLessEqual(len(row["payload_json"].encode("utf-8")), 2000)
        self.assertNotIn("raw_candles", row["payload_json"])

    def test_prune_decision_history_keeps_latest_rows(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config = self._config(tmpdir)
            now = datetime.now(timezone.utc)
            with connect_state_db(config) as connection:
                for index in range(6):
                    created_at = (now - timedelta(minutes=index)).isoformat()
                    connection.execute(
                        """
                        INSERT INTO decisions (
                            created_at, action, selected_symbol, selected_side,
                            selected_win_probability_pct, payload_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (created_at, "HOLD", None, None, None, '{"blob":"' + ("x" * 1000) + '"}'),
                    )
                connection.commit()

            result = prune_decision_history(config, keep_hours=72, max_rows=3)
            with connect_state_db(config) as connection:
                count = connection.execute("SELECT COUNT(*) AS count FROM decisions").fetchone()["count"]

        self.assertEqual(result["deleted_over_limit"], 3)
        self.assertEqual(count, 3)
