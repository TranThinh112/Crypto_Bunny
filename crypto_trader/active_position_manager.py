from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from .config import project_path
from .market import create_exchange
from .models import to_jsonable
from .storage import append_trade_execution_event, get_journal_state, list_journal_state_prefix, list_trade_execution_rows, set_journal_state, update_trade_execution

LOGGER = logging.getLogger(__name__)

ACTIVE_POSITION_LOG_PREFIX = "active_position_manager_log"
ACTIVE_POSITION_LAST_ACTION_PREFIX = "active_position_manager_last_action"


def _float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    number = _float(value)
    return default if number is None else number


def _settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("active_position_manager", {}) if isinstance(config.get("active_position_manager"), dict) else {}
    protected_positions = raw.get("protected_positions")
    if not isinstance(protected_positions, list):
        protected_positions = []
    return {
        "enabled": bool(raw.get("enabled", True)),
        "shadow_mode": bool(raw.get("shadow_mode", True)),
        "auto_execute_enabled": bool(raw.get("auto_execute_enabled", False)),
        "effective_from": str(raw.get("effective_from") or ""),
        "apply_to_existing_positions": bool(raw.get("apply_to_existing_positions", False)),
        "execute_bad_cut": bool(raw.get("execute_bad_cut", False)),
        "bad_cut_once_per_position": bool(raw.get("bad_cut_once_per_position", True)),
        "bad_cut_reset_on_scale_in": bool(raw.get("bad_cut_reset_on_scale_in", True)),
        "execute_good_exit": bool(raw.get("execute_good_exit", False)),
        "execute_remainder_cut": bool(raw.get("execute_remainder_cut", False)),
        "execute_dca": bool(raw.get("execute_dca", False)),
        "execute_scale_in": bool(raw.get("execute_scale_in", False)),
        "review_interval_seconds": max(60, int(raw.get("review_interval_seconds", 300) or 300)),
        "notify_telegram": bool(raw.get("notify_telegram", True)),
        "notify_cooldown_seconds": max(60, int(raw.get("notify_cooldown_seconds", 900) or 900)),
        "max_dca_count": max(0, int(raw.get("max_dca_count", 1) or 1)),
        "max_scale_in_count": max(0, int(raw.get("max_scale_in_count", 1) or 1)),
        "dca_r_min": _safe_float(raw.get("dca_r_min"), -0.65),
        "dca_r_max": _safe_float(raw.get("dca_r_max"), -0.25),
        "bad_cut_r": _safe_float(raw.get("bad_cut_r"), -0.9),
        "good_exit_r": _safe_float(raw.get("good_exit_r"), 0.7),
        "scale_in_r": _safe_float(raw.get("scale_in_r"), 0.9),
        "protect_profit_r": _safe_float(raw.get("protect_profit_r"), 0.5),
        "trend_break_progress_limit_pct": _safe_float(raw.get("trend_break_progress_limit_pct"), -25.0),
        "trend_exhaustion_progress_pct": _safe_float(raw.get("trend_exhaustion_progress_pct"), 80.0),
        "partial_cut_fraction": max(0.05, min(1.0, _safe_float(raw.get("partial_cut_fraction"), 0.25))),
        "good_exit_fraction": max(0.05, min(1.0, _safe_float(raw.get("good_exit_fraction"), 0.25))),
        "dca_fraction": max(0.05, min(1.0, _safe_float(raw.get("dca_fraction"), 0.25))),
        "scale_in_fraction": max(0.05, min(1.0, _safe_float(raw.get("scale_in_fraction"), 0.25))),
        "protected_positions": [item for item in protected_positions if isinstance(item, dict)],
    }


def _parse_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _applies_to_row(row: dict[str, Any], settings: dict[str, Any]) -> bool:
    if settings.get("apply_to_existing_positions"):
        return True
    effective_at = _parse_time(settings.get("effective_from"))
    if effective_at is None:
        return False
    created_at = _parse_time(row.get("created_at"))
    if created_at is None:
        return False
    return created_at >= effective_at


