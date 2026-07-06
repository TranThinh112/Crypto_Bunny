from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import project_path
from .models import Decision, TradeCandidate, to_jsonable


ACTIVE_PENDING_STATUSES = ("LC_OKX", "WAIT_SLOT", "OPEN")
DEFAULT_MARKET_SCAN_MAX_JSON_BYTES = 8000
DEPRECATED_JOURNAL_STATE_KEYS = {
    "ai_internal_market_scan_latest",
}
DASHBOARD_SNAPSHOT_PREFIX = "dashboard_snapshot:"


def state_db_path(config: dict[str, Any]) -> Path:
    return project_path(config, config.get("state_db_path", "data/bot_state.sqlite"))


def _connect(config: dict[str, Any]) -> sqlite3.Connection:
    path = state_db_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    _ensure_schema(connection)
    return connection


@contextmanager
def connect_state_db(config: dict[str, Any]) -> Any:
    connection = _connect(config)
    try:
        yield connection
    finally:
        connection.close()


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            action TEXT NOT NULL,
            selected_symbol TEXT,
            selected_side TEXT,
            selected_win_probability_pct REAL,
            payload_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT NOT NULL,
            symbol TEXT NOT NULL,
            base TEXT NOT NULL,
            side TEXT NOT NULL,
            entry REAL NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit REAL NOT NULL,
            quantity REAL,
            order_usdt REAL NOT NULL,
            confidence REAL NOT NULL,
            win_probability_pct REAL,
            risk_reward REAL NOT NULL,
            leverage REAL NOT NULL,
            close_price REAL,
            close_reason TEXT,
            pnl_pct REAL,
            payload_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL,
            symbol TEXT NOT NULL,
            base TEXT NOT NULL,
            side TEXT NOT NULL,
            exchange_order_id TEXT,
            entry REAL NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit REAL NOT NULL,
            quantity REAL,
            order_usdt REAL NOT NULL,
            confidence REAL NOT NULL,
            win_probability_pct REAL,
            risk_reward REAL NOT NULL,
            payload_json TEXT NOT NULL,
            close_reason TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS journal_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_memory (
            key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT,
            opened_at TEXT,
            closed_at TEXT,
            pnl_usdt REAL,
            pnl_pct REAL,
            outcome TEXT NOT NULL,
            source TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS market_guard_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            last REAL,
            move_pct REAL,
            candle_range_pct REAL,
            wick_pct REAL,
            wick_body_ratio REAL,
            volume_ratio REAL,
            severity TEXT NOT NULL,
            alert_reasons_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS market_scan_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            source TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            timeframe TEXT,
            confidence REAL NOT NULL,
            win_probability_pct REAL,
            risk_reward REAL NOT NULL,
            score REAL NOT NULL,
            indicator_json TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_market_scan_observed
        ON market_scan_observations(created_at, symbol)
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_market_guard_symbol_observed
        ON market_guard_observations(symbol, observed_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_market_guard_observed
        ON market_guard_observations(observed_at)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_trade_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            symbol TEXT,
            timeframe TEXT,
            decision TEXT NOT NULL,
            confidence REAL,
            rule_score REAL,
            side TEXT NOT NULL,
            entry_price REAL,
            stop_loss REAL,
            take_profit1 REAL,
            take_profit2 REAL,
            risk_reward REAL,
            funding_rate REAL,
            open_interest_change REAL,
            rsi REAL,
            macd_signal REAL,
            trend TEXT,
            volume_change REAL,
            news_score REAL,
            reason_json TEXT NOT NULL,
            raw_prompt TEXT,
            raw_response TEXT,
            order_id TEXT,
            trade_status TEXT,
            pnl REAL,
            closed_at TEXT,
            prompt_version TEXT,
            prompt_hash TEXT,
            model_name TEXT,
            model_version TEXT,
            strategy_version TEXT,
            validator_version TEXT,
            recovery_version TEXT,
            health_version TEXT,
            experiment_name TEXT,
            market_regime TEXT,
            regime_confidence REAL,
            snapshot_json TEXT,
            payload_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ai_trade_decisions_created
        ON ai_trade_decisions(created_at DESC)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            position_slot INTEGER,
            parent_position_id INTEGER,
            side TEXT NOT NULL,
            entry_price REAL NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit REAL NOT NULL,
            risk_reward REAL NOT NULL,
            risk_percent REAL NOT NULL,
            rule_score REAL,
            gpt_confidence REAL,
            status TEXT NOT NULL,
            pnl REAL,
            reject_reason TEXT,
            closed_at TEXT,
            payload_json TEXT NOT NULL,
            market_regime TEXT,
            regime_confidence REAL,
            strategy_version TEXT,
            rule_engine_version TEXT,
            validator_version TEXT,
            recovery_version TEXT,
            health_version TEXT,
            prompt_version TEXT,
            prompt_hash TEXT,
            model_name TEXT,
            model_version TEXT,
            system_version TEXT,
            decision_engine_version TEXT,
            bunny_version TEXT,
            health_monitor_version TEXT,
            slot_refill_version TEXT,
            experiment_name TEXT,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            latency_ms REAL,
            snapshot_json TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trade_executions_status_created
        ON trade_executions(status, created_at DESC)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS trading_system_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            mechanism_name TEXT NOT NULL,
            is_recovery_mode INTEGER NOT NULL,
            global_loss_streak INTEGER NOT NULL,
            is_paused INTEGER NOT NULL,
            paused_until TEXT,
            current_normal_min_rule_score REAL NOT NULL,
            current_normal_min_gpt_confidence REAL NOT NULL,
            updated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS trading_health_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            mechanism_name TEXT NOT NULL,
            is_healthy INTEGER NOT NULL,
            is_warning INTEGER NOT NULL,
            is_critical INTEGER NOT NULL,
            total_trades INTEGER NOT NULL,
            win_count INTEGER NOT NULL,
            loss_count INTEGER NOT NULL,
            breakeven_count INTEGER NOT NULL,
            win_rate REAL NOT NULL,
            gross_profit REAL NOT NULL,
            gross_loss REAL NOT NULL,
            profit_factor REAL NOT NULL,
            total_pnl REAL NOT NULL,
            max_drawdown_percent REAL NOT NULL,
            risk_multiplier REAL NOT NULL,
            score_adjustment REAL NOT NULL,
            confidence_adjustment REAL NOT NULL,
            is_paused INTEGER NOT NULL,
            paused_until TEXT,
            reason TEXT,
            updated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            rule_score REAL,
            gpt_confidence REAL,
            risk_reward REAL NOT NULL,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            is_used INTEGER NOT NULL DEFAULT 0,
            used_at TEXT,
            payload_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trade_candidates_recent
        ON trade_candidates(created_at DESC, is_used, rule_score DESC)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS market_regime_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            regime TEXT NOT NULL,
            confidence REAL NOT NULL,
            indicators_json TEXT NOT NULL,
            reason TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_market_regime_created
        ON market_regime_history(created_at DESC)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS strategy_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL,
            is_active INTEGER NOT NULL,
            traffic_percent REAL NOT NULL DEFAULT 100,
            indicators_json TEXT NOT NULL,
            rules_json TEXT NOT NULL,
            risk_config_json TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS prompt_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version TEXT NOT NULL UNIQUE,
            hash TEXT NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL,
            is_active INTEGER NOT NULL,
            files_json TEXT NOT NULL,
            prompt_hash TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_model_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_name TEXT NOT NULL,
            model_version TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            prompt_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_experiments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            prompt_version TEXT NOT NULL,
            traffic_percent REAL NOT NULL,
            enabled INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS prompt_metrics (
            prompt_version TEXT PRIMARY KEY,
            prompt_hash TEXT NOT NULL,
            total_requests INTEGER NOT NULL,
            average_prompt_tokens REAL NOT NULL,
            average_completion_tokens REAL NOT NULL,
            average_latency REAL NOT NULL,
            estimated_cached_tokens REAL NOT NULL,
            estimated_dynamic_tokens REAL NOT NULL,
            cache_hit_percent REAL NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS replay_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_execution_id INTEGER NOT NULL,
            prompt_version TEXT,
            strategy_version TEXT,
            model_version TEXT,
            old_decision TEXT NOT NULL,
            new_decision TEXT NOT NULL,
            old_confidence REAL,
            new_confidence REAL,
            latency REAL,
            replay_at TEXT NOT NULL,
            decision_changed INTEGER NOT NULL,
            confidence_changed INTEGER NOT NULL,
            reason_changed INTEGER NOT NULL,
            old_reason_json TEXT NOT NULL,
            new_reason_json TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_replay_history_trade
        ON replay_history(trade_execution_id, replay_at DESC)
        """
    )
    _ensure_column(connection, "pending_orders", "journal_id", "INTEGER")
    connection.commit()


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {str(row[1]) for row in rows}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _is_deprecated_journal_state_key(key: str) -> bool:
    return str(key) in DEPRECATED_JOURNAL_STATE_KEYS


def delete_journal_state(config: dict[str, Any], key: str) -> None:
    with _connect(config) as connection:
        connection.execute("DELETE FROM journal_state WHERE key = ?", (key,))
        connection.commit()


def delete_journal_state_prefix(config: dict[str, Any], prefix: str) -> None:
    with _connect(config) as connection:
        connection.execute("DELETE FROM journal_state WHERE key LIKE ?", (f"{prefix}%",))
        connection.commit()


def clear_dashboard_snapshot_cache(config: dict[str, Any]) -> None:
    delete_journal_state_prefix(config, DASHBOARD_SNAPSHOT_PREFIX)


def purge_deprecated_journal_state(config: dict[str, Any]) -> list[str]:
    removed: list[str] = []
    with _connect(config) as connection:
        for key in sorted(DEPRECATED_JOURNAL_STATE_KEYS):
            cursor = connection.execute("DELETE FROM journal_state WHERE key = ?", (key,))
            if int(cursor.rowcount or 0) > 0:
                removed.append(key)
        connection.commit()
    return removed


def get_journal_state(config: dict[str, Any], key: str) -> str | None:
    if _is_deprecated_journal_state_key(key):
        delete_journal_state(config, key)
        return None
    with _connect(config) as connection:
        row = connection.execute("SELECT value FROM journal_state WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def set_journal_state(config: dict[str, Any], key: str, value: str) -> None:
    if _is_deprecated_journal_state_key(key):
        delete_journal_state(config, key)
        return
    now = datetime.now(timezone.utc).isoformat()
    with _connect(config) as connection:
        connection.execute(
            """
            INSERT INTO journal_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, now),
        )
        connection.commit()


def next_global_counter(config: dict[str, Any], name: str) -> int:
    key = f"counter:{name}"
    with _connect(config) as connection:
        row = connection.execute("SELECT value FROM journal_state WHERE key = ?", (key,)).fetchone()
        value = int(row["value"]) + 1 if row else 1
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO journal_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, str(value), now),
        )
        connection.commit()
    return value


