from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from dotenv import load_dotenv

from .storage import get_journal_state, set_journal_state


def _env_bool(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def telegram_enabled(config: dict[str, Any]) -> bool:
    load_dotenv()
    telegram_config = config.get("notifications", {}).get("telegram", {})
    if not telegram_config.get("enabled", True):
        return False
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def telegram_notify_scans(config: dict[str, Any]) -> bool:
    telegram_config = config.get("notifications", {}).get("telegram", {})
    default = bool(telegram_config.get("notify_scans", True))
    return _env_bool("TELEGRAM_NOTIFY_SCANS", default)


def telegram_buttons_enabled(config: dict[str, Any]) -> bool:
    telegram_config = config.get("notifications", {}).get("telegram", {})
    return bool(telegram_config.get("buttons_enabled", True))


def telegram_replace_previous_enabled(config: dict[str, Any]) -> bool:
    telegram_config = config.get("notifications", {}).get("telegram", {})
    return bool(telegram_config.get("replace_previous_message", True))


def telegram_control_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "\U0001f50e Scan ngay", "callback_data": "scan_now"},
            ],
            [
                {"text": "\U0001f4ca VT/PNL/SD", "callback_data": "view_positions_account"},
                {"text": "\U0001f4b5 SD", "callback_data": "view_sd"},
                {"text": "\U0001f7e1 LC", "callback_data": "view_lc"},
            ],
            [
                {"text": "\U0001f4cb Chưa Duyệt", "callback_data": "view_undecided_lc"},
                {"text": "\U0001f514 Thông báo nội bộ", "callback_data": "view_internal_notifications"},
            ],
            [
                {"text": "\U0001f6e1 Guard", "callback_data": "view_guard"},
                {"text": "\U0001f9e0 Memory", "callback_data": "view_memory"},
                {"text": "\U0001f916 AI", "callback_data": "view_ai"},
            ],
            [
                {"text": "\u2699\ufe0f Setup", "callback_data": "setup_menu"},
            ],
        ]
    }


def telegram_setup_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "\U0001f4b0 Set USDT", "callback_data": "set_order_usdt"},
                {"text": "\u2699\ufe0f Đòn bẩy", "callback_data": "set_leverage"},
            ],
            [
                {"text": "\U0001f4c8 Max VT", "callback_data": "set_max_positions"},
                {"text": "\u2b05\ufe0f Dashboard", "callback_data": "view_menu"},
            ],
        ]
    }


def _format_usdt_button(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def telegram_order_usdt_keyboard(config: dict[str, Any]) -> dict[str, Any]:
    sizing = config.get("position_sizing", {})
    raw_presets = sizing.get("base_margin_presets_usdt") or [2, 3, 5, 10, 15, 20]
    try:
        current = float(sizing.get("base_margin_usdt", 2) or 2)
    except (TypeError, ValueError):
        current = 2.0

    presets: list[float] = []
    for item in raw_presets:
        try:
            value = float(item)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in presets:
            presets.append(value)
    if current not in presets:
        presets.insert(0, current)

    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for value in presets[:9]:
        selected = abs(value - current) < 1e-9
        label = f"{'✅ ' if selected else ''}{_format_usdt_button(value)} USDT"
        row.append({"text": label, "callback_data": f"set_order_usdt:{value:g}"})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "⬅️ Dashboard", "callback_data": "view_menu"}])
    return {"inline_keyboard": rows}


def telegram_leverage_keyboard(config: dict[str, Any]) -> dict[str, Any]:
    exchange = config.get("exchange", {})
    raw_presets = exchange.get("leverage_presets") or [5, 10, 15, 20, 25]
    try:
        current = int(float(exchange.get("leverage", 10) or 10))
    except (TypeError, ValueError):
        current = 10

    presets: list[int] = []
    for item in raw_presets:
        try:
            value = int(float(item))
        except (TypeError, ValueError):
            continue
        if 5 <= value <= 25 and value not in presets:
            presets.append(value)
    if current not in presets and 5 <= current <= 25:
        presets.insert(0, current)

    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for value in presets[:9]:
        label = f"{'✅ ' if value == current else ''}{value}x"
        row.append({"text": label, "callback_data": f"set_leverage:{value}"})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "⬅️ Dashboard", "callback_data": "view_menu"}])
    return {"inline_keyboard": rows}


def telegram_max_positions_keyboard(config: dict[str, Any]) -> dict[str, Any]:
    try:
        current = int(float(config.get("risk", {}).get("max_active_trades", 1) or 1))
    except (TypeError, ValueError):
        current = 1
    presets = [1, 2, 3, 4, 5, 7, 10]
    if current not in presets and 1 <= current <= 10:
        presets.insert(0, current)
    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for value in presets:
        prefix = "\u2705 " if value == current else ""
        label = f"{prefix}{value} VT"
        row.append({"text": label, "callback_data": f"set_max_positions:{value}"})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "\u2b05\ufe0f Dashboard", "callback_data": "view_menu"}])
    return {"inline_keyboard": rows}