def _snapshot_position(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = _parse_json(row.get("snapshot_json")) or _parse_json(row.get("payload_json"))
    position = payload.get("position") if isinstance(payload.get("position"), dict) else {}
    info = position.get("info") if isinstance(position.get("info"), dict) else {}
    return position, info


def _first_number(*values: Any) -> float | None:
    for value in values:
        number = _float(value)
        if number is not None:
            return number
    return None


def _event_history(row: dict[str, Any]) -> list[dict[str, Any]]:
    raw = row.get("trade_event_history_json")
    if not raw:
        return []
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def _contract_size(row: dict[str, Any]) -> float:
    position, info = _snapshot_position(row)
    for value in (row.get("contract_size"), position.get("contractSize"), info.get("ctVal")):
        number = _float(value)
        if number is not None and number > 0:
            return number
    return 1.0

def _position_margin_mode(position: dict[str, Any], info: dict[str, Any]) -> str | None:
    value = position.get("marginMode") or position.get("margin_mode") or info.get("mgnMode") or info.get("tdMode")
    text = str(value or "").strip().lower()
    return text if text in {"cross", "isolated"} else None


def _position_id(row: dict[str, Any], position: dict[str, Any], info: dict[str, Any]) -> str:
    return str(row.get("exchange_position_id") or position.get("id") or position.get("posId") or info.get("posId") or "").strip()

def _is_protected_position(row: dict[str, Any], decision: dict[str, Any], settings: dict[str, Any]) -> bool:
    protected_positions = settings.get("protected_positions")
    if not isinstance(protected_positions, list):
        return False
    position, info = _snapshot_position(row)
    row_id = str(row.get("id") or "").strip()
    symbol = str(row.get("symbol") or decision.get("symbol") or "").upper().strip()
    side = str(row.get("side") or decision.get("side") or "").upper().strip()
    pos_id = _position_id(row, position, info)
    for item in protected_positions:
        if not isinstance(item, dict) or item.get("enabled") is False:
            continue
        expected_trade_id = str(item.get("trade_execution_id") or "").strip()
        expected_symbol = str(item.get("symbol") or "").upper().strip()
        expected_side = str(item.get("side") or "").upper().strip()
        expected_pos_id = str(item.get("exchange_position_id") or item.get("pos_id") or "").strip()
        if expected_trade_id and expected_trade_id != row_id:
            continue
        if expected_symbol and expected_symbol != symbol:
            continue
        if expected_side and expected_side != side:
            continue
        if expected_pos_id and expected_pos_id != pos_id:
            continue
        return bool(expected_trade_id or expected_symbol or expected_side or expected_pos_id)
    return False

def _pnl_at(row: dict[str, Any], price: float | None, quantity: float | None = None) -> float | None:
    position, info = _snapshot_position(row)
    entry = _first_number(row.get("entry_price"), row.get("initial_entry_price"), position.get("entry_price"), position.get("entryPrice"), info.get("avgPx"))
    target = _float(price)
    qty = _float(quantity if quantity is not None else row.get("quantity"))
    if entry is None or target is None or qty is None:
        return None
    side = str(row.get("side") or "").lower()
    gross = target - entry if side == "long" else entry - target
    return round(gross * qty * _contract_size(row), 6)


def _risk_per_contract(row: dict[str, Any]) -> float | None:
    position, info = _snapshot_position(row)
    entry = _first_number(row.get("initial_entry_price"), row.get("entry_price"), position.get("entry_price"), position.get("entryPrice"), info.get("avgPx"))
    stop = _float(row.get("initial_stop_loss") or row.get("stop_loss"))
    if entry is None or stop is None:
        return None
    side = str(row.get("side") or "").lower()
    risk = entry - stop if side == "long" else stop - entry
    return risk if risk > 0 else None


def _r_multiple(row: dict[str, Any]) -> float | None:
    position, info = _snapshot_position(row)
    entry = _first_number(row.get("entry_price"), row.get("initial_entry_price"), position.get("entry_price"), position.get("entryPrice"), info.get("avgPx"))
    mark = _first_number(row.get("mark_price"), row.get("current_price"), position.get("mark_price"), position.get("markPrice"), info.get("markPx"), position.get("last"))
    risk = _risk_per_contract(row)
    if entry is None or mark is None or risk is None:
        return None
    side = str(row.get("side") or "").lower()
    move = mark - entry if side == "long" else entry - mark
    return round(move / risk, 4)


def _tp_progress_pct(row: dict[str, Any]) -> float | None:
    position, info = _snapshot_position(row)
    entry = _first_number(row.get("entry_price"), row.get("initial_entry_price"), position.get("entry_price"), position.get("entryPrice"), info.get("avgPx"))
    mark = _first_number(row.get("mark_price"), row.get("current_price"), position.get("mark_price"), position.get("markPrice"), info.get("markPx"), position.get("last"))
    target = _float(row.get("take_profit"))
    if entry is None or mark is None or target is None:
        return None
    side = str(row.get("side") or "").lower()
    reward = target - entry if side == "long" else entry - target
    if reward <= 0:
        return None
    progress = (mark - entry if side == "long" else entry - mark) / reward * 100.0
    return round(progress, 2)


def _count_events(row: dict[str, Any], event_type: str) -> int:
    return sum(1 for event in _event_history(row) if str(event.get("type") or "") == event_type)


def _event_time(event: dict[str, Any]) -> datetime | None:
    return _parse_time(event.get("created_at") or event.get("at") or event.get("closed_at"))


def _bad_cut_done(row: dict[str, Any], settings: dict[str, Any]) -> bool:
    if not settings.get("bad_cut_once_per_position", True):
        return False
    if not bool(row.get("bad_cut_done")) and _count_events(row, "active_position_bad_cut") <= 0:
        return False
    if not settings.get("bad_cut_reset_on_scale_in", True):
        return True
    history = _event_history(row)
    bad_cut_times = [
        parsed
        for parsed in (_event_time(event) for event in history if str(event.get("type") or "") == "active_position_bad_cut")
        if parsed is not None
    ]
    bad_cut_at = _parse_time(row.get("bad_cut_at"))
    if bad_cut_at is not None:
        bad_cut_times.append(bad_cut_at)
    if not bad_cut_times:
        return bool(row.get("bad_cut_done"))
    latest_bad_cut_at = max(bad_cut_times)
    for event in history:
        if str(event.get("type") or "") not in {"active_position_scale_in", "open", "position_reopened"}:
            continue
        event_at = _event_time(event)
        if event_at is not None and event_at > latest_bad_cut_at:
            return False
    return True


def _decision_for_row(row: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    side = str(row.get("side") or "").lower()
    symbol = str(row.get("symbol") or "-")
    position, info = _snapshot_position(row)
    quantity = _first_number(row.get("quantity"), position.get("contracts"), info.get("pos"), info.get("availPos"))
    quantity = abs(quantity) if quantity is not None else None
    entry = _first_number(row.get("entry_price"), row.get("initial_entry_price"), position.get("entry_price"), position.get("entryPrice"), info.get("avgPx"))
    mark = _first_number(row.get("mark_price"), row.get("current_price"), position.get("mark_price"), position.get("markPrice"), info.get("markPx"), position.get("last"))
    stop_loss = _float(row.get("stop_loss"))
    take_profit = _float(row.get("take_profit"))
    pnl = _float(row.get("pnl"))
    r_value = _r_multiple(row)
    progress = _tp_progress_pct(row)
    reasons: list[str] = []
    action = "HOLD"
    decision = "Giữ vị thế"
    fraction = None
    amount = None
    expected_pnl = pnl

    partial_profit_done = bool(row.get("partial_take_profit_done"))
    loss_guard_done = bool(row.get("loss_guard_partial_done"))
    bad_cut_done = _bad_cut_done(row, settings)
    dca_count = _count_events(row, "active_position_dca")
    scale_count = _count_events(row, "active_position_scale_in")

    if r_value is None:
        reasons.append("Chưa đủ entry/SL/mark để tính R.")
    elif partial_profit_done and r_value < settings["protect_profit_r"]:
        action = "GOOD_EXIT_REVIEW"
        decision = "Xem xét đóng phần còn lại để bảo toàn lãi"
        fraction = 1.0
        reasons.append("Vị thế đã chốt lời 30%; không chốt thêm partial lần hai.")
        reasons.append("Giá đã suy yếu sau partial profit, chỉ xem xét đóng phần còn lại để bảo toàn lãi.")
    elif partial_profit_done:
        action = "HOLD_AFTER_PARTIAL"
        decision = "Giữ phần còn lại sau chốt lời 30%"
        reasons.append("Vị thế đã chốt lời 30%; ưu tiên module Chốt lời & Bảo vệ tiếp tục dời SL/TP theo nấc.")
        reasons.append("Module chủ động không chốt thêm nếu chưa có dấu hiệu đảo chiều rõ.")
    elif bad_cut_done and r_value <= settings["bad_cut_r"]:
        action = "HOLD_AFTER_BAD_CUT"
        decision = "Giữ phần còn lại sau khi đã cắt lỗ chủ động"
        reasons.append("BAD_CUT skipped: already executed for this position.")
        reasons.append("Phần còn lại để OKX SL hoặc tín hiệu vị thế mới xử lý; bot không cắt lặp.")
    elif loss_guard_done and r_value <= settings["bad_cut_r"]:
        action = "BAD_CUT_REMAINDER"
        decision = "Xem xét đóng phần còn lại sau chốt lỗ 25%"
        fraction = 1.0
        reasons.append("Vị thế đã chốt lỗ 25%; không lặp lại chốt lỗ từng phần.")
        reasons.append("Nếu setup tiếp tục hỏng, chỉ xem xét đóng phần còn lại để tránh lỗ lan rộng.")
    elif loss_guard_done:
        action = "HOLD_AFTER_LOSS_CUT"
        decision = "Giữ phần còn lại sau chốt lỗ 25%"
        reasons.append("Vị thế đã chốt lỗ 25%; tiếp tục theo dõi hồi phục hoặc hỏng setup.")
    elif r_value <= settings["bad_cut_r"]:
        action = "BAD_CUT"
        decision = "Cắt lỗ chủ động"
        fraction = settings["partial_cut_fraction"]
        reasons.append(f"Lệnh đang âm {r_value:.2f}R, vượt ngưỡng cắt chủ động {settings['bad_cut_r']:.2f}R.")
        if progress is not None and progress <= settings["trend_break_progress_limit_pct"]:
            reasons.append("Giá đi xa khỏi setup ban đầu, xác suất hồi phục kém.")
    elif (
        settings["dca_r_min"] <= r_value <= settings["dca_r_max"]
        and dca_count < settings["max_dca_count"]
        and not loss_guard_done
    ):
        action = "DCA_REVIEW"
        decision = "Xem xét DCA có kiểm soát"
        fraction = settings["dca_fraction"]
        reasons.append(f"Lệnh đang âm {r_value:.2f}R trong vùng DCA cho phép.")
        reasons.append("Chỉ được DCA nếu trend vẫn đúng và Risk/Capital còn cho phép.")
    elif r_value >= settings["scale_in_r"] and scale_count < settings["max_scale_in_count"]:
        action = "SCALE_IN_REVIEW"
        decision = "Xem xét thêm khối lượng khi đang thắng"
        fraction = settings["scale_in_fraction"]
        reasons.append(f"Lệnh đang lời {r_value:.2f}R, đủ điều kiện xem xét scale-in.")
        reasons.append("Chỉ thêm khi có pullback/continuation hợp lệ và RR sau khi thêm vẫn tốt.")
    elif r_value >= settings["good_exit_r"] and progress is not None and progress >= settings["trend_exhaustion_progress_pct"]:
        action = "GOOD_EXIT_REVIEW"
        decision = "Xem xét chốt lời ngắn"
        fraction = settings["good_exit_fraction"]
        reasons.append(f"Đã đi được {progress:.2f}% tới TP, cần kiểm tra dấu hiệu đảo chiều.")
    elif r_value >= settings["protect_profit_r"]:
        action = "PROTECT_PROFIT"
        decision = "Ưu tiên bảo vệ lợi nhuận"
        reasons.append(f"Lệnh đang lời {r_value:.2f}R, nên ưu tiên SL dương/chốt một phần theo lifecycle.")
    else:
        reasons.append("Chưa có tín hiệu đủ mạnh để can thiệp; tiếp tục theo dõi.")

    if fraction is not None and quantity is not None:
        amount = round(quantity * float(fraction), 8)
    if action in {"BAD_CUT", "BAD_CUT_REMAINDER", "GOOD_EXIT_REVIEW"} and mark is not None and amount is not None:
        expected_pnl = _pnl_at(row, mark, amount)
    elif action in {"DCA_REVIEW", "SCALE_IN_REVIEW"} and mark is not None and amount is not None:
        expected_pnl = None

    return {
        "trade_execution_id": row.get("id"),
        "symbol": symbol,
        "side": side.upper(),
        "action": action,
        "decision": decision,
        "shadow_mode": bool(settings["shadow_mode"]),
        "auto_execute_enabled": bool(settings["auto_execute_enabled"]),
        "td_mode": _position_margin_mode(position, info),
        "entry": entry,
        "mark_price": mark,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "quantity": quantity,
        "contract_size": _contract_size(row),
        "pnl": pnl,
        "r_multiple": r_value,
        "tp_progress_pct": progress,
        "fraction": fraction,
        "amount": amount,
        "expected_pnl": expected_pnl,
        "reasons": reasons,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "bad_cut_done": bad_cut_done,
    }


def _log_key(decision: dict[str, Any]) -> str:
    return ":".join(
        [
            ACTIVE_POSITION_LOG_PREFIX,
            str(decision.get("created_at") or ""),
            str(decision.get("trade_execution_id") or "-"),
            str(decision.get("action") or "-"),
        ]
    )


def _last_action_key(decision: dict[str, Any]) -> str:
    return ":".join(
        [
            ACTIVE_POSITION_LAST_ACTION_PREFIX,
            str(decision.get("trade_execution_id") or "-"),
            str(decision.get("symbol") or "-"),
            str(decision.get("side") or "-"),
        ]
    )


def _persist_decision(config: dict[str, Any], decision: dict[str, Any]) -> None:
    payload = to_jsonable(decision)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        set_journal_state(config, _log_key(payload), body)
        set_journal_state(config, _last_action_key(payload), body)
    except Exception as exc:
        LOGGER.warning("Skipping active position journal log after storage error: %s", exc)
    try:
        log_path = project_path(config, "logs/active_position_manager.jsonl")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(body + "\n")
    except Exception as exc:
        LOGGER.warning("Skipping active position file log after filesystem error: %s", exc)


def _should_notify(config: dict[str, Any], decision: dict[str, Any], settings: dict[str, Any]) -> bool:
    if not settings["notify_telegram"] or decision.get("action") in {"HOLD", "HOLD_AFTER_PARTIAL", "HOLD_AFTER_LOSS_CUT"}:
        return False
    key = f"{_last_action_key(decision)}:telegram"
    previous = _parse_json(get_journal_state(config, key))
    now = datetime.now(timezone.utc)
    previous_time = None
    if previous.get("notified_at"):
        try:
            previous_time = datetime.fromisoformat(str(previous.get("notified_at")).replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            previous_time = None
    if previous_time is not None and (now - previous_time).total_seconds() < settings["notify_cooldown_seconds"]:
        if previous.get("action") == decision.get("action"):
            return False
    return True


def _remember_notification(config: dict[str, Any], decision: dict[str, Any]) -> None:
    key = f"{_last_action_key(decision)}:telegram"
    payload = {"notified_at": datetime.now(timezone.utc).isoformat(), "action": decision.get("action")}
    try:
        set_journal_state(config, key, json.dumps(payload, ensure_ascii=False))
    except Exception:
        return


def _notify(config: dict[str, Any], decision: dict[str, Any], settings: dict[str, Any]) -> bool:
    if not _should_notify(config, decision, settings):
        return False


def _amount_to_precision(exchange: Any, symbol: str, amount: float) -> str:
    method = getattr(exchange, "amount_to_precision", None)
    if callable(method):
        return str(method(symbol, amount))
    return f"{amount:.8f}".rstrip("0").rstrip(".")


def _execution_allowed(action: str, settings: dict[str, Any], decision: dict[str, Any]) -> bool:
    if not decision.get("applies"):
        return False
    if settings["shadow_mode"] or not settings["auto_execute_enabled"]:
        return False
    if action == "BAD_CUT":
        return bool(settings["execute_bad_cut"])
    if action == "GOOD_EXIT_REVIEW":
        return bool(settings["execute_good_exit"])
    if action == "BAD_CUT_REMAINDER":
        return bool(settings["execute_remainder_cut"])
    if action == "DCA_REVIEW":
        return bool(settings["execute_dca"])
    if action == "SCALE_IN_REVIEW":
        return bool(settings["execute_scale_in"])
    return False


def _execute_reduce_only_close(config: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    mode = str(config.get("mode") or "dry_run")
    action = str(decision.get("action") or "")
    symbol = str(decision.get("symbol") or "")
    side = str(decision.get("side") or "").lower()
    amount = _float(decision.get("amount"))
    quantity = _float(decision.get("quantity"))
    if action not in {"BAD_CUT", "GOOD_EXIT_REVIEW", "BAD_CUT_REMAINDER"}:
        return {"submitted": False, "reason": "action_not_reduce_only_close"}
    if not symbol or side not in {"long", "short"}:
        return {"submitted": False, "reason": "invalid_symbol_or_side"}
    if amount is None or amount <= 0:
        return {"submitted": False, "reason": "invalid_amount"}
    if quantity is not None and quantity > 0:
        amount = min(amount, quantity)
    if mode == "dry_run":
        return {"submitted": False, "mode": mode, "reason": "dry_run"}
    exchange = create_exchange(config, authenticated=True)
    exchange.load_markets()
    order_side = "sell" if side == "long" else "buy"
    params: dict[str, Any] = {
        "tdMode": decision.get("td_mode") or config.get("exchange", {}).get("td_mode", "isolated"),
        "reduceOnly": True,
    }
    if config.get("exchange", {}).get("position_side_mode") == "long_short":
        params["posSide"] = side
    variants: list[dict[str, Any]] = [dict(params)]
    for td_mode in ("cross", "isolated"):
        retry = dict(params)
        retry["tdMode"] = td_mode
        variants.append(retry)
        without_pos_side = dict(retry)
        without_pos_side.pop("posSide", None)
        variants.append(without_pos_side)
    seen: set[tuple[tuple[str, str], ...]] = set()
    last_exc: Exception | None = None
    used_params: dict[str, Any] | None = None
    order: dict[str, Any] | None = None
    for candidate_params in variants:
        key = tuple(sorted((str(item_key), str(item_value)) for item_key, item_value in candidate_params.items()))
        if key in seen:
            continue
        seen.add(key)
        try:
            order = exchange.create_order(
                symbol,
                "market",
                order_side,
                _amount_to_precision(exchange, symbol, amount),
                None,
                candidate_params,
            )
            used_params = candidate_params
            break
        except Exception as exc:
            last_exc = exc
            continue
    if order is None:
        exc = last_exc or RuntimeError("OKX close order was not submitted")
        return {
            "submitted": False,
            "mode": mode,
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "error": str(exc),
            "request": {"type": "market", "side": order_side, "amount": amount, "params": params},
            "attempted_params": variants,
        }
    return {
        "submitted": True,
        "mode": mode,
        "symbol": symbol,
        "side": side,
        "amount": amount,
        "order": to_jsonable(order),
        "request": {"type": "market", "side": order_side, "amount": amount, "params": used_params or params},
    }


def _execute_decision(config: dict[str, Any], decision: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    action = str(decision.get("action") or "")
    if not _execution_allowed(action, settings, decision):
        return {
            "submitted": False,
            "reason": "not_applied_to_existing_position" if not decision.get("applies") else "execution_disabled_for_action",
            "shadow_mode": settings["shadow_mode"],
            "auto_execute_enabled": settings["auto_execute_enabled"],
            "effective_from": settings.get("effective_from"),
            "apply_to_existing_positions": settings.get("apply_to_existing_positions"),
        }
    if action in {"BAD_CUT", "GOOD_EXIT_REVIEW", "BAD_CUT_REMAINDER"}:
        return _execute_reduce_only_close(config, decision)
    return {"submitted": False, "reason": "execution_not_implemented_for_action"}
    try:
        from .notifier import send_telegram_message
        from .reporting import format_active_position_decision_message

        sent = send_telegram_message(
            config,
            format_active_position_decision_message(config, decision),
            with_buttons=False,
            replace_previous=False,
            allow_during_startup_quiet=True,
        )
        if sent:
            _remember_notification(config, decision)
        return bool(sent)
    except Exception as exc:
        LOGGER.warning("Skipping active position Telegram notification: %s", exc)
        return False


def evaluate_open_positions(
    config: dict[str, Any],
    *,
    rows: list[dict[str, Any]] | None = None,
    notify: bool = True,
    persist: bool = True,
) -> dict[str, Any]:
    settings = _settings(config)
    if not settings["enabled"]:
        return {"enabled": False, "reason": "disabled", "items": []}
    if rows is None:
        rows = list_trade_execution_rows(config, statuses=["OPEN"], limit=100, order="created_asc")
    decisions = [_decision_for_row(row, settings) for row in rows if isinstance(row, dict)]
    action_counts: dict[str, int] = {}
    notified = 0
    for row, decision in zip(rows, decisions):
        decision["applies"] = _applies_to_row(row, settings)
        decision["effective_from"] = settings.get("effective_from")
        action = str(decision.get("action") or "HOLD")
        action_counts[action] = action_counts.get(action, 0) + 1
        protected_position = _is_protected_position(row, decision, settings)
        decision["protected_position"] = protected_position
        if not persist:
            execution_result = {
                "submitted": False,
                "reason": "read_only_preview",
                "shadow_mode": settings["shadow_mode"],
                "auto_execute_enabled": settings["auto_execute_enabled"],
            }
        elif protected_position and action in {"BAD_CUT", "GOOD_EXIT_REVIEW", "BAD_CUT_REMAINDER"}:
            execution_result = {
                "submitted": False,
                "reason": "protected_position",
                "protected_position": True,
                "symbol": decision.get("symbol"),
                "side": decision.get("side"),
                "trade_execution_id": decision.get("trade_execution_id"),
            }
        else:
            execution_result = _execute_decision(config, decision, settings)
        decision["execution"] = execution_result
        if persist:
            _persist_decision(config, decision)
        trade_id = decision.get("trade_execution_id")
        if persist and trade_id is not None and action == "BAD_CUT" and execution_result.get("submitted"):
            bad_cut_at = datetime.now(timezone.utc).isoformat()
            try:
                update_trade_execution(
                    config,
                    int(trade_id),
                    {
                        "bad_cut_done": True,
                        "bad_cut_at": bad_cut_at,
                        "bad_cut_amount": decision.get("amount"),
                        "bad_cut_trigger_r": settings.get("bad_cut_r"),
                        "bad_cut_price": decision.get("mark_price"),
                    },
                )
            except Exception as exc:
                LOGGER.warning("Skipping active position bad cut marker update: %s", exc)
            try:
                append_trade_execution_event(
                    config,
                    int(trade_id),
                    {
                        "type": "active_position_bad_cut",
                        "created_at": bad_cut_at,
                        "action": action,
                        "amount": decision.get("amount"),
                        "trigger_r": settings.get("bad_cut_r"),
                        "r_multiple": decision.get("r_multiple"),
                        "price": decision.get("mark_price"),
                        "execution": decision.get("execution"),
                    },
                )
            except Exception as exc:
                LOGGER.warning("Skipping active position bad cut event append: %s", exc)
        if persist and trade_id is not None and action not in {"HOLD", "HOLD_AFTER_PARTIAL", "HOLD_AFTER_LOSS_CUT"}:
            try:
                append_trade_execution_event(
                    config,
                    int(trade_id),
                    {
                        "type": "active_position_review",
                        "created_at": decision.get("created_at"),
                        "action": action,
                        "decision": decision.get("decision"),
                        "shadow_mode": decision.get("shadow_mode"),
                        "auto_execute_enabled": decision.get("auto_execute_enabled"),
                        "r_multiple": decision.get("r_multiple"),
                        "tp_progress_pct": decision.get("tp_progress_pct"),
                        "amount": decision.get("amount"),
                        "expected_pnl": decision.get("expected_pnl"),
                        "reasons": decision.get("reasons"),
                        "execution": decision.get("execution"),
                    },
                )
            except Exception as exc:
                LOGGER.warning("Skipping active position trade event append: %s", exc)
        if persist and notify and _notify(config, decision, settings):
            notified += 1
    return {
        "enabled": True,
        "shadow_mode": settings["shadow_mode"],
        "auto_execute_enabled": settings["auto_execute_enabled"],
        "review_interval_seconds": settings["review_interval_seconds"],
        "open_count": len(decisions),
        "action_counts": action_counts,
        "notified": notified,
        "items": decisions,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def latest_active_position_decisions(config: dict[str, Any], *, limit: int = 20) -> list[dict[str, Any]]:
    rows = list_journal_state_prefix(config, ACTIVE_POSITION_LAST_ACTION_PREFIX, limit=max(1, int(limit)))
    items: list[dict[str, Any]] = []
    for row in rows:
        payload = _parse_json(row.get("value"))
        if payload:
            items.append(payload)
    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return items[:limit]
