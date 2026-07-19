from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from .ledger import read_events
from .lc_pipeline import lc_pipeline_dashboard_payload
from .market import create_exchange
from .storage import (
    count_pending_orders,
    get_journal_state,
    list_pending_orders,
    list_trade_memory,
    next_daily_counter,
    recent_market_scan_memory,
    save_trade_memory,
    set_journal_state,
)


LOCAL_TZ = timezone(timedelta(hours=7))
_ACCOUNT_CACHE_LOCK = threading.Lock()
_ACCOUNT_CACHE: dict[str, tuple[datetime, dict[str, Any]]] = {}


def _cache_ttl(config: dict[str, Any]) -> int:
    return max(0, int(config.get("notifications", {}).get("telegram", {}).get("button_cache_ttl_seconds", 15) or 0))


def _cache_key(config: dict[str, Any], name: str) -> str:
    return f"{name}:{config.get('mode', 'dry_run')}:{config.get('runtime', {}).get('instance_role', 'primary')}"


def _cache_get(config: dict[str, Any], name: str) -> dict[str, Any] | None:
    ttl = _cache_ttl(config)
    if ttl <= 0:
        return None
    key = _cache_key(config, name)
    now = datetime.now(timezone.utc)
    with _ACCOUNT_CACHE_LOCK:
        cached = _ACCOUNT_CACHE.get(key)
    if not cached:
        return None
    created_at, payload = cached
    if (now - created_at).total_seconds() > ttl:
        return None
    result = dict(payload)
    result["cached_for_buttons"] = True
    result["cache_age_seconds"] = round((now - created_at).total_seconds(), 1)
    return result