def next_daily_counter(config: dict[str, Any], name: str, date_key: str) -> int:
    key = f"counter:{name}:{date_key}"
    with _connect(config) as connection:
        row = connection.execute("SELECT value FROM journal_state WHERE key = ?", (key,)).fetchone()
        value = int(row["value"]) + 1 if row else 1
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO journal_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, str(value), now),
        )
        connection.commit()
    return value


def _round_float(value: Any, digits: int = 6) -> Any:
    if isinstance(value, bool):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return round(number, digits)


def _trim_list(items: Any, limit: int) -> list[Any]:
    return list(items[:limit]) if isinstance(items, list) else []


def _compact_market_indicator(indicator: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(indicator, dict):
        return {}
    keep_keys = {
        "timeframe",
        "last",
        "trend",
        "rsi",
        "atr_pct",
        "volume_ratio",
        "spread_pct",
        "ema_gap_pct",
        "price_vs_ema_fast_pct",
        "price_vs_ema_slow_pct",
        "support_distance_pct",
        "resistance_distance_pct",
        "range_position",
        "signal_summary",
        "direction",
        "trend_context",
        "strongest_pattern",
        "bullish_score",
        "bearish_score",
    }
    compact: dict[str, Any] = {}
    for key in keep_keys:
        value = indicator.get(key)
        if value not in (None, "", [], {}):
            compact[key] = _round_float(value)
    patterns = indicator.get("candlestick_patterns")
    if isinstance(patterns, dict):
        compact["candlestick_patterns"] = {
            "direction": patterns.get("direction"),
            "trend_context": patterns.get("trend_context"),
            "strongest_pattern": patterns.get("strongest_pattern"),
            "signal_summary": patterns.get("signal_summary"),
            "patterns": _trim_list(patterns.get("patterns"), 3),
            "bullish_score": _round_float(patterns.get("bullish_score"), 3),
            "bearish_score": _round_float(patterns.get("bearish_score"), 3),
        }
        compact["candlestick_patterns"] = {
            key: value for key, value in compact["candlestick_patterns"].items()
            if value not in (None, "", [], {})
        }
    higher_timeframes = indicator.get("higher_timeframes")
    if isinstance(higher_timeframes, dict):
        compact["higher_timeframes"] = {
            str(frame): _compact_market_indicator(data)
            for frame, data in higher_timeframes.items()
            if isinstance(data, dict) and str(frame).lower() in {"5m", "15m", "1h", "4h"}
        }
        if not compact["higher_timeframes"]:
            compact.pop("higher_timeframes", None)
    return {key: value for key, value in compact.items() if value not in (None, "", [], {})}


def _compact_market_scan_payload(candidate: TradeCandidate, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": candidate.symbol,
        "base": candidate.base,
        "side": candidate.side,
        "confidence": _round_float(candidate.confidence, 3),
        "win_probability_pct": _round_float(candidate.win_probability_pct, 3),
        "risk_reward": _round_float(candidate.risk_reward, 3),
        "entry": _round_float(candidate.entry),
        "stop_loss": _round_float(candidate.stop_loss),
        "take_profit": _round_float(candidate.take_profit),
        "order_usdt": _round_float(candidate.order_usdt, 4),
        "quantity": _round_float(candidate.quantity, 8),
        "spread_pct": _round_float(candidate.spread_pct, 5),
        "news_score": _round_float(candidate.news_score, 4),
        "news_count": candidate.news_count,
        "target_mode": candidate.target_mode,
        "take_profit_pct": _round_float(candidate.take_profit_pct, 3),
        "stop_loss_pct": _round_float(candidate.stop_loss_pct, 3),
        "price_take_profit_pct": _round_float(candidate.price_take_profit_pct, 4),
        "price_stop_loss_pct": _round_float(candidate.price_stop_loss_pct, 4),
        "setup_quality": candidate.setup_quality,
        "market_regime": candidate.market_regime,
        "regime_confidence": _round_float(candidate.regime_confidence, 3),
        "scan_source": candidate.scan_source,
        "indicator_summary": _compact_market_indicator(candidate.indicator_summary),
        "reasons": _trim_list(payload.get("reasons"), 4),
        "warnings": _trim_list(payload.get("warnings"), 3),
    }


def _compact_frame_payload(candidate: TradeCandidate, frame_name: str, frame_indicator: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": candidate.symbol,
        "side": candidate.side,
        "timeframe": str(frame_name),
        "confidence": _round_float(candidate.confidence, 3),
        "win_probability_pct": _round_float(candidate.win_probability_pct, 3),
        "risk_reward": _round_float(candidate.risk_reward, 3),
        "frame_summary": _compact_market_indicator(frame_indicator),
    }


def _json_limited(payload: dict[str, Any], max_bytes: int) -> str:
    text = json.dumps(
        {key: value for key, value in payload.items() if value not in (None, "", [], {})},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    encoded = text.encode("utf-8")
    if len(encoded) <= max(256, max_bytes):
        return text
    trimmed = dict(payload)
    for key in ("reasons", "warnings"):
        if isinstance(trimmed.get(key), list):
            trimmed[key] = trimmed[key][:1]
    for key in ("indicator_summary", "frame_summary"):
        if isinstance(trimmed.get(key), dict):
            compact = dict(trimmed[key])
            compact.pop("higher_timeframes", None)
            compact.pop("candlestick_patterns", None)
            trimmed[key] = compact
    text = json.dumps(
        {key: value for key, value in trimmed.items() if value not in (None, "", [], {})},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(text.encode("utf-8")) <= max(256, max_bytes):
        return text
    return json.dumps(
        {
            "symbol": payload.get("symbol"),
            "side": payload.get("side"),
            "timeframe": payload.get("timeframe"),
            "confidence": payload.get("confidence"),
            "win_probability_pct": payload.get("win_probability_pct"),
            "risk_reward": payload.get("risk_reward"),
            "entry": payload.get("entry"),
            "stop_loss": payload.get("stop_loss"),
            "take_profit": payload.get("take_profit"),
            "truncated": True,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def save_decision(config: dict[str, Any], decision: Decision) -> int:
    payload = to_jsonable(decision)
    selected = decision.selected
    prune_decision_history(config)
    try:
        return _insert_decision_payload(config, decision, payload, selected)
    except sqlite3.OperationalError as exc:
        if "disk" not in str(exc).lower() and "full" not in str(exc).lower():
            raise
        run_storage_maintenance(config, emergency=True)
        return _insert_decision_payload(config, decision, payload, selected)


def _insert_decision_payload(
    config: dict[str, Any],
    decision: Decision,
    payload: dict[str, Any],
    selected: TradeCandidate | None,
) -> int:
    with _connect(config) as connection:
        cursor = connection.execute(
            """
            INSERT INTO decisions (
                created_at,
                action,
                selected_symbol,
                selected_side,
                selected_win_probability_pct,
                payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                payload["created_at"],
                decision.action,
                selected.symbol if selected else None,
                selected.side if selected else None,
                selected.win_probability_pct if selected else None,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def prune_decision_history(
    config: dict[str, Any],
    *,
    keep_hours: int | None = None,
    max_rows: int | None = None,
) -> dict[str, int]:
    history_config = config.get("decision_history", {})
    keep_hours = int(keep_hours or history_config.get("keep_hours", 24) or 24)
    max_rows = int(max_rows or history_config.get("max_rows", 120) or 120)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, keep_hours))).isoformat()
    deleted_old = 0
    deleted_over_limit = 0
    with _connect(config) as connection:
        cursor = connection.execute(
            """
            DELETE FROM decisions
            WHERE created_at < ?
              AND id NOT IN (SELECT id FROM decisions ORDER BY id DESC LIMIT 1)
            """,
            (cutoff,),
        )
        deleted_old = int(cursor.rowcount or 0)
        cursor = connection.execute(
            """
            DELETE FROM decisions
            WHERE id IN (
                SELECT id
                FROM (
                    SELECT id, ROW_NUMBER() OVER (ORDER BY created_at DESC, id DESC) AS row_number
                    FROM decisions
                )
                WHERE row_number > ?
            )
            """,
            (max(1, max_rows),),
        )
        deleted_over_limit = int(cursor.rowcount or 0)
        connection.commit()
    return {"deleted_old": deleted_old, "deleted_over_limit": deleted_over_limit}


def save_market_scan_observations(
    config: dict[str, Any],
    candidates: list[TradeCandidate],
    *,
    source: str,
    limit: int = 100,
) -> int:
    if not candidates:
        return 0
    memory_config = config.get("market_scan_memory", {})
    max_json_bytes = int(memory_config.get("max_json_bytes", DEFAULT_MARKET_SCAN_MAX_JSON_BYTES) or DEFAULT_MARKET_SCAN_MAX_JSON_BYTES)
    now = datetime.now(timezone.utc).isoformat()
    rows: list[tuple[Any, ...]] = []
    for candidate in candidates[: max(1, int(limit or 100))]:
        payload = to_jsonable(candidate)
        indicator = _compact_market_indicator(to_jsonable(candidate.indicator_summary or {}))
        compact_payload = _compact_market_scan_payload(candidate, payload if isinstance(payload, dict) else {})
        score = float(candidate.win_probability_pct or candidate.confidence or 0)
        timeframe = str(indicator.get("timeframe") or config.get("strategy", {}).get("timeframe") or "")
        rows.append(
            (
                now,
                source,
                candidate.symbol,
                candidate.side,
                timeframe,
                candidate.confidence,
                candidate.win_probability_pct,
                candidate.risk_reward,
                score,
                _json_limited(indicator, max_json_bytes),
                _json_limited(compact_payload, max_json_bytes),
            )
        )
        higher_timeframes = payload.get("higher_timeframes") if isinstance(payload, dict) else {}
        if isinstance(higher_timeframes, dict):
            for frame_name, frame_payload in higher_timeframes.items():
                if not isinstance(frame_payload, dict):
                    continue
                frame_indicator = _compact_market_indicator(dict(frame_payload))
                frame_indicator.setdefault("timeframe", str(frame_name))
                frame_payload_json = _compact_frame_payload(candidate, str(frame_name), frame_indicator)
                frame_score = float(
                    frame_indicator.get("bullish_score")
                    or frame_indicator.get("bearish_score")
                    or (frame_indicator.get("candlestick_patterns") or {}).get("bullish_score")
                    or (frame_indicator.get("candlestick_patterns") or {}).get("bearish_score")
                    or candidate.confidence
                    or 0
                )
                rows.append(
                    (
                        now,
                        source,
                        candidate.symbol,
                        candidate.side,
                        str(frame_name),
                        candidate.confidence,
                        candidate.win_probability_pct,
                        candidate.risk_reward,
                        frame_score,
                        _json_limited(frame_indicator, max_json_bytes),
                        _json_limited(frame_payload_json, max_json_bytes),
                    )
                )
    prune_market_scan_observations(config)
    try:
        _insert_market_scan_rows(config, rows)
    except sqlite3.OperationalError as exc:
        if "disk" not in str(exc).lower() and "full" not in str(exc).lower():
            raise
        run_storage_maintenance(config, emergency=True)
        _insert_market_scan_rows(config, rows)
    run_storage_maintenance(config, vacuum=False)
    return len(rows)


def _insert_market_scan_rows(config: dict[str, Any], rows: list[tuple[Any, ...]]) -> None:
    with _connect(config) as connection:
        connection.executemany(
            """
            INSERT INTO market_scan_observations (
                created_at,
                source,
                symbol,
                side,
                timeframe,
                confidence,
                win_probability_pct,
                risk_reward,
                score,
                indicator_json,
                payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        connection.commit()


def prune_market_scan_observations(
    config: dict[str, Any],
    *,
    keep_hours: int | None = None,
    max_rows_per_symbol_timeframe: int | None = None,
) -> dict[str, int]:
    memory_config = config.get("market_scan_memory", {})
    keep_hours = int(keep_hours or memory_config.get("keep_hours", 72) or 72)
    max_rows = int(
        max_rows_per_symbol_timeframe
        or memory_config.get("max_rows_per_symbol_timeframe", 200)
        or 200
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, keep_hours))).isoformat()
    deleted_old = 0
    deleted_over_limit = 0
    with _connect(config) as connection:
        cursor = connection.execute(
            "DELETE FROM market_scan_observations WHERE created_at < ?",
            (cutoff,),
        )
        deleted_old = int(cursor.rowcount or 0)
        cursor = connection.execute(
            """
            DELETE FROM market_scan_observations
            WHERE id IN (
                SELECT id
                FROM (
                    SELECT
                        id,
                        ROW_NUMBER() OVER (
                            PARTITION BY symbol, timeframe
                            ORDER BY created_at DESC, id DESC
                        ) AS row_number
                    FROM market_scan_observations
                )
                WHERE row_number > ?
            )
            """,
            (max(1, max_rows),),
        )
        deleted_over_limit = int(cursor.rowcount or 0)
        connection.commit()
        try:
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_market_scan_timeframe_symbol_created
                ON market_scan_observations(timeframe, symbol, created_at DESC)
                """
            )
            connection.commit()
        except sqlite3.OperationalError:
            pass
    return {
        "deleted_old": deleted_old,
        "deleted_over_limit": deleted_over_limit,
    }


def compact_market_scan_observations(config: dict[str, Any], *, batch_limit: int = 5000) -> dict[str, int]:
    memory_config = config.get("market_scan_memory", {})
    max_json_bytes = int(memory_config.get("max_json_bytes", DEFAULT_MARKET_SCAN_MAX_JSON_BYTES) or DEFAULT_MARKET_SCAN_MAX_JSON_BYTES)
    checked = 0
    compacted = 0
    with _connect(config) as connection:
        rows = connection.execute(
            """
            SELECT id, indicator_json, payload_json
            FROM market_scan_observations
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, int(batch_limit or 5000)),),
        ).fetchall()
        for row in rows:
            checked += 1
            try:
                indicator = json.loads(str(row["indicator_json"] or "{}"))
            except json.JSONDecodeError:
                indicator = {}
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
            except json.JSONDecodeError:
                payload = {}
            compact_indicator = _compact_market_indicator(indicator)
            compact_payload: dict[str, Any] = {}
            if isinstance(payload, dict):
                for key in (
                    "symbol",
                    "base",
                    "side",
                    "timeframe",
                    "confidence",
                    "win_probability_pct",
                    "risk_reward",
                    "entry",
                    "stop_loss",
                    "take_profit",
                    "order_usdt",
                    "quantity",
                    "spread_pct",
                    "news_score",
                    "news_count",
                    "target_mode",
                    "take_profit_pct",
                    "stop_loss_pct",
                    "price_take_profit_pct",
                    "price_stop_loss_pct",
                    "setup_quality",
                    "market_regime",
                    "regime_confidence",
                    "scan_source",
                    "source",
                ):
                    if payload.get(key) not in (None, "", [], {}):
                        compact_payload[key] = _round_float(payload.get(key))
                if isinstance(payload.get("indicator_summary"), dict):
                    compact_payload["indicator_summary"] = _compact_market_indicator(payload.get("indicator_summary"))
                if isinstance(payload.get("frame_summary"), dict):
                    compact_payload["frame_summary"] = _compact_market_indicator(payload.get("frame_summary"))
                compact_payload["reasons"] = _trim_list(payload.get("reasons"), 4)
                compact_payload["warnings"] = _trim_list(payload.get("warnings"), 3)
            compact_indicator_json = _json_limited(compact_indicator, max_json_bytes)
            compact_payload_json = _json_limited(compact_payload, max_json_bytes)
            old_indicator_json = str(row["indicator_json"] or "{}")
            old_payload_json = str(row["payload_json"] or "{}")
            if (
                compact_indicator_json != old_indicator_json
                or compact_payload_json != old_payload_json
                or len(old_indicator_json.encode("utf-8")) > max_json_bytes
                or len(old_payload_json.encode("utf-8")) > max_json_bytes
            ):
                connection.execute(
                    """
                    UPDATE market_scan_observations
                    SET indicator_json = ?, payload_json = ?
                    WHERE id = ?
                    """,
                    (compact_indicator_json, compact_payload_json, row["id"]),
                )
                compacted += 1
        connection.commit()
    return {"checked": checked, "compacted": compacted}


def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def storage_stats(config: dict[str, Any]) -> dict[str, Any]:
    db_path = state_db_path(config)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(db_path.parent)
    files = {
        "db": {"path": str(db_path), "bytes": _file_size(db_path)},
        "wal": {"path": str(db_path) + "-wal", "bytes": _file_size(Path(str(db_path) + "-wal"))},
        "shm": {"path": str(db_path) + "-shm", "bytes": _file_size(Path(str(db_path) + "-shm"))},
    }
    tables = [
        "decisions",
        "paper_trades",
        "pending_orders",
        "journal_state",
        "trade_memory",
        "market_guard_observations",
        "market_scan_observations",
        "ai_trade_decisions",
        "trade_executions",
        "trading_system_state",
        "trading_health_state",
        "trade_candidates",
        "market_regime_history",
        "strategy_versions",
        "prompt_versions",
        "replay_history",
    ]
    row_counts: dict[str, int] = {}
    payload_bytes: dict[str, int] = {}
    market_scan_by_timeframe: list[dict[str, Any]] = []
    with _connect(config) as connection:
        for table in tables:
            try:
                row = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
                row_counts[table] = int(row["count"] if row else 0)
            except sqlite3.Error:
                row_counts[table] = 0
            try:
                row = connection.execute(f"SELECT COALESCE(SUM(LENGTH(payload_json)), 0) AS bytes FROM {table}").fetchone()
                payload_bytes[table] = int(row["bytes"] if row else 0)
            except sqlite3.Error:
                payload_bytes[table] = 0
        try:
            rows = connection.execute(
                """
                SELECT timeframe, COUNT(*) AS rows, COUNT(DISTINCT symbol) AS symbols, MAX(created_at) AS latest_at
                FROM market_scan_observations
                GROUP BY timeframe
                ORDER BY timeframe
                """
            ).fetchall()
            market_scan_by_timeframe = [dict(row) for row in rows]
        except sqlite3.Error:
            market_scan_by_timeframe = []
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(db_path),
        "files": files,
        "disk": {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "free_percent": round((usage.free / usage.total) * 100, 2) if usage.total else 0,
        },
        "row_counts": row_counts,
        "payload_bytes": payload_bytes,
        "market_scan_by_timeframe": market_scan_by_timeframe,
        "retention": config.get("market_scan_memory", {}),
    }


def run_storage_maintenance(
    config: dict[str, Any],
    *,
    vacuum: bool = False,
    emergency: bool = False,
) -> dict[str, Any]:
    memory_config = config.get("market_scan_memory", {})
    decision_config = config.get("decision_history", {})
    if emergency:
        prune_result = prune_market_scan_observations(
            config,
            keep_hours=int(memory_config.get("emergency_keep_hours", 24) or 24),
            max_rows_per_symbol_timeframe=int(memory_config.get("emergency_max_rows_per_symbol_timeframe", 50) or 50),
        )
        decision_prune_result = prune_decision_history(
            config,
            keep_hours=int(decision_config.get("emergency_keep_hours", 6) or 6),
            max_rows=int(decision_config.get("emergency_max_rows", 30) or 30),
        )
    else:
        prune_result = prune_market_scan_observations(config)
        decision_prune_result = prune_decision_history(config)
    compact_result = {"checked": 0, "compacted": 0}
    checkpoint: list[Any] = []
    optimized = False
    vacuumed = False
    errors: list[str] = []
    try:
        compact_result = compact_market_scan_observations(
            config,
            batch_limit=int(memory_config.get("compact_batch_limit", 5000) or 5000),
        )
    except sqlite3.Error as exc:
        errors.append(f"compact_market_scan: {exc}")
    try:
        with _connect(config) as connection:
            try:
                checkpoint = [tuple(row) for row in connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()]
            except sqlite3.Error as exc:
                errors.append(f"wal_checkpoint: {exc}")
            try:
                connection.execute("PRAGMA optimize")
                optimized = True
            except sqlite3.Error as exc:
                errors.append(f"optimize: {exc}")
            if vacuum:
                try:
                    connection.execute("VACUUM")
                    vacuumed = True
                except sqlite3.Error as exc:
                    errors.append(f"vacuum: {exc}")
    except sqlite3.Error as exc:
        errors.append(f"maintenance: {exc}")
    return {
        "ok": not errors,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "prune": prune_result,
        "decision_prune": decision_prune_result,
        "compact": compact_result,
        "emergency": emergency,
        "checkpoint": checkpoint,
        "optimized": optimized,
        "vacuumed": vacuumed,
        "errors": errors,
        "stats": storage_stats(config),
    }


def recent_market_scan_memory(
    config: dict[str, Any],
    *,
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    lookback_hours: int = 12,
    per_symbol_timeframe_limit: int = 3,
    total_limit: int = 1000,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    clauses = ["created_at >= ?"]
    params: list[Any] = [
        (datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))).isoformat()
    ]
    symbol_list = [str(item) for item in (symbols or []) if str(item)]
    timeframe_list = [str(item) for item in (timeframes or []) if str(item)]
    if symbol_list:
        placeholders = ", ".join("?" for _ in symbol_list)
        clauses.append(f"symbol IN ({placeholders})")
        params.extend(symbol_list)
    if timeframe_list:
        placeholders = ", ".join("?" for _ in timeframe_list)
        clauses.append(f"timeframe IN ({placeholders})")
        params.extend(timeframe_list)
    params.append(max(10, int(total_limit)))
    where = " AND ".join(clauses)
    with _connect(config) as connection:
        rows = connection.execute(
            f"""
            SELECT *
            FROM market_scan_observations
            WHERE {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()

    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    counters: dict[tuple[str, str], int] = {}
    for row in rows:
        item = dict(row)
        symbol = str(item.get("symbol") or "")
        timeframe = str(item.get("timeframe") or "")
        if not symbol or not timeframe:
            continue
        key = (symbol, timeframe)
        if counters.get(key, 0) >= max(1, int(per_symbol_timeframe_limit)):
            continue
        indicator = json.loads(str(item.get("indicator_json") or "{}"))
        payload = json.loads(str(item.get("payload_json") or "{}"))
        grouped.setdefault(symbol, {}).setdefault(timeframe, []).append(
            {
                "created_at": item.get("created_at"),
                "source": item.get("source"),
                "side": item.get("side"),
                "confidence": item.get("confidence"),
                "win_probability_pct": item.get("win_probability_pct"),
                "risk_reward": item.get("risk_reward"),
                "score": item.get("score"),
                "indicator": indicator,
                "payload": payload,
            }
        )
        counters[key] = counters.get(key, 0) + 1
    return grouped


def latest_decision_payload(config: dict[str, Any]) -> dict[str, Any] | None:
    with _connect(config) as connection:
        row = connection.execute(
            "SELECT payload_json FROM decisions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return json.loads(str(row["payload_json"]))


def list_paper_trades(config: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
    with _connect(config) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM paper_trades
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def active_paper_trades(config: dict[str, Any]) -> list[dict[str, Any]]:
    with _connect(config) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM paper_trades
            WHERE status = 'OPEN'
            ORDER BY id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def list_pending_orders(config: dict[str, Any], status: str = "OPEN", limit: int = 100) -> list[dict[str, Any]]:
    with _connect(config) as connection:
        if status in {"OPEN", "ACTIVE"}:
            placeholders = ", ".join("?" for _ in ACTIVE_PENDING_STATUSES)
            rows = connection.execute(
                f"""
                SELECT *
                FROM pending_orders
                WHERE status IN ({placeholders})
                ORDER BY id DESC
                LIMIT ?
                """,
                (*ACTIVE_PENDING_STATUSES, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT *
                FROM pending_orders
                WHERE status = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (status, limit),
            ).fetchall()
    return [dict(row) for row in rows]


def open_pending_symbols(config: dict[str, Any]) -> set[str]:
    return {str(order["symbol"]) for order in list_pending_orders(config, status="OPEN")}


def _pending_expiry(now: datetime, *, max_age_days: float = 3, max_age_hours: float | None = None) -> datetime:
    if max_age_hours is not None:
        return now + timedelta(hours=max(0.1, float(max_age_hours)))
    return now + timedelta(days=max(0.1, float(max_age_days)))


def save_pending_order(
    config: dict[str, Any],
    candidate: TradeCandidate,
    exchange_order_id: str | None,
    *,
    status: str | None = None,
    max_age_days: float = 3,
    max_age_hours: float | None = None,
    journal_id: int | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    payload = to_jsonable(candidate)
    normalized_status = str(status or ("LC_OKX" if exchange_order_id else "OPEN")).upper()
    with _connect(config) as connection:
        cursor = connection.execute(
            """
            INSERT INTO pending_orders (
                created_at,
                updated_at,
                expires_at,
                status,
                symbol,
                base,
                side,
                exchange_order_id,
                entry,
                stop_loss,
                take_profit,
                quantity,
                order_usdt,
                confidence,
                win_probability_pct,
                risk_reward,
                payload_json
                , journal_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now.isoformat(),
                now.isoformat(),
                _pending_expiry(now, max_age_days=max_age_days, max_age_hours=max_age_hours).isoformat(),
                normalized_status,
                candidate.symbol,
                candidate.base,
                candidate.side,
                exchange_order_id,
                candidate.entry,
                candidate.stop_loss,
                candidate.take_profit,
                candidate.quantity,
                candidate.order_usdt,
                candidate.confidence,
                candidate.win_probability_pct,
                candidate.risk_reward,
                json.dumps(payload, ensure_ascii=False),
                journal_id,
            ),
        )
        connection.commit()
        order_id = int(cursor.lastrowid)
    return [order for order in list_pending_orders(config, limit=100) if int(order["id"]) == order_id][0]


