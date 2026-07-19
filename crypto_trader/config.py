from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import zlib
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


LOGGER = logging.getLogger(__name__)
RUNTIME_CONFIG_OVERRIDES_STATE_KEY = "runtime_config_overrides"
_RUNTIME_CONFIG_OVERRIDES_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
_RUNTIME_CONFIG_OVERRIDES_LOCK = threading.Lock()


DEFAULT_CONFIG: dict[str, Any] = {
    "mode": "dry_run",
    "report_path": "reports/latest_decision.json",
    "ledger_path": "data/trades.jsonl",
    "runtime": {
        "interval_seconds": 60,
        "instance_role": "primary",
    },
    "trailing_stop": {
        "enabled": True,
        "activation_r_multiple": 1.0,
        "atr_timeframe": "1m",
        "atr_period": 14,
        "atr_multiplier": 1.5,
        "min_improvement_price": 0.0,
        "trigger_price_type": "last",
        "algo_order_types": ["oco", "conditional", "trigger"],
        "symbol_overrides": {
            "BTC": {
                "min_improvement_points": 2000,
                "point_value": 0.01,
            },
        },
    },
    "runtime_config_overrides": {
        "enabled": True,
        "state_key": RUNTIME_CONFIG_OVERRIDES_STATE_KEY,
        "cache_ttl_seconds": 5,
        "allow_embedded_atlas_uri": False,
    },
    "database": {
        "backend": "atlas",
        "atlas": {
            "uri": "mongodb+srv://ttthinh2005_db_user:abc123456789@cluster0.58iwirh.mongodb.net/Bunny_Runtime?appName=Cluster0",
            "database": "Bunny_Runtime",
            "ai_database": "AI_Bunny",
            "uri_env": "MONGODB_URI",
            "database_env": "MONGODB_DATABASE",
            "ai_database_env": "MONGODB_AI_DATABASE",
            "app_name": "Crypto_Bunny",
            "server_selection_timeout_ms": 10000,
            "connect_timeout_ms": 10000,
            "socket_timeout_ms": 15000,
            "wait_queue_timeout_ms": 10000,
        },
    },
    "automation": {
        "enabled": True,
        "scan_interval_seconds": 60,
        "execute_demo": True,
        "execute_live": False,
        "initial_delay_seconds": 5,
    },
    "news": {
        "lookback_hours": 24,
        "max_items_per_feed": 40,
        "require_symbol_news": True,
        "timeout_seconds": 5,
        "feeds": [
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
            "https://cointelegraph.com/rss",
            "https://decrypt.co/feed",
        ],
    },
    "exchange": {
        "name": "okx",
        "account_type": "swap",
        "td_mode": "isolated",
        "leverage": 10,
        "api_key_env": "OKX_API_KEY",
        "secret_env": "OKX_SECRET",
        "passphrase_env": "OKX_PASSPHRASE",
        "default_settle": "USDT",
        "position_side_mode": "net",
        "timeout_ms": 10000,
        "snapshot_workers": 3,
        "leverage_presets": [5, 10, 15, 20, 25],
    },
    "strategy": {
        "timeframe": "1m",
        "ohlcv_limit": 180,
        "min_confidence": 75,
        "min_win_probability_pct": 0.0,
        "min_risk_reward": 1.5,
        "target": {
            "mode": "roi_percent",
            "take_profit_pct": 75,
            "stop_loss_pct": 50,
            "risk_reward_ratio": 2.0,
            "percent_basis": "roi_percent",
        },
        "confirmation_timeframes": {
            "enabled": True,
            "frames": ["5m", "15m", "1h", "4h"],
            "ohlcv_limit": 180,
            "weights": {
                "5m": 3,
                "15m": 6,
                "1h": 9,
                "4h": 11,
            },
        },
        "candlestick_patterns": {
            "enabled": True,
            "weights": {
                "1m": 3,
                "5m": 4,
                "15m": 6,
                "1h": 9,
                "4h": 11,
            },
        },
        "universe": {
            "enabled": True,
            "mode": "top_volume_24h",
            "quote": "USDT",
            "max_symbols": 40,
            "asset_class": "crypto",
            "priority_symbols_enabled": True,
            "priority_symbols": [
                "BTC/USDT:USDT",
                "SOL/USDT:USDT",
                "ETH/USDT:USDT",
                "BNB/USDT:USDT",
                "XRP/USDT:USDT",
            ],
            "weekday_priority_enabled": True,
            "weekday_priority_symbols": ["XAU/USDT:USDT"],
            "weekday_priority_timezone": "Asia/Ho_Chi_Minh",
            "exclude_bases": [],
            "exclude_keywords": [],
        },
        "long_short_bias": {
            "enabled": True,
            "target_long_ratio": 0.6,
            "strength": 10,
        },
        "symbols": [
            "BTC/USDT:USDT",
            "ETH/USDT:USDT",
            "SOL/USDT:USDT",
            "BNB/USDT:USDT",
            "XRP/USDT:USDT",
            "DOGE/USDT:USDT",
            "ADA/USDT:USDT",
            "LINK/USDT:USDT",
            "AVAX/USDT:USDT",
            "LTC/USDT:USDT",
        ],
    },
    "risk": {
        "order_usdt": 20,
        "max_active_trades": 1,
        "max_daily_orders": 3,
        "max_daily_planned_risk_usdt": 10,
        "cooldown_minutes": 60,
        "max_spread_pct": 0.15,
        "min_stop_distance_pct": 0.35,
        "max_stop_distance_pct": 6.0,
        "news_conflict_threshold": 2.0,
    },
    "trading_risk": {
        "mechanism_name": "Bunny minimize losses",
        "max_concurrent_positions": 1,
        "global_loss_streak_threshold": 2,
        "symbol_loss_streak_threshold": 2,
        "normal_risk_percent": 1.0,
        "recovery_mode_risk_percent": 0.5,
        "normal_min_rule_score": 75,
        "normal_min_gpt_confidence": 75,
        "normal_min_risk_reward": 1.5,
        "strong_setup_rule_score": 85,
        "strong_setup_gpt_confidence": 88,
        "strong_setup_min_risk_reward": 2.0,
        "recovery_min_rule_score": 90,
        "recovery_min_gpt_confidence": 92,
        "recovery_min_risk_reward": 2.5,
        "enable_adaptive_threshold": True,
        "weekly_target_min_trades": 3,
        "weekly_target_max_trades": 7,
        "adaptive_score_step": 3,
        "adaptive_confidence_step": 3,
        "absolute_min_rule_score": 75,
        "absolute_min_gpt_confidence": 80,
        "absolute_min_risk_reward": 1.5,
        "pause_trading_loss_streak": 4,
        "pause_trading_hours": 24,
        "max_safe_funding_rate_abs": 0.03,
        "max_entry_distance_pct": 0.6,
    },
    "execution": {
        "order_type": "market",
        "attach_tp_sl": True,
        "live_confirm_file": ".allow-live-trading",
        "enable_live": False,
    },
    "ai": {
        "enabled": True,
        "api_key_env": "OPENAI_API_KEY",
        "manual_only": False,
        "allow_api_calls": True,
        "replay": {
            "allow_api_calls": False,
        },
        "debug": {
            "allow_api_calls": False,
        },
        "internal": {
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "api_key_env": "OPENAI_API_KEY_INTERNAL",
            "market_scan_enabled": True,
            "market_scan_interval_seconds": 14400,
            "market_scan_fixed_schedule": True,
            "market_scan_slot_tolerance_minutes": 3,
            "market_scan_timezone": "Asia/Ho_Chi_Minh",
            "market_scan_source_symbols": 30,
            "market_scan_max_symbols": 3,
            "market_scan_min_approved_symbols": 1,
            "market_scan_min_win_probability_pct": 63,
            "market_scan_use_ai": True,
            "market_scan_use_shortlist": True,
            "market_scan_to_pending": True,
            "market_scan_pending_limit": 1,
            "market_scan_require_ai_for_pending": True,
            "compact_ai_payload": True,
            "lc_pipeline_enabled": True,
            "lc_pipeline_top_limit": 3,
            "lc_pipeline_undecided_max": 6,
            "lc_pipeline_undecided_prune_floor": 6,
            "lc_pipeline_undecided_prune_drop": 3,
            "lc_pipeline_internal_lc_max": 3,
            "lc_pipeline_promote_after_hours": 6,
            "lc_pipeline_undecided_max_age_hours": 12,
            "lc_pipeline_recheck_interval_minutes": 90,
            "lc_pipeline_slot_tolerance_minutes": 3,
            "lc_pipeline_min_win_probability_pct": 62,
            "lc_pipeline_one_hour_min_win_probability_pct": 61,
            "lc_pipeline_two_hour_min_win_probability_pct": 62,
            "lc_pipeline_four_hour_min_win_probability_pct": 63,
            "lc_pipeline_relaxed_min_win_probability_pct": 55,
            "lc_pipeline_relaxed_min_confidence": 70,
            "lc_pipeline_relaxed_min_risk_reward": 1.5,
            "lc_pipeline_notify_one_hour_summary": True,
            "lc_pipeline_notify_two_hour_summary": True,
            "lc_pipeline_notify_mini_pool_summary": True,
            "lc_pipeline_two_hour_icon": "🟡",
            "lc_pipeline_promote_survivors": True,
            "lc_pipeline_promote_to_pending": True,
            "timeout_seconds": 15,
        },
        "okx": {
            "provider": "local_policy",
            "model": "gpt-5.5",
            "api_key_env": "OPENAI_API_KEY_OKX",
            "approval_enabled": True,
            "auto_openai_enabled": False,
            "auto_lc_okx_review_once_enabled": True,
            "manual_openai_enabled": False,
            "ask_internal_before_entry": True,
            "require_external_approval": False,
            "timeout_seconds": 20,
        },
    },
    "pending_orders": {
        "enabled": True,
        "max_age_days": 5,
        "local_max_age_hours": 6,
        "exchange_max_age_days": 5,
        "order_type": "limit",
        "review": {
            "enabled": True,
            "min_confidence": 70,
            "min_win_probability_pct": 50,
            "max_confidence_drop": 12,
            "max_win_probability_drop_pct": 8,
            "min_risk_reward": 1.5,
            "max_entry_drift_pct": 1.2,
            "use_market_guard_memory": True,
            "cancel_on_guard_avoid": True,
            "cancel_on_opposite_guard_direction": True,
            "opposite_guard_min_risk_score": 4.0,
        },
    },
    "market_guard": {
        "enabled": True,
        "interval_seconds": 60,
        "notify_telegram": True,
        "notify_interval_seconds": 600,
        "timeframe": "1m",
        "ohlcv_limit": 40,
        "lookback_candles": 5,
        "price_move_5m_pct": 0.8,
        "critical_price_move_5m_pct": 1.4,
        "candle_range_pct": 0.9,
        "critical_candle_range_pct": 1.8,
        "wick_pct": 0.45,
        "wick_body_ratio": 2.5,
        "volume_ratio": 2.5,
        "pause_new_entries_seconds": 900,
        "max_symbols": 30,
        "layer_5m_samples": 5,
        "layer_20m_samples": 20,
        "memory_keep_hours": 6,
        "use_memory_in_strategy": True,
    },
    "market_scan_memory": {
        "keep_hours": 24,
        "max_rows_per_symbol_timeframe": 120,
        "max_json_bytes": 4096,
        "max_saved_candidates_per_scan": 20,
        "compact_batch_limit": 100,
        "prune_overflow_batch_limit": 500,
        "emergency_keep_hours": 6,
        "emergency_max_rows_per_symbol_timeframe": 30,
    },
    "decision_history": {
        "keep_hours": 168,
        "max_rows": 120,
        "emergency_keep_hours": 24,
        "emergency_max_rows": 30,
    },
    "storage_maintenance": {
        "enabled": True,
        "interval_seconds": 900,
        "emergency": False,
    },
    "journal_state_retention": {
        "enabled": True,
        "system_checklist_keep_days": 30,
        "daily_start_balance_keep_days": 10,
        "wait_slot_notification_history_keep_days": 7,
        "lc_pipeline_candidate_cache_keep_hours": 6,
        "telegram_message_id_keep_days": 10,
        "dashboard_snapshot_keep_days": 1,
        "mini_setup_keep_days": 7,
    },
    "storage_retention": {
        "pending_orders_keep_days": 5,
        "internal_pending_orders_keep_days": 5,
        "trade_candidates_keep_days": 2,
        "ai_trade_decisions_keep_days": 180,
        "trade_executions_keep_days": 365,
        "prompt_versions_keep_days": 365,
        "strategy_versions_keep_days": 365,
        "ai_experiments_keep_days": 365,
        "replay_history_keep_days": 30,
        "paper_trades_keep_days": 30,
        "market_regime_history_keep_days": 7,
    },
    "runtime_sync": {
        "position_close_grace_seconds": 120,
    },
    "market_regime": {
        "enabled": True,
        "history_limit": 200,
        "derivatives_metrics_enabled": True,
        "fear_greed_enabled": True,
        "fear_greed_url": "https://api.alternative.me/fng/?limit=1&format=json",
        "fear_greed_timeout_seconds": 3,
        "high_volatility_atr_pct": 4.0,
        "low_volatility_atr_pct": 1.2,
        "sideway_adx_max": 20,
        "trend_adx_min": 25,
    },
    "market_pattern_engine": {
        "enabled": True,
        "max_snapshots_per_scan": 3,
        "max_candles": 220,
        "requested_detectors": [
            "candlestick",
            "market_structure",
            "support_resistance",
            "chart_patterns",
            "smart_money",
        ],
    },
    "bunny_health_monitor": {
        "enabled": True,
        "lookback_trades": 20,
        "min_trades_for_evaluation": 5,
        "min_win_rate": 50.0,
        "min_profit_factor": 1.2,
        "max_drawdown_percent": 10.0,
        "risk_reduction_percent": 40.0,
        "score_increase_step": 4.0,
        "confidence_increase_step": 4.0,
        "critical_win_rate": 35.0,
        "critical_profit_factor": 0.8,
        "critical_drawdown_percent": 15.0,
        "critical_pause_hours": 12,
    },
    "slot_refill": {
        "enable_auto_refill": True,
        "refill_delay_seconds": 10,
        "max_refill_attempts_per_slot": 3,
        "candidate_lookback_minutes": 240,
        "min_candidate_rule_score": 78,
        "allow_refill_in_recovery_mode": True,
    },
    "prompt_engine": {
        "enabled": True,
        "directory": "Prompts",
        "default_prompt_version": "prompt-v1",
        "system_version": "system-v1",
        "decision_engine_version": "decision-engine-v1",
        "validator_version": "validator-v1",
        "recovery_version": "recovery-v1",
        "health_version": "health-v1",
        "slot_refill_version": "slot-refill-v1",
        "bunny_version": "bunny-v1",
    },
    "strategy_versioning": {
        "enabled": True,
        "default_version": "strategy-v1",
        "rule_engine_version": "rule-engine-v1",
        "validator_version": "validator-v1",
        "recovery_version": "recovery-v1",
        "health_version": "health-v1",
        "allow_ab_testing": True,
    },
    "replay_engine": {
        "enabled": True,
        "default_batch_limit": 100,
    },
    "notifications": {
        "telegram": {
            "enabled": True,
            "notify_scans": True,
            "notify_ai_api_calls": True,
            "timeout_seconds": 8,
            "retry_count": 2,
            "buttons_enabled": True,
            "replace_previous_message": False,
            "polling_enabled": True,
            "poll_timeout_seconds": 1,
            "button_cache_ttl_seconds": 15,
            "account_report_interval_seconds": 18000,
            "daily_summary_enabled": True,
            "startup_message_enabled": True,
            "startup_quiet_seconds": 300,
            "trade_memory_limit": 100,
        },
    },
    "position_sizing": {
        "enabled": True,
        "base_margin_usdt": 2.0,
        "target_profit_usdt": 0.30,
        "tp_roi": 0.75,
        "sl_roi": 0.50,
        "open_fee": 0.0005,
        "close_fee": 0.0005,
        "safety_buffer": 0.02,
        "max_recovery_step": 4,
        "max_margin_usdt": 50,
        "max_cycle_loss_usdt": 50,
        "min_recovery_confidence": 88,
        "min_recovery_win_probability_pct": 58,
        "block_recovery_on_market_guard": True,
        "block_recovery_same_symbol_side": True,
        "max_recovery_4h_rsi_long": 76,
        "min_recovery_4h_rsi_short": 24,
        "base_margin_presets_usdt": [2, 3, 5, 10, 15, 20, 30, 50],
        "history_limit": 100,
        "bootstrap_existing_history": False,
        "reset_orphaned_blocked_state": True,
    },
    "capital_sync": {
        "enabled": True,
        "capital_source": "OKX",
        "refresh_interval_seconds": 60,
        "use_realized_capital_only": True,
        "exclude_unrealized_pnl": True,
        "quote_currency": "USDT",
    },
    "capital_reserve": {
        "enabled": True,
        "base_reserve_percent": 20,
        "warning_reserve_percent": 25,
        "recovery_reserve_percent": 30,
        "critical_reserve_percent": 40,
        "allow_reserve_usage": False,
        "emergency_allow_reserve_usage": True,
        "min_trading_capital": 10,
    },
    "configuration_impact": {
        "high_leverage_threshold": 20,
        "high_recovery_level_threshold": 4,
    },
    "paper_trading": {
        "enabled": True,
        "auto_scan_enabled": True,
        "scan_interval_seconds": 60,
        "max_active_trades": 1,
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _safe_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _runtime_config_overrides_settings(config: dict[str, Any]) -> dict[str, Any]:
    settings = config.get("runtime_config_overrides", {})
    return settings if isinstance(settings, dict) else {}


def runtime_config_overrides_should_attempt(config: dict[str, Any]) -> bool:
    settings = _runtime_config_overrides_settings(config)
    if not _safe_bool(settings.get("enabled"), True):
        return False
    database = config.get("database", {})
    if str(database.get("backend", "atlas") or "atlas").strip().lower() != "atlas":
        return False
    if os.getenv("PYTEST_CURRENT_TEST") or config.get("_atlas_test_mode"):
        return True
    atlas = database.get("atlas", {}) if isinstance(database.get("atlas"), dict) else {}
    uri_env = str(atlas.get("uri_env", "MONGODB_URI") or "MONGODB_URI")
    if os.getenv(uri_env, "").strip():
        return True
    if any(os.getenv(name, "").strip() for name in ("RAILWAY_ENVIRONMENT_NAME", "RAILWAY_SERVICE_ID", "RAILWAY_DEPLOYMENT_ID")):
        return True
    return bool(_safe_bool(settings.get("allow_embedded_atlas_uri"), False) and str(atlas.get("uri", "") or "").strip())


def _runtime_config_overrides_cache_ttl(config: dict[str, Any]) -> float:
    settings = _runtime_config_overrides_settings(config)
    try:
        return max(0.0, float(settings.get("cache_ttl_seconds", 5) or 0))
    except (TypeError, ValueError):
        return 5.0


def _runtime_config_overrides_cache_key(config: dict[str, Any]) -> str:
    atlas = config.get("database", {}).get("atlas", {})
    return str(
        config.get("_config_path")
        or config.get("_config_dir")
        or atlas.get("database")
        or "default"
    )


def invalidate_runtime_config_overrides_cache(config: dict[str, Any] | None = None) -> None:
    with _RUNTIME_CONFIG_OVERRIDES_LOCK:
        if config is None:
            _RUNTIME_CONFIG_OVERRIDES_CACHE.clear()
            return
        _RUNTIME_CONFIG_OVERRIDES_CACHE.pop(_runtime_config_overrides_cache_key(config), None)


def _decode_journal_state_value(row: dict[str, Any] | None) -> str | None:
    if not row:
        return None
    if row.get("value_compressed") and row.get("value_encoding") == "zlib+base64":
        try:
            raw = zlib.decompress(base64.b64decode(str(row.get("value_compressed"))))
            return raw.decode("utf-8")
        except Exception:
            return None
    value = row.get("value")
    return None if value is None else str(value)


def _runtime_config_overrides_allowed(overrides: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    if not isinstance(overrides, dict):
        return clean

    position_sizing = overrides.get("position_sizing")
    if isinstance(position_sizing, dict) and "base_margin_usdt" in position_sizing:
        clean.setdefault("position_sizing", {})["base_margin_usdt"] = position_sizing["base_margin_usdt"]

    risk = overrides.get("risk")
    if isinstance(risk, dict):
        allowed_risk = {
            key: risk[key]
            for key in ("order_usdt", "max_active_trades")
            if key in risk
        }
        if allowed_risk:
            clean["risk"] = allowed_risk

    exchange = overrides.get("exchange")
    if isinstance(exchange, dict) and "leverage" in exchange:
        clean.setdefault("exchange", {})["leverage"] = exchange["leverage"]

    paper_trading = overrides.get("paper_trading")
    if isinstance(paper_trading, dict) and "max_active_trades" in paper_trading:
        clean.setdefault("paper_trading", {})["max_active_trades"] = paper_trading["max_active_trades"]

    trading_risk = overrides.get("trading_risk")
    if isinstance(trading_risk, dict) and "max_concurrent_positions" in trading_risk:
        clean.setdefault("trading_risk", {})["max_concurrent_positions"] = trading_risk["max_concurrent_positions"]

    return clean


def _load_runtime_config_overrides(config: dict[str, Any]) -> dict[str, Any] | None:
    if not runtime_config_overrides_should_attempt(config):
        return None

    cache_key = _runtime_config_overrides_cache_key(config)
    ttl = _runtime_config_overrides_cache_ttl(config)
    now = time.monotonic()
    if ttl > 0:
        with _RUNTIME_CONFIG_OVERRIDES_LOCK:
            cached = _RUNTIME_CONFIG_OVERRIDES_CACHE.get(cache_key)
            if cached and cached[0] > now:
                return deepcopy(cached[1])

    try:
        from .atlas_mirror import atlas_database_for_collection

        settings = _runtime_config_overrides_settings(config)
        state_key = str(settings.get("state_key", RUNTIME_CONFIG_OVERRIDES_STATE_KEY) or RUNTIME_CONFIG_OVERRIDES_STATE_KEY)
        row = atlas_database_for_collection(config, "journal_state")["journal_state"].find_one(
            {"_id": state_key},
            {"_id": 0, "value": 1, "value_compressed": 1, "value_encoding": 1},
        )
        raw = _decode_journal_state_value(row)
        payload = json.loads(raw) if raw else {}
        overrides = payload.get("overrides") if isinstance(payload, dict) else {}
        clean = _runtime_config_overrides_allowed(overrides if isinstance(overrides, dict) else {})
    except Exception as exc:
        LOGGER.warning("Skipping runtime config overrides: %s", exc)
        clean = None

    if ttl > 0:
        with _RUNTIME_CONFIG_OVERRIDES_LOCK:
            _RUNTIME_CONFIG_OVERRIDES_CACHE[cache_key] = (now + ttl, deepcopy(clean))
    return clean


def runtime_config_override_payload(overrides: dict[str, Any], *, source: str = "ui") -> str:
    clean = _runtime_config_overrides_allowed(overrides)
    return json.dumps(
        {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "overrides": clean,
        },
        ensure_ascii=False,
    )


def load_config(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return deepcopy(DEFAULT_CONFIG)

    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        user_config = yaml.safe_load(handle) or {}
    config = deep_merge(DEFAULT_CONFIG, user_config)
    config["_config_dir"] = str(config_path.resolve().parent)
    config["_config_path"] = str(config_path.resolve())
    runtime_overrides = _load_runtime_config_overrides(config)
    if runtime_overrides:
        config = deep_merge(config, runtime_overrides)
        config["_runtime_config_overrides_applied"] = True
    return config


def project_path(config: dict[str, Any], value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return Path(config.get("_config_dir") or ".").resolve() / path