def _cache_set(config: dict[str, Any], name: str, payload: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(payload)
    enriched["cached_for_buttons"] = False
    with _ACCOUNT_CACHE_LOCK:
        _ACCOUNT_CACHE[_cache_key(config, name)] = (datetime.now(timezone.utc), dict(enriched))
    return enriched


def local_time(value: datetime | None = None) -> datetime:
    return (value or datetime.now(timezone.utc)).astimezone(LOCAL_TZ)


def date_key(value: datetime | None = None) -> str:
    return local_time(value).strftime("%Y-%m-%d")


def date_label(value: datetime | None = None) -> str:
    return local_time(value).strftime("%d/%m/%Y")


def date_time_label(value: datetime | None = None) -> str:
    return local_time(value).strftime("%d/%m/%Y %H:%M")


def _date_time_seconds_label(value: datetime | None = None) -> str:
    return local_time(value).strftime("%d/%m/%Y %H:%M:%S")


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if numeric > 10_000_000_000:
            numeric = numeric / 1000
        return datetime.fromtimestamp(numeric, tz=timezone.utc)


def _float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_number(value: Any, digits: int = 2, suffix: str = "") -> str:
    number = _float(value)
    if number is None:
        return "-"
    return f"{number:,.{digits}f}{suffix}"


def _fmt_signed(value: Any, digits: int = 2, suffix: str = "") -> str:
    number = _float(value)
    if number is None:
        return "-"
    sign = "+" if number > 0 else ""
    return f"{sign}{number:,.{digits}f}{suffix}"


def _pnl_icon(value: Any) -> str:
    number = _float(value)
    if number is None or number == 0:
        return "⚪"
    return "🟢" if number > 0 else "🔴"


def _side_icon(value: Any) -> str:
    side = str(value or "").lower()
    if side == "long":
        return "🟢"
    if side == "short":
        return "🔴"
    return "⚪"


def _coin_base(symbol: Any) -> str:
    text = str(symbol or "")
    if "/" in text:
        return text.split("/", 1)[0].upper()
    if "-" in text:
        return text.split("-", 1)[0].upper()
    return text.upper()


def _coin_icon(symbol: Any) -> str:
    icons = {
        "BTC": "🟠₿",
        "ETH": "🔷Ξ",
        "SOL": "🟣◎",
        "BNB": "🟡BNB",
        "XRP": "⚫XRP",
        "DOGE": "🟡Ð",
        "ADA": "🔵ADA",
        "LINK": "🔗LINK",
        "AVAX": "🔺AVAX",
        "LTC": "⚪Ł",
    }
    return icons.get(_coin_base(symbol), "🪙")


def _result_icon(value: Any) -> str:
    result = str(value or "").lower()
    if result == "order_submitted":
        return "🟢"
    if result == "pending_created":
        return "🟡"
    if result == "error":
        return "🚨"
    if result in {"no_order", "hold"}:
        return "🔵"
    if result in {"skipped_busy", "disabled"}:
        return "🟡"
    return "⚪"


def _reason_vi(reason: Any) -> str:
    text = str(reason or "").strip()
    if not text:
        return ""
    replacements = [
        (
            r"^No recent symbol-specific news confirmed the setup$",
            "Chưa có tin tức gần đây xác nhận setup cho cặp này",
        ),
        (r"^No candidate passed risk checks$", "Chưa có ứng viên nào qua kiểm tra rủi ro"),
        (r"^No candidate was produced$", "Chưa tạo được ứng viên giao dịch"),
        (r"^Spread unavailable$", "Chưa có dữ liệu spread"),
        (
            r"^Active OKX position/order already exists for (.+)$",
            r"Đã có vị thế/lệnh OKX đang mở cho \1",
        ),
        (
            r"^Active trade limit reached: (.+)$",
            r"Đã đủ giới hạn vị thế/lệnh đang mở: \1",
        ),
        (
            r"^Cannot verify active OKX positions/orders$",
            "Không xác minh được vị thế/lệnh OKX đang mở",
        ),
        (
            r"^Confidence ([0-9.]+) is below minimum ([0-9.]+)$",
            r"Độ tin cậy \1 thấp hơn ngưỡng tối thiểu \2",
        ),
        (
            r"^Win probability is unavailable; minimum is ([0-9.]+)%$",
            r"Chưa có ước tính tỉ lệ thắng; ngưỡng tối thiểu là \1%",
        ),
        (
            r"^Win probability ([0-9.]+)% is below minimum ([0-9.]+)%$",
            r"Tỉ lệ thắng \1% thấp hơn ngưỡng tối thiểu \2%",
        ),
        (
            r"^Risk/reward ([0-9.]+) is below minimum ([0-9.]+)$",
            r"Risk/reward \1 thấp hơn ngưỡng tối thiểu \2",
        ),
        (
            r"^Spread ([0-9.]+)% exceeds maximum ([0-9.]+)%$",
            r"Spread \1% vượt mức tối đa \2%",
        ),
        (
            r"^Stop distance ([0-9.]+)% is below minimum ([0-9.]+)%$",
            r"Khoảng cách SL \1% thấp hơn mức tối thiểu \2%",
        ),
        (
            r"^Stop distance ([0-9.]+)% exceeds maximum ([0-9.]+)%$",
            r"Khoảng cách SL \1% vượt mức tối đa \2%",
        ),
        (
            r"^News sentiment conflicts with LONG setup \((.+)\)$",
            r"Tâm lý tin tức xung đột với setup LONG (\1)",
        ),
        (
            r"^News sentiment conflicts with SHORT setup \((.+)\)$",
            r"Tâm lý tin tức xung đột với setup SHORT (\1)",
        ),
        (
            r"^Cooldown active for another ([0-9]+) minute\(s\)$",
            r"Đang cooldown thêm \1 phút",
        ),
        (
            r"^Daily order limit reached: (.+)$",
            r"Đã chạm giới hạn lệnh trong ngày: \1",
        ),
        (
            r"^Daily planned risk would be ([0-9.]+) USDT, above ([0-9.]+)$",
            r"Rủi ro dự kiến trong ngày sẽ là \1 USDT, vượt \2",
        ),
        (
            r"^Exchange order is no longer open$",
            "Lệnh trên OKX không còn mở",
        ),
        (
            r"^Pending setup no longer passes scan$",
            "Setup lệnh chờ không còn đạt điều kiện sau lần scan mới",
        ),
        (
            r"^Gi(?:á|\?) entry m(?:ới|\?i) l(?:ệ|\?)ch ([0-9.]+)% so v(?:ới|\?i) LC c(?:ũ|\?) \((?:ngưỡng|ng\?ng) ([0-9.]+)%\)$",
            r"Giá entry mới lệch \1% so với LC cũ (ngưỡng \2%)",
        ),
        (
            r"^PNL/SD khong co trong dry_run$",
            "PNL/SD không có trong dry_run",
        ),
        (
            r"^dry_run has no OKX trade history$",
            "dry_run không có lịch sử giao dịch OKX",
        ),
        (
            r"^Trade memory sync failed: (.+)$",
            r"Đồng bộ bộ nhớ giao dịch thất bại: \1",
        ),
    ]
    parts = [part.strip() for part in text.split(";") if part.strip()]
    if not parts:
        parts = [text]
    translated_parts: list[str] = []
    for part in parts:
        translated = part
        for pattern, replacement in replacements:
            if re.search(pattern, translated):
                translated = re.sub(pattern, replacement, translated)
                break
        translated_parts.append(translated)
    return "; ".join(translated_parts)


def _source_label(value: Any) -> str:
    source = str(value or "new_scan")
    if source == "old_rescan":
        return "🔁 cũ"
    if source == "new_and_old_rescan":
        return "🔄 mới+cũ"
    return "🆕 mới"


def _candidate_line(index: int, candidate: dict[str, Any]) -> str:
    symbol = candidate.get("symbol") or "-"
    side = str(candidate.get("side") or "-").upper()
    source = _source_label(candidate.get("scan_source"))
    win = _fmt_number(candidate.get("win_probability_pct"), 2, "%")
    confidence = _fmt_number(candidate.get("confidence"), 2)
    rr = _fmt_number(candidate.get("risk_reward"), 2)
    delta = _float(candidate.get("win_delta_pct"))
    delta_text = "" if delta is None else f" | {_pnl_icon(delta)} Δ {_fmt_signed(delta, 2, '%')}"
    return f"{index}. {_coin_icon(symbol)} {_side_icon(candidate.get('side'))} {symbol} {side} [{source}] | 🎯 Tỉ lệ {win} | 📊 Tin cậy {confidence} | RR {rr}{delta_text}"


def format_scan_message(config: dict[str, Any], payload: dict[str, Any] | None, status: dict[str, Any]) -> str:
    now = datetime.now(timezone.utc)
    scan_no = next_daily_counter(config, "SC", date_key(now))
    result = status.get("last_result", "unknown")
    lines = [
        f"🔎🔵 SC #{scan_no} {date_label(now)}",
        f"{_result_icon(result)} Kết quả: {result} | ⚙️ Chế độ: {status.get('mode', '-')}",
    ]
    if status.get("action"):
        lines.append(f"🎬 Hành động: {status.get('action')}")

    candidates = (payload or {}).get("candidates") or []
    lines.append("🏆 Top 5 cặp giao dịch tốt:")
    if candidates:
        lines.extend(_candidate_line(index, candidate) for index, candidate in enumerate(candidates[:5], 1))
    elif result == "error" and status.get("error"):
        lines.append("⚪ Chưa có danh sách ứng viên vì scan bị lỗi trước khi hoàn tất")
    else:
        lines.append("⚪ Không có ứng viên")

    guard_layers = ((payload or {}).get("scan_comparison") or {}).get("market_guard_layers") or {}
    top_risk = guard_layers.get("top_risk") or []
    if top_risk:
        summary = []
        for item in top_risk[:3]:
            summary.append(
                f"{_coin_icon(item.get('symbol'))} {item.get('symbol', '-')} risk {_fmt_number(item.get('risk_score'), 1)}"
            )
        lines.append("🛡️ Bộ nhớ Guard 5p/20p: " + " | ".join(summary))

    confirmation = config.get("strategy", {}).get("confirmation_timeframes", {})
    if isinstance(confirmation, dict) and confirmation.get("enabled", True):
        frames = ", ".join(str(item).upper() for item in confirmation.get("frames", ["1h", "4h"]))
        lines.append(f"🧭 Khung rộng: {frames} đang lọc xu hướng")

    sizing = ((payload or {}).get("scan_comparison") or {}).get("position_sizing") or {}
    if sizing.get("enabled"):
        if sizing.get("blocked"):
            lines.append(f"⛔ Recovery: tạm dừng - {sizing.get('block_reason') or '-'}")
        else:
            lines.append(
                "♻️ Recovery: "
                f"cyclePnL {_fmt_signed(sizing.get('cycle_pnl_usdt'), 4)} USDT | "
                f"step {sizing.get('recovery_step', 0)}/{sizing.get('max_recovery_step', 0)} | "
                f"size {_fmt_number(sizing.get('margin_usdt'), 4)} USDT"
            )

    reasons = status.get("risk_reasons") or []
    if reasons:
        lines.append("⚠️ Rủi ro: " + " | ".join(_reason_vi(item) for item in reasons[:3]))
    if status.get("error"):
        lines.append(f"🚨 Lỗi: {status.get('error')}")
    if status.get("next_scan_at"):
        next_scan = _parse_time(status.get("next_scan_at"))
        lines.append(f"⏭️ Scan tiếp: {date_time_label(next_scan)}")
    return "\n".join(lines)


def _selected_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    selected = (payload or {}).get("selected") or {}
    return selected if isinstance(selected, dict) else {}


def format_execution_messages(payload: dict[str, Any] | None) -> list[str]:
    execution = (payload or {}).get("execution") or {}
    if not execution.get("submitted"):
        return []
    selected = _selected_summary(payload)
    journal_type = execution.get("journal_type") or "VT"
    journal_id = execution.get("journal_id") or "-"
    raw = execution.get("raw") if isinstance(execution.get("raw"), dict) else {}
    local_pending = bool(raw.get("local_pending"))
    header_icon = "🟢💼" if journal_type == "VT" else "🟡⏳"
    prefix = f"{header_icon} {journal_type} #{journal_id} {date_label()}"
    if journal_type == "VT":
        verb = "Đã vào lệnh"
    elif local_pending:
        verb = "Đã lưu lệnh chờ nội bộ"
    else:
        verb = "Đã set lệnh chờ"
    order_id = execution.get("order_id") or ("nội bộ, chưa gửi OKX" if local_pending else "-")
    lines = [
        prefix,
        f"{_coin_icon(selected.get('symbol'))} {_side_icon(selected.get('side'))} {verb}: {selected.get('symbol', '-')} {str(selected.get('side', '-')).upper()}",
        f"🎯 Giá vào: {_fmt_number(selected.get('entry'), 6)} | 🛑 SL: {_fmt_number(selected.get('stop_loss'), 6)} | ✅ TP: {_fmt_number(selected.get('take_profit'), 6)}",
        f"📦 KL: {_fmt_number(selected.get('quantity'), 6)} | 🆔 ID lệnh: {order_id}",
    ]
    return ["\n".join(lines)]


def _trade_execution_close_label(row: dict[str, Any]) -> str:
    close_reason = str(row.get("close_reason") or "").strip().lower()
    if close_reason in {"tp", "take_profit", "cham_tp", "hit_tp"}:
        return "Chạm TP"
    if close_reason in {"sl", "stop_loss", "cham_sl", "hit_sl"}:
        return "Chạm SL"
    if close_reason in {"manual", "tu_dong", "tudong", "self_closed", "user_closed"}:
        return "Tự đóng"
    status = str(row.get("status") or "").upper()
    if status == "CLOSED":
        return "Tự đóng"
    if status == "WIN":
        return "Đóng lãi"
    if status == "LOSS":
        return "Đóng lỗ"
    if status == "BREAKEVEN":
        return "Hòa vốn"
    return "Đã đóng"


def _trade_execution_payload(row: dict[str, Any]) -> dict[str, Any]:
    if isinstance(row.get("payload"), dict):
        return row.get("payload") or {}
    for key in ("payload_json", "snapshot_json"):
        raw = row.get(key)
        if not raw:
            continue
        try:
            parsed = json.loads(str(raw))
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _trade_execution_pnl_pct(row: dict[str, Any]) -> float | None:
    pnl_pct = _float(row.get("pnl_pct"))
    if pnl_pct is not None:
        return pnl_pct
    payload = _trade_execution_payload(row)
    basis = _float(payload.get("order_usdt"))
    if basis is None or basis <= 0:
        basis = _float(payload.get("margin_usdt"))
    pnl = _float(row.get("pnl"))
    if pnl is None or basis is None or basis <= 0:
        return None
    return pnl / basis * 100


def format_trade_execution_close_message(config: dict[str, Any], row: dict[str, Any]) -> str:
    closed_at = _parse_time(row.get("closed_at"))
    vt_id = row.get("id") or row.get("trade_execution_id") or "-"
    pnl = _float(row.get("pnl"))
    pnl_pct = _trade_execution_pnl_pct(row)
    lines = [
        f"{_pnl_icon(pnl)} VT #{vt_id} | {_trade_execution_close_label(row)}",
        f"{_coin_icon(row.get('symbol'))} {_side_icon(row.get('side'))} Cặp: {row.get('symbol', '-')} {str(row.get('side', '-')).upper()}",
        f"💰 Lợi nhuận: {_fmt_signed(pnl, 2)} USDT ({_fmt_signed(pnl_pct, 2, '%')})",
        f"🕒 Đóng lúc: {_date_time_seconds_label(closed_at) if closed_at else '-'}",
    ]
    return "\n".join(lines)


def format_pending_event_messages(payload: dict[str, Any] | None) -> list[str]:
    pending = ((payload or {}).get("scan_comparison") or {}).get("pending_orders") or {}
    messages: list[str] = []
    for event in pending.get("events") or []:
        event_type = event.get("type")
        if event_type == "pending_ai_deferred":
            messages.append(
                "\n".join(
                    [
                        f"OKX AI giữ LC #{event.get('lc_id', '-')} {date_label()}",
                        f"{_coin_icon(event.get('symbol'))} {_side_icon(event.get('side'))} Cặp: {event.get('symbol', '-')} {str(event.get('side', '-')).upper()}",
                        f"Lý do: {event.get('reason') or '-'}",
                    ]
                )
            )
            continue
        if event_type == "pending_converted":
            source = str(event.get("source") or "")
            from_status = str(event.get("from_status") or "")
            source_label = "LC_OKX" if source.startswith("lc_okx") or from_status == "LC_OKX" else "LC nội bộ"
            action_label = (
                "đã được chuyển thành VT"
                if source_label == "LC_OKX"
                else "đã được chuyển thẳng thành VT"
            )
            messages.append(
                "\n".join(
                    [
                        f"{source_label} #{event.get('lc_id', '-')} {date_label()} {action_label} #{event.get('vt_id', '-')}",
                        f"{_coin_icon(event.get('symbol'))} {_side_icon(event.get('side'))} Cặp: {event.get('symbol', '-')} {str(event.get('side', '-')).upper()}",
                        f"OKX order ID: {event.get('exchange_order_id') or '-'}",
                    ]
                )
            )
            continue
        if event_type == "pending_submitted":
            messages.append(
                "\n".join(
                    [
                        f"LC_OKX #{event.get('lc_id', '-')} {date_label()} đã được gửi lên OKX",
                        f"{_coin_icon(event.get('symbol'))} {_side_icon(event.get('side'))} Cặp: {event.get('symbol', '-')} {str(event.get('side', '-')).upper()}",
                        f"OKX order ID: {event.get('exchange_order_id') or '-'}",
                        f"Tuổi LC nội bộ: {float(event.get('local_age_hours') or 0):.1f} giờ | Hạn OKX: {float(event.get('expires_in_days') or 1.5):.1f} ngày",
                    ]
                )
            )
            continue
        if event_type == "pending_converted":
            messages.append(
                "\n".join(
                    [
                        f"🟢✅ LC #{event.get('lc_id', '-')} {date_label()} đã được chuyển thành VT #{event.get('vt_id', '-')}",
                        f"{_coin_icon(event.get('symbol'))} {_side_icon(event.get('side'))} Cặp: {event.get('symbol', '-')} {str(event.get('side', '-')).upper()}",
                        f"🆔 ID lệnh: {event.get('exchange_order_id') or '-'}",
                    ]
                )
            )
        elif event_type == "pending_submitted":
            messages.append(
                "\n".join(
                    [
                        f"🟡📤 LC #{event.get('lc_id', '-')} {date_label()} đã được gửi lên OKX",
                        f"{_coin_icon(event.get('symbol'))} {_side_icon(event.get('side'))} Cặp: {event.get('symbol', '-')} {str(event.get('side', '-')).upper()}",
                        f"🆔 ID lệnh: {event.get('exchange_order_id') or '-'}",
                        f"⏳ Thời gian sống: {float(event.get('expires_in_days') or 1.5):.1f} ngày",
                    ]
                )
            )
        elif event_type == "pending_canceled":
            messages.append(
                "\n".join(
                    [
                        f"🔴❌ LC #{event.get('lc_id', '-')} {date_label()} đã được hủy",
                        f"{_coin_icon(event.get('symbol'))} {_side_icon(event.get('side'))} Cặp: {event.get('symbol', '-')} {str(event.get('side', '-')).upper()}",
                        f"⚠️ Lý do: {_reason_vi(event.get('reason') or '-')}",
                    ]
                )
            )
    return messages


def _position_side(position: dict[str, Any]) -> str:
    info = position.get("info", {}) if isinstance(position.get("info"), dict) else {}
    side = position.get("side") or info.get("posSide")
    if side and side != "net":
        return str(side)
    contracts = _float(position.get("contracts") or info.get("pos")) or 0
    if contracts > 0:
        return "long"
    if contracts < 0:
        return "short"
    return "-"


def _position_pnl_pct(position: dict[str, Any], pnl_usdt: float | None) -> float | None:
    info = position.get("info", {}) if isinstance(position.get("info"), dict) else {}
    percentage = _float(position.get("percentage"))
    if percentage is not None:
        return percentage
    for value in (info.get("uplRatio"), info.get("pnlRatio")):
        number = _float(value)
        if number is None:
            continue
        if abs(number) <= 5:
            return number * 100
        return number
    margin = _float(position.get("initialMargin") or info.get("imr") or info.get("margin"))
    if margin and pnl_usdt is not None:
        return pnl_usdt / margin * 100
    return None


def _balance_usdt(balance: dict[str, Any]) -> float | None:
    usdt = balance.get("USDT") if isinstance(balance.get("USDT"), dict) else {}
    for key in ("total", "free"):
        value = _float(usdt.get(key))
        if value is not None:
            return value
    total = balance.get("total") if isinstance(balance.get("total"), dict) else {}
    value = _float(total.get("USDT"))
    if value is not None:
        return value
    info = balance.get("info", {}) if isinstance(balance.get("info"), dict) else {}
    for account in info.get("data") or []:
        for key in ("totalEq", "adjEq", "eq"):
            value = _float(account.get(key))
            if value is not None:
                return value
        for detail in account.get("details") or []:
            if str(detail.get("ccy") or "").upper() == "USDT":
                for key in ("eq", "cashBal", "availBal"):
                    value = _float(detail.get(key))
                    if value is not None:
                        return value
    return None


def _latest_targets_by_position(config: dict[str, Any]) -> dict[tuple[str, str], dict[str, float | None]]:
    targets: dict[tuple[str, str], dict[str, float | None]] = {}
    for event in read_events(config):
        if not event.get("submitted"):
            continue
        symbol = str(event.get("symbol") or "")
        side = str(event.get("side") or "").lower()
        if not symbol or not side:
            continue
        stop_loss = _float(event.get("stop_loss"))
        take_profit = _float(event.get("take_profit"))
        if stop_loss is None and take_profit is None:
            continue
        targets[(symbol, side)] = {"stop_loss": stop_loss, "take_profit": take_profit}
    return targets


def _position_targets(
    targets: dict[tuple[str, str], dict[str, float | None]],
    symbol: str,
    side: str,
) -> dict[str, float | None]:
    return targets.get((symbol, side.lower()), {"stop_loss": None, "take_profit": None})


def _target_from_payload(payload: dict[str, Any]) -> dict[str, float | None]:
    info = payload.get("info", {}) if isinstance(payload.get("info"), dict) else {}
    source = {**info, **payload}
    stop_loss = None
    take_profit = None
    for key in ("slTriggerPx", "slOrdPx", "stopLossPrice", "stopLoss", "stop_loss"):
        stop_loss = _float(source.get(key))
        if stop_loss is not None:
            break
    for key in ("tpTriggerPx", "tpOrdPx", "takeProfitPrice", "takeProfit", "take_profit"):
        take_profit = _float(source.get(key))
        if take_profit is not None:
            break
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
                stop_loss = _float(item.get("slTriggerPx") or item.get("slOrdPx"))
            if take_profit is None:
                take_profit = _float(item.get("tpTriggerPx") or item.get("tpOrdPx"))
    return {"stop_loss": stop_loss, "take_profit": take_profit}


def _targets_from_open_orders(exchange: Any) -> dict[tuple[str, str], dict[str, float | None]]:
    targets: dict[tuple[str, str], dict[str, float | None]] = {}
    try:
        orders = exchange.fetch_open_orders()
    except Exception:
        return targets
    for order in orders:
        if not isinstance(order, dict):
            continue
        info = order.get("info", {}) if isinstance(order.get("info"), dict) else {}
        symbol = str(order.get("symbol") or info.get("instId") or "")
        raw_side = str(order.get("side") or info.get("side") or "").lower()
        if not symbol:
            continue
        close_side = "long" if raw_side == "sell" else "short" if raw_side == "buy" else raw_side
        target = _target_from_payload(order)
        existing = targets.setdefault((symbol, close_side), {"stop_loss": None, "take_profit": None})
        existing["stop_loss"] = existing.get("stop_loss") or target.get("stop_loss")
        existing["take_profit"] = existing.get("take_profit") or target.get("take_profit")
    return targets


def _symbol_from_inst_id(exchange: Any, inst_id: str) -> str:
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


def _side_from_algo(row: dict[str, Any]) -> str:
    pos_side = str(row.get("posSide") or "").strip().lower()
    if pos_side in {"long", "short"}:
        return pos_side
    raw_side = str(row.get("side") or "").strip().lower()
    return "long" if raw_side == "sell" else "short" if raw_side == "buy" else raw_side


def _pending_algo_orders(exchange: Any) -> list[dict[str, Any]]:
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
        except Exception:
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


def _targets_from_algo_orders(exchange: Any) -> dict[tuple[str, str], dict[str, float | None]]:
    targets: dict[tuple[str, str], dict[str, float | None]] = {}
    for row in _pending_algo_orders(exchange):
        symbol = _symbol_from_inst_id(exchange, str(row.get("instId") or ""))
        side = _side_from_algo(row)
        if not symbol or side not in {"long", "short"}:
            continue
        target = _target_from_payload(row)
        if target.get("stop_loss") is None and target.get("take_profit") is None:
            continue
        existing = targets.setdefault((symbol, side), {"stop_loss": None, "take_profit": None})
        existing["stop_loss"] = existing.get("stop_loss") or target.get("stop_loss")
        existing["take_profit"] = existing.get("take_profit") or target.get("take_profit")
    return targets


def _position_rows(config: dict[str, Any], exchange: Any) -> list[dict[str, Any]]:
    targets = _latest_targets_by_position(config)
    order_targets = _targets_from_open_orders(exchange)
    algo_targets = _targets_from_algo_orders(exchange)
    positions: list[dict[str, Any]] = []
    for item in exchange.fetch_positions():
        info = item.get("info", {}) if isinstance(item.get("info"), dict) else {}
        contracts = _float(item.get("contracts") or info.get("pos")) or 0
        if abs(contracts) <= 0:
            continue
        symbol = str(item.get("symbol") or info.get("instId") or "-")
        side = _position_side(item)
        pnl_usdt = _float(item.get("unrealizedPnl") or info.get("upl"))
        target = _position_targets(targets, symbol, side)
        order_target = _position_targets(order_targets, symbol, side)
        algo_target = _position_targets(algo_targets, symbol, side)
        direct_target = _target_from_payload(item)
        stop_loss = direct_target.get("stop_loss") or algo_target.get("stop_loss") or order_target.get("stop_loss") or target.get("stop_loss")
        take_profit = direct_target.get("take_profit") or algo_target.get("take_profit") or order_target.get("take_profit") or target.get("take_profit")
        positions.append(
            {
                "symbol": symbol,
                "side": side,
                "contracts": abs(contracts),
                "entry_price": _float(item.get("entryPrice") or info.get("avgPx")),
                "mark_price": _float(item.get("markPrice") or info.get("markPx")),
                "pnl_usdt": pnl_usdt,
                "pnl_pct": _position_pnl_pct(item, pnl_usdt),
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "tp_sl_status": "ok" if stop_loss is not None and take_profit is not None else "missing",
            }
        )
    return positions


def fetch_positions_snapshot(config: dict[str, Any], *, use_cache: bool = False) -> dict[str, Any]:
    if config.get("mode") == "dry_run":
        return {
            "ok": False,
            "error": "PNL/SD không có trong dry_run",
            "positions": [],
            "pending_count": count_pending_orders(config),
        }
    if use_cache:
        cached = _cache_get(config, "positions")
        if cached is not None:
            return cached

    exchange = create_exchange(config, authenticated=True)
    exchange.load_markets()
    positions = _position_rows(config, exchange)
    return _cache_set(config, "positions", {
        "ok": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "positions": positions,
        "pending_count": count_pending_orders(config),
    })


def fetch_balance_snapshot(config: dict[str, Any], *, use_cache: bool = False) -> dict[str, Any]:
    if config.get("mode") == "dry_run":
        return {
            "ok": False,
            "error": "PNL/SD không có trong dry_run",
            "balance_usdt": None,
            "pending_count": count_pending_orders(config),
        }
    if use_cache:
        cached = _cache_get(config, "balance")
        if cached is not None:
            return cached

    exchange = create_exchange(config, authenticated=True)
    balance = exchange.fetch_balance()
    return _cache_set(config, "balance", {
        "ok": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "balance_usdt": _balance_usdt(balance),
        "pending_count": count_pending_orders(config),
    })


def fetch_account_snapshot(config: dict[str, Any], *, use_cache: bool = False) -> dict[str, Any]:
    if config.get("mode") == "dry_run":
        return {
            "ok": False,
            "error": "PNL/SD không có trong dry_run",
            "positions": [],
            "balance_usdt": None,
            "pending_count": count_pending_orders(config),
        }
    if use_cache:
        cached = _cache_get(config, "account")
        if cached is not None:
            return cached
        positions = fetch_positions_snapshot(config, use_cache=True)
        balance = fetch_balance_snapshot(config, use_cache=True)
        if positions.get("ok") and balance.get("ok"):
            return _cache_set(config, "account", {
                "ok": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "balance_usdt": balance.get("balance_usdt"),
                "positions": positions.get("positions") or [],
                "pending_count": count_pending_orders(config),
            })
        return {
            "ok": False,
            "error": positions.get("error") or balance.get("error") or "Không lấy được dữ liệu tài khoản",
            "balance_usdt": balance.get("balance_usdt"),
            "positions": positions.get("positions") or [],
            "pending_count": count_pending_orders(config),
        }

    exchange = create_exchange(config, authenticated=True)
    exchange.load_markets()
    balance = exchange.fetch_balance()
    positions = _position_rows(config, exchange)
    return _cache_set(config, "account", {
        "ok": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "balance_usdt": _balance_usdt(balance),
        "positions": positions,
        "pending_count": count_pending_orders(config),
    })


def _history_key(row: dict[str, Any]) -> str:
    info = row.get("info", {}) if isinstance(row.get("info"), dict) else {}
    symbol = row.get("symbol") or info.get("instId") or ""
    ident = row.get("id") or info.get("posId") or info.get("uTime") or info.get("cTime") or info.get("closeTime")
    return f"{symbol}:{ident or 'unknown'}"


def _history_pnl(row: dict[str, Any]) -> float | None:
    info = row.get("info", {}) if isinstance(row.get("info"), dict) else {}
    for key in ("pnl", "realizedPnl", "realisedPnl"):
        value = _float(row.get(key))
        if value is not None:
            return value
    for key in ("pnl", "realizedPnl", "realisedPnl"):
        value = _float(info.get(key))
        if value is not None:
            return value
    return None


def _history_time(row: dict[str, Any]) -> datetime | None:
    info = row.get("info", {}) if isinstance(row.get("info"), dict) else {}
    return _parse_time(row.get("timestamp") or info.get("uTime") or info.get("cTime") or info.get("closeTime"))


def sync_trade_memory_from_exchange(config: dict[str, Any]) -> dict[str, Any]:
    if config.get("mode") == "dry_run":
        return {"synced": False, "new": [], "warnings": ["dry_run has no OKX trade history"]}
    limit = int(config.get("notifications", {}).get("telegram", {}).get("trade_memory_limit", 100) or 100)
    try:
        exchange = create_exchange(config, authenticated=True)
        exchange.load_markets()
        history = exchange.fetch_positions_history(None, None, limit)
    except Exception as exc:
        return {"synced": False, "new": [], "warnings": [f"Trade memory sync failed: {exc}"]}

    new_records: list[dict[str, Any]] = []
    for row in history:
        if not isinstance(row, dict):
            continue
        info = row.get("info", {}) if isinstance(row.get("info"), dict) else {}
        symbol = str(row.get("symbol") or info.get("instId") or "")
        pnl = _history_pnl(row)
        if not symbol or pnl is None:
            continue
        closed_at = _history_time(row)
        pnl_pct = _float(row.get("percentage") or info.get("pnlRatio"))
        if pnl_pct is not None and abs(pnl_pct) <= 5:
            pnl_pct *= 100
        record = {
            "key": _history_key(row),
            "symbol": symbol,
            "side": str(row.get("side") or info.get("posSide") or ""),
            "opened_at": None,
            "closed_at": closed_at.isoformat() if closed_at else None,
            "pnl_usdt": round(pnl, 6),
            "pnl_pct": None if pnl_pct is None else round(pnl_pct, 4),
            "source": "okx_positions_history",
            "payload": row,
        }
        if save_trade_memory(config, record, limit=limit):
            new_records.append(record)
    return {"synced": True, "new": new_records, "warnings": []}


def _ensure_daily_start_balance(config: dict[str, Any], snapshot: dict[str, Any], now: datetime) -> None:
    balance = _float(snapshot.get("balance_usdt"))
    if balance is None:
        return
    key = f"daily_start_balance:{date_key(now)}"
    if get_journal_state(config, key) is None:
        set_journal_state(config, key, str(round(balance, 6)))


def _closed_records_for_date(config: dict[str, Any], summary_date_key: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for record in list_trade_memory(config, limit=100):
        closed_at = _parse_time(record.get("closed_at"))
        if closed_at and date_key(closed_at) == summary_date_key:
            records.append(record)
    return records


def format_account_report(snapshot: dict[str, Any], memory_sync: dict[str, Any]) -> str:
    lines = [f"💰📊 PNL/SD {date_time_label()}"]
    if not snapshot.get("ok", True):
        lines.append(f"🚨 Lỗi: {_reason_vi(snapshot.get('error') or '-')}")
        return "\n".join(lines)

    lines.append(f"💵 SD: {_fmt_number(snapshot.get('balance_usdt'), 2)} USDT")
    positions = snapshot.get("positions") or []
    lines.append(f"📌 Vị thế hiện tại: {len(positions)}")
    if positions:
        for index, position in enumerate(positions, 1):
            lines.append(
                f"{index}. {_coin_icon(position.get('symbol'))} {_pnl_icon(position.get('pnl_usdt'))} {_side_icon(position.get('side'))} {position.get('symbol', '-')} {str(position.get('side', '-')).upper()}: "
                f"{_fmt_signed(position.get('pnl_usdt'), 4)} USDT ({_fmt_signed(position.get('pnl_pct'), 2, '%')})"
            )
    else:
        lines.append("⚪ Không có vị thế mở")
    lines.append(f"🟡 LC pending/OKX: {snapshot.get('pending_count', 0)}")
    new_records = memory_sync.get("new") or []
    if new_records:
        wins = sum(1 for item in new_records if float(item.get("pnl_usdt") or 0) > 0)
        losses = sum(1 for item in new_records if float(item.get("pnl_usdt") or 0) < 0)
        lines.append(f"🧠 Bộ nhớ mới: 🟢 {wins} thắng / 🔴 {losses} thua")
    for warning in memory_sync.get("warnings") or []:
        lines.append(f"⚠️ Cảnh báo: {_reason_vi(warning)}")
    return "\n".join(lines)


def _fmt_position_target(position: dict[str, Any], key: str) -> str:
    value = position.get(key)
    return _fmt_number(value, 6) if value is not None else "MISSING"


def format_positions_view(config: dict[str, Any]) -> str:
    try:
        snapshot = fetch_positions_snapshot(config, use_cache=True)
    except Exception as exc:
        return f"📌 VT {date_time_label()}\n🚨 Lỗi: Lấy vị thế thất bại: {exc}"
    if not snapshot.get("ok", True):
        return f"📌 VT {date_time_label()}\n🚨 Lỗi: {_reason_vi(snapshot.get('error') or '-')}"

    positions = snapshot.get("positions") or []
    lines = [f"📌 VT đang mở {date_time_label()}", f"📊 Tổng vị thế: {len(positions)}"]
    if not positions:
        lines.append("⚪ Không có vị thế mở")
        return "\n".join(lines)
    for index, position in enumerate(positions, 1):
        lines.append(
            f"{index}. {_coin_icon(position.get('symbol'))} {_pnl_icon(position.get('pnl_usdt'))} {_side_icon(position.get('side'))} "
            f"{position.get('symbol', '-')} {str(position.get('side', '-')).upper()}"
        )
        lines.append(
            f"   💰 PNL: {_fmt_signed(position.get('pnl_usdt'), 4)} USDT "
            f"({_fmt_signed(position.get('pnl_pct'), 2, '%')})"
        )
        lines.append(
            f"   🎯 Vào: {_fmt_number(position.get('entry_price'), 6)} | Mark: {_fmt_number(position.get('mark_price'), 6)}"
        )
        lines.append(
            f"   🛑 SL: {_fmt_position_target(position, 'stop_loss')} | ✅ TP: {_fmt_position_target(position, 'take_profit')}"
        )
        if position.get("tp_sl_status") == "missing":
            lines.append("   ⚠️ TP/SL MISSING: OKX/open order/journal không có giá bảo vệ.")
    return "\n".join(lines)


def format_balance_view(config: dict[str, Any]) -> str:
    try:
        balance = fetch_balance_snapshot(config, use_cache=True)
        positions = fetch_positions_snapshot(config, use_cache=True)
        snapshot = {
            "ok": bool(balance.get("ok") and positions.get("ok")),
            "error": balance.get("error") or positions.get("error"),
            "balance_usdt": balance.get("balance_usdt"),
            "positions": positions.get("positions") or [],
            "pending_count": count_pending_orders(config),
        }
    except Exception as exc:
        return f"💵 SD {date_time_label()}\n🚨 Lỗi: Lấy số dư thất bại: {exc}"
    if not snapshot.get("ok", True):
        return f"💵 SD {date_time_label()}\n🚨 Lỗi: {_reason_vi(snapshot.get('error') or '-')}"
    lines = [
        f"💵 SD tài khoản {date_time_label()}",
        f"💰 Số dư: {_fmt_number(snapshot.get('balance_usdt'), 2)} USDT",
        f"📌 VT đang mở: {len(snapshot.get('positions') or [])}",
        f"🟡 LC pending/OKX: {snapshot.get('pending_count', 0)}",
    ]
    return "\n".join(lines)


def format_pending_orders_view(config: dict[str, Any]) -> str:
    rows = sorted(
        list_pending_orders(config, status="OPEN", limit=50),
        key=lambda row: _float(row.get("win_probability_pct")) or 0,
        reverse=True,
    )
    lines = [f"🟡 LC pending/OKX {date_time_label()}", f"📊 Tổng LC pending: {len(rows)}"]
    if not rows:
        lines.append("⚪ Không có lệnh chờ")
        return "\n".join(lines)
    for index, row in enumerate(rows, 1):
        lc_id = row.get("journal_id") or row.get("id") or "-"
        expires_at = _parse_time(row.get("expires_at"))
        lines.append(
            f"{index}. 🟡 LC #{lc_id} {_coin_icon(row.get('symbol'))} {_side_icon(row.get('side'))} "
            f"{row.get('symbol', '-')} {str(row.get('side', '-')).upper()}"
        )
        lines.append(
            f"   🎯 Giá chờ: {_fmt_number(row.get('entry'), 6)} | 🛑 SL: {_fmt_number(row.get('stop_loss'), 6)} | ✅ TP: {_fmt_number(row.get('take_profit'), 6)}"
        )
        lines.append(
            f"   📦 KL: {_fmt_number(row.get('quantity'), 6)} | ⏳ Hết hạn: {date_time_label(expires_at) if expires_at else '-'}"
        )
        lines.append(f"   📈 Win rate: {_fmt_number(row.get('win_probability_pct'), 2, '%')}")
        raw_status = str(row.get("status") or "")
        if raw_status == "LC_OKX" or row.get("exchange_order_id"):
            status_label = "LC_OKX"
        elif raw_status == "WAIT_SLOT":
            status_label = "WAIT_SLOT"
        else:
            status_label = "LC noi bo"
        if status_label == "LC_OKX":
            lines.append(f"   Status: LC_OKX | OKX ID: {row.get('exchange_order_id') or '-'}")
        elif status_label == "WAIT_SLOT":
            lines.append("   Status: WAIT_SLOT, dang cho slot trong")
        else:
            lines.append("   Status: LC noi bo, chua gui OKX")
        continue
        if not row.get("exchange_order_id"):
            lines.append("   🟡 Trạng thái: nội bộ, chưa gửi OKX")
        else:
            lines.append(f"   📤 Trạng thái: đã gửi OKX | ID: {row.get('exchange_order_id')}")
    return "\n".join(lines)


def _lc_pending_source_label(row: dict[str, Any]) -> str:
    state = str(row.get("state") or "").upper()
    if state == "CHUA_DUYET":
        source = row.get("source_slot") or row.get("source") or row.get("slot")
        index = row.get("source_index")
    else:
        source = row.get("source") or row.get("slot")
        index = row.get("source_index")
    source_text = str(source or "").lower()
    source_time = None
    raw_label = str(row.get("source_label") or "").strip()
    if raw_label:
        parts = raw_label.split()
        source_time = parts[-1] if parts else None
    if not source_time:
        raw_source_time = row.get("source_time")
        if raw_source_time:
            try:
                source_time = datetime.fromisoformat(str(raw_source_time).replace("Z", "+00:00")).strftime("%H:%M:%S")
            except ValueError:
                source_time = None

    def _fmt(label: str) -> str:
        return f"{label} ({source_time})" if source_time else label

    if "four" in source_text or "4h" in source_text:
        return _fmt(f"4h #{index}") if index else "4h"
    if "two" in source_text or "2h" in source_text:
        return _fmt(f"2h #{index}") if index else "2h"
    if "hour" in source_text or "1h" in source_text:
        return _fmt(f"1h #{index}") if index else "1h"
    if state == "CHUA_DUYET":
        return _fmt(f"2h #{index}") if index else "2h"
    return "-"


def _lc_pending_origin_label(row: dict[str, Any]) -> str:
    origin_slot = str(row.get("origin_source_slot") or "").strip().lower()
    origin_index = row.get("origin_source_index")
    raw_label = str(row.get("origin_source_label") or "").strip()
    origin_time = None
    if raw_label:
        parts = raw_label.split()
        origin_time = parts[-1] if parts else None
    if not origin_time:
        raw_origin_time = row.get("origin_source_time")
        if raw_origin_time:
            try:
                origin_time = datetime.fromisoformat(str(raw_origin_time).replace("Z", "+00:00")).strftime("%H:%M:%S")
            except ValueError:
                origin_time = None

    def _fmt(label: str) -> str:
        return f"{label} ({origin_time})" if origin_time else label

    if "4h" in origin_slot or "four" in origin_slot:
        return _fmt(f"4h #{origin_index}") if origin_index else "4h"
    if "2h" in origin_slot or "two" in origin_slot:
        return _fmt(f"2h #{origin_index}") if origin_index else "2h"
    if "1h" in origin_slot or "hour" in origin_slot:
        return _fmt(f"1h #{origin_index}") if origin_index else "1h"
    return "-"


def _lc_pending_recheck_label(row: dict[str, Any]) -> str:
    index = row.get("recheck_daily_index")
    raw_label = str(row.get("recheck_label") or "").strip()
    recheck_time = None
    if raw_label:
        parts = raw_label.split()
        recheck_time = parts[-1] if parts else None
    if not recheck_time:
        raw_recheck_time = row.get("recheck_time")
        if raw_recheck_time:
            try:
                recheck_time = datetime.fromisoformat(str(raw_recheck_time).replace("Z", "+00:00")).strftime("%H:%M:%S")
            except ValueError:
                recheck_time = None
    if index:
        return f"RC #{index} ({recheck_time})" if recheck_time else f"RC #{index}"
    return "-"


def _duration_label(started_at: datetime | None, *, now: datetime | None = None) -> str:
    if not started_at:
        return "-"
    now = now or datetime.now(timezone.utc)
    seconds = max(0, int((now - started_at).total_seconds()))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def format_undecided_lc_view(config: dict[str, Any]) -> str:
    payload = lc_pipeline_dashboard_payload(config)
    rows = payload.get("undecided") if isinstance(payload.get("undecided"), list) else []
    lines = [f"📋 Chưa Duyệt {date_time_label()}", f"📊 Tổng: {len(rows)}"]
    if not rows:
        lines.append("⚪ Chưa có cặp Chưa Duyệt")
        return "\n".join(lines)
    for index, row in enumerate(rows[:10], 1):
        symbol = row.get("symbol") or "-"
        side = str(row.get("side") or "-").upper()
        scan_at = _parse_time(row.get("last_seen_at") or row.get("first_seen_at"))
        first_seen_at = _parse_time(row.get("first_seen_at"))
        source = _lc_pending_source_label(row)
        origin = _lc_pending_origin_label(row)
        recheck = _lc_pending_recheck_label(row)
        alive = _duration_label(first_seen_at)
        win = _fmt_number(row.get("win_probability_pct"), 2, "%")
        lines.append(
            f"{index}. {symbol} | {side} | Win {win} | {date_time_label(scan_at) if scan_at else '-'} | "
            f"{source} | {recheck} | gốc {origin} | sống {alive}"
        )
    return "\n".join(lines)


def _format_memory_signal(entry: dict[str, Any]) -> str:
    indicator = entry.get("indicator") if isinstance(entry.get("indicator"), dict) else {}
    patterns = indicator.get("candlestick_patterns") if isinstance(indicator.get("candlestick_patterns"), dict) else {}
    pattern_names = patterns.get("patterns") if isinstance(patterns.get("patterns"), list) else []
    strongest = ", ".join(str(item) for item in pattern_names[:2]) if pattern_names else "-"
    trend = indicator.get("trend") or indicator.get("trend_context") or "-"
    side = str(entry.get("side") or "-").upper()
    win = _fmt_number(entry.get("win_probability_pct"), 1)
    score = _fmt_number(entry.get("score"), 2)
    observed_at = date_time_label(_parse_time(entry.get("created_at")))
    return f"{observed_at} | {side} | win {win}% | score {score} | trend {trend} | nến {strongest}"


def format_market_scan_memory_view(config: dict[str, Any]) -> str:
    memory = recent_market_scan_memory(
        config,
        timeframes=["1m", "5m", "15m", "1h", "4h"],
        lookback_hours=24,
        per_symbol_timeframe_limit=2,
        total_limit=500,
    )
    lines = [f"🧠 Scan memory {date_time_label()}", f"📊 Symbol có dữ liệu: {len(memory)}"]
    if not memory:
        lines.append("⚪ Chưa có scan memory trong 24 giờ gần nhất")
        return "\n".join(lines)
    for symbol, frames in list(memory.items())[:8]:
        lines.append(f"\n{_coin_icon(symbol)} {symbol}")
        for timeframe in ["4h", "1h", "15m", "5m", "1m"]:
            entries = frames.get(timeframe) or []
            if not entries:
                continue
            latest = entries[0]
            lines.append(f"  {timeframe}: {_format_memory_signal(latest)}")
    return "\n".join(lines)


def format_pnl_sd_view(config: dict[str, Any]) -> str:
    try:
        snapshot = fetch_account_snapshot(config, use_cache=True)
    except Exception as exc:
        snapshot = {
            "ok": False,
            "error": f"Lấy PNL/SD thất bại: {exc}",
            "positions": [],
            "balance_usdt": None,
            "pending_count": count_pending_orders(config),
        }
    memory_sync = sync_trade_memory_from_exchange(config)
    return format_account_report(snapshot, memory_sync)


def format_positions_account_view(config: dict[str, Any]) -> str:
    try:
        snapshot = fetch_account_snapshot(config, use_cache=True)
    except Exception as exc:
        return f"📊 VT/PNL/SD {date_time_label()}\n🚨 Lỗi: Lấy tài khoản thất bại: {exc}"
    if not snapshot.get("ok", True):
        return f"📊 VT/PNL/SD {date_time_label()}\n🚨 Lỗi: {_reason_vi(snapshot.get('error') or '-')}"

    positions = snapshot.get("positions") or []
    lines = [
        f"📊 VT/PNL/SD {date_time_label()}",
        f"💵 SD: {_fmt_number(snapshot.get('balance_usdt'), 2)} USDT",
        f"📌 VT đang mở: {len(positions)}",
        f"🟡 LC pending/OKX: {snapshot.get('pending_count', 0)}",
    ]
    if not positions:
        lines.append("⚪ Không có vị thế mở")
        return "\n".join(lines)
    for index, position in enumerate(positions, 1):
        lines.append(
            f"{index}. {_coin_icon(position.get('symbol'))} {_pnl_icon(position.get('pnl_usdt'))} {_side_icon(position.get('side'))} "
            f"{position.get('symbol', '-')} {str(position.get('side', '-')).upper()}"
        )
        lines.append(
            f"   💰 PNL: {_fmt_signed(position.get('pnl_usdt'), 4)} USDT "
            f"({_fmt_signed(position.get('pnl_pct'), 2, '%')})"
        )
        lines.append(
            f"   🎯 Vào: {_fmt_number(position.get('entry_price'), 6)} | Mark: {_fmt_number(position.get('mark_price'), 6)}"
        )
        lines.append(
            f"   🛑 SL: {_fmt_position_target(position, 'stop_loss')} | ✅ TP: {_fmt_position_target(position, 'take_profit')}"
        )
    return "\n".join(lines)


def format_telegram_menu() -> str:
    return (
        "📲 Bảng điều khiển bot\n"
        "Bấm nút bên dưới để xem nhanh dữ liệu hiện tại.\n"
        "🔔 Các thông báo tự động theo thời gian vẫn giữ nguyên."
    )


def format_market_guard_message(status: dict[str, Any]) -> str:
    alerts = status.get("alerts") or []
    block = status.get("block") or {}
    lines = [
        f"🚨🛡️ Market Guard {date_time_label(_parse_time(status.get('created_at')))}",
        f"📊 Phát hiện biến động mạnh: {len(alerts)} cặp",
    ]
    if block.get("active"):
        blocked_until = _parse_time(block.get("blocked_until"))
        lines.append(f"⛔ Tạm dừng vào lệnh/ mở LC: đến {date_time_label(blocked_until)}")
    for index, alert in enumerate(alerts[:6], 1):
        severity = str(alert.get("severity") or "warning")
        icon = "🔴" if severity == "critical" else "🟡"
        symbol = alert.get("symbol") or "-"
        reasons = " | ".join(str(item) for item in (alert.get("reasons") or [])[:4])
        lines.append(
            f"{index}. {icon} {_coin_icon(symbol)} {symbol}: {reasons}"
        )
        lines.append(
            f"   Giá: {_fmt_number(alert.get('last'), 6)} | Δ: {_fmt_signed(alert.get('move_pct'), 2, '%')} | Râu: {_fmt_number(alert.get('wick_pct'), 2, '%')} | Vol: {_fmt_number(alert.get('volume_ratio'), 2)}x"
        )
    warnings = status.get("warnings") or []
    if warnings:
        lines.append("⚠️ " + " | ".join(_reason_vi(item) for item in warnings[:2]))
    notify_minutes = max(1, round(float(status.get("notify_interval_seconds") or 600) / 60))
    lines.append("Chi báo khi có biến động mạnh/rút râu mạnh; bỏ qua các nhịp tăng nhẹ 0.5-1%.")
    lines.append(f"🔕 Guard chỉ gửi Telegram tối đa {notify_minutes} phút/lần.")
    return "\n".join(lines)


def format_daily_summary(
    config: dict[str, Any],
    summary_date_key: str,
    snapshot: dict[str, Any],
    memory_sync: dict[str, Any],
) -> str:
    summary_date = datetime.fromisoformat(summary_date_key).replace(tzinfo=LOCAL_TZ)
    closed = _closed_records_for_date(config, summary_date_key)
    pnl = sum(float(item.get("pnl_usdt") or 0) for item in closed)
    wins = sum(1 for item in closed if float(item.get("pnl_usdt") or 0) > 0)
    losses = sum(1 for item in closed if float(item.get("pnl_usdt") or 0) < 0)
    start_balance = _float(get_journal_state(config, f"daily_start_balance:{summary_date_key}"))
    end_balance = _float(snapshot.get("balance_usdt"))
    lines = [f"📅📌 Tổng kết ngày {date_label(summary_date)}"]
    lines.append(f"💵 SD đầu ngày: {_fmt_number(start_balance, 2)} USDT")
    lines.append(f"{_pnl_icon(pnl)} Thắng/thua đã đóng: {_fmt_signed(pnl, 4)} USDT (🟢 {wins} thắng / 🔴 {losses} thua)")
    if start_balance is not None and end_balance is not None:
        lines.append(f"{_pnl_icon(end_balance - start_balance)} Chênh lệch SD: {_fmt_signed(end_balance - start_balance, 4)} USDT")
    lines.append(f"💵 SD cuối ngày: {_fmt_number(end_balance, 2)} USDT")
    lines.append(f"📌 Vị thế đang mở: {len(snapshot.get('positions') or [])}")
    lines.append(f"🟡 LC pending/OKX: {snapshot.get('pending_count', 0)}")
    for warning in memory_sync.get("warnings") or []:
        lines.append(f"⚠️ Cảnh báo: {_reason_vi(warning)}")
    return "\n".join(lines)


def _account_report_due(config: dict[str, Any], now: datetime) -> bool:
    interval = int(config.get("notifications", {}).get("telegram", {}).get("account_report_interval_seconds", 18_000) or 0)
    if interval <= 0:
        return False
    last_value = get_journal_state(config, "last_account_report_at")
    last = _parse_time(last_value)
    if last is None:
        return True
    return (now - last).total_seconds() >= interval


def _mark_account_report_sent(config: dict[str, Any], now: datetime) -> None:
    set_journal_state(config, "last_account_report_at", now.isoformat())


def _daily_rollover_date(config: dict[str, Any], now: datetime) -> str | None:
    today = date_key(now)
    last_seen = get_journal_state(config, "last_local_date_seen")
    if last_seen is None:
        set_journal_state(config, "last_local_date_seen", today)
        return None
    if last_seen != today:
        set_journal_state(config, "last_local_date_seen", today)
        return last_seen
    return None


def build_periodic_report_messages(config: dict[str, Any]) -> list[str]:
    telegram_config = config.get("notifications", {}).get("telegram", {})
    now = datetime.now(timezone.utc)
    summary_date = _daily_rollover_date(config, now) if telegram_config.get("daily_summary_enabled", True) else None
    account_due = _account_report_due(config, now)
    if not summary_date and not account_due:
        return []

    try:
        snapshot = fetch_account_snapshot(config)
    except Exception as exc:
        snapshot = {
            "ok": False,
            "error": f"Lấy PNL/SD thất bại: {exc}",
            "positions": [],
            "balance_usdt": None,
            "pending_count": count_pending_orders(config),
        }
    memory_sync = sync_trade_memory_from_exchange(config)
    _ensure_daily_start_balance(config, snapshot, now)

    messages: list[str] = []
    if summary_date:
        messages.append(format_daily_summary(config, summary_date, snapshot, memory_sync))
    if account_due:
        messages.append(format_account_report(snapshot, memory_sync))
        _mark_account_report_sent(config, now)
    return messages
