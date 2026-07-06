from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import DEFAULT_CONFIG, load_config, project_path
from .ai_coordinator import next_internal_market_scan_at, run_internal_market_scan_if_due
from .codex_features import (
    activate_strategy_version,
    ai_trade_decision_stats,
    build_market_prompt_dto,
    build_prompt,
    call_openai_json,
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
from .engine import run_once
from .lc_pipeline import format_internal_notifications_view, lc_pipeline_dashboard_payload
from .market import create_exchange
from .market_guard import (
    latest_market_guard_status,
    market_guard_block_status,
    market_guard_enabled,
    market_guard_interval,
    market_guard_notify_interval,
    run_market_guard,
)
from .models import to_jsonable
from .notifier import (
    answer_callback_query,
    delete_telegram_message,
    edit_telegram_chat_message,
    fetch_telegram_updates,
    send_telegram_chat_message,
    send_telegram_message,
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
    format_market_guard_message,
    format_market_scan_memory_view,
    format_pending_orders_view,
    format_pending_event_messages,
    format_positions_account_view,
    format_scan_message,
    format_telegram_menu,
    format_undecided_lc_view,
)
from .storage import (
    count_pending_orders,
    get_journal_state,
    latest_decision_payload,
    list_paper_trades,
    prune_market_scan_observations,
    recent_market_scan_memory,
    run_storage_maintenance,
    set_journal_state,
    storage_stats,
)
from .sizing import STATE_KEY as SIZING_STATE_KEY


STATIC_DIR = Path(__file__).resolve().parent / "static"
PRICE_CACHE_TTL_SECONDS = 55
MIN_LEVERAGE = 5
MAX_LEVERAGE = 25
MIN_BASE_MARGIN_USDT = 1.0


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


def _save_leverage(config_path: str | Path, leverage: int) -> dict[str, Any]:
    path = _config_file(config_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        user_config = yaml.safe_load(handle) or {}
    exchange = user_config.setdefault("exchange", {})
    exchange["leverage"] = leverage
    sizing = user_config.setdefault("position_sizing", {})
    try:
        base_margin = float(sizing.get("base_margin_usdt", DEFAULT_CONFIG["position_sizing"]["base_margin_usdt"]) or 0)
    except (TypeError, ValueError):
        base_margin = float(DEFAULT_CONFIG["position_sizing"]["base_margin_usdt"])
    if base_margin > 0:
        risk = user_config.setdefault("risk", {})
        risk["order_usdt"] = round(base_margin * leverage, 4)

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(user_config, handle, sort_keys=False, allow_unicode=True)
    tmp_path.replace(path)
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
    with path.open("r", encoding="utf-8") as handle:
        user_config = yaml.safe_load(handle) or {}
    sizing = user_config.setdefault("position_sizing", {})
    sizing["base_margin_usdt"] = round(margin_usdt, 4)
    try:
        leverage = float(user_config.get("exchange", {}).get("leverage", DEFAULT_CONFIG["exchange"]["leverage"]) or 1)
    except (TypeError, ValueError):
        leverage = float(DEFAULT_CONFIG["exchange"]["leverage"])
    risk = user_config.setdefault("risk", {})
    risk["order_usdt"] = round(margin_usdt * leverage, 4)

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(user_config, handle, sort_keys=False, allow_unicode=True)
    tmp_path.replace(path)
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
        "💰 Cài USDT cho lệnh sau\n"
        f"Đang dùng: {_margin_label(margin)} USDT margin/lệnh\n"
        f"Giá trị vị thế ước tính: {_margin_label(notional)} USDT ({leverage:g}x)\n"
        f"Giới hạn: {_margin_label(MIN_BASE_MARGIN_USDT)}-{_margin_label(max_margin)} USDT margin\n"
        "Chọn nút bên dưới hoặc gửi /usdt 5"
    )


def _setup_menu_message(config: dict[str, Any]) -> str:
    sizing = config.get("position_sizing", {})
    margin = float(sizing.get("base_margin_usdt", 2) or 2)
    leverage = int(float(config.get("exchange", {}).get("leverage", 10) or 10))
    max_positions = int(float(config.get("risk", {}).get("max_active_trades", 1) or 1))
    return (
        "⚙️ Setup giao dịch\n"
        f"USDT/lệnh: {_margin_label(margin)} USDT\n"
        f"Đòn bẩy: {leverage}x\n"
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
        return config, "⚠️ USDT/lệnh không hợp lệ. Ví dụ: /usdt 5", telegram_order_usdt_keyboard(config)
    max_margin = _max_base_margin_usdt(config)
    if not math.isfinite(margin) or margin < MIN_BASE_MARGIN_USDT or margin > max_margin:
        return (
            config,
            f"⚠️ Chỉ nhận từ {_margin_label(MIN_BASE_MARGIN_USDT)} đến {_margin_label(max_margin)} USDT margin/lệnh.",
            telegram_order_usdt_keyboard(config),
        )

    updated = _save_base_margin(config_path, margin)
    _sync_idle_sizing_state(updated, margin)
    leverage = float(updated.get("exchange", {}).get("leverage", 1) or 1)
    notional = margin * leverage
    message = (
        "✅ Đã lưu USDT cho lệnh sau\n"
        f"Margin/lệnh: {_margin_label(margin)} USDT\n"
        f"Giá trị vị thế ước tính: {_margin_label(notional)} USDT ({leverage:g}x)\n"
        "Áp dụng từ lệnh mở sau."
    )
    return updated, message, telegram_order_usdt_keyboard(updated)


def _leverage_menu_message(config: dict[str, Any]) -> str:
    leverage = int(float(config.get("exchange", {}).get("leverage", 10) or 10))
    margin = float(config.get("position_sizing", {}).get("base_margin_usdt", 2) or 2)
    notional = margin * leverage
    return (
        "⚙️ Cài đòn bẩy cho lệnh sau\n"
        f"Đang dùng: {leverage}x\n"
        f"Margin/lệnh hiện tại: {_margin_label(margin)} USDT\n"
        f"Giá trị vị thế ước tính: {_margin_label(notional)} USDT\n"
        f"Giới hạn: {MIN_LEVERAGE}-{MAX_LEVERAGE}x\n"
        "Chọn nút bên dưới hoặc gửi /lev 15"
    )


def _set_leverage_from_telegram(
    config_path: str | Path,
    config: dict[str, Any],
    raw_value: str,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    try:
        leverage = int(float(raw_value))
    except (TypeError, ValueError):
        return config, "⚠️ Đòn bẩy không hợp lệ. Ví dụ: /lev 15", telegram_leverage_keyboard(config)
    if leverage < MIN_LEVERAGE or leverage > MAX_LEVERAGE:
        return (
            config,
            f"⚠️ Chỉ nhận đòn bẩy từ {MIN_LEVERAGE}x đến {MAX_LEVERAGE}x.",
            telegram_leverage_keyboard(config),
        )

    updated = _save_leverage(config_path, leverage)
    margin = float(updated.get("position_sizing", {}).get("base_margin_usdt", 2) or 2)
    notional = margin * leverage
    message = (
        "✅ Đã lưu đòn bẩy cho lệnh sau\n"
        f"Đòn bẩy: {leverage}x\n"
        f"Margin/lệnh: {_margin_label(margin)} USDT\n"
        f"Giá trị vị thế ước tính: {_margin_label(notional)} USDT\n"
        "Áp dụng từ lệnh mở sau."
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
            "source": "sqlite" if latest else "none",
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


def _automation_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("automation", {}).get("enabled", True))


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




def _run_automation_cycle(app: FastAPI) -> None:
    now = datetime.now(timezone.utc)
    config = load_config(app.state.config_path)
    interval = _automation_interval(config)
    payload: dict[str, Any] | None = None
    status: dict[str, Any] = {
        "enabled": _automation_enabled(config),
        "interval_seconds": interval,
        "mode": config.get("mode", "dry_run"),
        "last_started_at": now.isoformat(),
        "next_scan_at": (now + timedelta(seconds=interval)).isoformat(),
    }
    if not status["enabled"]:
        status["last_result"] = "disabled"
        app.state.automation_status = status
        return

    execute, reason = _automation_should_execute(config)
    status["execute"] = execute
    status["execute_reason"] = reason

    if not app.state.lock.acquire(blocking=False):
        status["last_result"] = "skipped_busy"
        app.state.automation_status = status
        return

    try:
        decision_result = run_once(config, execute=execute)
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
                "error": str(exc),
            }
        )
    finally:
        app.state.automation_status = status
        app.state.lock.release()

    try:
        refresh_system_checklist_snapshot(config, automation=status)
    except Exception:
        pass
    try:
        timeframe_state_dashboard(config, force_refresh=True)
        scan_memory_dashboard(config, force_refresh=True)
        analytics_dashboard(config, force_refresh=True)
        replay_dashboard_payload(config, force_refresh=True)
        system_health_dashboard(config, force_refresh=True)
    except Exception:
        pass

    messages: list[str] = []
    should_notify_scan = status.get("last_result") == "error" or telegram_notify_scans(config)
    if should_notify_scan:
        messages.append(format_scan_message(config, payload, status))
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
        _run_automation_cycle(app)
        app.state.automation_stop.wait(interval)


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
        "📈 Cài số vị thế tối đa mở cùng lúc\n"
        f"Đang dùng: {current} vị thế\n"
        "Chọn nút bên dưới hoặc gửi /maxvt 3"
    )