def refresh_pending_order(
    config: dict[str, Any],
    order_id: int,
    candidate: TradeCandidate,
    *,
    status: str | None = None,
    max_age_days: float = 3,
    max_age_hours: float | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    payload = to_jsonable(candidate)
    with _connect(config) as connection:
        connection.execute(
            """
            UPDATE pending_orders
            SET updated_at = ?,
                expires_at = ?,
                status = COALESCE(?, status),
                entry = ?,
                stop_loss = ?,
                take_profit = ?,
                quantity = ?,
                order_usdt = ?,
                confidence = ?,
                win_probability_pct = ?,
                risk_reward = ?,
                payload_json = ?
            WHERE id = ?
            """,
            (
                now.isoformat(),
                _pending_expiry(now, max_age_days=max_age_days, max_age_hours=max_age_hours).isoformat(),
                str(status).upper() if status else None,
                candidate.entry,
                candidate.stop_loss,
                candidate.take_profit,
                candidate.quantity,
                candidate.order_usdt,
                candidate.confidence,
                candidate.win_probability_pct,
                candidate.risk_reward,
                json.dumps(payload, ensure_ascii=False),
                order_id,
            ),
        )
        connection.commit()


def set_pending_order_exchange_order(
    config: dict[str, Any],
    order_id: int,
    candidate: TradeCandidate,
    exchange_order_id: str,
    *,
    max_age_days: float = 1.5,
) -> None:
    now = datetime.now(timezone.utc)
    payload = to_jsonable(candidate)
    with _connect(config) as connection:
        connection.execute(
            """
            UPDATE pending_orders
            SET updated_at = ?,
                expires_at = ?,
                status = 'LC_OKX',
                exchange_order_id = ?,
                entry = ?,
                stop_loss = ?,
                take_profit = ?,
                quantity = ?,
                order_usdt = ?,
                confidence = ?,
                win_probability_pct = ?,
                risk_reward = ?,
                payload_json = ?
            WHERE id = ?
            """,
            (
                now.isoformat(),
                _pending_expiry(now, max_age_days=max_age_days).isoformat(),
                exchange_order_id,
                candidate.entry,
                candidate.stop_loss,
                candidate.take_profit,
                candidate.quantity,
                candidate.order_usdt,
                candidate.confidence,
                candidate.win_probability_pct,
                candidate.risk_reward,
                json.dumps(payload, ensure_ascii=False),
                order_id,
            ),
        )
        connection.commit()


def close_pending_order(config: dict[str, Any], order_id: int, status: str, reason: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect(config) as connection:
        connection.execute(
            """
            UPDATE pending_orders
            SET status = ?,
                updated_at = ?,
                close_reason = ?
            WHERE id = ?
            """,
            (status, now, reason, order_id),
        )
        connection.commit()


def count_pending_orders(config: dict[str, Any], status: str = "OPEN") -> int:
    with _connect(config) as connection:
        if status in {"OPEN", "ACTIVE"}:
            placeholders = ", ".join("?" for _ in ACTIVE_PENDING_STATUSES)
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM pending_orders WHERE status IN ({placeholders})",
                ACTIVE_PENDING_STATUSES,
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM pending_orders WHERE status = ?",
                (status,),
            ).fetchone()
    return int(row["count"] if row else 0)


def save_market_guard_observation(config: dict[str, Any], observation: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    created_at = str(observation.get("created_at") or now)
    observed_at = str(observation.get("observed_at") or created_at)
    symbol = str(observation.get("symbol") or "")
    if not symbol:
        return
    reasons = observation.get("reasons") or []
    with _connect(config) as connection:
        connection.execute(
            """
            INSERT INTO market_guard_observations (
                created_at,
                observed_at,
                symbol,
                last,
                move_pct,
                candle_range_pct,
                wick_pct,
                wick_body_ratio,
                volume_ratio,
                severity,
                alert_reasons_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, observed_at) DO UPDATE SET
                created_at = excluded.created_at,
                last = excluded.last,
                move_pct = excluded.move_pct,
                candle_range_pct = excluded.candle_range_pct,
                wick_pct = excluded.wick_pct,
                wick_body_ratio = excluded.wick_body_ratio,
                volume_ratio = excluded.volume_ratio,
                severity = excluded.severity,
                alert_reasons_json = excluded.alert_reasons_json
            """,
            (
                created_at,
                observed_at,
                symbol,
                observation.get("last"),
                observation.get("move_pct"),
                observation.get("candle_range_pct"),
                observation.get("wick_pct"),
                observation.get("wick_body_ratio"),
                observation.get("volume_ratio"),
                str(observation.get("severity") or "normal"),
                json.dumps(reasons, ensure_ascii=False),
            ),
        )
        connection.commit()


def list_market_guard_observations(
    config: dict[str, Any],
    *,
    symbol: str | None = None,
    limit: int = 200,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol)
    if since:
        clauses.append("observed_at >= ?")
        params.append(since.isoformat())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with _connect(config) as connection:
        rows = connection.execute(
            f"""
            SELECT *
            FROM market_guard_observations
            {where}
            ORDER BY observed_at DESC, id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["reasons"] = json.loads(str(item.get("alert_reasons_json") or "[]"))
        except json.JSONDecodeError:
            item["reasons"] = []
        result.append(item)
    return result


def prune_market_guard_observations(config: dict[str, Any], *, keep_hours: int = 24) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, keep_hours))).isoformat()
    with _connect(config) as connection:
        connection.execute(
            "DELETE FROM market_guard_observations WHERE observed_at < ?",
            (cutoff,),
        )
        connection.commit()


def save_trade_memory(config: dict[str, Any], record: dict[str, Any], *, limit: int = 100) -> bool:
    key = str(record.get("key") or "")
    symbol = str(record.get("symbol") or "")
    if not key or not symbol:
        return False

    now = datetime.now(timezone.utc).isoformat()
    pnl_usdt = record.get("pnl_usdt")
    outcome = "win" if float(pnl_usdt or 0) > 0 else "loss" if float(pnl_usdt or 0) < 0 else "flat"
    with _connect(config) as connection:
        existing = connection.execute("SELECT key FROM trade_memory WHERE key = ?", (key,)).fetchone()
        if existing:
            return False
        connection.execute(
            """
            INSERT INTO trade_memory (
                key,
                created_at,
                updated_at,
                symbol,
                side,
                opened_at,
                closed_at,
                pnl_usdt,
                pnl_pct,
                outcome,
                source,
                payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                now,
                now,
                symbol,
                record.get("side"),
                record.get("opened_at"),
                record.get("closed_at"),
                pnl_usdt,
                record.get("pnl_pct"),
                outcome,
                str(record.get("source") or "okx"),
                json.dumps(record.get("payload") or record, ensure_ascii=False),
            ),
        )
        connection.execute(
            """
            DELETE FROM trade_memory
            WHERE key NOT IN (
                SELECT key
                FROM trade_memory
                ORDER BY COALESCE(closed_at, updated_at) DESC
                LIMIT ?
            )
            """,
            (limit,),
        )
        connection.commit()
    return True


def list_trade_memory(config: dict[str, Any], limit: int = 100) -> list[dict[str, Any]]:
    with _connect(config) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM trade_memory
            ORDER BY COALESCE(closed_at, updated_at) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def open_paper_trade(config: dict[str, Any], candidate: TradeCandidate) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    leverage = float(config.get("exchange", {}).get("leverage", 1) or 1)
    payload = to_jsonable(candidate)
    with _connect(config) as connection:
        cursor = connection.execute(
            """
            INSERT INTO paper_trades (
                created_at,
                updated_at,
                status,
                symbol,
                base,
                side,
                entry,
                stop_loss,
                take_profit,
                quantity,
                order_usdt,
                confidence,
                win_probability_pct,
                risk_reward,
                leverage,
                payload_json
            )
            VALUES (?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                now,
                candidate.symbol,
                candidate.base,
                candidate.side,
                candidate.entry,
                candidate.stop_loss,
                candidate.take_profit,
                candidate.quantity,
                candidate.order_usdt,
                candidate.confidence,
                candidate.win_probability_pct,
                candidate.risk_reward,
                leverage,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        connection.commit()
        trade_id = int(cursor.lastrowid)
    return {"id": trade_id, **list_paper_trades(config, limit=1)[0]}


def close_paper_trade(config: dict[str, Any], trade_id: int, close_price: float, reason: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    active = [trade for trade in active_paper_trades(config) if int(trade["id"]) == trade_id]
    if not active:
        raise ValueError(f"Paper trade {trade_id} is not open")
    trade = active[0]
    entry = float(trade["entry"])
    leverage = float(trade["leverage"] or 1)
    if trade["side"] == "long":
        pnl_pct = ((close_price - entry) / entry) * 100 * leverage
    else:
        pnl_pct = ((entry - close_price) / entry) * 100 * leverage
    with _connect(config) as connection:
        connection.execute(
            """
            UPDATE paper_trades
            SET status = 'CLOSED',
                updated_at = ?,
                close_price = ?,
                close_reason = ?,
                pnl_pct = ?
            WHERE id = ?
            """,
            (now, close_price, reason, round(pnl_pct, 4), trade_id),
        )
        connection.commit()
    return [trade for trade in list_paper_trades(config, limit=50) if int(trade["id"]) == trade_id][0]
