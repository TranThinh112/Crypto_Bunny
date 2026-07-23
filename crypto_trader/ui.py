from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from market_pattern_engine.api.router import router as market_pattern_router

from . import __version__
from .atlas_mirror import atlas_runtime_is_primary, atlas_runtime_is_read_only
from .capital import (
    analyze_configuration_change,
    apply_trading_config,
    calculate_position_size,
    check_capital_allocation,
    configuration_impact_history,
    configuration_versions,
    current_trading_config,
    latest_capital_reserve_state,
    latest_capital_snapshot,
    latest_position_size_calculation,
    position_size_history,
    refresh_capital_reserve_state,
    save_configuration_impact_report,
    save_position_size_calculation,
    sync_capital_from_okx,
)
from .config import (
    DEFAULT_CONFIG,
    RUNTIME_CONFIG_OVERRIDES_STATE_KEY,
    deep_merge,
    invalidate_runtime_config_overrides_cache,
    load_config,
    project_path,
    runtime_config_override_payload,
    runtime_config_overrides_should_attempt,
)
from .ai_coordinator import candidate_okx_review, next_internal_market_scan_at, run_internal_market_scan_if_due
from .codex_features import (
    activate_strategy_version,
    ai_trade_decision_stats,
    build_market_prompt_dto,
    build_prompt,
    call_openai_json,
    candidate_from_payload,
    close_trade_execution,
    create_ai_experiment,
    create_ai_trade_decision,
    create_strategy_version,
    current_market_regime,
    current_strategy_state,
    ensure_prompt_version,
    get_bunny_health_state,
    get_trading_system_state,
    list_ai_experiments,
    load_prompt_templates,
    market_regime_history,
    okx_review_explanation_vi,
    prompt_history,
    prompt_status,
    recent_ai_trade_decisions,
    recent_ai_call_history,
    refresh_bunny_health_state,
    replay_batch,
    replay_stats,
    replay_trade_execution,
    strategy_history,
    validate_entry,
)
from .dashboard_services import (
    attach_previous_system_checklist_snapshot,
    analytics_dashboard,
    refresh_system_checklist_snapshot,
    replay_dashboard_payload,
    scan_memory_dashboard,
    system_checklist_history as dashboard_system_checklist_history,
    system_checklist_payload,
    system_checklist_snapshot as dashboard_system_checklist_snapshot,
    system_checklist_summary as dashboard_system_checklist_summary,
    system_health_dashboard,
    timeframe_state_dashboard,
)
from .engine import (
    collect_lc_pipeline_candidates,
    force_mini_scan_from_latest_four_hour,
    format_wait_slot_notifications_view,
    load_lc_pipeline_candidate_cache,
    run_once,
    wait_slot_notification_timeline_messages,
)
from .lc_pipeline import (
    format_internal_lc_view,
    format_internal_notifications_view,
    internal_notification_timeline_messages,
    lc_pipeline_dashboard_payload,
    latest_lc_pipeline_four_hour_event,
    undecided_notification_timeline_messages,
    update_lc_internal_pipeline,
)
from .market import create_exchange
from .market_guard import (
    latest_market_guard_status,
    market_guard_block_status,
    market_guard_enabled,
    market_guard_interval,
    market_guard_notify_interval,
    run_market_guard,
)
from .models import RiskCheck
from .models import to_jsonable
from .notifier import (
    answer_callback_query,
    delete_telegram_message,
    edit_telegram_chat_message,
    fetch_telegram_updates,
    send_telegram_chat_message,
    send_telegram_message,
    set_telegram_startup_quiet_until,
    telegram_buttons_enabled,
    sync_telegram_commands,
    telegram_control_keyboard,
    telegram_leverage_keyboard,
    telegram_max_positions_keyboard,
    telegram_notify_scans,
    telegram_order_usdt_keyboard,
    telegram_setup_keyboard,
)
from .paper import simulate_paper_scan
from .reporting import (
    build_periodic_report_messages,
    format_execution_messages,
    format_balance_view,
    fetch_balance_snapshot,
    format_market_guard_message,
    format_market_scan_memory_view,
    format_pending_event_messages,
    format_positions_account_view,
    format_scan_message,
    format_telegram_menu,
    format_undecided_lc_view,
)
from .runtime_sync import sync_runtime_state
from .risk import active_trades_summary, evaluate_candidate
from .storage import (
    clear_dashboard_snapshot_cache,
    get_journal_state,
    latest_decision_payload,
    list_pending_orders,
    open_pending_symbols,
    list_paper_trades,
    prune_market_scan_observations,
    purge_deprecated_journal_state,
    recent_market_scan_memory,
    refresh_pending_order,
    run_storage_maintenance,
    set_journal_state,
    storage_stats,
)
from .sizing import STATE_KEY as SIZING_STATE_KEY
from .trailing_stop import run_trailing_stop_cycle


LOGGER = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"
PRICE_CACHE_TTL_SECONDS = 55
TELEGRAM_VIEW_CACHE_TTL_SECONDS = 4
SYSTEM_ERROR_NOTIFY_COOLDOWN_SECONDS = 900
STARTUP_TELEGRAM_MESSAGE = "\U0001f7e2 Bot Crypto \u0111\u00e3 kh\u1edfi \u0111\u1ed9ng"
_SYSTEM_ERROR_NOTIFY_LOCK = threading.Lock()
_SYSTEM_ERROR_NOTIFICATIONS: dict[str, tuple[str, datetime]] = {}


def _clean_system_error_text(error: Any) -> str:
    text = str(error or "").strip()
    if not text:
        return "Lỗi không xác định"
    return " ".join(text.split())


def _system_error_group(component: str, error_text: str) -> str:
    lower = error_text.lower()
    if (
        "read operation timed out" in lower
        or "serverselectiontimeouterror" in lower
        or "sockettimeoutms" in lower
        or ".mongodb.net:27017" in lower
    ):
        return "MongoDB Atlas timeout"
    if "okx requires" in lower or "apikey" in lower:
        return "OKX credential"
    return component


def _system_error_action(error_group: str) -> str:
    if error_group == "MongoDB Atlas timeout":
        return "Hệ thống sẽ tự thử lại. Nếu lặp lại, cần kiểm tra Atlas/network hoặc tăng timeout."
    if error_group == "OKX credential":
        return "Cần kiểm tra biến OKX_API_KEY/OKX_SECRET/OKX_PASSPHRASE trên Railway."
    return "Hệ thống sẽ tự động thử lại."


def _is_railway_runtime() -> bool:
    return bool(os.getenv("RAILWAY_SERVICE_ID") or os.getenv("RAILWAY_DEPLOYMENT_ID"))


def _notify_system_error(config: dict[str, Any], component: str, error: Any) -> bool:
    now = datetime.now(timezone.utc)
    raw_error_text = _clean_system_error_text(error)
    if "cannot schedule new futures after interpreter shutdown" in raw_error_text.lower():
        LOGGER.info("Suppressing background shutdown error from %s: %s", component, raw_error_text)
        return False
    message_text = raw_error_text
    error_group = _system_error_group(component, message_text)
    fingerprint_source = f"{error_group}|{message_text[:300]}"
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8", errors="replace")).hexdigest()[:16]
    notification_key = error_group
    with _SYSTEM_ERROR_NOTIFY_LOCK:
        previous = _SYSTEM_ERROR_NOTIFICATIONS.get(notification_key)
        if previous and previous[0] == fingerprint:
            age_seconds = (now - previous[1]).total_seconds()
            if age_seconds < SYSTEM_ERROR_NOTIFY_COOLDOWN_SECONDS:
                return False
        _SYSTEM_ERROR_NOTIFICATIONS[notification_key] = (fingerprint, now)
    LOGGER.error("%s failed: %s", component, message_text)
    if not _is_railway_runtime():
        LOGGER.warning("Suppressing Telegram system error from local runtime: %s", component)
        return False
    return send_telegram_message(
        config,
        "\U0001f6a8 LỖI HỆ THỐNG\n"
        f"Module: {component}\n"
        f"Nhóm lỗi: {error_group}\n"
        f"Thời gian: {now.astimezone(_system_timezone(config)).strftime('%d/%m/%Y %H:%M:%S')}\n"
        f"Lỗi: {message_text[:1200]}\n"
        f"Hành động: {_system_error_action(error_group)}",
        with_buttons=False,
        replace_previous=False,
    )


def _file_signature(path: Path) -> str | None:
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(payload).hexdigest()[:12]


def _manual_okx_pending_record(config: dict[str, Any], lc_id: int) -> dict[str, Any] | None:
    for record in list_pending_orders(config, status="ACTIVE", limit=500):
        try:
            journal_id = int(record.get("journal_id") or 0)
        except (TypeError, ValueError):
            journal_id = 0
        try:
            order_id = int(record.get("id") or 0)
        except (TypeError, ValueError):
            order_id = 0
        if lc_id in {journal_id, order_id}:
            return record
    return None