def _set_max_positions_from_telegram(
    config_path: str | Path,
    config: dict[str, Any],
    raw_value: str,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    try:
        max_positions = int(float(raw_value))
    except (TypeError, ValueError):
        return config, "⚠️ Số vị thế không hợp lệ. Ví dụ: /maxvt 3", telegram_max_positions_keyboard(config)
    if max_positions < 1 or max_positions > 10:
        return config, "⚠️ Chỉ nhận số vị thế từ 1 đến 10.", telegram_max_positions_keyboard(config)

    updated = _save_max_positions(config_path, max_positions)
    message = (
        "✅ Đã lưu số vị thế tối đa\n"
        f"Max vị thế mở cùng lúc: {max_positions}\n"
        "Áp dụng từ chu kỳ scan/lệnh tiếp theo."
    )
    return updated, message, telegram_max_positions_keyboard(updated)


def _ai_history_keyboard(*, expanded: bool, has_more: bool) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    if has_more and not expanded:
        rows.append([{"text": "🔎 Xem thêm 10 lần cũ", "callback_data": "view_ai_more"}])
    if expanded:
        rows.append([{"text": "🔙 Thu gọn 5 lần gần nhất", "callback_data": "view_ai"}])
    rows.append([{"text": "📲 Menu", "callback_data": "view_menu"}])
    return {"inline_keyboard": rows}


def _telegram_side_label(value: Any) -> str:
    side = str(value or "").strip().lower()
    if side == "long":
        return "LONG"
    if side == "short":
        return "SHORT"
    return str(value or "-").upper() if value else "-"


def _ai_symbol_detail_lines(item: dict[str, Any]) -> list[str]:
    details = item.get("candidate_details") if isinstance(item.get("candidate_details"), list) else []
    approved = [str(symbol) for symbol in item.get("approved_symbols") or [] if str(symbol)]
    scores = item.get("setup_scores") if isinstance(item.get("setup_scores"), dict) else {}
    lines: list[str] = []
    if details:
        lines.append("3 cặp giao dịch được mini đánh giá:")
        for index, detail in enumerate(details[:3], start=1):
            if not isinstance(detail, dict):
                continue
            symbol = str(detail.get("symbol") or "-")
            chosen = " ✅ mini gửi LC" if symbol in approved else ""
            metric_parts = []
            if detail.get("win_probability_pct") is not None:
                metric_parts.append(f"Win {_telegram_number(detail.get('win_probability_pct'), '%')}")
            if detail.get("confidence") is not None:
                metric_parts.append(f"Tin cậy {_telegram_number(detail.get('confidence'))}")
            if detail.get("risk_reward") is not None:
                metric_parts.append(f"R:R {_telegram_number(detail.get('risk_reward'))}")
            if scores.get(symbol) is not None:
                metric_parts.append(f"Mini score {_telegram_number(scores.get(symbol))}")
            lines.append(f"{index}. {symbol} | {_telegram_side_label(detail.get('side'))}{chosen}")
            if metric_parts:
                lines.append("   " + " | ".join(metric_parts))
            reasons = [str(reason) for reason in detail.get("reasons") or [] if str(reason)]
            if reasons:
                lines.append("   Lý do:")
                for reason in reasons[:3]:
                    lines.append(f"   - {reason[:180]}")
        if approved:
            lines.append("Mini chọn gửi:")
            for symbol in approved[:3]:
                lines.append(f"- {symbol}")
        return lines

    symbols = [str(symbol) for symbol in item.get("symbols") or [] if str(symbol)]
    if symbols:
        lines.append("Cặp giao dịch:")
        for index, symbol in enumerate(symbols[:5], start=1):
            marker = " ✅ mini gửi LC" if symbol in approved else ""
            lines.append(f"{index}. {symbol}{marker}")
    return lines


def _format_ai_call_history_view(config: dict[str, Any], *, expanded: bool = False) -> str:
    limit = 15 if expanded else 5
    items = recent_ai_call_history(config, limit=limit)
    if not items:
        return "🤖 AI: chưa có lịch sử gọi GPT nào được lưu."
    title = "🤖 Lịch sử gọi AI gần nhất"
    title += " (15 lần, mới nhất ở dưới)" if expanded else " (5 lần, mới nhất ở dưới)"
    lines = [title]
    for item in items:
        role = str(item.get("role") or "ai").upper()
        created_at = str(item.get("created_at") or "")
        try:
            created_label = datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone(
                _system_timezone(config)
            ).strftime("%d/%m/%Y %H:%M:%S VN")
        except ValueError:
            created_label = created_at[:16] or "-"
        lines.append("")
        lines.append(f"🕒 {created_label}")
        lines.append(f"Vai trò: {role}")
        lines.append(f"Model: {item.get('model', '-')}")
        lines.append(f"Trạng thái: {item.get('status', '-')}")
        lines.extend(_ai_symbol_detail_lines(item))
        reason = str(item.get("reason") or "")
        if reason:
            lines.append("Lý do AI:")
            for part in reason.replace("; ", "\n").splitlines():
                text = part.strip()
                if text:
                    lines.append(f"- {text[:220]}")
    return "\n".join(lines)

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
        pending_count = count_pending_orders(config)
    except Exception:
        pending_count = "-"

    auto_label = "bật" if status.get("enabled") else "tắt"
    execute_label = "có gửi lệnh" if status.get("execute") else "chỉ theo dõi"
    okx_label = "sẵn sàng" if demo.get("ready") else demo.get("message", "chưa sẵn sàng")

    lines = [
        "📲 Bảng điều khiển Telegram",
        f"⚙️ Mode: {config.get('mode', '-')} | Auto: {auto_label} | {execute_label}",
        f"🧪 OKX demo: {okx_label}",
        (
            "💰 Lệnh sau: "
            f"{_telegram_number(margin, ' USDT')} margin | "
            f"{_telegram_number(leverage, 'x')} | "
            f"vị thế {_telegram_number(notional, ' USDT')}"
        ),
        f"🟡 LC đang chờ: {pending_count}",
    ]
    lines.insert(3, f"AI: internal {ai_internal.get('model', '-')} | OKX {ai_okx.get('model', '-')}")
    try:
        next_mini = _telegram_vn_time(config, next_internal_market_scan_at(config))
        lines.insert(4, f"🤖 Mini scan tiếp: {next_mini}")
    except Exception:
        pass
    if top:
        lines.append(
            "🏆 Top hiện tại: "
            f"{top.get('symbol', '-')} {str(top.get('side', '-')).upper()} | "
            f"tin cậy {_telegram_number(top.get('confidence'))}"
        )
    else:
        lines.append("🏆 Top hiện tại: chưa có dữ liệu scan")
    if risk.get("passed") is not None:
        lines.append(f"🛡 Risk gate: {'PASS' if risk.get('passed') else 'BLOCK'}")
    elif status.get("risk_passed") is not None:
        lines.append(f"🛡 Risk gate: {'PASS' if status.get('risk_passed') else 'BLOCK'}")
    if reasons:
        lines.append("⚠️ Lý do: " + " | ".join(str(item) for item in reasons[:2]))
    if status.get("last_finished_at"):
        lines.append(f"🕒 Scan gần nhất: {_telegram_vn_time(config, status.get('last_finished_at'))}")
    if status.get("next_scan_at"):
        lines.append(f"⏭ Scan tự động tiếp: {_telegram_vn_time(config, status.get('next_scan_at'))}")
    lines.append("Bam nut ben duoi hoac go /menu, /setup trong Telegram.")
    return "\n".join(lines)


def _telegram_guard_message(config: dict[str, Any], app: FastAPI | None = None) -> str:
    status = getattr(app.state, "market_guard_status", None) if app is not None else None
    status = status or latest_market_guard_status(config)
    if status:
        return format_market_guard_message(status)
    return "🛡 Market Guard chưa có dữ liệu. Bấm Scan ngay hoặc chờ chu kỳ guard kế tiếp."


def _run_telegram_scan(app: FastAPI | None, config_path: str | Path) -> tuple[dict[str, Any], str, dict[str, Any]]:
    config = load_config(config_path)
    if app is None:
        return config, "⚠️ Scan ngay chỉ khả dụng khi bot UI server đang chạy.", telegram_control_keyboard()
    if not app.state.lock.acquire(blocking=False):
        return config, "⏳ Bot đang bận scan chu kỳ khác. Thử lại sau vài giây.", telegram_control_keyboard()
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
        return config, f"🚨 Scan lỗi: {exc}", telegram_control_keyboard()
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
        return config, format_pending_orders_view(config), None
    if action == "view_undecided_lc":
        return config, format_undecided_lc_view(config), None
    if action == "view_internal_notifications":
        return config, format_internal_notifications_view(config), telegram_control_keyboard()
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


def _handle_telegram_update(config: dict[str, Any], update: dict[str, Any], config_path: str | Path, app: FastAPI | None = None) -> None:
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        callback_id = str(callback.get("id") or "")
        action = str(callback.get("data") or "view_menu")
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
        if callback_id:
            answer_callback_query(config, callback_id, "Đang chạy scan..." if action == "scan_now" else "Đang lấy dữ liệu...")
        if not _telegram_chat_allowed(config, chat_id):
            return
        thread_id = message.get("message_thread_id")
        message_id = message.get("message_id")
        response_config, response_text, reply_markup = _telegram_action_response(config, action, config_path, app)
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
            delete_telegram_message(response_config, chat_id, message_id)
            send_telegram_chat_message(
                response_config,
                chat_id,
                response_text,
                message_thread_id=thread_id,
                reply_markup=reply_markup,
            )
            return
        if action == "view_menu" and message_id is not None:
            edited = edit_telegram_chat_message(
                response_config,
                chat_id,
                message_id,
                response_text,
                reply_markup=reply_markup,
            )
            if edited:
                return
            delete_telegram_message(response_config, chat_id, message_id)
            send_telegram_chat_message(
                response_config,
                chat_id,
                response_text,
                message_thread_id=thread_id,
                reply_markup=reply_markup,
            )
            return
        send_telegram_chat_message(
            response_config,
            chat_id,
            response_text,
            message_thread_id=thread_id,
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
        "/scan": "scan_now",
        "/guard": "view_guard",
        "/vt": "view_positions_account",
        "/sd": "view_sd",
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
    try:
        config = load_config(app.state.config_path)
        sync_telegram_commands(config)
        stored = get_journal_state(config, "telegram_update_offset")
        offset_value = int(stored) if stored else None
    except Exception:
        offset_value = None

    while not app.state.automation_stop.is_set():
        try:
            config = load_config(app.state.config_path)
            sync_telegram_commands(config)
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
        except Exception:
            app.state.automation_stop.wait(5)


def _parse_iso_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _market_guard_notify_due(config: dict[str, Any], now: datetime) -> bool:
    last = _parse_iso_time(get_journal_state(config, "market_guard_last_notify_at"))
    if last is None:
        return True
    return (now - last).total_seconds() >= market_guard_notify_interval(config)


def _mark_market_guard_notified(config: dict[str, Any], now: datetime) -> None:
    set_journal_state(config, "market_guard_last_notify_at", now.isoformat())


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
            if (status.get("alerts") or []) and _market_guard_notify_due(config, now):
                send_telegram_message(config, format_market_guard_message(status))
                _mark_market_guard_notified(config, now)
        except Exception as exc:
            app.state.market_guard_status = {
                "enabled": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "alerts": [],
                "warnings": [f"Market guard error: {exc}"],
                "block": None,
            }
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
        order_target = order_targets.get((str(symbol), side), {"stop_loss": None, "take_profit": None})
        stop_loss = direct_target.get("stop_loss") or order_target.get("stop_loss")
        take_profit = direct_target.get("take_profit") or order_target.get("take_profit")
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
        "message": f"{len(positions)} open position(s), {len(open_orders)} open order(s)",
    }


def create_app(config_path: str = "config.example.yaml") -> FastAPI:
    app = FastAPI(title="Crypto Signal Bot UI")
    app.state.config_path = config_path
    app.state.lock = threading.Lock()
    app.state.automation_stop = threading.Event()
    app.state.automation_status = {
        "enabled": False,
        "last_result": "not_started",
    }
    app.state.market_guard_status = None
    app.state.price_cache = None

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.on_event("startup")
    def start_automation() -> None:
        config = load_config(app.state.config_path)
        sync_telegram_commands(config)
        initial_delay = max(0, int(config.get("automation", {}).get("initial_delay_seconds", 5) or 0))
        interval = _automation_interval(config)
        enabled = _automation_enabled(config)
        now = datetime.now(timezone.utc)
        app.state.automation_status = {
            "enabled": enabled,
            "interval_seconds": interval,
            "mode": config.get("mode", "dry_run"),
            "last_result": "waiting_initial_delay" if enabled else "disabled",
            "next_scan_at": (now + timedelta(seconds=initial_delay)).isoformat() if enabled else None,
        }
        send_telegram_message(
            config,
            "🟢 Bot crypto đã khởi động\n"
            f"⚙️ Chế độ: {config.get('mode', 'dry_run')}\n"
            f"🤖 Tự động: {'bật' if enabled else 'tắt'}\n"
            f"⏱️ Chu kỳ: {interval}s\n"
            f"🛡️ Guard: {'bật' if market_guard_enabled(config) else 'tắt'} / {market_guard_interval(config)}s, báo Telegram mỗi {market_guard_notify_interval(config) // 60} phút\n"
            "📲 Có thể bấm nút bên dưới để xem VT/PNL/SD, SD, LC bất cứ lúc nào.",
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

    @app.on_event("shutdown")
    def stop_automation() -> None:
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

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "mode": load_config(app.state.config_path).get("mode", "dry_run"),
            "automation": _automation_status_payload(app),
        }

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
        return lc_pipeline_dashboard_payload(config)

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
    def system_checklist_endpoint(date: str | None = None) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        if date:
            snapshot = dashboard_system_checklist_snapshot(config, date)
            if snapshot is None:
                raise HTTPException(status_code=404, detail=f"No system checklist snapshot for {date}")
            return snapshot
        return system_checklist_payload(config, automation=_automation_status_payload(app))

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
            forced_internal_scan = run_internal_market_scan_if_due(config)
            decision_result = run_once(config, execute=True)
            after_prompt = prompt_status(config)
            decision_payload = to_jsonable(decision_result)
            scan_comparison = decision_payload.get("scan_comparison") or {}
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
            "threshold_win_probability_pct": 80,
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
            "🧪 Fake AI sandbox hoàn tất\n"
            f"✅ Mini gọi: {internal_config.get('model', 'gpt-5.4-mini')}\n"
            f"Mini duyệt: {', '.join(approved_symbols) if approved_symbols else 'không duyệt rõ, dùng BTC fake để test OKX'}\n"
            f"✅ OKX gọi: {okx_config.get('model', 'gpt-5.5')}\n"
            f"OKX quyết định: {okx_decision.get('decision') or okx_decision.get('approved')}\n"
            f"Lệnh mô phỏng: {selected.get('side')} {selected.get('symbol')}\n"
            "⚠️ Không gửi lệnh thật lên OKX."
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
        try:
            return close_trade_execution(config, trade_execution_id, status, pnl)
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