def _telegram_api_request(config: dict[str, Any], method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    if not telegram_enabled(config):
        return None

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    timeout = float(config.get("notifications", {}).get("telegram", {}).get("timeout_seconds", 8))
    retries = int(config.get("notifications", {}).get("telegram", {}).get("retry_count", 2) or 0)
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        method="POST",
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {"ok": 200 <= response.status < 300}
        except urllib.error.HTTPError as exc:
            retry_after = 0.0
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
                retry_after = float(error_payload.get("parameters", {}).get("retry_after") or 0)
            except Exception:
                retry_after = 0.0
            if attempt >= retries:
                return None
            time.sleep(min(max(retry_after, 1.0), 5.0))
        except Exception:
            if attempt >= retries:
                return None
            time.sleep(1.0)
    return None


def _message_state_key(chat_id: str | int, thread_id: str | int | None = None) -> str:
    suffix = f":{thread_id}" if thread_id else ""
    return f"telegram_last_message_id:{chat_id}{suffix}"


def delete_telegram_message(config: dict[str, Any], chat_id: str | int, message_id: str | int) -> bool:
    response = _telegram_api_request(
        config,
        "deleteMessage",
        {"chat_id": chat_id, "message_id": message_id},
    )
    return bool(response and response.get("ok"))


def edit_telegram_chat_message(
    config: dict[str, Any],
    chat_id: str | int,
    message_id: str | int,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
) -> bool:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:3900],
        "disable_web_page_preview": "true",
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    response = _telegram_api_request(config, "editMessageText", payload)
    return bool(response and response.get("ok"))


def _remember_sent_message(
    config: dict[str, Any],
    chat_id: str | int,
    thread_id: str | int | None,
    response: dict[str, Any] | None,
    *,
    replace_previous: bool,
) -> bool:
    if not response or not response.get("ok"):
        return False
    result = response.get("result") if isinstance(response.get("result"), dict) else {}
    message_id = result.get("message_id")
    if message_id is None:
        return True

    key = _message_state_key(chat_id, thread_id)
    old_message_id = get_journal_state(config, key)
    if replace_previous and old_message_id and str(old_message_id) != str(message_id):
        delete_telegram_message(config, chat_id, old_message_id)
    set_journal_state(config, key, str(message_id))
    return True


def send_telegram_message(
    config: dict[str, Any],
    text: str,
    *,
    with_buttons: bool | None = None,
    replace_previous: bool | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> bool:
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    thread_id = os.getenv("TELEGRAM_MESSAGE_THREAD_ID", "").strip()
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:3900],
        "disable_web_page_preview": "true",
    }
    if thread_id:
        payload["message_thread_id"] = thread_id
    if with_buttons is None:
        with_buttons = telegram_buttons_enabled(config)
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    elif with_buttons:
        payload["reply_markup"] = json.dumps(telegram_control_keyboard(), ensure_ascii=False)
    response = _telegram_api_request(config, "sendMessage", payload)
    if replace_previous is None:
        replace_previous = telegram_replace_previous_enabled(config)
    return _remember_sent_message(config, chat_id, thread_id or None, response, replace_previous=replace_previous)


def send_telegram_chat_message(
    config: dict[str, Any],
    chat_id: str | int,
    text: str,
    *,
    with_buttons: bool = True,
    message_thread_id: str | int | None = None,
    replace_previous: bool | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> bool:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:3900],
        "disable_web_page_preview": "true",
    }
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    elif with_buttons:
        payload["reply_markup"] = json.dumps(telegram_control_keyboard(), ensure_ascii=False)
    response = _telegram_api_request(config, "sendMessage", payload)
    if replace_previous is None:
        replace_previous = telegram_replace_previous_enabled(config)
    return _remember_sent_message(config, chat_id, message_thread_id, response, replace_previous=replace_previous)


def answer_callback_query(config: dict[str, Any], callback_query_id: str, text: str = "Đã nhận") -> bool:
    response = _telegram_api_request(
        config,
        "answerCallbackQuery",
        {"callback_query_id": callback_query_id, "text": text[:180]},
    )
    return bool(response and response.get("ok"))


def fetch_telegram_updates(config: dict[str, Any], *, offset: int | None = None) -> list[dict[str, Any]]:
    if not telegram_enabled(config):
        return []
    poll_timeout = int(config.get("notifications", {}).get("telegram", {}).get("poll_timeout_seconds", 5) or 5)
    payload: dict[str, Any] = {
        "timeout": poll_timeout,
        "allowed_updates": json.dumps(["message", "callback_query"]),
    }
    if offset is not None:
        payload["offset"] = offset
    response = _telegram_api_request(config, "getUpdates", payload)
    if not response or not response.get("ok"):
        return []
    result = response.get("result")
    return result if isinstance(result, list) else []


def format_automation_message(status: dict[str, Any]) -> str:
    result = status.get("last_result", "unknown")
    mode = status.get("mode", "-")
    lines = [f"🔎 Bot crypto scan: {result}", f"⚙️ Chế độ: {mode}"]
    if status.get("selected_symbol"):
        lines.append(f"🎯 Đã chọn: {status.get('selected_symbol')}")
    elif status.get("top_symbol"):
        lines.append(f"🏆 Top: {status.get('top_symbol')} tin cậy={status.get('top_confidence')}")
    if status.get("action"):
        lines.append(f"🎬 Hành động: {status.get('action')}")
    if status.get("risk_passed") is not None:
        lines.append(f"🛡️ Qua kiểm tra rủi ro: {status.get('risk_passed')}")
    reasons = status.get("risk_reasons") or []
    if reasons:
        lines.append("⚠️ Rủi ro: " + " | ".join(str(item) for item in reasons[:3]))
    if status.get("execution_submitted"):
        lines.append(f"🟢 Đã gửi lệnh: {status.get('order_id') or '-'}")
    if status.get("error"):
        lines.append(f"🚨 Lỗi: {status.get('error')}")
    if status.get("next_scan_at"):
        lines.append(f"⏭️ Scan tiếp: {status.get('next_scan_at')}")
    return "\n".join(lines)