def _manual_okx_candidate_from_record(record: dict[str, Any]) -> Any | None:
    try:
        payload = json.loads(str(record.get("payload_json") or "{}"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return candidate_from_payload(payload)
    except Exception:
        return None


def _code_signature() -> dict[str, Any]:
    base_dir = Path(__file__).resolve().parent
    targets = {
        "ui": base_dir / "ui.py",
        "lc_pipeline": base_dir / "lc_pipeline.py",
        "reporting": base_dir / "reporting.py",
        "codex_features": base_dir / "codex_features.py",
    }
    signatures: dict[str, Any] = {}
    for key, path in targets.items():
        signatures[key] = {
            "file": path.name,
            "sha12": _file_signature(path),
        }
    combined = hashlib.sha256(
        "|".join(f"{key}:{item['sha12'] or '-'}" for key, item in signatures.items()).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "combined_sha16": combined,
        "files": signatures,
    }


def _build_runtime_metadata() -> dict[str, Any]:
    return {
        "app_version": __version__,
        "build": {
            "commit_sha": os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("SOURCE_COMMIT"),
            "deployment_id": os.getenv("RAILWAY_DEPLOYMENT_ID"),
            "public_domain": os.getenv("RAILWAY_PUBLIC_DOMAIN"),
            "service_id": os.getenv("RAILWAY_SERVICE_ID"),
            "environment": os.getenv("RAILWAY_ENVIRONMENT_NAME"),
        },
        "code_signature": _code_signature(),
        "feature_flags": {
            "four_hour_fixed_boundaries": True,
            "trade_execution_close_reason": True,
            "trade_execution_close_telegram_v2": True,
        },
    }
TELEGRAM_TIMELINE_CACHE_TTL_SECONDS = 4
TELEGRAM_COMMANDS_SYNC_INTERVAL_SECONDS = 600
MIN_LEVERAGE = 5
MAX_LEVERAGE = 25
MIN_BASE_MARGIN_USDT = 1.0
SCAN_TELEGRAM_SLOT_KEY = "telegram_last_scan_notify_slot"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _config_file(path: str | Path) -> Path:
    config_path = Path(path).expanduser()
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    return config_path.resolve()


def _persist_runtime_config_overrides(config: dict[str, Any], overrides: dict[str, Any], *, source: str) -> None:
    if not runtime_config_overrides_should_attempt(config):
        return
    raw = get_journal_state(config, RUNTIME_CONFIG_OVERRIDES_STATE_KEY)
    existing_overrides: dict[str, Any] = {}
    if raw:
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict) and isinstance(payload.get("overrides"), dict):
                existing_overrides = payload["overrides"]
        except json.JSONDecodeError:
            existing_overrides = {}
    merged = deep_merge(existing_overrides, overrides)
    set_journal_state(config, RUNTIME_CONFIG_OVERRIDES_STATE_KEY, runtime_config_override_payload(merged, source=source))
    invalidate_runtime_config_overrides_cache(config)


def _save_leverage(config_path: str | Path, leverage: int) -> dict[str, Any]:
    path = _config_file(config_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Config file not found: {path}")
    current_config = load_config(path)
    with path.open("r", encoding="utf-8") as handle:
        user_config = yaml.safe_load(handle) or {}
    exchange = user_config.setdefault("exchange", {})
    exchange["leverage"] = leverage
    sizing = user_config.setdefault("position_sizing", {})
    try:
        base_margin = float(
            current_config.get("position_sizing", {}).get(
                "base_margin_usdt",
                sizing.get("base_margin_usdt", DEFAULT_CONFIG["position_sizing"]["base_margin_usdt"]),
            )
            or 0
        )
    except (TypeError, ValueError):
        base_margin = float(DEFAULT_CONFIG["position_sizing"]["base_margin_usdt"])
    if base_margin > 0:
        risk = user_config.setdefault("risk", {})
        risk["order_usdt"] = round(base_margin * leverage, 4)

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(user_config, handle, sort_keys=False, allow_unicode=True)
    tmp_path.replace(path)
    updated = load_config(path)
    _persist_runtime_config_overrides(
        updated,
        {
            "exchange": {"leverage": leverage},
            "risk": {"order_usdt": round(base_margin * leverage, 4)},
        },
        source="ui.leverage",
    )
    return load_config(path)


def _max_base_margin_usdt(config: dict[str, Any]) -> float:
    default_max = float(DEFAULT_CONFIG["position_sizing"].get("max_margin_usdt", 20) or 20)
    try:
        value = float(config.get("position_sizing", {}).get("max_margin_usdt", default_max) or default_max)
    except (TypeError, ValueError):
        return default_max
    return max(MIN_BASE_MARGIN_USDT, value)


def _save_base_margin(config_path: str | Path, margin_usdt: float) -> dict[str, Any]:
    path = _config_file(config_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Config file not found: {path}")
    current_config = load_config(path)
    with path.open("r", encoding="utf-8") as handle:
        user_config = yaml.safe_load(handle) or {}
    sizing = user_config.setdefault("position_sizing", {})
    sizing["base_margin_usdt"] = round(margin_usdt, 4)
    try:
        leverage = float(
            current_config.get("exchange", {}).get(
                "leverage",
                user_config.get("exchange", {}).get("leverage", DEFAULT_CONFIG["exchange"]["leverage"]),
            )
            or 1
        )
    except (TypeError, ValueError):
        leverage = float(DEFAULT_CONFIG["exchange"]["leverage"])
    risk = user_config.setdefault("risk", {})
    risk["order_usdt"] = round(margin_usdt * leverage, 4)

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(user_config, handle, sort_keys=False, allow_unicode=True)
    tmp_path.replace(path)
    updated = load_config(path)
    _persist_runtime_config_overrides(
        updated,
        {
            "position_sizing": {"base_margin_usdt": round(margin_usdt, 4)},
            "risk": {"order_usdt": round(margin_usdt * leverage, 4)},
        },
        source="ui.order_usdt",
    )
    return load_config(path)


def _save_max_positions(config_path: str | Path, max_positions: int) -> dict[str, Any]:
    path = _config_file(config_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        user_config = yaml.safe_load(handle) or {}
    risk = user_config.setdefault("risk", {})
    risk["max_active_trades"] = max_positions
    paper = user_config.setdefault("paper_trading", {})
    paper["max_active_trades"] = max_positions
    trading_risk = user_config.setdefault("trading_risk", {})
    trading_risk["max_concurrent_positions"] = max_positions

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(user_config, handle, sort_keys=False, allow_unicode=True)
    tmp_path.replace(path)
    updated = load_config(path)
    _persist_runtime_config_overrides(
        updated,
        {
            "risk": {"max_active_trades": max_positions},
            "paper_trading": {"max_active_trades": max_positions},
            "trading_risk": {"max_concurrent_positions": max_positions},
        },
        source="ui.max_positions",
    )
    return load_config(path)


def _sync_idle_sizing_state(config: dict[str, Any], margin_usdt: float) -> None:
    raw = get_journal_state(config, SIZING_STATE_KEY)
    if not raw:
        return
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        return
    cycle_pnl = float(state.get("cycle_pnl_usdt") or 0)
    recovery_step = int(state.get("recovery_step") or 0)
    if state.get("blocked") or recovery_step > 0 or abs(cycle_pnl) > 1e-9:
        return
    state["next_margin_usdt"] = round(margin_usdt, 4)
    set_journal_state(config, SIZING_STATE_KEY, json.dumps(state, ensure_ascii=False))


def _margin_label(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _effective_order_usdt(config: dict[str, Any]) -> float:
    sizing = config.get("position_sizing", {})
    leverage = float(config.get("exchange", {}).get("leverage", 1) or 1)
    if sizing.get("enabled", False):
        margin = float(sizing.get("base_margin_usdt", 0) or 0)
        return round(margin * leverage, 4)
    return round(float(config.get("risk", {}).get("order_usdt", 0) or 0), 4)


def _order_usdt_menu_message(config: dict[str, Any]) -> str:
    sizing = config.get("position_sizing", {})
    margin = float(sizing.get("base_margin_usdt", 2) or 2)
    leverage = float(config.get("exchange", {}).get("leverage", 1) or 1)
    notional = margin * leverage
    max_margin = _max_base_margin_usdt(config)
    return (
        "ðŸ’° CÃ i USDT cho lá»‡nh sau\n"
        f"Äang dÃ¹ng: {_margin_label(margin)} USDT margin/lá»‡nh\n"
        f"GiÃ¡ trá»‹ vá»‹ tháº¿ Æ°á»›c tÃ­nh: {_margin_label(notional)} USDT ({leverage:g}x)\n"
        f"Giá»›i háº¡n: {_margin_label(MIN_BASE_MARGIN_USDT)}-{_margin_label(max_margin)} USDT margin\n"
        "Chá»n nÃºt bÃªn dÆ°á»›i hoáº·c gá»­i /usdt 5"
    )


def _setup_menu_message(config: dict[str, Any]) -> str:
    sizing = config.get("position_sizing", {})
    margin = float(sizing.get("base_margin_usdt", 2) or 2)
    leverage = int(float(config.get("exchange", {}).get("leverage", 10) or 10))
    max_positions = int(float(config.get("risk", {}).get("max_active_trades", 1) or 1))
    return (
        "âš™ï¸ Setup giao dá»‹ch\n"
        f"USDT/lá»‡nh: {_margin_label(margin)} USDT\n"
        f"ÄÃ²n báº©y: {leverage}x\n"
        f"Max VT: {max_positions}\n"
        "Chon nut ben duoi hoac go /setup."
    )


def _set_base_margin_from_telegram(
    config_path: str | Path,
    config: dict[str, Any],
    raw_value: str,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    try:
        margin = float(raw_value)
    except (TypeError, ValueError):
        return config, "âš ï¸ USDT/lá»‡nh khÃ´ng há»£p lá»‡. VÃ­ dá»¥: /usdt 5", telegram_order_usdt_keyboard(config)
    max_margin = _max_base_margin_usdt(config)
    if not math.isfinite(margin) or margin < MIN_BASE_MARGIN_USDT or margin > max_margin:
        return (
            config,
            f"âš ï¸ Chá»‰ nháº­n tá»« {_margin_label(MIN_BASE_MARGIN_USDT)} Ä‘áº¿n {_margin_label(max_margin)} USDT margin/lá»‡nh.",
            telegram_order_usdt_keyboard(config),
        )

    updated = _save_base_margin(config_path, margin)
    _sync_idle_sizing_state(updated, margin)
    leverage = float(updated.get("exchange", {}).get("leverage", 1) or 1)
    notional = margin * leverage
    message = (
        "âœ… ÄÃ£ lÆ°u USDT cho lá»‡nh sau\n"
        f"Margin/lá»‡nh: {_margin_label(margin)} USDT\n"
        f"GiÃ¡ trá»‹ vá»‹ tháº¿ Æ°á»›c tÃ­nh: {_margin_label(notional)} USDT ({leverage:g}x)\n"
        "Ãp dá»¥ng tá»« lá»‡nh má»Ÿ sau."
    )
    return updated, message, telegram_order_usdt_keyboard(updated)


def _leverage_menu_message(config: dict[str, Any]) -> str:
    leverage = int(float(config.get("exchange", {}).get("leverage", 10) or 10))
    margin = float(config.get("position_sizing", {}).get("base_margin_usdt", 2) or 2)
    notional = margin * leverage
    return (
        "âš™ï¸ CÃ i Ä‘Ã²n báº©y cho lá»‡nh sau\n"
        f"Äang dÃ¹ng: {leverage}x\n"
        f"Margin/lá»‡nh hiá»‡n táº¡i: {_margin_label(margin)} USDT\n"
        f"GiÃ¡ trá»‹ vá»‹ tháº¿ Æ°á»›c tÃ­nh: {_margin_label(notional)} USDT\n"
        f"Giá»›i háº¡n: {MIN_LEVERAGE}-{MAX_LEVERAGE}x\n"
        "Chá»n nÃºt bÃªn dÆ°á»›i hoáº·c gá»­i /lev 15"
    )


def _set_leverage_from_telegram(
    config_path: str | Path,
    config: dict[str, Any],
    raw_value: str,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    try:
        leverage = int(float(raw_value))
    except (TypeError, ValueError):
        return config, "âš ï¸ ÄÃ²n báº©y khÃ´ng há»£p lá»‡. VÃ­ dá»¥: /lev 15", telegram_leverage_keyboard(config)
    if leverage < MIN_LEVERAGE or leverage > MAX_LEVERAGE:
        return (
            config,
            f"âš ï¸ Chá»‰ nháº­n Ä‘Ã²n báº©y tá»« {MIN_LEVERAGE}x Ä‘áº¿n {MAX_LEVERAGE}x.",
            telegram_leverage_keyboard(config),
        )

    updated = _save_leverage(config_path, leverage)
    margin = float(updated.get("position_sizing", {}).get("base_margin_usdt", 2) or 2)
    notional = margin * leverage
    message = (
        "âœ… ÄÃ£ lÆ°u Ä‘Ã²n báº©y cho lá»‡nh sau\n"
        f"ÄÃ²n báº©y: {leverage}x\n"
        f"Margin/lá»‡nh: {_margin_label(margin)} USDT\n"
        f"GiÃ¡ trá»‹ vá»‹ tháº¿ Æ°á»›c tÃ­nh: {_margin_label(notional)} USDT\n"
        "Ãp dá»¥ng tá»« lá»‡nh má»Ÿ sau."
    )
    return updated, message, telegram_leverage_keyboard(updated)


def _read_report(config: dict[str, Any]) -> dict[str, Any]:
    path = project_path(config, config.get("report_path", "reports/latest_decision.json"))
    if not path.exists():
        latest = latest_decision_payload(config)
        return {
            "report_exists": latest is not None,
            "report_path": str(path),
            "decision": latest,
            "source": "atlas" if latest else "none",
            "paper_state": _paper_state(config),
        }
    with path.open("r", encoding="utf-8") as handle:
        return {
            "report_exists": True,
            "report_path": str(path),
            "decision": json.load(handle),
            "source": "json",
            "paper_state": _paper_state(config),
        }


def _paper_state(config: dict[str, Any]) -> dict[str, Any]:
    trades = list_paper_trades(config, limit=20)
    open_trades = [trade for trade in trades if trade.get("status") == "OPEN"]
    return {
        "enabled": bool(config.get("paper_trading", {}).get("enabled", True)),
        "auto_scan_enabled": bool(config.get("paper_trading", {}).get("auto_scan_enabled", True)),
        "scan_interval_seconds": int(config.get("paper_trading", {}).get("scan_interval_seconds", 600)),
        "open_trades": open_trades,
        "trades": trades,
    }


def _decision_focus(decision: dict[str, Any] | None) -> dict[str, Any]:
    if not decision:
        return {"symbol": None, "side": None, "status": "none"}
    selected = decision.get("selected")
    if selected:
        return {"symbol": selected.get("symbol"), "side": selected.get("side"), "status": "selected"}
    candidates = decision.get("candidates") or []
    if candidates:
        candidate = candidates[0]
        return {"symbol": candidate.get("symbol"), "side": candidate.get("side"), "status": "candidate"}
    return {"symbol": None, "side": None, "status": "none"}


def _okx_demo_status(config: dict[str, Any]) -> dict[str, Any]:
    load_dotenv()
    names = {
        "api_key": config["exchange"].get("api_key_env", "OKX_API_KEY"),
        "secret": config["exchange"].get("secret_env", "OKX_SECRET"),
        "passphrase": config["exchange"].get("passphrase_env", "OKX_PASSPHRASE"),
    }
    missing = [name for name in names.values() if not os.getenv(name, "")]
    mode = str(config.get("mode", "dry_run"))
    ready = mode == "demo" and not missing
    if mode != "demo":
        message = f"OKX demo is inactive because mode is {mode}"
    elif missing:
        message = f"Missing OKX demo env: {', '.join(missing)}"
    else:
        message = "OKX demo credentials are configured"
    return {
        "mode": mode,
        "ready": ready,
        "missing_env": missing,
        "simulated_trading_header": mode == "demo",
        "message": message,
    }


def _automation_interval(config: dict[str, Any]) -> int:
    automation = config.get("automation", {})
    fallback = config.get("paper_trading", {}).get("scan_interval_seconds", 60)
    return max(60, int(automation.get("scan_interval_seconds", fallback) or 60))


def _telegram_startup_quiet_seconds(config: dict[str, Any]) -> int:
    raw = config.get("notifications", {}).get("telegram", {}).get("startup_quiet_seconds", 300)
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 300


def _telegram_startup_quiet_active(app: FastAPI, now: datetime | None = None) -> bool:
    quiet_until = getattr(app.state, "telegram_startup_quiet_until", None)
    if not isinstance(quiet_until, datetime):
        return False
    now = now or datetime.now(timezone.utc)
    if quiet_until.tzinfo is None:
        quiet_until = quiet_until.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc) < quiet_until.astimezone(timezone.utc)


def _telegram_disabled_config(config: dict[str, Any]) -> dict[str, Any]:
    return deep_merge(config, {"notifications": {"telegram": {"enabled": False}}})


def _telegram_background_config(app: FastAPI, config: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    if _telegram_startup_quiet_active(app, now):
        return _telegram_disabled_config(config)
    return config


def _lc_pipeline_worker_interval(config: dict[str, Any]) -> int:
    internal = config.get("ai", {}).get("internal", {})
    fallback = _automation_interval(config)
    return max(60, int(internal.get("lc_pipeline_worker_interval_seconds", fallback) or fallback))


def _lc_pipeline_slot_poll_interval(config: dict[str, Any]) -> int:
    internal = config.get("ai", {}).get("internal", {})
    return max(5, min(60, int(internal.get("lc_pipeline_slot_poll_seconds", 10) or 10)))


def _next_automation_cycle_at(now: datetime, interval_seconds: int) -> datetime:
    interval = max(60, int(interval_seconds or 60))
    current = int(now.timestamp())
    next_tick = ((current // interval) + 1) * interval
    return datetime.fromtimestamp(next_tick, tz=timezone.utc)


def _automation_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("automation", {}).get("enabled", True)) and atlas_runtime_is_primary(config)


def _lc_pipeline_worker_enabled(config: dict[str, Any]) -> bool:
    internal = config.get("ai", {}).get("internal", {})
    return bool(internal.get("lc_pipeline_enabled", True)) and _automation_enabled(config)


def _app_is_stopping(app: FastAPI) -> bool:
    stop_event = getattr(app.state, "automation_stop", None)
    return bool(getattr(app.state, "shutdown_started", False)) or bool(
        stop_event is not None and stop_event.is_set()
    )


def _is_interpreter_shutdown_error(exc: BaseException) -> bool:
    return "cannot schedule new futures after interpreter shutdown" in str(exc).lower()


def _should_suppress_background_error(app: FastAPI, exc: BaseException) -> bool:
    return _app_is_stopping(app) or _is_interpreter_shutdown_error(exc)


def _automation_should_execute(config: dict[str, Any]) -> tuple[bool, str]:
    mode = str(config.get("mode", "dry_run"))
    automation = config.get("automation", {})
    guard_block = market_guard_block_status(config)
    if guard_block.get("active"):
        return False, f"Market guard active until {guard_block.get('blocked_until')}"
    if mode == "demo":
        status = _okx_demo_status(config)
        if not status["ready"]:
            return False, status["message"]
        if not automation.get("execute_demo", True):
            return False, "automation.execute_demo is false"
        return True, "OKX demo auto trade is enabled"
    if mode == "live":
        if not automation.get("execute_live", False):
            return False, "automation.execute_live is false"
        return True, "OKX live auto trade is enabled"
    return False, f"mode is {mode}; analysis only"


def _automation_status_payload(app: FastAPI) -> dict[str, Any]:
    status = getattr(app.state, "automation_status", {}).copy()
    status["lock_held"] = bool(getattr(app.state, "lock", None) and app.state.lock.locked())
    guard_status = getattr(app.state, "market_guard_status", None)
    if guard_status is None:
        try:
            guard_status = latest_market_guard_status(load_config(app.state.config_path))
        except Exception:
            guard_status = None
    if guard_status is not None:
        status["market_guard"] = guard_status
    try:
        config = load_config(app.state.config_path)
        status["enabled"] = _automation_enabled(config)
        status["interval_seconds"] = _automation_interval(config)
        status["mode"] = config.get("mode", "dry_run")
    except Exception as exc:
        status.setdefault("error", str(exc))
    return status


def _lc_pipeline_status_payload(app: FastAPI) -> dict[str, Any]:
    status = getattr(app.state, "lc_pipeline_status", {}).copy()
    try:
        config = load_config(app.state.config_path)
        status["enabled"] = _lc_pipeline_worker_enabled(config)
        status["interval_seconds"] = _lc_pipeline_worker_interval(config)
        status["mode"] = config.get("mode", "dry_run")
    except Exception as exc:
        status.setdefault("error", str(exc))
    return status


def _system_timezone(config: dict[str, Any] | None = None) -> tzinfo:
    config = config or {}
    timezone_name = (
        config.get("timezone")
        or config.get("ai", {}).get("internal", {}).get("market_scan_timezone")
        or "Asia/Ho_Chi_Minh"
    )
    try:
        return ZoneInfo(str(timezone_name))
    except Exception:
        return timezone(timedelta(hours=7), "Asia/Ho_Chi_Minh")


def _today_key(config: dict[str, Any] | None = None) -> str:
    return datetime.now(_system_timezone(config)).date().isoformat()


def _scan_notification_slot_id(config: dict[str, Any], now: datetime) -> str:
    local_now = now.astimezone(_system_timezone(config))
    slot_minute = (local_now.minute // 15) * 15
    slot = local_now.replace(minute=slot_minute, second=0, microsecond=0)
    return slot.isoformat()


def _periodic_scan_notification_due(config: dict[str, Any], now: datetime) -> bool:
    if not telegram_notify_scans(config):
        return False
    local_now = now.astimezone(_system_timezone(config))
    if local_now.minute % 15 != 0:
        return False
    slot_id = _scan_notification_slot_id(config, now)
    return get_journal_state(config, SCAN_TELEGRAM_SLOT_KEY) != slot_id


def _remember_periodic_scan_notification(config: dict[str, Any], now: datetime) -> None:
    set_journal_state(config, SCAN_TELEGRAM_SLOT_KEY, _scan_notification_slot_id(config, now))


def _publish_automation_status(app: FastAPI, status: dict[str, Any], **fields: Any) -> None:
    status.update(fields)
    app.state.automation_status = dict(status)


def _set_automation_phase(
    app: FastAPI,
    status: dict[str, Any],
    phase: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> None:
    _publish_automation_status(
        app,
        status,
        automation_phase=phase,
        automation_phase_started_at=datetime.now(timezone.utc).isoformat(),
        automation_phase_metadata=metadata or None,
    )




def _storage_maintenance_settings(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("storage_maintenance", {})


def _storage_maintenance_enabled(config: dict[str, Any]) -> bool:
    return bool(_storage_maintenance_settings(config).get("enabled", True))


def _storage_maintenance_interval(config: dict[str, Any]) -> int:
    raw = _storage_maintenance_settings(config).get("interval_seconds", 900)
    try:
        return max(60, int(raw or 900))
    except (TypeError, ValueError):
        return 900


def _maybe_run_storage_maintenance(
    app: FastAPI,
    config: dict[str, Any],
    *,
    now: datetime,
    source: str,
) -> None:
    if not atlas_runtime_is_primary(config) or not _storage_maintenance_enabled(config):
        return
    interval = _storage_maintenance_interval(config)
    last_started_at = getattr(app.state, "storage_maintenance_started_at", None)
    if isinstance(last_started_at, datetime) and (now - last_started_at).total_seconds() < interval:
        return
    lock = getattr(app.state, "storage_maintenance_lock", None)
    if lock is None or not lock.acquire(blocking=False):
        return
    app.state.storage_maintenance_started_at = now
    try:
        result = run_storage_maintenance(
            config,
            emergency=bool(_storage_maintenance_settings(config).get("emergency", False)),
            include_stats=False,
        )
        app.state.storage_maintenance_finished_at = datetime.now(timezone.utc)
        app.state.storage_maintenance_last_result = result
        if result.get("errors"):
            LOGGER.warning("Storage maintenance (%s) completed with errors: %s", source, result["errors"])
    except Exception as exc:
        app.state.storage_maintenance_last_result = {
            "ok": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "error": str(exc),
        }
        _notify_system_error(config, f"Storage maintenance ({source})", exc)
    finally:
        lock.release()


def _run_automation_cycle(app: FastAPI) -> None:
    now = datetime.now(timezone.utc)
    config = load_config(app.state.config_path)
    interval = _automation_interval(config)
    next_scan_at = _next_automation_cycle_at(now, interval)
    payload: dict[str, Any] | None = None
    status: dict[str, Any] = {
        "enabled": _automation_enabled(config),
        "interval_seconds": interval,
        "mode": config.get("mode", "dry_run"),
        "last_started_at": now.isoformat(),
        "next_scan_at": next_scan_at.isoformat(),
    }
    _maybe_run_storage_maintenance(app, config, now=now, source="automation")
    if not status["enabled"]:
        status["last_result"] = "disabled"
        app.state.automation_status = status
        return

    execute, reason = _automation_should_execute(config)
    status["execute"] = execute
    status["execute_reason"] = reason
    _publish_automation_status(app, status)

    if not app.state.lock.acquire(blocking=False):
        status["last_result"] = "skipped_busy"
        _publish_automation_status(app, status, automation_phase="skipped_busy")
        return

    try:
        _set_automation_phase(app, status, "runtime_sync")
        try:
            sync_runtime_state(config)
        except Exception as sync_exc:
            status["runtime_sync_error"] = str(sync_exc)
            _publish_automation_status(app, status)
            _notify_system_error(config, "Äá»“ng bá»™ OKX/MongoDB", sync_exc)

        _set_automation_phase(app, status, "trailing_stop")
        try:
            status["trailing_stop"] = run_trailing_stop_cycle(config)
        except Exception as trailing_exc:
            status["trailing_stop_error"] = str(trailing_exc)
            _publish_automation_status(app, status)
            _notify_system_error(config, "Trailing Stop", trailing_exc)

        def _progress(phase: str, metadata: dict[str, Any] | None = None) -> None:
            _set_automation_phase(app, status, phase, metadata=metadata)

        _set_automation_phase(app, status, "run_once")
        decision_result = run_once(config, execute=execute, progress_callback=_progress)
        payload = to_jsonable(decision_result)
        execution = payload.get("execution") or {}
        execution_raw = execution.get("raw") if isinstance(execution.get("raw"), dict) else {}
        if execution.get("submitted") and execution_raw.get("local_pending"):
            last_result = "pending_created"
        else:
            last_result = "order_submitted" if execution.get("submitted") else "no_order"
        risk = payload.get("risk_check") or {}
        selected = payload.get("selected") or {}
        top = (payload.get("candidates") or [{}])[0] or {}
        status.update(
            {
                "last_finished_at": datetime.now(timezone.utc).isoformat(),
                "last_result": last_result,
                "automation_phase": "completed",
                "action": payload.get("action"),
                "selected_symbol": selected.get("symbol"),
                "top_symbol": top.get("symbol"),
                "top_confidence": top.get("confidence"),
                "risk_passed": risk.get("passed"),
                "risk_reasons": risk.get("reasons") or [],
                "execution_submitted": bool(execution.get("submitted") and not execution_raw.get("local_pending")),
                "order_id": execution.get("order_id"),
            }
        )
    except Exception as exc:
        status.update(
            {
                "last_finished_at": datetime.now(timezone.utc).isoformat(),
                "last_result": "error",
                "automation_phase": "error",
                "error": str(exc),
            }
        )
        _notify_system_error(config, "Automation", exc)
    finally:
        _publish_automation_status(app, status)
        app.state.lock.release()

    try:
        checklist = refresh_system_checklist_snapshot(config, automation=status)
        failed_modules = [
            item for item in checklist.get("modules") or []
            if str(item.get("status") or "").lower() == "fail"
        ]
        if failed_modules:
            labels = ", ".join(
                f"#{item.get('number')} {item.get('name')}"
                for item in failed_modules
            )
            _notify_system_error(config, "Kiá»ƒm tra 8 module", f"Module Ä‘ang FAIL: {labels}")
    except Exception as exc:
        _notify_system_error(config, "Cáº­p nháº­t System Checklist", exc)
    try:
        timeframe_state_dashboard(config, force_refresh=True)
        scan_memory_dashboard(config, force_refresh=True)
        analytics_dashboard(config, force_refresh=True)
        replay_dashboard_payload(config, force_refresh=True)
        system_health_dashboard(config, force_refresh=True)
    except Exception as exc:
        _notify_system_error(config, "Dashboard", exc)

    messages: list[str] = []
    startup_quiet = _telegram_startup_quiet_active(app, now)
    should_notify_scan = telegram_notify_scans(config) and (
        status.get("last_result") == "error" or _periodic_scan_notification_due(config, now)
    )
    if should_notify_scan:
        if startup_quiet:
            if status.get("last_result") != "error":
                _remember_periodic_scan_notification(config, now)
        else:
            sent = send_telegram_message(
                config,
                format_scan_message(config, payload, status),
                with_buttons=False,
                replace_previous=False,
            )
            if sent and status.get("last_result") != "error":
                _remember_periodic_scan_notification(config, now)
    if startup_quiet:
        build_periodic_report_messages(config)
        return
    if payload:
        messages.extend(format_pending_event_messages(payload))
        messages.extend(format_execution_messages(payload))
    messages.extend(build_periodic_report_messages(config))
    for message in messages:
        send_telegram_message(config, message, with_buttons=False, replace_previous=False)


def _automation_worker(app: FastAPI) -> None:
    while not app.state.automation_stop.is_set():
        try:
            config = load_config(app.state.config_path)
            interval = _automation_interval(config)
        except Exception:
            interval = 60
        try:
            _run_automation_cycle(app)
        except Exception as exc:
            try:
                _notify_system_error(load_config(app.state.config_path), "Automation Worker", exc)
            except Exception:
                LOGGER.exception("Automation worker failed before Telegram notification")
        next_run_at = _next_automation_cycle_at(datetime.now(timezone.utc), interval)
        wait_seconds = max(1.0, (next_run_at - datetime.now(timezone.utc)).total_seconds())
        app.state.automation_stop.wait(wait_seconds)


def _run_lc_pipeline_worker_cycle(app: FastAPI) -> None:
    if _app_is_stopping(app):
        return
    now = datetime.now(timezone.utc)
    config = load_config(app.state.config_path)
    if _app_is_stopping(app):
        return
    notification_config = config
    interval = _lc_pipeline_worker_interval(config)
    next_scan_at = _next_automation_cycle_at(now, interval)
    status: dict[str, Any] = {
        "enabled": _lc_pipeline_worker_enabled(config),
        "interval_seconds": interval,
        "mode": config.get("mode", "dry_run"),
        "last_started_at": now.isoformat(),
        "next_scan_at": next_scan_at.isoformat(),
    }
    if not status["enabled"]:
        status["last_result"] = "disabled"
        app.state.lc_pipeline_status = status
        return

    if not app.state.lc_pipeline_lock.acquire(blocking=False):
        status["last_result"] = "skipped_busy"
        app.state.lc_pipeline_status = status
        return

    try:
        if _app_is_stopping(app):
            status["last_result"] = "stopping"
            return
        cycle_result = collect_lc_pipeline_candidates(notification_config)
        if _app_is_stopping(app):
            status["last_result"] = "stopping"
            return
        app.state.lc_pipeline_candidate_cache = cycle_result
        pipeline = update_lc_internal_pipeline(
            notification_config,
            list(cycle_result.get("candidates") or []),
            now=datetime.now(timezone.utc),
        )
        status.update(
            {
                "last_finished_at": datetime.now(timezone.utc).isoformat(),
                "last_result": "updated",
                "candidate_count": cycle_result.get("candidate_count"),
                "source_symbol_count": cycle_result.get("source_symbol_count"),
                "created_hourly": bool(pipeline.get("created_hourly")),
                "created_two_hour": bool(pipeline.get("created_two_hour")),
                "created_four_hour": bool(pipeline.get("created_four_hour")),
                "hourly_slot": pipeline.get("hourly_slot"),
                "two_hour_slot": pipeline.get("two_hour_slot"),
                "four_hour_slot": pipeline.get("four_hour_slot"),
            }
        )
    except Exception as exc:
        status.update(
            {
                "last_finished_at": datetime.now(timezone.utc).isoformat(),
                "last_result": "error",
                "error": str(exc),
            }
        )
        if _should_suppress_background_error(app, exc):
            LOGGER.info("Suppressing LC Pipeline error during shutdown: %s", exc)
        else:
            _notify_system_error(config, "LC Pipeline", exc)
    finally:
        app.state.lc_pipeline_status = status
        app.state.lc_pipeline_lock.release()


def _lc_pipeline_worker(app: FastAPI) -> None:
    while not app.state.automation_stop.is_set():
        try:
            config = load_config(app.state.config_path)
            interval = _lc_pipeline_worker_interval(config)
        except Exception:
            interval = 60
        try:
            if _app_is_stopping(app):
                return
            _run_lc_pipeline_worker_cycle(app)
        except Exception as exc:
            if _should_suppress_background_error(app, exc):
                LOGGER.info("Suppressing LC pipeline worker error during shutdown: %s", exc)
            else:
                try:
                    _notify_system_error(load_config(app.state.config_path), "LC Pipeline Worker", exc)
                except Exception:
                    LOGGER.exception("LC pipeline worker failed before Telegram notification")
        next_run_at = _next_automation_cycle_at(datetime.now(timezone.utc), interval)
        wait_seconds = max(1.0, (next_run_at - datetime.now(timezone.utc)).total_seconds())
        if app.state.automation_stop.wait(wait_seconds):
            return


def _run_lc_pipeline_slot_cycle(app: FastAPI) -> None:
    if _app_is_stopping(app):
        return
    now = datetime.now(timezone.utc)
    config = load_config(app.state.config_path)
    if _app_is_stopping(app):
        return
    notification_config = config
    if not _lc_pipeline_worker_enabled(config):
        return
    if not app.state.lc_pipeline_slot_lock.acquire(blocking=False):
        return
    try:
        cache = getattr(app.state, "lc_pipeline_candidate_cache", None) or {}
        if not cache:
            cache = load_lc_pipeline_candidate_cache(config) or {}
            if cache:
                app.state.lc_pipeline_candidate_cache = cache
        candidates = list(cache.get("candidates") or [])
        created_at_raw = cache.get("created_at")
        created_at = None
        if created_at_raw:
            try:
                created_at = datetime.fromisoformat(str(created_at_raw).replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                created_at = None
        max_age_seconds = max(
            60,
            int(config.get("ai", {}).get("internal", {}).get("lc_pipeline_candidate_cache_max_age_seconds", 900) or 900),
        )
        if not candidates or created_at is None:
            persisted = load_lc_pipeline_candidate_cache(config) or {}
            if persisted:
                cache = persisted
                app.state.lc_pipeline_candidate_cache = persisted
                candidates = list(cache.get("candidates") or [])
                created_at_raw = cache.get("created_at")
                if created_at_raw:
                    try:
                        created_at = datetime.fromisoformat(str(created_at_raw).replace("Z", "+00:00")).astimezone(timezone.utc)
                    except ValueError:
                        created_at = None
        if not candidates or created_at is None:
            return
        if (now - created_at).total_seconds() > max_age_seconds:
            persisted = load_lc_pipeline_candidate_cache(config) or {}
            persisted_created_at_raw = persisted.get("created_at") if persisted else None
            persisted_created_at = None
            if persisted_created_at_raw:
                try:
                    persisted_created_at = datetime.fromisoformat(
                        str(persisted_created_at_raw).replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                except ValueError:
                    persisted_created_at = None
            if persisted and persisted_created_at and (now - persisted_created_at).total_seconds() <= max_age_seconds:
                cache = persisted
                app.state.lc_pipeline_candidate_cache = persisted
                candidates = list(cache.get("candidates") or [])
                created_at = persisted_created_at
            else:
                return
        if not candidates or created_at is None:
            return
        if _app_is_stopping(app):
            return
        pipeline = update_lc_internal_pipeline(notification_config, candidates, now=now)
        if _app_is_stopping(app):
            return
        mini_scan = run_internal_market_scan_if_due(notification_config)
        current_status = getattr(app.state, "lc_pipeline_status", {}).copy()
        current_status.update(
            {
                "last_slot_check_at": now.isoformat(),
                "slot_cache_created_at": created_at.isoformat(),
                "created_hourly": bool(pipeline.get("created_hourly")),
                "created_two_hour": bool(pipeline.get("created_two_hour")),
                "created_four_hour": bool(pipeline.get("created_four_hour")),
                "hourly_slot": pipeline.get("hourly_slot"),
                "two_hour_slot": pipeline.get("two_hour_slot"),
                "four_hour_slot": pipeline.get("four_hour_slot"),
            }
        )
        if isinstance(mini_scan, dict):
            current_status.update(
                {
                    "mini_scan_created_at": mini_scan.get("created_at"),
                    "mini_scan_slot_id": mini_scan.get("slot_id"),
                    "mini_scan_status": mini_scan.get("status"),
                    "mini_scan_skipped": bool(mini_scan.get("skipped")),
                    "mini_scan_skip_reason": mini_scan.get("skip_reason"),
                }
            )
        app.state.lc_pipeline_status = current_status
    except Exception as exc:
        current_status = getattr(app.state, "lc_pipeline_status", {}).copy()
        current_status.update(
            {
                "last_slot_check_at": now.isoformat(),
                "last_result": "error",
                "error": str(exc),
            }
        )
        app.state.lc_pipeline_status = current_status
        _notify_system_error(config, "Lá»‹ch pool 1h/2h/4h vÃ  Mini", exc)
    finally:
        app.state.lc_pipeline_slot_lock.release()


def _lc_pipeline_slot_worker(app: FastAPI) -> None:
    while not app.state.automation_stop.is_set():
        try:
            config = load_config(app.state.config_path)
            interval = _lc_pipeline_slot_poll_interval(config)
        except Exception:
            interval = 10
        try:
            if _app_is_stopping(app):
                return
            _run_lc_pipeline_slot_cycle(app)
        except Exception as exc:
            if _should_suppress_background_error(app, exc):
                LOGGER.info("Suppressing LC slot worker error during shutdown: %s", exc)
            else:
                try:
                    _notify_system_error(load_config(app.state.config_path), "LC Slot Worker", exc)
                except Exception:
                    LOGGER.exception("LC slot worker failed before Telegram notification")
        if app.state.automation_stop.wait(interval):
            return


def _telegram_polling_enabled(config: dict[str, Any]) -> bool:
    telegram_config = config.get("notifications", {}).get("telegram", {})
    return bool(telegram_config.get("polling_enabled", True)) and telegram_buttons_enabled(config)


def _telegram_chat_allowed(config: dict[str, Any], chat_id: Any) -> bool:
    expected = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return bool(expected) and str(chat_id) == expected



def _max_positions_menu_message(config: dict[str, Any]) -> str:
    try:
        current = int(float(config.get("risk", {}).get("max_active_trades", 1) or 1))
    except (TypeError, ValueError):
        current = 1
    return (
        "ðŸ“ˆ CÃ i sá»‘ vá»‹ tháº¿ tá»‘i Ä‘a má»Ÿ cÃ¹ng lÃºc\n"
        f"Äang dÃ¹ng: {current} vá»‹ tháº¿\n"
        "Chá»n nÃºt bÃªn dÆ°á»›i hoáº·c gá»­i /maxvt 3"
    )


def _set_max_positions_from_telegram(
    config_path: str | Path,
    config: dict[str, Any],
    raw_value: str,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    try:
        max_positions = int(float(raw_value))
    except (TypeError, ValueError):
        return config, "âš ï¸ Sá»‘ vá»‹ tháº¿ khÃ´ng há»£p lá»‡. VÃ­ dá»¥: /maxvt 3", telegram_max_positions_keyboard(config)
    if max_positions < 1 or max_positions > 10:
        return config, "âš ï¸ Chá»‰ nháº­n sá»‘ vá»‹ tháº¿ tá»« 1 Ä‘áº¿n 10.", telegram_max_positions_keyboard(config)

    updated = _save_max_positions(config_path, max_positions)
    message = (
        "âœ… ÄÃ£ lÆ°u sá»‘ vá»‹ tháº¿ tá»‘i Ä‘a\n"
        f"Max vá»‹ tháº¿ má»Ÿ cÃ¹ng lÃºc: {max_positions}\n"
        "Ãp dá»¥ng tá»« chu ká»³ scan/lá»‡nh tiáº¿p theo."
    )
    return updated, message, telegram_max_positions_keyboard(updated)


def _ai_history_keyboard(*, expanded: bool, has_more: bool) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    if has_more and not expanded:
        rows.append([{"text": "ðŸ”Ž Xem thÃªm 10 láº§n cÅ©", "callback_data": "view_ai_more"}])
    if expanded:
        rows.append([{"text": "ðŸ”™ Thu gá»n 5 láº§n gáº§n nháº¥t", "callback_data": "view_ai"}])
    rows.append([{"text": "ðŸ“² Menu", "callback_data": "view_menu"}])
    return {"inline_keyboard": rows}


def _telegram_side_label(value: Any) -> str:
    side = str(value or "").strip().lower()
    if side == "long":
        return "LONG"
    if side == "short":
        return "SHORT"
    return str(value or "-").upper() if value else "-"


def _mini_reason_parts(value: Any) -> list[str]:
    parts: list[str] = []
    for raw in str(value or "").replace("; ", "\n").splitlines():
        text = raw.strip().lstrip("-").strip()
        if text:
            parts.append(text)
    return parts


def _mini_reason_score(text: str) -> tuple[int, int]:
    lower = text.lower()
    score = 0
    if "strategic " in lower:
        score += 5
    if "trend confirms" in lower or "aligned " in lower:
        score += 4
    if "uptrend" in lower or "downtrend" in lower:
        score += 4
    if "volume support" in lower or "strong volume" in lower:
        score += 3
    if "market regime" in lower:
        score += 3
    if "candlestick supports" in lower or "no critical warnings" in lower:
        score += 1
    if "local policy approved" in lower:
        score += 1
    if "hesitation lowers confidence" in lower:
        score -= 1
    if "no 4h data" in lower:
        score -= 1
    if "lacks volume support" in lower or "mixed 1h bearish candle" in lower:
        score -= 2
    return score, -len(text)


def _mini_reason_vi(text: str) -> str:
    source = str(text or "").strip()
    if not source:
        return ""
    if match := re.search(
        r"Aligned 1h/5m (bullish|bearish) with volume support",
        source,
        re.IGNORECASE,
    ):
        direction = "tÄƒng" if match.group(1).lower() == "bullish" else "giáº£m"
        return f"1h/5m Ä‘ang Ä‘á»“ng thuáº­n xu hÆ°á»›ng {direction} vÃ  cÃ³ á»§ng há»™ khá»‘i lÆ°á»£ng."
    if match := re.search(
        r"RR only ([0-9.]+) and no 4h data, but local policy approved it\.?",
        source,
        re.IGNORECASE,
    ):
        return f"R:R chá»‰ {match.group(1)}, chÆ°a cÃ³ dá»¯ liá»‡u 4h, nhÆ°ng váº«n Ä‘Æ°á»£c local policy duyá»‡t."
    if match := re.search(
        r"([A-Z0-9]+) has aligned 1h/5m uptrend and strong volume\.?",
        source,
        re.IGNORECASE,
    ):
        return f"{match.group(1).upper()} Ä‘ang Ä‘á»“ng thuáº­n xu hÆ°á»›ng tÄƒng 1h/5m vÃ  khá»‘i lÆ°á»£ng máº¡nh."
    if match := re.search(
        r"([A-Z0-9]+) has aligned 1h/5m downtrend and strong volume\.?",
        source,
        re.IGNORECASE,
    ):
        return f"{match.group(1).upper()} Ä‘ang Ä‘á»“ng thuáº­n xu hÆ°á»›ng giáº£m 1h/5m vÃ  khá»‘i lÆ°á»£ng máº¡nh."
    if match := re.search(
        r"([A-Z0-9]+) lacks volume support and has mixed 1h bearish candle\.?",
        source,
        re.IGNORECASE,
    ):
        return f"{match.group(1).upper()} thiáº¿u á»§ng há»™ khá»‘i lÆ°á»£ng, náº¿n 1h cÃ²n láº«n tÃ­n hiá»‡u giáº£m."
    if match := re.search(
        r"([A-Z0-9]+) lacks volume support and has mixed 1h bullish candle\.?",
        source,
        re.IGNORECASE,
    ):
        return f"{match.group(1).upper()} thiáº¿u á»§ng há»™ khá»‘i lÆ°á»£ng, náº¿n 1h cÃ²n láº«n tÃ­n hiá»‡u tÄƒng."
    if match := re.search(r"Strategic long bias target 60/40 adds ([0-9.]+) point", source, re.IGNORECASE):
        return f"ThiÃªn hÆ°á»›ng long chiáº¿n lÆ°á»£c cá»™ng thÃªm {match.group(1)} Ä‘iá»ƒm."
    if match := re.search(r"Strategic long bias requires stronger short evidence \(([0-9.]+) point edge\)", source, re.IGNORECASE):
        return f"ThiÃªn hÆ°á»›ng long Ä‘ang máº¡nh, short cáº§n thÃªm lá»£i tháº¿ {match.group(1)} Ä‘iá»ƒm."
    if match := re.search(r"Market regime is neutral: breadth ([0-9.]+)% favors longs", source, re.IGNORECASE):
        return f"Thá»‹ trÆ°á»ng trung tÃ­nh, Ä‘á»™ rá»™ng {match.group(1)}% nghiÃªng vá» long."
    if match := re.search(r"Market regime is bullish: breadth ([0-9.]+)% favors longs", source, re.IGNORECASE):
        return f"Thá»‹ trÆ°á»ng nghiÃªng tÄƒng, Ä‘á»™ rá»™ng {match.group(1)}% á»§ng há»™ long."
    if match := re.search(r"Market regime is bearish: breadth ([0-9.]+)% allows shorts", source, re.IGNORECASE):
        return f"Thá»‹ trÆ°á»ng nghiÃªng giáº£m, Ä‘á»™ rá»™ng {match.group(1)}% cho phÃ©p short."
    if re.search(r"5M trend confirms long", source, re.IGNORECASE):
        return "Xu hÆ°á»›ng 5m xÃ¡c nháº­n LONG."
    if re.search(r"5M trend confirms short", source, re.IGNORECASE):
        return "Xu hÆ°á»›ng 5m xÃ¡c nháº­n SHORT."
    if re.search(r"1H trend confirms long", source, re.IGNORECASE):
        return "Xu hÆ°á»›ng 1h xÃ¡c nháº­n LONG."
    if re.search(r"1H trend confirms short", source, re.IGNORECASE):
        return "Xu hÆ°á»›ng 1h xÃ¡c nháº­n SHORT."
    if re.search(r"1M candlestick supports LONG", source, re.IGNORECASE):
        return "Náº¿n 1m Ä‘ang á»§ng há»™ LONG."
    if re.search(r"1M candlestick supports SHORT", source, re.IGNORECASE):
        return "Náº¿n 1m Ä‘ang á»§ng há»™ SHORT."
    if match := re.search(
        r"Aligned (long|short) bias with 1h/5m (uptrend|downtrend), modest RR ([0-9.]+), and no critical warnings",
        source,
        re.IGNORECASE,
    ):
        side = match.group(1).upper()
        trend = "xu hÆ°á»›ng tÄƒng 1h/5m" if match.group(2).lower() == "uptrend" else "xu hÆ°á»›ng giáº£m 1h/5m"
        return f"Mini tháº¥y {side} Ä‘á»“ng thuáº­n vá»›i {trend}, R:R {match.group(3)}, chÆ°a cÃ³ cáº£nh bÃ¡o lá»›n."
    if re.search(r"no critical warnings", source, re.IGNORECASE):
        return "ChÆ°a cÃ³ cáº£nh bÃ¡o lá»›n."
    if match := re.search(r"modest RR ([0-9.]+)", source, re.IGNORECASE):
        return f"R:R Ä‘ang á»Ÿ má»©c {match.group(1)}."
    if re.search(r"5m hesitation lowers confidence", source, re.IGNORECASE):
        return "Khung 5m cÃ²n do dá»± nÃªn Ä‘á»™ tin cáº­y bá»‹ giáº£m."
    compact = source
    replacements = {
        "Market regime is neutral": "Thá»‹ trÆ°á»ng trung tÃ­nh",
        "Market regime is bullish": "Thá»‹ trÆ°á»ng nghiÃªng tÄƒng",
        "Market regime is bearish": "Thá»‹ trÆ°á»ng nghiÃªng giáº£m",
        "favors longs": "nghiÃªng vá» long",
        "allows shorts": "cho phÃ©p short",
        "trend confirms long": "xÃ¡c nháº­n LONG",
        "trend confirms short": "xÃ¡c nháº­n SHORT",
        "no critical warnings": "khÃ´ng cÃ³ cáº£nh bÃ¡o lá»›n",
        "hesitation lowers confidence": "cÃ²n do dá»± nÃªn giáº£m Ä‘á»™ tin cáº­y",
    }
    for old, new in replacements.items():
        compact = compact.replace(old, new)
    return compact[:180]


def _top_mini_reasons(reasons: list[str], limit: int = 2) -> list[str]:
    ranked = sorted(enumerate(reasons), key=lambda item: (-_mini_reason_score(item[1])[0], _mini_reason_score(item[1])[1], item[0]))
    selected = sorted(ranked[:limit], key=lambda item: item[0])
    return [_mini_reason_vi(text) for _, text in selected if _mini_reason_vi(text)]


def _ai_symbol_detail_lines(item: dict[str, Any]) -> list[str]:
    details = item.get("candidate_details") if isinstance(item.get("candidate_details"), list) else []
    approved = [str(symbol) for symbol in item.get("approved_symbols") or [] if str(symbol)]
    scores = item.get("setup_scores") if isinstance(item.get("setup_scores"), dict) else {}
    lines: list[str] = []
    if details:
        lines.append("3 cáº·p giao dá»‹ch Ä‘Æ°á»£c mini Ä‘Ã¡nh giÃ¡:")
        for index, detail in enumerate(details[:3], start=1):
            if not isinstance(detail, dict):
                continue
            symbol = str(detail.get("symbol") or "-")
            chosen = " âœ… mini gá»­i LC" if symbol in approved else ""
            metric_parts = []
            if detail.get("win_probability_pct") is not None:
                metric_parts.append(f"Win {_telegram_number(detail.get('win_probability_pct'), '%')}")
            if detail.get("confidence") is not None:
                metric_parts.append(f"Tin cáº­y {_telegram_number(detail.get('confidence'))}")
            if detail.get("risk_reward") is not None:
                metric_parts.append(f"R:R {_telegram_number(detail.get('risk_reward'))}")
            if scores.get(symbol) is not None:
                metric_parts.append(f"Mini score {_telegram_number(scores.get(symbol))}")
            lines.append(f"{index}. {symbol} | {_telegram_side_label(detail.get('side'))}{chosen}")
            if metric_parts:
                lines.append("   " + " | ".join(metric_parts))
            reasons = [str(reason) for reason in detail.get("reasons") or [] if str(reason)]
            if reasons:
                lines.append("   LÃ½ do:")
                for reason in reasons[:3]:
                    lines.append(f"   - {reason[:180]}")
        if approved:
            lines.append("Mini chá»n gá»­i:")
            for symbol in approved[:3]:
                lines.append(f"- {symbol}")
        return lines

    symbols = [str(symbol) for symbol in item.get("symbols") or [] if str(symbol)]
    if symbols:
        lines.append("Cáº·p giao dá»‹ch:")
        for index, symbol in enumerate(symbols[:5], start=1):
            marker = " âœ… mini gá»­i LC" if symbol in approved else ""
            lines.append(f"{index}. {symbol}{marker}")
    return lines


def _ai_symbol_detail_lines_v2(item: dict[str, Any]) -> list[str]:
    details = item.get("candidate_details") if isinstance(item.get("candidate_details"), list) else []
    approved = [str(symbol) for symbol in item.get("approved_symbols") or [] if str(symbol)]
    scores = item.get("setup_scores") if isinstance(item.get("setup_scores"), dict) else {}
    lines: list[str] = []
    if details:
        lines.append("3 cáº·p mini Ä‘Ã£ Ä‘Ã¡nh giÃ¡:")
        for index, detail in enumerate(details[:3], start=1):
            if not isinstance(detail, dict):
                continue
            symbol = str(detail.get("symbol") or "-")
            chosen = " âœ… mini gá»­i LC" if symbol in approved else ""
            metric_parts = []
            if detail.get("win_probability_pct") is not None:
                metric_parts.append(f"Win {_telegram_number(detail.get('win_probability_pct'), '%')}")
            if detail.get("confidence") is not None:
                metric_parts.append(f"Tin cáº­y {_telegram_number(detail.get('confidence'))}")
            if detail.get("risk_reward") is not None:
                metric_parts.append(f"R:R {_telegram_number(detail.get('risk_reward'))}")
            if scores.get(symbol) is not None:
                metric_parts.append(f"Äiá»ƒm mini {_telegram_number(scores.get(symbol))}")
            lines.append(f"{index}. {symbol} | {_telegram_side_label(detail.get('side'))}{chosen}")
            if metric_parts:
                lines.append("   " + " | ".join(metric_parts))
            reasons = [str(reason) for reason in detail.get("reasons") or [] if str(reason)]
            short_reasons = _top_mini_reasons(reasons, limit=2)
            if short_reasons:
                lines.append("   LÃ½ do gá»­i:")
                for reason in short_reasons:
                    lines.append(f"   - {reason}")
        if approved:
            lines.append("Mini chá»n:")
            for symbol in approved[:3]:
                lines.append(f"- {symbol}")
        return lines

    symbols = [str(symbol) for symbol in item.get("symbols") or [] if str(symbol)]
    if symbols:
        lines.append("Cáº·p giao dá»‹ch:")
        for index, symbol in enumerate(symbols[:5], start=1):
            marker = " âœ… mini gá»­i LC" if symbol in approved else ""
            lines.append(f"{index}. {symbol}{marker}")
    return lines


def _mini_comment_lines(reason: Any, *, limit: int = 2) -> list[str]:
    return _top_mini_reasons(_mini_reason_parts(reason), limit=limit)


def _ai_history_header(*, expanded: bool) -> str:
    return (
        "ðŸ¤– Lá»‹ch sá»­ gá»i AI gáº§n nháº¥t (15 láº§n, má»›i nháº¥t á»Ÿ dÆ°á»›i)"
        if expanded
        else "ðŸ¤– Lá»‹ch sá»­ gá»i AI gáº§n nháº¥t (5 láº§n, má»›i nháº¥t á»Ÿ dÆ°á»›i)"
    )


def _format_ai_call_history_entry(config: dict[str, Any], item: dict[str, Any]) -> str:
    if str(item.get("review_kind") or "") == "lc_okx_review":
        try:
            created_label = datetime.fromisoformat(str(item.get("created_at") or "").replace("Z", "+00:00")).astimezone(
                _system_timezone(config)
            ).strftime("%d/%m/%Y %H:%M:%S VN")
        except ValueError:
            created_label = str(item.get("created_at") or "-")
        lc_id = item.get("lc_okx_id")
        symbol = str(item.get("symbol") or ((item.get("symbols") or ["-"])[0]))
        side = _telegram_side_label(item.get("side"))
        lines = [
            f"ðŸ•’ {created_label}",
            f"Vai trÃ²: OKX",
            f"Model: {item.get('model', '-')}",
            f"Tráº¡ng thÃ¡i: {item.get('status', '-')}",
            f"LC_OKX: #{lc_id if lc_id not in (None, '') else '-'}",
            f"Cáº·p: {symbol} | {side}",
            f"Giáº£i thÃ­ch: {okx_review_explanation_vi(item)[:180]}",
        ]
        return "\n".join(lines)
    role = str(item.get("role") or "ai").upper()
    created_at = str(item.get("created_at") or "")
    try:
        created_label = datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone(
            _system_timezone(config)
        ).strftime("%d/%m/%Y %H:%M:%S VN")
    except ValueError:
        created_label = created_at[:16] or "-"
    lines = [
        f"ðŸ•’ {created_label}",
        f"Vai trÃ²: {role}",
        f"Model: {item.get('model', '-')}",
        f"Tráº¡ng thÃ¡i: {item.get('status', '-')}",
    ]
    lines.extend(_ai_symbol_detail_lines_v2(item))
    reason = str(item.get("reason") or "")
    if reason:
        lines.append("Nháº­n xÃ©t cá»§a mini:")
        for text in _mini_comment_lines(reason, limit=2):
            lines.append(f"- {text[:180]}")
    return "\n".join(lines)


def ai_call_history_timeline_messages(config: dict[str, Any], *, expanded: bool = False) -> list[str]:
    limit = 15 if expanded else 5
    items = recent_ai_call_history(config, limit=limit)
    return [_format_ai_call_history_entry(config, item) for item in items if isinstance(item, dict)]


def _format_ai_call_history_view(config: dict[str, Any], *, expanded: bool = False) -> str:
    items = ai_call_history_timeline_messages(config, expanded=expanded)
    if not items:
        return "ðŸ¤– AI: chÆ°a cÃ³ lá»‹ch sá»­ gá»i GPT nÃ o Ä‘Æ°á»£c lÆ°u."
    return "\n\n".join([_ai_history_header(expanded=expanded), *items])

def _telegram_number(value: Any, suffix: str = "") -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(number):
        return "-"
    text = str(int(number)) if number.is_integer() else f"{number:g}"
    return f"{text}{suffix}"


def _telegram_vn_time(config: dict[str, Any], value: Any) -> str:
    if not value:
        return "-"
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(_system_timezone(config)).strftime("%d/%m/%Y %H:%M:%S VN")
    except (TypeError, ValueError):
        return str(value)


def _telegram_dashboard_message(
    config: dict[str, Any],
    app: FastAPI | None = None,
    *,
    payload: dict[str, Any] | None = None,
    status: dict[str, Any] | None = None,
) -> str:
    if status is None and app is not None:
        status = _automation_status_payload(app)
    status = status or {}
    status.setdefault("enabled", _automation_enabled(config))
    status.setdefault("interval_seconds", _automation_interval(config))
    execute = status.get("execute")
    if execute is None:
        execute, execute_reason = _automation_should_execute(config)
        status["execute"] = execute
        status["execute_reason"] = execute_reason
    decision = payload
    if decision is None:
        try:
            decision = _read_report(config).get("decision") or {}
        except Exception:
            decision = {}

    candidates = decision.get("candidates") or []
    selected = decision.get("selected") or {}
    top = selected or (candidates[0] if candidates else {})
    risk = decision.get("risk_check") or {}
    reasons = risk.get("reasons") or status.get("risk_reasons") or []
    sizing = config.get("position_sizing", {})
    leverage = config.get("exchange", {}).get("leverage")
    margin = sizing.get("base_margin_usdt")
    notional = _effective_order_usdt(config)
    demo = _okx_demo_status(config)
    ai_config = config.get("ai", {})
    ai_internal = ai_config.get("internal", {}) if isinstance(ai_config.get("internal"), dict) else {}
    ai_okx = ai_config.get("okx", {}) if isinstance(ai_config.get("okx"), dict) else {}
    try:
        lc_payload = lc_pipeline_dashboard_payload(config)
        internal_lc_count = int((lc_payload.get("counts") or {}).get("internal_lc", 0))
    except Exception:
        internal_lc_count = 0
    try:
        balance_snapshot = fetch_balance_snapshot(config, use_cache=True)
        balance_label = (
            f"{_telegram_number(balance_snapshot.get('balance_usdt'), ' USDT')}"
            if balance_snapshot.get("ok")
            else "khong lay duoc"
        )
    except Exception:
        balance_label = "khong lay duoc"

    auto_label = "báº­t" if status.get("enabled") else "táº¯t"
    execute_label = "cÃ³ gá»­i lá»‡nh" if status.get("execute") else "chá»‰ theo dÃµi"
    okx_label = "sáºµn sÃ ng" if demo.get("ready") else demo.get("message", "chÆ°a sáºµn sÃ ng")

    lines = [
        "ðŸ“² Báº£ng Ä‘iá»u khiá»ƒn Telegram",
        f"âš™ï¸ Mode: {config.get('mode', '-')} | Auto: {auto_label} | {execute_label}",
        f"ðŸ§ª OKX demo: {okx_label}",
        f"ðŸ’µ So du: {balance_label}",
        (
            "ðŸ’° Lá»‡nh sau: "
            f"{_telegram_number(margin, ' USDT')} margin | "
            f"{_telegram_number(leverage, 'x')} | "
            f"vá»‹ tháº¿ {_telegram_number(notional, ' USDT')}"
        ),
        f"ðŸŸ¡ LC ná»™i bá»™: {internal_lc_count}",
    ]
    lines.insert(3, f"AI: internal {ai_internal.get('model', '-')} | OKX {ai_okx.get('model', '-')}")
    try:
        next_mini = _telegram_vn_time(config, next_internal_market_scan_at(config))
        lines.insert(4, f"ðŸ¤– Mini scan tiáº¿p: {next_mini}")
    except Exception as exc:
        _notify_system_error(config, "TÃ­nh lá»‹ch Mini", exc)
    if top:
        lines.append(
            "ðŸ† Top hiá»‡n táº¡i: "
            f"{top.get('symbol', '-')} {str(top.get('side', '-')).upper()} | "
            f"tin cáº­y {_telegram_number(top.get('confidence'))}"
        )
    else:
        lines.append("ðŸ† Top hiá»‡n táº¡i: chÆ°a cÃ³ dá»¯ liá»‡u scan")
    if risk.get("passed") is not None:
        lines.append(f"ðŸ›¡ Risk gate: {'PASS' if risk.get('passed') else 'BLOCK'}")
    elif status.get("risk_passed") is not None:
        lines.append(f"ðŸ›¡ Risk gate: {'PASS' if status.get('risk_passed') else 'BLOCK'}")
    if reasons:
        lines.append("âš ï¸ LÃ½ do: " + " | ".join(str(item) for item in reasons[:2]))
    if status.get("last_finished_at"):
        lines.append(f"ðŸ•’ Scan gáº§n nháº¥t: {_telegram_vn_time(config, status.get('last_finished_at'))}")
    if status.get("next_scan_at"):
        lines.append(f"â­ Scan tá»± Ä‘á»™ng tiáº¿p: {_telegram_vn_time(config, status.get('next_scan_at'))}")
    lines.append("Bam nut ben duoi hoac go /menu, /setup trong Telegram.")
    return "\n".join(lines)


def _telegram_guard_message(config: dict[str, Any], app: FastAPI | None = None) -> str:
    status = getattr(app.state, "market_guard_status", None) if app is not None else None
    status = status or latest_market_guard_status(config)
    if status:
        return format_market_guard_message(status)
    return "ðŸ›¡ Market Guard chÆ°a cÃ³ dá»¯ liá»‡u. Báº¥m Scan ngay hoáº·c chá» chu ká»³ guard káº¿ tiáº¿p."


def _run_telegram_scan(app: FastAPI | None, config_path: str | Path) -> tuple[dict[str, Any], str, dict[str, Any]]:
    config = load_config(config_path)
    if atlas_runtime_is_read_only(config):
        return (
            config,
            "âš ï¸ Runtime hiá»‡n táº¡i Ä‘ang á»Ÿ cháº¿ Ä‘á»™ chá»‰ Ä‘á»c. Chá»‰ Railway primary má»›i Ä‘Æ°á»£c phÃ©p scan vÃ  ghi state.",
            telegram_control_keyboard(),
        )
    if app is None:
        return config, "âš ï¸ Scan ngay chá»‰ kháº£ dá»¥ng khi bot UI server Ä‘ang cháº¡y.", telegram_control_keyboard()
    if not app.state.lock.acquire(blocking=False):
        return config, "â³ Bot Ä‘ang báº­n scan chu ká»³ khÃ¡c. Thá»­ láº¡i sau vÃ i giÃ¢y.", telegram_control_keyboard()
    try:
        config = load_config(config_path)
        started = datetime.now(timezone.utc)
        decision_result = run_once(config, execute=False)
        payload = to_jsonable(decision_result)
        execution = payload.get("execution") or {}
        risk = payload.get("risk_check") or {}
        selected = payload.get("selected") or {}
        top = (payload.get("candidates") or [{}])[0] or {}
        status = {
            "enabled": _automation_enabled(config),
            "interval_seconds": _automation_interval(config),
            "mode": config.get("mode", "dry_run"),
            "last_started_at": started.isoformat(),
            "last_finished_at": datetime.now(timezone.utc).isoformat(),
            "last_result": "order_submitted" if execution.get("submitted") else "no_order",
            "action": payload.get("action"),
            "selected_symbol": selected.get("symbol"),
            "top_symbol": top.get("symbol"),
            "top_confidence": top.get("confidence"),
            "risk_passed": risk.get("passed"),
            "risk_reasons": risk.get("reasons") or [],
            "execution_submitted": bool(execution.get("submitted")),
            "order_id": execution.get("order_id"),
        }
        return config, format_scan_message(config, payload, status), telegram_control_keyboard()
    except Exception as exc:
        return config, f"ðŸš¨ Scan lá»—i: {exc}", telegram_control_keyboard()
    finally:
        app.state.lock.release()


def _telegram_action_response(
    config: dict[str, Any],
    action: str,
    config_path: str | Path,
    app: FastAPI | None = None,
) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    if action == "view_menu":
        return config, _telegram_dashboard_message(config, app), telegram_control_keyboard()
    if action == "scan_now":
        return _run_telegram_scan(app, config_path)
    if action == "view_guard":
        return config, _telegram_guard_message(config, app), telegram_control_keyboard()
    if action == "view_positions_account":
        return config, format_positions_account_view(config), None
    if action == "view_vt":
        return config, format_positions_account_view(config), None
    if action == "view_sd":
        return config, format_balance_view(config), None
    if action == "view_lc":
        return config, format_internal_lc_view(config), None
    if action == "view_undecided_lc":
        return config, format_undecided_lc_view(config), None
    if action == "view_internal_notifications":
        return config, format_internal_notifications_view(config), telegram_control_keyboard()
    if action == "view_wait_slot_notifications":
        return config, format_wait_slot_notifications_view(config), telegram_control_keyboard()
    if action == "view_memory":
        return config, format_market_scan_memory_view(config), None
    if action == "view_ai":
        has_more = len(recent_ai_call_history(config, limit=6)) > 5
        return config, _format_ai_call_history_view(config), _ai_history_keyboard(expanded=False, has_more=has_more)
    if action == "view_ai_more":
        return config, _format_ai_call_history_view(config, expanded=True), _ai_history_keyboard(
            expanded=True,
            has_more=False,
        )
    if action == "view_pnl_sd":
        return config, format_positions_account_view(config), None
    if action in {"view_setup", "setup_menu"} or "setup" in action.lower():
        return config, _setup_menu_message(config), telegram_setup_keyboard()
    if action == "set_order_usdt":
        return config, _order_usdt_menu_message(config), telegram_order_usdt_keyboard(config)
    if action.startswith("set_order_usdt:"):
        return _set_base_margin_from_telegram(config_path, config, action.split(":", 1)[1])
    if action == "set_leverage":
        return config, _leverage_menu_message(config), telegram_leverage_keyboard(config)
    if action.startswith("set_leverage:"):
        return _set_leverage_from_telegram(config_path, config, action.split(":", 1)[1])
    if action == "set_max_positions":
        return config, _max_positions_menu_message(config), telegram_max_positions_keyboard(config)
    if action.startswith("set_max_positions:"):
        return _set_max_positions_from_telegram(config_path, config, action.split(":", 1)[1])
    return config, _telegram_dashboard_message(config, app), telegram_control_keyboard()


def _telegram_action_message(config: dict[str, Any], action: str) -> str:
    if action == "set_order_usdt" or action.startswith("set_order_usdt:"):
        return _order_usdt_menu_message(config)
    if action == "set_leverage" or action.startswith("set_leverage:"):
        return _leverage_menu_message(config)
    if action == "set_max_positions" or action.startswith("set_max_positions:"):
        return _max_positions_menu_message(config)
    return _telegram_action_response(config, action, config.get("_config_path") or ".")[1]


def _telegram_cache_bucket(app: FastAPI | None) -> dict[str, Any] | None:
    if app is None:
        return None
    cache = getattr(app.state, "telegram_view_cache", None)
    if isinstance(cache, dict):
        return cache
    cache = {}
    app.state.telegram_view_cache = cache
    return cache


def _telegram_cached_value(
    app: FastAPI | None,
    key: str,
    *,
    ttl_seconds: int,
    builder: Any,
) -> Any:
    cache = _telegram_cache_bucket(app)
    if cache is None:
        return builder()
    now = datetime.now(timezone.utc)
    cached = cache.get(key)
    if isinstance(cached, dict):
        created_at = cached.get("created_at")
        if isinstance(created_at, datetime) and (now - created_at).total_seconds() <= max(1, int(ttl_seconds)):
            return cached.get("value")
    value = builder()
    cache[key] = {"created_at": now, "value": value}
    return value


def _send_timeline_sequence(
    config: dict[str, Any],
    chat_id: Any,
    *,
    thread_id: Any,
    header_text: str,
    timeline_messages: list[str],
    empty_text: str,
) -> None:
    if not timeline_messages:
        send_telegram_chat_message(
            config,
            chat_id,
            empty_text,
            message_thread_id=thread_id,
            with_buttons=False,
        )
        return
    send_telegram_chat_message(
        config,
        chat_id,
        header_text,
        message_thread_id=thread_id,
        with_buttons=False,
    )
    for text in timeline_messages:
        send_telegram_chat_message(
            config,
            chat_id,
            text,
            message_thread_id=thread_id,
            with_buttons=False,
        )


def _send_ai_history_sequence(
    config: dict[str, Any],
    chat_id: Any,
    *,
    thread_id: Any,
    expanded: bool,
    message_id: Any | None = None,
) -> None:
    timeline_messages = ai_call_history_timeline_messages(config, expanded=expanded)
    has_more = len(recent_ai_call_history(config, limit=6)) > 5 if not expanded else False
    reply_markup = _ai_history_keyboard(expanded=expanded, has_more=has_more)
    if not timeline_messages:
        empty_text = "ðŸ¤– AI: chÆ°a cÃ³ lá»‹ch sá»­ gá»i GPT nÃ o Ä‘Æ°á»£c lÆ°u."
        if message_id is not None:
            edited = edit_telegram_chat_message(
                config,
                chat_id,
                message_id,
                empty_text,
                reply_markup=reply_markup,
            )
            if edited:
                return
        send_telegram_chat_message(
            config,
            chat_id,
            empty_text,
            message_thread_id=thread_id,
            with_buttons=False,
            reply_markup=reply_markup,
        )
        return
    header_text = _ai_history_header(expanded=expanded)
    if message_id is not None:
        edited = edit_telegram_chat_message(
            config,
            chat_id,
            message_id,
            header_text,
            reply_markup=reply_markup,
        )
        if not edited:
            send_telegram_chat_message(
                config,
                chat_id,
                header_text,
                message_thread_id=thread_id,
                with_buttons=False,
                reply_markup=reply_markup,
            )
    else:
        send_telegram_chat_message(
            config,
            chat_id,
            header_text,
            message_thread_id=thread_id,
            with_buttons=False,
            reply_markup=reply_markup,
        )
    for text in timeline_messages:
        send_telegram_chat_message(
            config,
            chat_id,
            text,
            message_thread_id=thread_id,
            with_buttons=False,
        )


def _handle_telegram_update(config: dict[str, Any], update: dict[str, Any], config_path: str | Path, app: FastAPI | None = None) -> None:
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        callback_id = str(callback.get("id") or "")
        action = str(callback.get("data") or "view_menu")
        if callback_id:
            answer_callback_query(config, callback_id, "Äang cháº¡y scan..." if action == "scan_now" else "Äang láº¥y dá»¯ liá»‡u...")
        try:
            set_journal_state(
                config,
                "telegram_last_callback",
                json.dumps(
                    {
                        "action": action,
                        "message_id": message.get("message_id"),
                        "text": str(message.get("text") or "")[:120],
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception:
            pass
        if not _telegram_chat_allowed(config, chat_id):
            return
        thread_id = message.get("message_thread_id")
        message_id = message.get("message_id")
        if action == "view_internal_notifications":
            timeline_messages = _telegram_cached_value(
                app,
                "timeline:view_internal_notifications",
                ttl_seconds=TELEGRAM_TIMELINE_CACHE_TTL_SECONDS,
                builder=lambda: internal_notification_timeline_messages(config),
            )
            _send_timeline_sequence(
                config,
                chat_id,
                thread_id=thread_id,
                header_text="ðŸ”” ThÃ´ng bÃ¡o ná»™i bá»™",
                timeline_messages=timeline_messages,
                empty_text="ðŸ”” ThÃ´ng bÃ¡o ná»™i bá»™: chÆ°a cÃ³ dá»¯ liá»‡u 1h/2h/4h/Mini.",
            )
            return
        if action == "view_wait_slot_notifications":
            timeline_messages = _telegram_cached_value(
                app,
                "timeline:view_wait_slot_notifications",
                ttl_seconds=TELEGRAM_TIMELINE_CACHE_TTL_SECONDS,
                builder=lambda: wait_slot_notification_timeline_messages(config),
            )
            _send_timeline_sequence(
                config,
                chat_id,
                thread_id=thread_id,
                header_text="ðŸŸ¡ ThÃ´ng bÃ¡o Wait Slot",
                timeline_messages=timeline_messages,
                empty_text="ðŸŸ¡ Wait Slot: chÆ°a cÃ³ thÃ´ng bÃ¡o nÃ o.",
            )
            return
        if action == "view_undecided_lc":
            timeline_messages = _telegram_cached_value(
                app,
                "timeline:view_undecided_lc",
                ttl_seconds=TELEGRAM_TIMELINE_CACHE_TTL_SECONDS,
                builder=lambda: undecided_notification_timeline_messages(config),
            )
            empty_text = _telegram_cached_value(
                app,
                "view:view_undecided_lc:empty",
                ttl_seconds=TELEGRAM_VIEW_CACHE_TTL_SECONDS,
                builder=lambda: format_undecided_lc_view(config),
            )
            _send_timeline_sequence(
                config,
                chat_id,
                thread_id=thread_id,
                header_text="ðŸ“‹ ThÃ´ng bÃ¡o ChÆ°a duyá»‡t",
                timeline_messages=timeline_messages,
                empty_text=empty_text,
            )
            return
        if action in {"view_ai", "view_ai_more"}:
            _send_ai_history_sequence(
                config,
                chat_id,
                thread_id=thread_id,
                expanded=action == "view_ai_more",
                message_id=message_id,
            )
            return
        response_config, response_text, reply_markup = _telegram_cached_value(
            app,
            f"view:{action}",
            ttl_seconds=TELEGRAM_VIEW_CACHE_TTL_SECONDS,
            builder=lambda: _telegram_action_response(config, action, config_path, app),
        ) if action in {
            "view_menu",
            "view_guard",
            "view_positions_account",
            "view_vt",
            "view_sd",
            "view_lc",
            "view_wait_slot_notifications",
            "view_memory",
            "view_ai",
            "view_ai_more",
            "view_pnl_sd",
            "setup_menu",
            "view_setup",
            "set_order_usdt",
            "set_leverage",
            "set_max_positions",
        } else _telegram_action_response(config, action, config_path, app)
        inline_view_actions = {
            "view_menu",
            "view_guard",
            "view_lc",
            "view_undecided_lc",
            "view_wait_slot_notifications",
            "view_memory",
            "view_ai",
            "view_ai_more",
            "view_positions_account",
            "view_vt",
            "view_pnl_sd",
        }
        if action in inline_view_actions and message_id is not None:
            edited = edit_telegram_chat_message(
                response_config,
                chat_id,
                message_id,
                response_text,
                reply_markup=reply_markup,
            )
            if edited:
                return
            send_telegram_chat_message(
                response_config,
                chat_id,
                response_text,
                message_thread_id=thread_id,
                with_buttons=reply_markup is None,
                reply_markup=reply_markup,
            )
            return
        if (action in {"view_setup", "setup_menu"} or "setup" in action.lower()) and message_id is not None:
            edited = edit_telegram_chat_message(
                response_config,
                chat_id,
                message_id,
                response_text,
                reply_markup=reply_markup,
            )
            if edited:
                return
            send_telegram_chat_message(
                response_config,
                chat_id,
                response_text,
                message_thread_id=thread_id,
                with_buttons=reply_markup is None,
                reply_markup=reply_markup,
            )
            return
        send_telegram_chat_message(
            response_config,
            chat_id,
            response_text,
            message_thread_id=thread_id,
            with_buttons=reply_markup is None,
            reply_markup=reply_markup,
        )
        return

    message = update.get("message")
    if not isinstance(message, dict):
        return
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not _telegram_chat_allowed(config, chat_id):
        return
    text = str(message.get("text") or "").strip().lower()
    parts = text.split()
    command = parts[0].split("@", 1)[0] if parts else ""
    if command == "/usdt":
        if len(parts) == 1:
            response_config, response_text, reply_markup = _telegram_action_response(config, "set_order_usdt", config_path, app)
            send_telegram_chat_message(
                response_config,
                chat_id,
                response_text,
                message_thread_id=message.get("message_thread_id"),
                reply_markup=reply_markup,
            )
            return
        value = parts[1] if len(parts) > 1 else ""
        response_config, response_text, reply_markup = _set_base_margin_from_telegram(config_path, config, value)
        send_telegram_chat_message(
            response_config,
            chat_id,
            response_text,
            message_thread_id=message.get("message_thread_id"),
            reply_markup=reply_markup,
        )
        return
    if command in {"/lev", "/leverage", "/donbay"}:
        if len(parts) == 1:
            response_config, response_text, reply_markup = _telegram_action_response(config, "set_leverage", config_path, app)
            send_telegram_chat_message(
                response_config,
                chat_id,
                response_text,
                message_thread_id=message.get("message_thread_id"),
                reply_markup=reply_markup,
            )
            return
        value = parts[1] if len(parts) > 1 else ""
        response_config, response_text, reply_markup = _set_leverage_from_telegram(config_path, config, value)
        send_telegram_chat_message(
            response_config,
            chat_id,
            response_text,
            message_thread_id=message.get("message_thread_id"),
            reply_markup=reply_markup,
        )
        return
    if command in {"/maxvt", "/maxpos", "/maxpositions"}:
        if len(parts) == 1:
            response_config, response_text, reply_markup = _telegram_action_response(
                config, "set_max_positions", config_path, app
            )
            send_telegram_chat_message(
                response_config,
                chat_id,
                response_text,
                message_thread_id=message.get("message_thread_id"),
                reply_markup=reply_markup,
            )
            return
        value = parts[1] if len(parts) > 1 else ""
        response_config, response_text, reply_markup = _set_max_positions_from_telegram(config_path, config, value)
        send_telegram_chat_message(
            response_config,
            chat_id,
            response_text,
            message_thread_id=message.get("message_thread_id"),
            reply_markup=reply_markup,
        )
        return
    command_map = {
        "/start": "view_menu",
        "/menu": "view_menu",
        "/ui": "view_menu",
        "/dashboard": "view_menu",
        "/setup": "setup_menu",
        "/guard": "view_guard",
        "/vt": "view_positions_account",
        "/lc": "view_lc",
        "/chuaduyet": "view_undecided_lc",
        "/noibo": "view_internal_notifications",
        "/thongbao": "view_internal_notifications",
        "/memory": "view_memory",
        "/ai": "view_ai",
        "/pnl": "view_positions_account",
        "/lev": "set_leverage",
        "/leverage": "set_leverage",
        "/donbay": "set_leverage",
        "/usdt": "set_order_usdt",
        "/maxvt": "set_max_positions",
        "/maxpos": "set_max_positions",
        "/maxpositions": "set_max_positions",
    }
    action = command_map.get(command)
    if not action:
        return
    if action == "view_internal_notifications":
        _send_timeline_sequence(
            config,
            chat_id,
            thread_id=message.get("message_thread_id"),
            header_text="ðŸ”” ThÃ´ng bÃ¡o ná»™i bá»™",
            timeline_messages=internal_notification_timeline_messages(config),
            empty_text="ðŸ”” ThÃ´ng bÃ¡o ná»™i bá»™: chÆ°a cÃ³ dá»¯ liá»‡u 1h/2h/4h/Mini.",
        )
        return
    if action == "view_wait_slot_notifications":
        _send_timeline_sequence(
            config,
            chat_id,
            thread_id=message.get("message_thread_id"),
            header_text="ðŸŸ¡ ThÃ´ng bÃ¡o Wait Slot",
            timeline_messages=wait_slot_notification_timeline_messages(config),
            empty_text="ðŸŸ¡ Wait Slot: chÆ°a cÃ³ thÃ´ng bÃ¡o nÃ o.",
        )
        return
    if action == "view_undecided_lc":
        _send_timeline_sequence(
            config,
            chat_id,
            thread_id=message.get("message_thread_id"),
            header_text="ðŸ“‹ ThÃ´ng bÃ¡o ChÆ°a duyá»‡t",
            timeline_messages=undecided_notification_timeline_messages(config),
            empty_text=format_undecided_lc_view(config),
        )
        return
    if action == "view_ai":
        _send_ai_history_sequence(
            config,
            chat_id,
            thread_id=message.get("message_thread_id"),
            expanded=False,
        )
        return
    response_config, response_text, reply_markup = _telegram_action_response(config, action, config_path, app)
    send_telegram_chat_message(
        response_config,
        chat_id,
        response_text,
        message_thread_id=message.get("message_thread_id"),
        reply_markup=reply_markup,
    )


def _telegram_button_worker(app: FastAPI) -> None:
    offset_value = None
    config: dict[str, Any] | None = None
    try:
        config = load_config(app.state.config_path)
        sync_telegram_commands(config)
        app.state.telegram_commands_next_sync_at = datetime.now(timezone.utc) + timedelta(
            seconds=TELEGRAM_COMMANDS_SYNC_INTERVAL_SECONDS
        )
        stored = get_journal_state(config, "telegram_update_offset")
        offset_value = int(stored) if stored else None
    except Exception as exc:
        offset_value = None
        if config is not None:
            _notify_system_error(config, "Telegram Polling khá»Ÿi táº¡o", exc)

    while not app.state.automation_stop.is_set():
        try:
            config = load_config(app.state.config_path)
            next_sync_at = getattr(app.state, "telegram_commands_next_sync_at", None)
            if not isinstance(next_sync_at, datetime) or datetime.now(timezone.utc) >= next_sync_at:
                sync_telegram_commands(config)
                app.state.telegram_commands_next_sync_at = datetime.now(timezone.utc) + timedelta(
                    seconds=TELEGRAM_COMMANDS_SYNC_INTERVAL_SECONDS
                )
            if not _telegram_polling_enabled(config):
                app.state.automation_stop.wait(5)
                continue
            updates = fetch_telegram_updates(config, offset=offset_value)
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset_value = max(offset_value or 0, update_id + 1)
                    set_journal_state(config, "telegram_update_offset", str(offset_value))
                _handle_telegram_update(config, update, app.state.config_path, app)
            if not updates:
                app.state.automation_stop.wait(1)
        except Exception as exc:
            if config is not None:
                _notify_system_error(config, "Telegram Polling", exc)
            app.state.automation_stop.wait(5)


def _parse_iso_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _market_guard_notify_due(config: dict[str, Any], now: datetime) -> bool:
    if not bool(config.get("market_guard", {}).get("notify_telegram", True)):
        return False
    last = _parse_iso_time(get_journal_state(config, "market_guard_last_notify_at"))
    if last is None:
        return True
    return (now - last).total_seconds() >= market_guard_notify_interval(config)


def _mark_market_guard_notified(config: dict[str, Any], now: datetime) -> None:
    set_journal_state(config, "market_guard_last_notify_at", now.isoformat())


def _market_guard_notification_status(config: dict[str, Any], status: dict[str, Any]) -> dict[str, Any] | None:
    guard_config = config.get("market_guard", {})
    move_threshold = _safe_float(guard_config.get("price_move_5m_pct"), 0.8)
    critical_move_threshold = _safe_float(guard_config.get("critical_price_move_5m_pct"), 1.4)
    wick_threshold = _safe_float(guard_config.get("wick_pct"), 0.45)
    wick_ratio_threshold = _safe_float(guard_config.get("wick_body_ratio"), 2.5)
    volume_threshold = _safe_float(guard_config.get("volume_ratio"), 2.5)
    range_threshold = _safe_float(guard_config.get("critical_candle_range_pct"), 1.8)

    filtered_alerts: list[dict[str, Any]] = []
    for alert in status.get("alerts") or []:
        if not isinstance(alert, dict):
            continue
        move_pct = _safe_float(alert.get("move_pct"))
        abs_move = abs(move_pct)
        candle_range_pct = _safe_float(alert.get("candle_range_pct"))
        wick_pct = _safe_float(alert.get("wick_pct"))
        wick_body_ratio = _safe_float(alert.get("wick_body_ratio"))
        volume_ratio = _safe_float(alert.get("volume_ratio"), 1.0)
        severity = str(alert.get("severity") or "warning").lower()

        strong_wick = (
            wick_pct >= max(0.65, wick_threshold * 1.35)
            and wick_body_ratio >= max(3.0, wick_ratio_threshold)
        )
        strong_move = abs_move >= critical_move_threshold
        strong_range = candle_range_pct >= range_threshold
        strong_volume = volume_ratio >= max(3.0, volume_threshold)
        stacked_shock = abs_move >= max(1.0, move_threshold) and volume_ratio >= max(2.0, volume_threshold * 0.8)
        mild_positive_move_only = (
            0.5 <= move_pct <= 1.0
            and not strong_wick
            and not strong_range
            and not strong_volume
        )
        if mild_positive_move_only:
            continue
        if severity == "critical" or strong_wick or strong_move or strong_range or strong_volume or stacked_shock:
            filtered_alerts.append(alert)

    if not filtered_alerts:
        return None
    return {**status, "alerts": filtered_alerts}


def _market_guard_worker(app: FastAPI) -> None:
    while not app.state.automation_stop.is_set():
        interval = 60
        try:
            config = load_config(app.state.config_path)
            interval = market_guard_interval(config)
            if not market_guard_enabled(config):
                app.state.market_guard_status = {
                    "enabled": False,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "alerts": [],
                    "warnings": [],
                    "block": market_guard_block_status(config),
                }
                app.state.automation_stop.wait(interval)
                continue

            status = run_market_guard(config)
            app.state.market_guard_status = status
            now = datetime.now(timezone.utc)
            notify_status = _market_guard_notification_status(config, status)
            if notify_status and _market_guard_notify_due(config, now):
                if not _telegram_startup_quiet_active(app, now):
                    send_telegram_message(config, format_market_guard_message(notify_status), replace_previous=False)
                _mark_market_guard_notified(config, now)
        except Exception as exc:
            app.state.market_guard_status = {
                "enabled": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "alerts": [],
                "warnings": [f"Market guard error: {exc}"],
                "block": None,
            }
            if 'config' in locals():
                _notify_system_error(config, "Market Guard", exc)
        app.state.automation_stop.wait(interval)


def _position_side(position: dict[str, Any]) -> str:
    side = position.get("side") or position.get("info", {}).get("posSide")
    if side and side != "net":
        return str(side)
    contracts = float(position.get("contracts") or position.get("info", {}).get("pos") or 0)
    if contracts > 0:
        return "long"
    if contracts < 0:
        return "short"
    return "-"


def _price_row(symbol: str, ticker: dict[str, Any], *, stale: bool = False, error: str | None = None) -> dict[str, Any]:
    row = {
        "symbol": symbol,
        "last": ticker.get("last"),
        "bid": ticker.get("bid"),
        "ask": ticker.get("ask"),
        "percentage_24h": ticker.get("percentage"),
        "timestamp": ticker.get("timestamp"),
        "datetime": ticker.get("datetime"),
        "stale": stale,
    }
    if error:
        row["error"] = error
    return row


def _empty_price_row(symbol: str, error: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "last": None,
        "bid": None,
        "ask": None,
        "percentage_24h": None,
        "timestamp": None,
        "datetime": None,
        "stale": False,
        "error": error,
    }


def _okx_target_from_payload(payload: dict[str, Any]) -> dict[str, float | None]:
    info = payload.get("info", {}) if isinstance(payload.get("info"), dict) else {}
    source = {**info, **payload}
    stop_loss = None
    take_profit = None
    for key in ("slTriggerPx", "slOrdPx", "stopLossPrice", "stopLoss", "stop_loss"):
        stop_loss = _safe_float(source.get(key), math.nan)
        if math.isfinite(stop_loss):
            break
        stop_loss = None
    for key in ("tpTriggerPx", "tpOrdPx", "takeProfitPrice", "takeProfit", "take_profit"):
        take_profit = _safe_float(source.get(key), math.nan)
        if math.isfinite(take_profit):
            break
        take_profit = None
    attach_orders = source.get("attachAlgoOrds")
    if isinstance(attach_orders, str):
        try:
            attach_orders = json.loads(attach_orders)
        except json.JSONDecodeError:
            attach_orders = []
    if isinstance(attach_orders, list):
        for item in attach_orders:
            if not isinstance(item, dict):
                continue
            if stop_loss is None:
                value = _safe_float(item.get("slTriggerPx") or item.get("slOrdPx"), math.nan)
                stop_loss = value if math.isfinite(value) else None
            if take_profit is None:
                value = _safe_float(item.get("tpTriggerPx") or item.get("tpOrdPx"), math.nan)
                take_profit = value if math.isfinite(value) else None
    return {"stop_loss": stop_loss, "take_profit": take_profit}


def _okx_targets_from_orders(open_orders: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, float | None]]:
    targets: dict[tuple[str, str], dict[str, float | None]] = {}
    for order in open_orders:
        symbol = str(order.get("symbol") or "")
        raw_side = str(order.get("side") or "").lower()
        if not symbol:
            continue
        position_side = "long" if raw_side == "sell" else "short" if raw_side == "buy" else raw_side
        target = _okx_target_from_payload(order.get("raw") or order)
        current = targets.setdefault((symbol, position_side), {"stop_loss": None, "take_profit": None})
        current["stop_loss"] = current.get("stop_loss") or target.get("stop_loss")
        current["take_profit"] = current.get("take_profit") or target.get("take_profit")
    return targets


def _okx_symbol_from_inst_id(exchange: Any, inst_id: str) -> str:
    if not inst_id:
        return ""
    markets_by_id = getattr(exchange, "markets_by_id", {}) or {}
    market = markets_by_id.get(inst_id)
    if isinstance(market, list):
        market = market[0] if market else None
    if isinstance(market, dict) and market.get("symbol"):
        return str(market["symbol"])
    if inst_id.endswith("-SWAP"):
        parts = inst_id[:-5].split("-")
        if len(parts) >= 2:
            base = "-".join(parts[:-1])
            quote = parts[-1]
            return f"{base}/{quote}:{quote}"
    return inst_id


def _okx_position_side_from_algo(row: dict[str, Any]) -> str:
    pos_side = str(row.get("posSide") or "").strip().lower()
    if pos_side in {"long", "short"}:
        return pos_side
    raw_side = str(row.get("side") or "").strip().lower()
    return "long" if raw_side == "sell" else "short" if raw_side == "buy" else raw_side


def _okx_pending_algo_orders(exchange: Any) -> list[dict[str, Any]]:
    fetch_algos = getattr(exchange, "privateGetTradeOrdersAlgoPending", None)
    if not callable(fetch_algos):
        fetch_algos = getattr(exchange, "private_get_trade_orders_algo_pending", None)
    if not callable(fetch_algos):
        return []

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ord_type in ("oco", "conditional", "trigger"):
        try:
            response = fetch_algos({"ordType": ord_type})
        except Exception as exc:
            logger.warning("OKX pending algo fetch failed for %s: %s", ord_type, exc)
            continue
        chunk = response.get("data") if isinstance(response, dict) else response
        if not isinstance(chunk, list):
            continue
        for row in chunk:
            if not isinstance(row, dict):
                continue
            key = str(row.get("algoId") or f"{row.get('instId')}:{row.get('ordType')}:{row.get('side')}:{row.get('posSide')}")
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    return rows


def _okx_targets_from_algo_orders(exchange: Any) -> dict[tuple[str, str], dict[str, float | None]]:
    targets: dict[tuple[str, str], dict[str, float | None]] = {}
    for row in _okx_pending_algo_orders(exchange):
        symbol = _okx_symbol_from_inst_id(exchange, str(row.get("instId") or ""))
        side = _okx_position_side_from_algo(row)
        if not symbol or side not in {"long", "short"}:
            continue
        target = _okx_target_from_payload(row)
        if target.get("stop_loss") is None and target.get("take_profit") is None:
            continue
        current = targets.setdefault((symbol, side), {"stop_loss": None, "take_profit": None})
        current["stop_loss"] = current.get("stop_loss") or target.get("stop_loss")
        current["take_profit"] = current.get("take_profit") or target.get("take_profit")
    return targets


def _open_okx_positions(config: dict[str, Any]) -> dict[str, Any]:
    status = _okx_demo_status(config) if config.get("mode") == "demo" else {"ready": config.get("mode") == "live"}
    if config.get("mode") == "dry_run":
        return {
            "enabled": False,
            "mode": config.get("mode"),
            "positions": [],
            "open_orders": [],
            "message": "OKX positions are unavailable in dry_run",
        }
    if config.get("mode") == "demo" and not status.get("ready"):
        return {
            "enabled": False,
            "mode": config.get("mode"),
            "positions": [],
            "open_orders": [],
            "message": status.get("message", "OKX demo is not ready"),
        }

    exchange = create_exchange(config, authenticated=True)
    exchange.load_markets()
    open_orders = []
    for order in exchange.fetch_open_orders():
        open_orders.append(
            {
                "id": order.get("id"),
                "symbol": order.get("symbol"),
                "side": order.get("side"),
                "type": order.get("type"),
                "amount": order.get("amount"),
                "filled": order.get("filled"),
                "remaining": order.get("remaining"),
                "price": order.get("price"),
                "status": order.get("status"),
                "datetime": order.get("datetime"),
                "raw": order,
            }
        )
    order_targets = _okx_targets_from_orders(open_orders)
    algo_targets = _okx_targets_from_algo_orders(exchange)
    for order in open_orders:
        order.pop("raw", None)

    positions = []
    for item in exchange.fetch_positions():
        info = item.get("info", {}) if isinstance(item.get("info"), dict) else {}
        contracts = float(item.get("contracts") or info.get("pos") or 0)
        if abs(contracts) <= 0:
            continue
        symbol = item.get("symbol") or info.get("instId")
        side = _position_side(item)
        direct_target = _okx_target_from_payload(item)
        algo_target = algo_targets.get((str(symbol), side), {"stop_loss": None, "take_profit": None})
        order_target = order_targets.get((str(symbol), side), {"stop_loss": None, "take_profit": None})
        stop_loss = direct_target.get("stop_loss") or algo_target.get("stop_loss") or order_target.get("stop_loss")
        take_profit = direct_target.get("take_profit") or algo_target.get("take_profit") or order_target.get("take_profit")
        positions.append(
            {
                "symbol": symbol,
                "side": side,
                "contracts": abs(contracts),
                "entry_price": item.get("entryPrice") or info.get("avgPx"),
                "mark_price": item.get("markPrice") or info.get("markPx"),
                "notional": item.get("notional") or info.get("notionalUsd"),
                "leverage": item.get("leverage") or info.get("lever"),
                "unrealized_pnl": item.get("unrealizedPnl") or info.get("upl"),
                "percentage": item.get("percentage"),
                "margin_mode": item.get("marginMode") or info.get("mgnMode"),
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "tp_sl_status": "ok" if stop_loss is not None and take_profit is not None else "missing",
            }
        )

    return {
        "enabled": True,
        "mode": config.get("mode"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "positions": positions,
        "open_orders": open_orders,
        "algo_target_count": len(algo_targets),
        "message": f"{len(positions)} open position(s), {len(open_orders)} open order(s)",
    }


def create_app(config_path: str = "config.example.yaml") -> FastAPI:
    app = FastAPI(title="Crypto Signal Bot UI")
    app.include_router(market_pattern_router)
    app.state.config_path = config_path
    app.state.lock = threading.Lock()
    app.state.mini_force_lock = threading.Lock()
    app.state.automation_stop = threading.Event()
    app.state.shutdown_started = False
    app.state.lc_pipeline_lock = threading.Lock()
    app.state.lc_pipeline_slot_lock = threading.Lock()
    app.state.storage_maintenance_lock = threading.Lock()
    app.state.storage_maintenance_started_at = None
    app.state.storage_maintenance_finished_at = None
    app.state.storage_maintenance_last_result = None
    app.state.automation_status = {
        "enabled": False,
        "last_result": "not_started",
        "automation_phase": "idle",
    }
    app.state.lc_pipeline_status = {
        "enabled": False,
        "last_result": "not_started",
    }
    app.state.lc_pipeline_candidate_cache = {}
    app.state.market_guard_status = None
    app.state.price_cache = None
    app.state.telegram_view_cache = {}
    app.state.telegram_commands_next_sync_at = None
    app.state.started_at = None
    app.state.telegram_startup_quiet_until = None

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(Exception)
    async def unhandled_application_error(request: Request, exc: Exception) -> JSONResponse:
        LOGGER.exception("Unhandled API error on %s", request.url.path, exc_info=exc)
        try:
            config = load_config(app.state.config_path)
            _notify_system_error(config, f"API {request.url.path}", exc)
        except Exception:
            LOGGER.exception("Could not send Telegram notification for API error")
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    @app.on_event("startup")
    def start_automation() -> None:
        app.state.shutdown_started = False
        config = load_config(app.state.config_path)
        now = datetime.now(timezone.utc)
        quiet_seconds = _telegram_startup_quiet_seconds(config)
        quiet_until = now + timedelta(seconds=quiet_seconds) if quiet_seconds else now
        app.state.started_at = now
        app.state.telegram_startup_quiet_until = quiet_until
        set_telegram_startup_quiet_until(quiet_until)
        try:
            purge_deprecated_journal_state(config)
        except Exception as exc:
            LOGGER.warning("Skipping deprecated journal state purge during startup: %s", exc)
        clear_dashboard_snapshot_cache(config)
        if _is_railway_runtime():
            try:
                sync_runtime_state(config)
            except Exception as exc:
                _notify_system_error(config, "Khởi động đồng bộ OKX/MongoDB", exc)
            sync_telegram_commands(config)
        else:
            LOGGER.info("Skipping blocking OKX/Telegram startup sync on local runtime")
        initial_delay = max(0, int(config.get("automation", {}).get("initial_delay_seconds", 5) or 0))
        interval = _automation_interval(config)
        enabled = _automation_enabled(config)
        app.state.automation_status = {
            "enabled": enabled,
            "interval_seconds": interval,
            "mode": config.get("mode", "dry_run"),
            "last_result": "waiting_initial_delay" if enabled else "disabled",
            "automation_phase": "waiting_initial_delay" if enabled else "disabled",
            "next_scan_at": (now + timedelta(seconds=initial_delay)).isoformat() if enabled else None,
        }
        if not _is_railway_runtime():
            app.state.automation_status.update(
                {
                    "enabled": False,
                    "last_result": "local_ui_only",
                    "automation_phase": "local_ui_only",
                    "next_scan_at": None,
                }
            )
            LOGGER.info("Local runtime is UI-only; background trading workers are disabled")
            return
        if _is_railway_runtime() and config.get("notifications", {}).get("telegram", {}).get("startup_message_enabled", True):
            send_telegram_message(
                config,
                STARTUP_TELEGRAM_MESSAGE,
                with_buttons=False,
                replace_previous=False,
                allow_during_startup_quiet=True,
            )

        def delayed_worker() -> None:
            if app.state.automation_stop.wait(initial_delay):
                return
            _automation_worker(app)

        app.state.automation_thread = threading.Thread(
            target=delayed_worker,
            name="crypto-auto-scan",
            daemon=True,
        )
        app.state.automation_thread.start()
        app.state.telegram_thread = threading.Thread(
            target=lambda: _telegram_button_worker(app),
            name="crypto-telegram-buttons",
            daemon=True,
        )
        app.state.telegram_thread.start()
        app.state.market_guard_thread = threading.Thread(
            target=lambda: _market_guard_worker(app),
            name="crypto-market-guard",
            daemon=True,
        )
        app.state.market_guard_thread.start()
        app.state.lc_pipeline_thread = threading.Thread(
            target=lambda: _lc_pipeline_worker(app),
            name="crypto-lc-pipeline",
            daemon=True,
        )
        app.state.lc_pipeline_thread.start()
        app.state.lc_pipeline_slot_thread = threading.Thread(
            target=lambda: _lc_pipeline_slot_worker(app),
            name="crypto-lc-slot",
            daemon=True,
        )
        app.state.lc_pipeline_slot_thread.start()

    @app.on_event("shutdown")
    def stop_automation() -> None:
        app.state.shutdown_started = True
        app.state.automation_stop.set()
        thread = getattr(app.state, "automation_thread", None)
        if thread and thread.is_alive():
            thread.join(timeout=5)
        telegram_thread = getattr(app.state, "telegram_thread", None)
        if telegram_thread and telegram_thread.is_alive():
            telegram_thread.join(timeout=5)
        market_guard_thread = getattr(app.state, "market_guard_thread", None)
        if market_guard_thread and market_guard_thread.is_alive():
            market_guard_thread.join(timeout=5)
        lc_pipeline_thread = getattr(app.state, "lc_pipeline_thread", None)
        if lc_pipeline_thread and lc_pipeline_thread.is_alive():
            lc_pipeline_thread.join(timeout=5)
        lc_pipeline_slot_thread = getattr(app.state, "lc_pipeline_slot_thread", None)
        if lc_pipeline_slot_thread and lc_pipeline_slot_thread.is_alive():
            lc_pipeline_slot_thread.join(timeout=5)
        set_telegram_startup_quiet_until(None)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        payload = {
            "ok": True,
            "mode": load_config(app.state.config_path).get("mode", "dry_run"),
            "automation": _automation_status_payload(app),
            "lc_pipeline_worker": _lc_pipeline_status_payload(app),
        }
        payload.update(_build_runtime_metadata())
        return payload

    @app.get("/api/version")
    def api_version() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        payload = {
            "ok": True,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": config.get("mode", "dry_run"),
            "runtime_role": config.get("runtime", {}).get("instance_role", "primary"),
            "config_file": Path(app.state.config_path).name,
        }
        payload.update(_build_runtime_metadata())
        return payload

    @app.get("/api/decision")
    def decision() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return _read_report(config)

    @app.post("/api/analyze")
    def analyze() -> dict[str, Any]:
        if not app.state.lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="Analysis is already running")
        try:
            config = load_config(app.state.config_path)
            if atlas_runtime_is_read_only(config):
                raise HTTPException(
                    status_code=403,
                    detail="This runtime is read-only. Only Railway primary may run analysis and write Atlas state.",
                )
            decision_result = run_once(config, execute=False)
            return {
                "report_exists": True,
                "decision": to_jsonable(decision_result),
                "report_path": str(project_path(config, config.get("report_path", "reports/latest_decision.json"))),
                "paper_state": _paper_state(config),
            }
        finally:
            app.state.lock.release()

    @app.post("/api/paper-scan")
    def paper_scan() -> dict[str, Any]:
        if not app.state.lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="Analysis is already running")
        try:
            config = load_config(app.state.config_path)
            if atlas_runtime_is_read_only(config):
                raise HTTPException(
                    status_code=403,
                    detail="This runtime is read-only. Only Railway primary may run paper scan and write Atlas state.",
                )
            decision_result = run_once(config, execute=False)
            paper_result = simulate_paper_scan(config, decision_result)
            return {
                "report_exists": True,
                "decision": to_jsonable(decision_result),
                "paper_result": paper_result,
                "paper_state": _paper_state(config),
                "report_path": str(project_path(config, config.get("report_path", "reports/latest_decision.json"))),
            }
        finally:
            app.state.lock.release()

    @app.get("/api/okx-demo-status")
    def okx_demo_status() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return _okx_demo_status(config)

    @app.get("/api/automation-status")
    def automation_status() -> dict[str, Any]:
        return _automation_status_payload(app)

    @app.get("/api/market-guard")
    def market_guard_status() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        status = getattr(app.state, "market_guard_status", None) or latest_market_guard_status(config)
        return status or {
            "enabled": market_guard_enabled(config),
            "created_at": None,
            "alerts": [],
            "warnings": [],
            "block": market_guard_block_status(config),
        }

    @app.get("/api/market-scan-memory")
    def market_scan_memory(
        symbol: str | None = None,
        timeframe: str | None = None,
        lookback_hours: int = 24,
        per_symbol_timeframe_limit: int = 3,
    ) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        symbols = [item.strip() for item in (symbol or "").split(",") if item.strip()] or None
        timeframes = [item.strip() for item in (timeframe or "").split(",") if item.strip()] or None
        memory = recent_market_scan_memory(
            config,
            symbols=symbols,
            timeframes=timeframes,
            lookback_hours=max(1, min(168, int(lookback_hours or 24))),
            per_symbol_timeframe_limit=max(1, min(20, int(per_symbol_timeframe_limit or 3))),
            total_limit=1000,
        )
        return {
            "lookback_hours": max(1, min(168, int(lookback_hours or 24))),
            "symbols": sorted(memory.keys()),
            "memory": memory,
        }

    @app.get("/api/lc-pipeline")
    def lc_pipeline_endpoint() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        response = JSONResponse(lc_pipeline_dashboard_payload(config))
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    @app.post("/api/market-scan-memory/prune")
    def market_scan_memory_prune() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        result = prune_market_scan_observations(config)
        return {
            "ok": True,
            "retention": config.get("market_scan_memory", {}),
            **result,
        }

    @app.get("/api/storage/stats")
    def storage_stats_endpoint() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return storage_stats(config)

    @app.get("/capital/snapshot/latest")
    def capital_snapshot_latest_endpoint() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        snapshot = latest_capital_snapshot(config)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="No capital snapshot yet")
        return snapshot

    @app.post("/capital/sync")
    def capital_sync_endpoint() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return sync_capital_from_okx(config)

    @app.get("/capital-reserve/state")
    def capital_reserve_state_endpoint() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        state = latest_capital_reserve_state(config)
        if state is None:
            raise HTTPException(status_code=404, detail="No capital reserve state yet")
        return state

    @app.post("/capital-reserve/refresh")
    def capital_reserve_refresh_endpoint(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        payload = payload or {}
        return refresh_capital_reserve_state(
            config,
            mode=str(payload.get("mode") or "HEALTHY"),
            used_trading_capital=payload.get("used_trading_capital"),
        )

    @app.post("/capital-reserve/check-allocation")
    def capital_reserve_check_allocation_endpoint(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        payload = payload or {}
        return check_capital_allocation(
            config,
            payload.get("required_margin"),
            mode=str(payload.get("mode") or "HEALTHY"),
        )

    @app.post("/position-sizing/calculate")
    def position_sizing_calculate_endpoint(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        result = calculate_position_size(config, payload or {})
        return save_position_size_calculation(config, result)

    @app.get("/position-sizing/latest")
    def position_sizing_latest_endpoint() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        row = latest_position_size_calculation(config)
        if row is None:
            raise HTTPException(status_code=404, detail="No position sizing calculation yet")
        return row

    @app.get("/position-sizing/history")
    def position_sizing_history_endpoint(limit: int = 50) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return {"items": position_size_history(config, limit=limit)}

    @app.get("/configuration/current")
    def configuration_current_endpoint() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return current_trading_config(config)

    @app.post("/configuration/impact/analyze")
    def configuration_impact_analyze_endpoint(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        payload = payload or {}
        report = analyze_configuration_change(config, payload.get("proposed_config") or payload)
        save_configuration_impact_report(config, report)
        return report

    @app.post("/configuration/apply")
    def configuration_apply_endpoint(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        payload = payload or {}
        return apply_trading_config(
            config,
            payload.get("proposed_config") or {},
            confirm=bool(payload.get("confirm", False)),
            force=bool(payload.get("force", False)),
        )

    @app.get("/configuration/impact/history")
    def configuration_impact_history_endpoint(limit: int = 50) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return {"items": configuration_impact_history(config, limit=limit)}

    @app.get("/configuration/versions")
    def configuration_versions_endpoint(limit: int = 50) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return {"items": configuration_versions(config, limit=limit)}

    @app.post("/api/storage/maintenance")
    def storage_maintenance_endpoint(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        payload = payload or {}
        return run_storage_maintenance(
            config,
            vacuum=bool(payload.get("vacuum", False)),
            emergency=bool(payload.get("emergency", False)),
        )

    @app.get("/api/system-checklist")
    def system_checklist_endpoint(
        date: str | None = None,
        force_refresh: bool = False,
        ai_range: str = "current",
    ) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        if date:
            snapshot = dashboard_system_checklist_snapshot(config, date)
            if snapshot is None:
                raise HTTPException(status_code=404, detail=f"No system checklist snapshot for {date}")
            return attach_previous_system_checklist_snapshot(config, snapshot)
        return system_checklist_payload(
            config,
            automation=_automation_status_payload(app),
            force_refresh=force_refresh,
            ai_range=ai_range,
        )

    @app.get("/api/system-checklist/history")
    def system_checklist_history_endpoint(limit: int = 30) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        items = dashboard_system_checklist_history(config, limit=limit)
        return {"items": [{"date": item.get("date"), "ok_count": item.get("ok_count"), "total": item.get("total"), "module_count": item.get("module_count")} for item in items]}

    @app.get("/api/system-checklist/summary")
    def system_checklist_summary_endpoint(period: str = "week") -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return dashboard_system_checklist_summary(config, period)

    @app.get("/api/dashboard/timeframes")
    def dashboard_timeframes_endpoint(lookback_hours: int = 24) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return timeframe_state_dashboard(config, lookback_hours=lookback_hours)

    @app.get("/api/dashboard/scan-memory")
    def dashboard_scan_memory_endpoint(lookback_hours: int = 24, per_symbol_timeframe_limit: int = 5) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return scan_memory_dashboard(
            config,
            lookback_hours=lookback_hours,
            per_symbol_timeframe_limit=per_symbol_timeframe_limit,
        )

    @app.get("/api/dashboard/analytics")
    def dashboard_analytics_endpoint(lookback_hours: int = 24) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return analytics_dashboard(config, lookback_hours=lookback_hours)

    @app.get("/api/dashboard/replay")
    def dashboard_replay_endpoint(limit: int = 50) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return replay_dashboard_payload(config, limit=limit)

    @app.get("/api/dashboard/system-health")
    def dashboard_system_health_endpoint(history_limit: int = 30) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return system_health_dashboard(config, history_limit=history_limit)

    @app.get("/api/okx-positions")
    def okx_positions() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        try:
            return _open_okx_positions(config)
        except Exception as exc:
            return {
                "enabled": False,
                "mode": config.get("mode"),
                "positions": [],
                "open_orders": [],
                "message": f"OKX position fetch failed: {exc}",
            }

    @app.post("/api/okx/manual-review-once")
    def okx_manual_review_once(payload: dict[str, Any]) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        route = str(payload.get("route") or "lc_okx_setup_review")
        if route != "lc_okx_setup_review":
            raise HTTPException(status_code=400, detail="5.5 can only be inspected for route=lc_okx_setup_review")

        context: dict[str, Any] = {
            "route": route,
            "source": "manual_api",
        }
        record = None
        lc_id = payload.get("lc_id", payload.get("journal_id"))
        if lc_id is not None:
            try:
                resolved_lc_id = int(lc_id)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="lc_id must be an integer")
            record = _manual_okx_pending_record(config, resolved_lc_id)
            if record is None:
                raise HTTPException(status_code=404, detail=f"No active pending order found for lc_id={resolved_lc_id}")
            context["lc_id"] = resolved_lc_id
            context["from_status"] = str(record.get("status") or "")
            candidate = _manual_okx_candidate_from_record(record)
            if candidate is None:
                raise HTTPException(status_code=400, detail="Pending order does not contain a valid candidate payload")
        else:
            candidate_payload = payload.get("candidate")
            if not isinstance(candidate_payload, dict):
                raise HTTPException(status_code=400, detail="candidate payload is required when lc_id is not provided")
            try:
                candidate = candidate_from_payload(candidate_payload)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"Invalid candidate payload: {exc}") from exc

        risk_check = evaluate_candidate(
            config,
            candidate,
            check_active_trades=False,
            check_order_limits=True,
        )
        decision = candidate_okx_review(candidate, route="lc_okx_setup_review")
        if decision is None:
            raise HTTPException(
                status_code=409,
                detail="No stored initial Mini 5.5 review exists for this setup; 5.5 is not called manually.",
            )
        reviewed_payload = to_jsonable(candidate)
        persisted = False

        return {
            "ok": True,
            "manual_only": False,
            "one_shot": False,
            "persisted": persisted,
            "route": route,
            "lc_id": context.get("lc_id"),
            "candidate": reviewed_payload,
            "risk_check": {
                "passed": risk_check.passed,
                "reasons": risk_check.reasons,
                "warnings": risk_check.warnings,
            },
            "decision": decision,
        }

    @app.post("/api/demo-trade-scan")
    def demo_trade_scan() -> dict[str, Any]:
        if not app.state.lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="Analysis is already running")
        try:
            config = load_config(app.state.config_path)
            status = _okx_demo_status(config)
            if config.get("mode") != "demo":
                raise HTTPException(status_code=400, detail="OKX demo trading requires mode: demo")
            if status["missing_env"]:
                raise HTTPException(
                    status_code=400,
                    detail=f"Missing OKX demo env: {', '.join(status['missing_env'])}",
                )
            decision_result = run_once(config, execute=True)
            return {
                "report_exists": True,
                "decision": to_jsonable(decision_result),
                "demo_status": _okx_demo_status(config),
                "paper_state": _paper_state(config),
                "report_path": str(project_path(config, config.get("report_path", "reports/latest_decision.json"))),
            }
        finally:
            app.state.lock.release()

    @app.post("/api/force-mini-from-latest-4h")
    def force_mini_from_latest_four_hour() -> dict[str, Any]:
        if not app.state.mini_force_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="Mini force is already running")
        try:
            config = load_config(app.state.config_path)
            if atlas_runtime_is_read_only(config):
                raise HTTPException(
                    status_code=403,
                    detail="This runtime is read-only. Only Railway primary may run mini force and write Atlas state.",
                )
            latest_four_hour = latest_lc_pipeline_four_hour_event(config)
            if not latest_four_hour:
                raise HTTPException(status_code=409, detail="No latest 4h LC pool is available")
            demo_status = _okx_demo_status(config) if config.get("mode") == "demo" else None
            if demo_status and demo_status.get("missing_env"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Missing OKX demo env: {', '.join(demo_status['missing_env'])}",
                )
            result = force_mini_scan_from_latest_four_hour(config)
            return {
                "ok": True,
                "mode": config.get("mode"),
                "latest_four_hour": latest_four_hour,
                "internal_market_scan": result.get("internal_market_scan") or {},
                "mini_pending_queue": result.get("mini_pending_queue") or {},
                "demo_status": demo_status,
                "paper_state": _paper_state(config),
                "automation": _automation_status_payload(app),
            }
        finally:
            app.state.mini_force_lock.release()

    @app.post("/api/demo-ai-flow-check")
    def demo_ai_flow_check() -> dict[str, Any]:
        if not app.state.lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="Analysis is already running")
        try:
            config = load_config(app.state.config_path)
            status = _okx_demo_status(config)
            if config.get("mode") != "demo":
                raise HTTPException(status_code=400, detail="AI flow check requires mode: demo")
            if status["missing_env"]:
                raise HTTPException(
                    status_code=400,
                    detail=f"Missing OKX demo env: {', '.join(status['missing_env'])}",
                )
            before_prompt = prompt_status(config)
            decision_result = run_once(config, execute=True)
            after_prompt = prompt_status(config)
            decision_payload = to_jsonable(decision_result)
            scan_comparison = decision_payload.get("scan_comparison") or {}
            forced_internal_scan = scan_comparison.get("internal_market_scan") or {}
            mini_pending_queue = scan_comparison.get("mini_pending_queue") or {}
            return {
                "ok": True,
                "mode": config.get("mode"),
                "demo_status": _okx_demo_status(config),
                "prompt_before": before_prompt.get("metrics") or {},
                "prompt_after": after_prompt.get("metrics") or {},
                "forced_internal_scan": {
                    "provider": forced_internal_scan.get("provider"),
                    "model": forced_internal_scan.get("model"),
                    "approved_symbols": forced_internal_scan.get("approved_symbols") or [],
                    "selected_symbols": forced_internal_scan.get("selected_symbols") or [],
                    "selection_stale": bool(forced_internal_scan.get("selection_stale")),
                    "candidate_count": forced_internal_scan.get("candidate_count"),
                    "ai_review": forced_internal_scan.get("ai_review"),
                    "ai_review_error": forced_internal_scan.get("ai_review_error"),
                    "fallback": forced_internal_scan.get("fallback"),
                },
                "mini_pending_created": mini_pending_queue.get("created_orders") or [],
                "mini_pending_queue": mini_pending_queue,
                "okx_ai_approval": scan_comparison.get("ai_okx_approval"),
                "pending_events": (scan_comparison.get("pending_orders") or {}).get("events") or [],
                "pending_warnings": (scan_comparison.get("pending_orders") or {}).get("warnings") or [],
                "decision": {
                    "created_at": decision_payload.get("created_at"),
                    "action": decision_payload.get("action"),
                    "selected": decision_payload.get("selected"),
                    "risk_check": decision_payload.get("risk_check"),
                    "execution": decision_payload.get("execution"),
                },
            }
        finally:
            app.state.lock.release()

    @app.post("/api/demo-ai-fake-flow")
    def demo_ai_fake_flow() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        if config.get("mode") != "demo":
            raise HTTPException(status_code=400, detail="Fake AI flow requires mode: demo")

        ai_config = config.get("ai", {})
        internal_config = ai_config.get("internal", {})
        okx_config = ai_config.get("okx", {})
        fake_candidates = [
            {
                "symbol": "BTC/USDT:USDT",
                "side": "long",
                "confidence": 97.8,
                "win_probability_pct": 91.4,
                "risk_reward": 2.4,
                "entry": 61250.0,
                "stop_loss": 60620.0,
                "take_profit": 62890.0,
                "spread_pct": 0.01,
                "news_score": 0.35,
                "news_count": 3,
                "indicator_summary": {
                    "last": 61250.0,
                    "ema_fast": 61120.0,
                    "ema_slow": 60780.0,
                    "rsi": 61.2,
                    "atr_pct": 0.42,
                    "volume_ratio": 3.1,
                    "support": 60700.0,
                    "resistance": 63100.0,
                    "candlestick_patterns": {
                        "1m": {
                            "patterns": ["bullish_engulfing", "bullish_marubozu"],
                            "direction": "bullish",
                            "signal_summary": "bullish engulfing confirms continuation above EMA support",
                        }
                    },
                },
                "higher_timeframes": {
                    "5m": {"trend": "up", "rsi": 58.5, "ema_gap_pct": 0.45, "signal_summary": "trend aligned up"},
                    "1h": {"trend": "up", "rsi": 57.4, "ema_gap_pct": 1.05, "signal_summary": "structure supports long"},
                    "4h": {"trend": "up", "rsi": 55.2, "signal_summary": "higher timeframe bullish continuation"},
                },
                "reasons": [
                    "FAKE_SANDBOX: multi-timeframe long alignment",
                    "FAKE_SANDBOX: volume expansion and clean invalidation",
                ],
                "warnings": [],
            },
            {
                "symbol": "ETH/USDT:USDT",
                "side": "long",
                "confidence": 94.2,
                "win_probability_pct": 88.6,
                "risk_reward": 2.1,
                "entry": 3380.0,
                "stop_loss": 3342.0,
                "take_profit": 3460.0,
                "spread_pct": 0.015,
                "news_score": 0.22,
                "news_count": 2,
                "higher_timeframes": {
                    "5m": {"trend": "up", "rsi": 56.0, "signal_summary": "pullback held EMA"},
                    "1h": {"trend": "up", "rsi": 59.1, "signal_summary": "bull flag continuation"},
                    "4h": {"trend": "up", "rsi": 53.8, "signal_summary": "not overextended"},
                },
                "reasons": ["FAKE_SANDBOX: secondary long setup"],
                "warnings": ["Slightly weaker volume than BTC"],
            },
            {
                "symbol": "SOL/USDT:USDT",
                "side": "short",
                "confidence": 92.5,
                "win_probability_pct": 86.9,
                "risk_reward": 2.0,
                "entry": 145.2,
                "stop_loss": 147.1,
                "take_profit": 141.0,
                "spread_pct": 0.02,
                "news_score": -0.15,
                "news_count": 1,
                "higher_timeframes": {
                    "5m": {"trend": "down", "rsi": 43.0, "signal_summary": "lower high rejection"},
                    "1h": {"trend": "down", "rsi": 45.3, "signal_summary": "EMA rejection"},
                    "4h": {"trend": "neutral_down", "rsi": 48.0, "signal_summary": "failed breakout"},
                },
                "reasons": ["FAKE_SANDBOX: hedge short setup"],
                "warnings": ["Counter to configured long bias"],
            },
        ]
        fake_market_snapshot = {
            "provider": "fake_sandbox",
            "decision": "prefilter",
            "threshold_win_probability_pct": 62,
            "approved_symbols": [candidate["symbol"] for candidate in fake_candidates],
            "approved_count": len(fake_candidates),
            "candidate_count": len(fake_candidates),
            "warnings": ["FAKE_SANDBOX_ONLY: do not submit real exchange orders"],
        }
        fake_system_state = get_trading_system_state(config)
        fake_health_state = get_bunny_health_state(config)
        before_prompt = prompt_status(config)
        mini_prompt = build_prompt(
            config,
            build_market_prompt_dto(
                candidates=fake_candidates,
                market_snapshot=fake_market_snapshot,
                trading_system_state=fake_system_state,
                trading_health_state=fake_health_state,
                open_positions=[],
                recent_trades=[],
                extra={"fakeSandbox": True, "instruction": "Approve the best fake setup if coherent."},
            ),
            instruction_key="mini-analysis",
        )
        mini_response = call_openai_json(
            config,
            internal_config,
            mini_prompt,
            model_name=str(internal_config.get("model", "gpt-5.4-mini")),
            purpose="debug_fake_flow",
        )
        mini_decision = dict(mini_response["parsed"])
        approved_symbols = [str(symbol) for symbol in mini_decision.get("approved_symbols") or [] if str(symbol)]
        selected_symbol = approved_symbols[0] if approved_symbols else fake_candidates[0]["symbol"]
        selected = next(
            (candidate for candidate in fake_candidates if candidate["symbol"] == selected_symbol),
            fake_candidates[0],
        )
        okx_prompt = build_prompt(
            config,
            build_market_prompt_dto(
                candidates=[selected],
                market_snapshot={
                    "riskCheck": {"passed": True, "reasons": [], "warnings": ["FAKE_SANDBOX_ONLY"]},
                    "context": {
                        "route": "fake_sandbox_okx_final_check",
                        "source": "demo-ai-fake-flow",
                        "willSubmitRealOrder": False,
                    },
                },
                trading_system_state=fake_system_state,
                trading_health_state=fake_health_state,
                open_positions=[],
                recent_trades=[],
                extra={"fakeSandbox": True, "miniDecision": mini_decision},
            ),
            instruction_key="final-decision",
        )
        okx_response = call_openai_json(
            config,
            okx_config,
            okx_prompt,
            model_name=str(okx_config.get("model", "gpt-5.5")),
            purpose="debug_fake_flow",
            route="fake_sandbox_okx_final_check",
        )
        okx_decision = dict(okx_response["parsed"])
        simulated_order = {
            "submitted": False,
            "simulated": True,
            "symbol": selected.get("symbol"),
            "side": selected.get("side"),
            "entry": selected.get("entry"),
            "stop_loss": selected.get("stop_loss"),
            "take_profit": selected.get("take_profit"),
            "reason": "FAKE_SANDBOX_ONLY: GPT OKX final check completed; no real OKX order submitted",
            "okx_approved": bool(okx_decision.get("approved")),
        }
        after_prompt = prompt_status(config)
        summary = (
            "ðŸ§ª Fake AI sandbox hoÃ n táº¥t\n"
            f"âœ… Mini gá»i: {internal_config.get('model', 'gpt-5.4-mini')}\n"
            f"Mini duyá»‡t: {', '.join(approved_symbols) if approved_symbols else 'khÃ´ng duyá»‡t rÃµ, dÃ¹ng BTC fake Ä‘á»ƒ test OKX'}\n"
            f"âœ… OKX gá»i: {okx_config.get('model', 'gpt-5.5')}\n"
            f"OKX quyáº¿t Ä‘á»‹nh: {okx_decision.get('decision') or okx_decision.get('approved')}\n"
            f"Lá»‡nh mÃ´ phá»ng: {selected.get('side')} {selected.get('symbol')}\n"
            "âš ï¸ KhÃ´ng gá»­i lá»‡nh tháº­t lÃªn OKX."
        )
        send_telegram_message(config, summary, with_buttons=False)
        return {
            "ok": True,
            "fake_sandbox": True,
            "prompt_before": before_prompt.get("metrics") or {},
            "prompt_after": after_prompt.get("metrics") or {},
            "mini": {
                "model": str(internal_config.get("model", "gpt-5.4-mini")),
                "decision": mini_decision,
                "latency_ms": mini_response.get("latency_ms"),
                "prompt_tokens": mini_response.get("prompt_tokens"),
                "completion_tokens": mini_response.get("completion_tokens"),
            },
            "okx": {
                "model": str(okx_config.get("model", "gpt-5.5")),
                "decision": okx_decision,
                "latency_ms": okx_response.get("latency_ms"),
                "prompt_tokens": okx_response.get("prompt_tokens"),
                "completion_tokens": okx_response.get("completion_tokens"),
            },
            "fake_candidates": fake_candidates,
            "selected": selected,
            "simulated_order": simulated_order,
        }

    @app.get("/api/config")
    def config_summary() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return {
            "mode": config.get("mode"),
            "exchange": {
                "leverage": config.get("exchange", {}).get("leverage"),
                "leverage_presets": config.get("exchange", {}).get("leverage_presets", []),
                "td_mode": config.get("exchange", {}).get("td_mode"),
                "account_type": config.get("exchange", {}).get("account_type"),
            },
            "symbols": config.get("strategy", {}).get("symbols", []),
            "timeframe": config.get("strategy", {}).get("timeframe"),
            "min_confidence": config.get("strategy", {}).get("min_confidence"),
            "min_win_probability_pct": config.get("strategy", {}).get("min_win_probability_pct"),
            "min_risk_reward": config.get("strategy", {}).get("min_risk_reward"),
            "target": config.get("strategy", {}).get("target", {}),
            "universe": config.get("strategy", {}).get("universe", {}),
            "confirmation_timeframes": config.get("strategy", {}).get("confirmation_timeframes", {}),
            "candlestick_patterns": config.get("strategy", {}).get("candlestick_patterns", {}),
            "long_short_bias": config.get("strategy", {}).get("long_short_bias", {}),
            "order_usdt": _effective_order_usdt(config),
            "risk_order_usdt": config.get("risk", {}).get("order_usdt"),
            "order_margin_usdt": config.get("position_sizing", {}).get("base_margin_usdt"),
            "paper_trading": config.get("paper_trading", {}),
            "automation": config.get("automation", {}),
            "market_guard": config.get("market_guard", {}),
            "ai": config.get("ai", {}),
            "position_sizing": config.get("position_sizing", {}),
        }

    @app.post("/api/config/leverage")
    def update_leverage(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            leverage = int(payload.get("leverage"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Leverage must be a whole number")
        if leverage < MIN_LEVERAGE or leverage > MAX_LEVERAGE:
            raise HTTPException(status_code=400, detail=f"Leverage must be between {MIN_LEVERAGE}x and {MAX_LEVERAGE}x")

        config = _save_leverage(app.state.config_path, leverage)
        return {
            "mode": config.get("mode"),
            "exchange": {
                "leverage": config.get("exchange", {}).get("leverage"),
                "leverage_presets": config.get("exchange", {}).get("leverage_presets", []),
                "td_mode": config.get("exchange", {}).get("td_mode"),
                "account_type": config.get("exchange", {}).get("account_type"),
            },
            "message": "Leverage saved. New orders will use this value.",
        }

    @app.post("/api/config/order-usdt")
    def update_order_usdt(payload: dict[str, Any]) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        try:
            margin = float(payload.get("margin_usdt", payload.get("usdt")))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="USDT margin must be a number")
        max_margin = _max_base_margin_usdt(config)
        if not math.isfinite(margin) or margin < MIN_BASE_MARGIN_USDT or margin > max_margin:
            raise HTTPException(
                status_code=400,
                detail=f"USDT margin must be between {MIN_BASE_MARGIN_USDT:g} and {max_margin:g}",
            )

        config = _save_base_margin(app.state.config_path, margin)
        _sync_idle_sizing_state(config, margin)
        leverage = float(config.get("exchange", {}).get("leverage", 1) or 1)
        return {
            "mode": config.get("mode"),
            "position_sizing": config.get("position_sizing", {}),
            "estimated_notional_usdt": round(margin * leverage, 4),
            "message": "Order USDT margin saved. New orders will use this value.",
        }

    @app.post("/api/ai-decisions")
    def post_ai_decision(payload: dict[str, Any]) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return create_ai_trade_decision(config, payload)

    @app.get("/api/ai-decisions/stats")
    def ai_decision_stats_endpoint() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return ai_trade_decision_stats(config)

    @app.get("/api/ai-decisions/recent")
    def ai_decision_recent_endpoint(limit: int = 50) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return {"items": recent_ai_trade_decisions(config, limit=max(1, min(limit, 500)))}

    @app.get("/api/trading-risk/state")
    def trading_risk_state() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return get_trading_system_state(config)

    @app.post("/api/trading-risk/validate-entry")
    def trading_risk_validate(payload: dict[str, Any]) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return validate_entry(config, payload)

    @app.post("/api/trade-executions/close")
    def trade_execution_close(payload: dict[str, Any]) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        trade_execution_id = _safe_int(payload.get("tradeExecutionId", payload.get("trade_execution_id")), 0)
        if trade_execution_id <= 0:
            raise HTTPException(status_code=400, detail="tradeExecutionId is required")
        status = str(payload.get("status") or "").upper()
        if status not in {"WIN", "LOSS", "BREAKEVEN", "CLOSED"}:
            raise HTTPException(status_code=400, detail="status must be WIN, LOSS, BREAKEVEN, or CLOSED")
        pnl = _safe_float(payload.get("pnl"), 0.0)
        close_reason = payload.get("closeReason", payload.get("close_reason"))
        try:
            return close_trade_execution(config, trade_execution_id, status, pnl, None if close_reason is None else str(close_reason))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/bunny-health/state")
    def bunny_health_state() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return get_bunny_health_state(config)

    @app.post("/api/bunny-health/refresh")
    def bunny_health_refresh() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return refresh_bunny_health_state(config)

    @app.post("/api/replay/run")
    def replay_run(payload: dict[str, Any]) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        trade_execution_id = _safe_int(payload.get("tradeExecutionId", payload.get("trade_execution_id")), 0)
        if trade_execution_id <= 0:
            raise HTTPException(status_code=400, detail="tradeExecutionId is required")
        try:
            return replay_trade_execution(config, trade_execution_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/replay/batch")
    def replay_run_batch(payload: dict[str, Any]) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        limit = _safe_int(
            payload.get("limit", payload.get("count", config.get("replay_engine", {}).get("default_batch_limit", 100))),
            100,
        )
        return replay_batch(config, max(1, min(limit, 1000)))

    @app.get("/api/replay/stats")
    def replay_stats_endpoint() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return replay_stats(config)

    @app.get("/api/market-regime/current")
    def market_regime_current_endpoint() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return current_market_regime(config)

    @app.get("/api/market-regime/history")
    def market_regime_history_endpoint(limit: int = 100) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return {"items": market_regime_history(config, limit=max(1, min(limit, 500)))}

    @app.get("/api/strategy/current")
    def strategy_current_endpoint() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return current_strategy_state(config)

    @app.get("/api/strategy/history")
    def strategy_history_endpoint() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return {"items": strategy_history(config)}

    @app.post("/api/strategy/create")
    def strategy_create_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return create_strategy_version(config, payload)

    @app.post("/api/strategy/activate")
    def strategy_activate_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        version = str(payload.get("version") or "")
        if not version:
            raise HTTPException(status_code=400, detail="version is required")
        try:
            return activate_strategy_version(config, version)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/prompt/status")
    def prompt_status_endpoint() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return prompt_status(config)

    @app.post("/api/prompt/reload")
    def prompt_reload_endpoint() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        load_prompt_templates(config, force_reload=True)
        ensure_prompt_version(config)
        return prompt_status(config)

    @app.post("/api/prompt/build")
    def prompt_build_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        instruction_key = str(payload.get("instructionKey") or payload.get("instruction_key") or "final-decision")
        market_dto = payload.get("marketPromptDto") or payload.get("market_prompt_dto") or build_market_prompt_dto(
            candidates=payload.get("candidates") or [],
            market_snapshot=payload.get("marketSnapshot") or payload.get("market_snapshot") or {},
            trading_system_state=payload.get("tradingSystemState") or payload.get("trading_system_state") or {},
            trading_health_state=payload.get("tradingHealthState") or payload.get("trading_health_state") or {},
            open_positions=payload.get("openPositions") or payload.get("open_positions") or [],
            recent_trades=payload.get("recentTrades") or payload.get("recent_trades") or [],
        )
        result = build_prompt(
            config,
            market_dto,
            instruction_key=instruction_key,
            recovery_mode=bool(payload.get("recoveryMode", payload.get("recovery_mode", False))),
            health_warning=bool(payload.get("healthWarning", payload.get("health_warning", False))),
        )
        return {
            "promptVersion": result["prompt_version"],
            "promptHash": result["prompt_hash"],
            "experimentName": result["experiment_name"],
            "messages": result["messages"],
            "estimatedStaticTokens": result["estimated_static_tokens"],
            "estimatedDynamicTokens": result["estimated_dynamic_tokens"],
            "estimatedCacheHit": result["estimated_cache_hit"],
        }

    @app.get("/api/prompt/history")
    def prompt_history_endpoint() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return {"items": prompt_history(config)}

    @app.get("/api/ai-experiments")
    def ai_experiments_endpoint() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return {"items": list_ai_experiments(config)}

    @app.post("/api/ai-experiments")
    def ai_experiments_create_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        return create_ai_experiment(config, payload)

    @app.get("/api/prices")
    def prices() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        report = _read_report(config)
        focus = _decision_focus(report.get("decision"))
        decision_symbols = [
            str(candidate.get("symbol"))
            for candidate in ((report.get("decision") or {}).get("candidates") or [])
            if candidate.get("symbol")
        ]
        symbols = list(dict.fromkeys(config.get("strategy", {}).get("symbols", []) + decision_symbols))
        now = datetime.now(timezone.utc)
        cached = getattr(app.state, "price_cache", None)
        if cached:
            age = (now - cached["created_at"]).total_seconds()
            if age < PRICE_CACHE_TTL_SECONDS:
                payload = dict(cached["payload"])
                payload["cached"] = True
                payload["cache_age_seconds"] = round(age, 3)
                payload["served_at"] = now.isoformat()
                return payload

        cached_rows = {
            row.get("symbol"): row
            for row in ((cached or {}).get("payload", {}).get("prices") or [])
            if row.get("symbol")
        }
        rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        try:
            exchange = create_exchange(config, authenticated=False)
            exchange.load_markets()
        except Exception as exc:
            warning = f"market metadata fetch failed: {exc}"
            warnings.append(warning)
            for symbol in symbols:
                cached_row = cached_rows.get(symbol)
                if cached_row:
                    row = dict(cached_row)
                    row["stale"] = True
                    row["error"] = warning
                    rows.append(row)
                else:
                    rows.append(_empty_price_row(symbol, warning))
            return {
                "created_at": now.isoformat(),
                "served_at": now.isoformat(),
                "focus": focus,
                "prices": rows,
                "warnings": warnings,
                "cached": False,
            }

        for symbol in symbols:
            try:
                ticker = exchange.fetch_ticker(symbol)
                rows.append(_price_row(symbol, ticker))
            except Exception as exc:
                message = f"{symbol}: price fetch failed: {exc}"
                warnings.append(message)
                cached_row = cached_rows.get(symbol)
                if cached_row:
                    row = dict(cached_row)
                    row["stale"] = True
                    row["error"] = message
                    rows.append(row)
                else:
                    rows.append(_empty_price_row(symbol, message))

        payload = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "served_at": now.isoformat(),
            "focus": focus,
            "prices": rows,
            "warnings": warnings,
            "cached": False,
        }
        if rows:
            app.state.price_cache = {
                "created_at": now,
                "payload": payload,
            }
        return payload

    return app

def create_app_from_env() -> FastAPI:
    return create_app(os.environ.get("CRYPTO_TRADER_CONFIG", "config.example.yaml"))
