from __future__ import annotations

import tempfile
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

from crypto_trader.config import load_config
from crypto_trader.codex_features import close_trade_execution, record_trade_candidates, record_trade_execution, try_slot_refill
from crypto_trader.models import TradeCandidate
from crypto_trader.storage import connect_state_db, save_market_scan_observations, set_journal_state
from crypto_trader.ui import _telegram_action_response, create_app


class UiTest(TestCase):
    def _candidate(self) -> TradeCandidate:
        candidate = TradeCandidate(
            symbol="BTC/USDT:USDT",
            base="BTC",
            side="long",
            confidence=82.0,
            win_probability_pct=84.5,
            entry=100.0,
            stop_loss=97.5,
            take_profit=103.75,
            risk_reward=1.5,
            order_usdt=20.0,
            quantity=1.0,
            spread_pct=0.01,
            news_score=0.0,
            news_count=1,
            take_profit_pct=75,
            stop_loss_pct=50,
        )
        candidate.indicator_summary = {
            "timeframe": "1m",
            "trend": "up",
            "candlestick_patterns": {"patterns": ["bullish_marubozu"], "bullish_score": 1.4},
        }
        candidate.higher_timeframes = {
            "4h": {"trend": "up", "candlestick_patterns": {"patterns": ["morning_star"], "bullish_score": 3.5}},
        }
        return candidate

    def _feature_config(self, tmpdir: str, *, max_positions: int = 2) -> tuple[Path, dict]:
        config_path = Path(tmpdir) / "config.yaml"
        config_path.write_text(
            "mode: dry_run\n"
            f"state_db_path: {Path(tmpdir, 'state.sqlite').as_posix()}\n"
            "ai:\n"
            "  okx:\n"
            "    provider: local_policy\n"
            "trading_risk:\n"
            f"  max_concurrent_positions: {max_positions}\n"
            "  normal_min_rule_score: 80\n"
            "  normal_min_gpt_confidence: 80\n",
            encoding="utf-8",
        )
        return config_path, load_config(config_path)

    def test_config_endpoint_returns_strategy_summary(self) -> None:
        client = TestClient(create_app("config.example.yaml"))

        response = client.get("/api/config")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["mode"], "dry_run")
        self.assertIn("BTC/USDT:USDT", payload["symbols"])
        self.assertEqual(payload["order_margin_usdt"], 2.0)
        self.assertEqual(payload["order_usdt"], 30.0)
        self.assertEqual(payload["position_sizing"]["max_margin_usdt"], 50)
        self.assertEqual(payload["universe"]["mode"], "top_volume_24h")
        self.assertEqual(payload["universe"]["max_symbols"], 50)
        self.assertEqual(payload["ai"]["internal"]["model"], "gpt-5.4-mini")
        self.assertEqual(payload["ai"]["okx"]["model"], "gpt-5.5")

    def test_prices_endpoint_soft_fails_on_exchange_error(self) -> None:
        class FailingExchange:
            def load_markets(self) -> None:
                return None

            def fetch_ticker(self, symbol: str) -> dict[str, object]:
                raise RuntimeError("Too Many Requests")

        client = TestClient(create_app("config.example.yaml"))

        with patch("crypto_trader.ui.create_exchange", return_value=FailingExchange()):
            response = client.get("/api/prices")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["cached"])
        self.assertTrue(payload["warnings"])
        self.assertEqual(payload["prices"][0]["last"], None)
        self.assertIn("price fetch failed", payload["prices"][0]["error"])

    def test_prices_endpoint_uses_fresh_cache(self) -> None:
        app = create_app("config.example.yaml")
        app.state.price_cache = {
            "created_at": datetime.now(timezone.utc),
            "payload": {
                "created_at": "2026-07-01T00:00:00+00:00",
                "served_at": "2026-07-01T00:00:00+00:00",
                "focus": {"symbol": "BTC/USDT:USDT", "side": "long", "status": "selected"},
                "prices": [
                    {
                        "symbol": "BTC/USDT:USDT",
                        "last": 100000,
                        "bid": 99999,
                        "ask": 100001,
                        "percentage_24h": 1.2,
                        "timestamp": 1782864000000,
                        "datetime": "2026-07-01T00:00:00.000Z",
                        "stale": False,
                    }
                ],
                "warnings": [],
                "cached": False,
            },
        }
        client = TestClient(app)

        with patch("crypto_trader.ui.create_exchange", side_effect=AssertionError("exchange should not be called")):
            response = client.get("/api/prices")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["cached"])
        self.assertEqual(payload["prices"][0]["last"], 100000)

    def test_leverage_endpoint_limits_values_to_5_25x(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                "exchange:\n"
                "  leverage: 10\n"
                "position_sizing:\n"
                "  base_margin_usdt: 2\n"
                "risk:\n"
                "  order_usdt: 20\n",
                encoding="utf-8",
            )
            client = TestClient(create_app(str(config_path)))

            low_response = client.post("/api/config/leverage", json={"leverage": 4})
            high_response = client.post("/api/config/leverage", json={"leverage": 26})
            ok_response = client.post("/api/config/leverage", json={"leverage": 25})
            saved = load_config(config_path)

        self.assertEqual(low_response.status_code, 400)
        self.assertEqual(high_response.status_code, 400)
        self.assertEqual(ok_response.status_code, 200)
        self.assertEqual(ok_response.json()["exchange"]["leverage"], 25)
        self.assertEqual(saved["risk"]["order_usdt"], 50)

    def test_order_usdt_endpoint_limits_and_persists_base_margin(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                "exchange:\n"
                "  leverage: 25\n"
                "position_sizing:\n"
                "  base_margin_usdt: 2\n"
                "  max_margin_usdt: 20\n",
                encoding="utf-8",
            )
            client = TestClient(create_app(str(config_path)))

            low_response = client.post("/api/config/order-usdt", json={"margin_usdt": 0.5})
            high_response = client.post("/api/config/order-usdt", json={"margin_usdt": 25})
            ok_response = client.post("/api/config/order-usdt", json={"margin_usdt": 5})

        self.assertEqual(low_response.status_code, 400)
        self.assertEqual(high_response.status_code, 400)
        self.assertEqual(ok_response.status_code, 200)
        payload = ok_response.json()
        self.assertEqual(payload["position_sizing"]["base_margin_usdt"], 5)
        self.assertEqual(payload["estimated_notional_usdt"], 125)

    def test_trading_risk_state_endpoint_exposes_recovery_snapshot(self) -> None:
        client = TestClient(create_app("config.example.yaml"))

        response = client.get("/api/trading-risk/state")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("mechanismName", payload)
        self.assertIn("isRecoveryMode", payload)
        self.assertIn("maxConcurrentPositions", payload)

    def test_prompt_build_endpoint_returns_prompt_metadata(self) -> None:
        client = TestClient(create_app("config.example.yaml"))

        response = client.post(
            "/api/prompt/build",
            json={
                "instructionKey": "final-decision",
                "marketPromptDto": {
                    "scanTime": "2026-07-04T00:00:00+00:00",
                    "marketSnapshot": {"symbol": "BTC/USDT:USDT"},
                    "candidates": [{"symbol": "BTC/USDT:USDT", "side": "long"}],
                    "tradingSystemState": {"isRecoveryMode": False},
                    "tradingHealthState": {"isWarning": False},
                    "openPositions": [],
                    "recentTrades": [],
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("promptVersion", payload)
        self.assertIn("promptHash", payload)
        self.assertTrue(payload["messages"])

    def test_telegram_leverage_action_persists_config(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                "exchange:\n"
                "  leverage: 10\n"
                "  leverage_presets:\n"
                "  - 5\n"
                "  - 10\n"
                "  - 15\n"
                "  - 20\n"
                "  - 25\n"
                "position_sizing:\n"
                "  base_margin_usdt: 3\n"
                "risk:\n"
                "  order_usdt: 30\n",
                encoding="utf-8",
            )
            config = load_config(config_path)

            updated, message, keyboard = _telegram_action_response(config, "set_leverage:20", config_path)

        self.assertEqual(updated["exchange"]["leverage"], 20)
        self.assertEqual(updated["risk"]["order_usdt"], 60)
        self.assertIn("20x", message)
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
            if button.get("callback_data", "").startswith("set_leverage:")
        ]
        self.assertIn("set_leverage:20", callbacks)

    def test_telegram_dashboard_has_bot_ui_actions(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                "exchange:\n"
                "  leverage: 15\n"
                "position_sizing:\n"
                "  base_margin_usdt: 2\n"
                "  max_margin_usdt: 20\n",
                encoding="utf-8",
            )
            config = load_config(config_path)

            _, message, keyboard = _telegram_action_response(config, "view_menu", config_path)

        self.assertIn("Bảng điều khiển Telegram", message)
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
        ]
        self.assertIn("scan_now", callbacks)
        self.assertIn("view_guard", callbacks)
        self.assertIn("view_memory", callbacks)
        self.assertIn("view_undecided_lc", callbacks)

    def test_telegram_undecided_lc_action_formats_pipeline_state(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                f"state_db_path: {Path(tmpdir, 'state.sqlite').as_posix()}\n",
                encoding="utf-8",
            )
            config = load_config(config_path)
            set_journal_state(
                config,
                "lc_internal_pipeline_state",
                json.dumps(
                    {
                        "undecided": [
                            {
                                "symbol": "LIT/USDT:USDT",
                                "side": "long",
                                "first_seen_at": "2026-07-06T00:00:00+00:00",
                                "last_seen_at": "2026-07-06T03:00:00+00:00",
                                "state": "CHUA_DUYET",
                                "source_slot": "2h",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
            )

            _, message, keyboard = _telegram_action_response(config, "view_undecided_lc", config_path)

        self.assertIn("Chưa Duyệt", message)
        self.assertIn("1. LIT/USDT:USDT | LONG", message)
        self.assertIn("2h", message)
        self.assertIn("sống", message)
        self.assertIsNone(keyboard)

    def test_lc_pipeline_endpoint_returns_dashboard_state(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                f"state_db_path: {Path(tmpdir, 'state.sqlite').as_posix()}\n",
                encoding="utf-8",
            )
            config = load_config(config_path)
            set_journal_state(
                config,
                "lc_internal_pipeline_state",
                json.dumps(
                    {
                        "day_key": "2026-07-06",
                        "undecided": [
                            {
                                "symbol": "LIT/USDT:USDT",
                                "side": "long",
                                "first_seen_at": "2026-07-06T00:00:00+00:00",
                                "last_seen_at": "2026-07-06T03:00:00+00:00",
                                "state": "CHUA_DUYET",
                            }
                        ],
                        "internal_lc": [],
                    },
                    ensure_ascii=False,
                ),
            )
            client = TestClient(create_app(config_path))

            response = client.get("/api/lc-pipeline")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["counts"]["undecided"], 1)
        self.assertEqual(payload["undecided"][0]["symbol"], "LIT/USDT:USDT")
        self.assertIn("age_label", payload["undecided"][0])

    def test_market_scan_memory_endpoint_returns_recent_observations(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                f"state_db_path: {Path(tmpdir, 'state.sqlite').as_posix()}\n",
                encoding="utf-8",
            )
            config = load_config(config_path)
            save_market_scan_observations(config, [self._candidate()], source="test-scan", limit=10)
            client = TestClient(create_app(config_path))

            response = client.get("/api/market-scan-memory?symbol=BTC/USDT:USDT&timeframe=1m,4h")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("BTC/USDT:USDT", payload["symbols"])
        self.assertIn("1m", payload["memory"]["BTC/USDT:USDT"])
        self.assertIn("4h", payload["memory"]["BTC/USDT:USDT"])

    def test_telegram_memory_action_formats_recent_scan_memory(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                "mode: dry_run\n"
                f"state_db_path: {Path(tmpdir, 'state.sqlite').as_posix()}\n",
                encoding="utf-8",
            )
            config = load_config(config_path)
            save_market_scan_observations(config, [self._candidate()], source="test-scan", limit=10)

            _, message, keyboard = _telegram_action_response(config, "view_memory", config_path)

        self.assertIn("Scan memory", message)
        self.assertIn("BTC/USDT:USDT", message)
        self.assertIsNone(keyboard)

    def test_rejected_trade_execution_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            _config_path, config = self._feature_config(tmpdir)
            candidate = self._candidate()
            candidate.confidence = 60
            candidate.rule_score = 60

            record = record_trade_execution(config, candidate)

            with connect_state_db(config) as connection:
                rows = connection.execute("SELECT * FROM trade_executions").fetchall()

        self.assertEqual(record["status"], "REJECTED")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "REJECTED")
        self.assertTrue(rows[0]["reject_reason"])

    def test_slot_refill_uses_requested_free_slot(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            _config_path, config = self._feature_config(tmpdir, max_positions=2)
            candidate = self._candidate()
            candidate.confidence = 95
            candidate.rule_score = 95
            candidate.risk_reward = 3.0
            record_trade_candidates(config, [candidate])

            result = try_slot_refill(config, 2)

        self.assertTrue(result["refilled"])
        self.assertEqual(result["tradeExecution"]["position_slot"], 2)

    def test_replay_stats_endpoint_reports_performance_metrics(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            config_path, config = self._feature_config(tmpdir)
            candidate = self._candidate()
            candidate.confidence = 95
            candidate.rule_score = 95
            candidate.risk_reward = 3.0
            execution = record_trade_execution(config, candidate)
            close_trade_execution(config, int(execution["id"]), "WIN", 12.5)
            client = TestClient(create_app(config_path))

            replay_response = client.post("/api/replay/run", json={"tradeExecutionId": execution["id"]})
            stats_response = client.get("/api/replay/stats")

        self.assertEqual(replay_response.status_code, 200)
        self.assertEqual(stats_response.status_code, 200)
        stats = stats_response.json()
        self.assertEqual(stats["replayCount"], 1)
        self.assertIn("replayWinRate", stats)
        self.assertIn("replayProfitFactor", stats)
        self.assertIn("replayDrawdown", stats)
