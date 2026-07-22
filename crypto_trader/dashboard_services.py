from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .ai_coordinator import latest_internal_market_scan, next_internal_market_scan_at
from .capital import (
    analyze_configuration_change,
    build_capital_snapshot,
    calculate_capital_reserve_state,
    calculate_position_size,
    check_capital_allocation,
    infer_capital_mode,
    latest_capital_reserve_state,
    latest_capital_snapshot,
)
from .config import project_path
from .codex_features import (
    ai_call_decision_stats,
    ai_trade_decision_stats,
    _market_regime_aggregate_limit,
    _market_regime_top_symbols,
    current_market_regime,
    current_strategy_state,
    get_bunny_health_state,
    get_trading_system_state,
    market_regime_history,
    prompt_status,
    recent_ai_call_history,
    recent_ai_trade_decisions,
    replay_stats,
)
from .market_guard import market_guard_block_status
from .models import to_jsonable
from .sizing import STATE_KEY as SIZING_STATE_KEY
from .storage import (
    count_pending_orders,
    dashboard_snapshot_cache_version,
    get_journal_state,
    list_journal_state_prefix,
    list_market_guard_observations,
    list_paper_trades,
    list_replay_history_rows,
    list_trade_execution_rows,
    list_trade_memory,
    recent_market_scan_memory,
    set_journal_state,
    storage_stats,
)
from market_pattern_engine.infrastructure.config_loader import load_engine_config
from market_pattern_engine.repositories.analysis_repository import AnalysisRepository

SYSTEM_CHECKLIST_CURRENT_KEY = "system_checklist_current"
SYSTEM_CHECKLIST_PREVIOUS_KEY = "system_checklist_previous"
SYSTEM_CHECKLIST_DEFAULT_TTL_SECONDS = 300
DASHBOARD_SNAPSHOT_PREFIX = "dashboard_snapshot"
DASHBOARD_DEFAULT_TTL_SECONDS = 300


def _market_pattern_engine_dashboard() -> dict[str, Any]:
    try:
        repository = AnalysisRepository(config=load_engine_config())
        latest = repository.latest(limit=20)
        health = repository.health()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "latest": None, "latest_items": [], "counts": {}}
    latest_snapshot = latest[0] if latest else None
    counts = health.get("collections", {}) if isinstance(health, dict) else {}
    return {"ok": True, "error": None, "latest": latest_snapshot, "latest_items": latest, "counts": counts}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _json_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


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


def _system_report_date(config: dict[str, Any] | None = None, value: datetime | None = None) -> str:
    local_value = (value or datetime.now(timezone.utc)).astimezone(_system_timezone(config))
    if local_value.hour < 6:
        local_value -= timedelta(days=1)
    return local_value.date().isoformat()


def _local_calendar_day_bounds(config: dict[str, Any], date_key: str) -> tuple[str, str]:
    local_tz = _system_timezone(config)
    local_start = datetime.fromisoformat(str(date_key)).replace(tzinfo=local_tz)
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(timezone.utc).isoformat(), local_end.astimezone(timezone.utc).isoformat()


def _period_start(date_value: datetime, period: str) -> datetime:
    if period == "week":
        return date_value - timedelta(days=date_value.weekday())
    if period == "month":
        return date_value.replace(day=1)
    if period == "year":
        return date_value.replace(month=1, day=1)
    return date_value


def _timeframe_minutes(value: str) -> int:
    text = str(value or "").strip().lower()
    if not text:
        return 10**9
    unit = text[-1]
    number = text[:-1]
    if not number.isdigit():
        return 10**9
    amount = int(number)
    if unit == "m":
        return amount
    if unit == "h":
        return amount * 60
    if unit == "d":
        return amount * 1440
    if unit == "w":
        return amount * 10080
    return 10**9


def _configured_timeframes(config: dict[str, Any]) -> list[str]:
    strategy = config.get("strategy", {})
    primary = str(strategy.get("timeframe") or "1m")
    frames = [primary]
    confirmation = strategy.get("confirmation_timeframes", {})
    if bool(confirmation.get("enabled", True)):
        frames.extend(str(item) for item in confirmation.get("frames", []) if str(item))
    unique = []
    seen: set[str] = set()
    for frame in sorted(frames, key=_timeframe_minutes):
        if frame not in seen:
            unique.append(frame)
            seen.add(frame)
    return unique


def _flatten_scan_memory(memory: dict[str, dict[str, list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for symbol, frame_map in memory.items():
        for timeframe, entries in frame_map.items():
            for entry in entries or []:
                rows.append(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        **entry,
                    }
                )
    return rows


def _latest_scan_timeframe_context(scan_payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in scan_payload.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        for item in candidate.get("code_timeframe_analysis") or []:
            if not isinstance(item, dict):
                continue
            frame = str(item.get("timeframe") or "")
            if frame:
                grouped[frame].append(
                    {
                        "symbol": candidate.get("symbol"),
                        "side": candidate.get("side"),
                        "signal_summary": item.get("signal_summary"),
                        "trend": item.get("trend") or item.get("trend_context"),
                        "rsi": item.get("rsi"),
                    }
                )
        mini_context = candidate.get("mini_context_4h")
        if isinstance(mini_context, dict):
            grouped["4h"].append(
                {
                    "symbol": candidate.get("symbol"),
                    "side": candidate.get("side"),
                    "signal_summary": mini_context.get("signal_summary"),
                    "trend": mini_context.get("trend") or mini_context.get("trend_context"),
                    "rsi": mini_context.get("rsi"),
                }
            )
    return grouped


def _paper_trade_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = Counter(str(row.get("status") or "-") for row in rows)
    latest = rows[0] if rows else {}
    return {
        "count": len(rows),
        "status_counts": dict(status_counts),
        "latest_created_at": latest.get("created_at"),
        "latest_status": latest.get("status"),
    }


def _trade_memory_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes = Counter(str(row.get("outcome") or "-") for row in rows)
    pnl_values = [_safe_float(row.get("pnl_usdt")) for row in rows]
    return {
        "count": len(rows),
        "outcome_counts": dict(outcomes),
        "total_pnl_usdt": round(sum(pnl_values), 6),
        "average_pnl_usdt": round(_avg(pnl_values), 6) if pnl_values else 0.0,
        "latest_closed_at": next((row.get("closed_at") for row in rows if row.get("closed_at")), None),
    }


def system_checklist_history(config: dict[str, Any], *, limit: int = 30) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    rows = list_journal_state_prefix(config, "system_checklist:", limit=max(1, min(limit, 366)))
    for row in rows:
        try:
            payload = json.loads(str(row.get("value")))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        payload.setdefault("date", str(row.get("key") or "").split(":", 1)[-1])
        payload.setdefault("updated_at", row.get("updated_at"))
        items.append(payload)
    return items


def system_checklist_snapshot(config: dict[str, Any], date_key: str) -> dict[str, Any] | None:
    raw = get_journal_state(config, f"system_checklist:{date_key}")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _current_system_checklist_snapshot(config: dict[str, Any]) -> dict[str, Any] | None:
    raw = get_journal_state(config, SYSTEM_CHECKLIST_CURRENT_KEY)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _latest_system_checklist_snapshot(config: dict[str, Any]) -> dict[str, Any] | None:
    current = _current_system_checklist_snapshot(config)
    if current is not None:
        return current
    history = system_checklist_history(config, limit=1)
    latest = history[0] if history else None
    return latest if isinstance(latest, dict) else None


def _preferred_system_checklist_snapshot(config: dict[str, Any], date_key: str) -> dict[str, Any] | None:
    current = _current_system_checklist_snapshot(config)
    if isinstance(current, dict) and str(current.get("date") or "") == date_key:
        return current
    dated = system_checklist_snapshot(config, date_key)
    if isinstance(dated, dict):
        return dated
    return None


def _snapshot_age_seconds(payload: dict[str, Any]) -> float | None:
    created_at = _parse_time(payload.get("created_at"))
    if created_at is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - created_at).total_seconds())


def _cache_token(value: Any) -> str:
    text = str(value)
    return "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in text)


def _dashboard_snapshot_key(config: dict[str, Any], name: str, **params: Any) -> str:
    params = {"v": dashboard_snapshot_cache_version(config), **params}
    if not params:
        return f"{DASHBOARD_SNAPSHOT_PREFIX}:{name}"
    suffix = ";".join(f"{_cache_token(key)}={_cache_token(value)}" for key, value in sorted(params.items()))
    return f"{DASHBOARD_SNAPSHOT_PREFIX}:{name}:{suffix}"


def _cached_payload(config: dict[str, Any], key: str) -> dict[str, Any] | None:
    raw = get_journal_state(config, key)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _persist_cached_payload(config: dict[str, Any], key: str, payload: dict[str, Any]) -> None:
    set_journal_state(config, key, json.dumps(to_jsonable(payload), ensure_ascii=False))


def _get_or_build_cached_payload(
    config: dict[str, Any],
    *,
    key: str,
    builder: Callable[[], dict[str, Any]],
    force_refresh: bool = False,
    max_age_seconds: int | None = DASHBOARD_DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    if not force_refresh:
        snapshot = _cached_payload(config, key)
        if snapshot is not None:
            age_seconds = _snapshot_age_seconds(snapshot)
            if max_age_seconds is None or age_seconds is None or age_seconds <= max(0, int(max_age_seconds)):
                return snapshot
    payload = builder()
    _persist_cached_payload(config, key, payload)
    return payload


def _persist_system_checklist_snapshot(config: dict[str, Any], payload: dict[str, Any]) -> None:
    clean_payload = to_jsonable(_strip_system_checklist_comparison_snapshot(payload))
    current = _current_system_checklist_snapshot(config)
    current_clean = (
        to_jsonable(_strip_system_checklist_comparison_snapshot(current))
        if isinstance(current, dict)
        else None
    )
    current_created_at = str((current_clean or {}).get("created_at") or "")
    next_created_at = str(clean_payload.get("created_at") or "")
    if current_clean and (current_created_at != next_created_at or current_clean != clean_payload):
        set_journal_state(config, SYSTEM_CHECKLIST_PREVIOUS_KEY, json.dumps(current_clean, ensure_ascii=False))
    body = json.dumps(clean_payload, ensure_ascii=False)
    set_journal_state(config, SYSTEM_CHECKLIST_CURRENT_KEY, body)
    set_journal_state(config, f"system_checklist:{clean_payload['date']}", body)


def _strip_system_checklist_comparison_snapshot(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    clean_payload = dict(payload)
    clean_payload.pop("previous_snapshot", None)
    return clean_payload


def _raw_previous_system_checklist_snapshot(config: dict[str, Any]) -> dict[str, Any] | None:
    raw = get_journal_state(config, SYSTEM_CHECKLIST_PREVIOUS_KEY)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _fallback_previous_system_checklist_snapshot(
    config: dict[str, Any],
    current_payload: dict[str, Any],
) -> dict[str, Any] | None:
    current_date = str(current_payload.get("date") or "")
    current_created_at = str(current_payload.get("created_at") or "")
    history = system_checklist_history(config, limit=366)
    for item in history:
        if not isinstance(item, dict):
            continue
        item_date = str(item.get("date") or "")
        item_created_at = str(item.get("created_at") or "")
        if current_created_at and item_created_at == current_created_at:
            continue
        if current_date and item_date == current_date:
            continue
        return _strip_system_checklist_comparison_snapshot(item)
    return None


def attach_previous_system_checklist_snapshot(
    config: dict[str, Any],
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    clean_payload = _strip_system_checklist_comparison_snapshot(payload)
    if not clean_payload:
        return {}
    previous = _raw_previous_system_checklist_snapshot(config)
    previous_clean = _strip_system_checklist_comparison_snapshot(previous) if isinstance(previous, dict) else None
    current_created_at = str(clean_payload.get("created_at") or "")
    if previous_clean and str(previous_clean.get("created_at") or "") == current_created_at:
        previous_clean = None
    if previous_clean is None:
        previous_clean = _fallback_previous_system_checklist_snapshot(config, clean_payload)
    enriched = dict(clean_payload)
    enriched["previous_snapshot"] = previous_clean
    return enriched


def system_checklist_summary(config: dict[str, Any], period: str) -> dict[str, Any]:
    period = period if period in {"week", "month", "year"} else "week"
    today = datetime.fromisoformat(_system_report_date(config))
    start = _period_start(today, period).date().isoformat()
    end = today.date().isoformat()
    snapshots = [
        item for item in system_checklist_history(config, limit=366)
        if start <= str(item.get("date") or "") <= end
    ]
    status_counts = {"ok": 0, "warn": 0, "fail": 0}
    module_status_counts = {"ok": 0, "warn": 0, "fail": 0}
    for snapshot in snapshots:
        for item in snapshot.get("criteria") or snapshot.get("items") or []:
            status = str((item or {}).get("status") or "fail")
            status_counts[status if status in status_counts else "fail"] += 1
        for item in snapshot.get("modules") or []:
            status = str((item or {}).get("status") or "fail")
            module_status_counts[status if status in module_status_counts else "fail"] += 1
    return {
        "period": period,
        "start": start,
        "end": end,
        "snapshot_count": len(snapshots),
        "status_counts": status_counts,
        "module_status_counts": module_status_counts,
        "dates": [item.get("date") for item in snapshots],
    }


def _evidence_line(label: str, value: Any) -> dict[str, str]:
    return {"label": label, "value": "-" if value is None else str(value)}


def _check_item(
    name: str,
    ok: bool,
    detail: str,
    *,
    required: bool = False,
    warning: bool = False,
    evidence: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    status = "ok" if ok else "warn" if warning and not required else "fail"
    return {
        "name": name,
        "target": "✅ Bắt buộc" if required else "✅",
        "status": status,
        "ok": ok,
        "required": required,
        "detail": detail,
        "evidence": evidence or [],
    }


def _module_file_index(config: dict[str, Any]) -> dict[int, dict[str, Any]]:
    module_dir = project_path(config, "module")
    files: dict[int, dict[str, Any]] = {}
    if not module_dir.exists():
        return files
    for path in sorted(module_dir.rglob("*.txt")):
        match = re.match(r"\s*(\d+)", path.name)
        if not match:
            continue
        number = int(match.group(1))
        try:
            stat = path.stat()
            preview = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[:3]
        except OSError:
            stat = None
            preview = []
        relative_path = str(path.relative_to(module_dir))
        files[number] = {
            "file_name": path.name,
            "folder": path.parent.name,
            "relative_path": relative_path,
            "path": str(path),
            "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat() if stat else None,
            "size_bytes": stat.st_size if stat else None,
            "preview": [line.strip() for line in preview if line.strip()],
        }
    return files


def _module_row(label: str, value: Any, meaning: str, *, attention: bool = False) -> dict[str, Any]:
    return {
        "label": label,
        "value": "-" if value is None else value,
        "meaning": meaning,
        "attention": attention,
    }


def _module_bool_percent(value: Any) -> float:
    return 100.0 if bool(value) else 0.0


def _module_percent(part: Any, total: Any) -> float | None:
    whole = _safe_float(total)
    if whole <= 0:
        return None
    return round(_safe_float(part) / whole * 100.0, 2)


def _module_minutes_until(value: Any) -> float | None:
    target = _parse_time(value)
    if target is None:
        return None
    return round(max(0.0, (target - datetime.now(timezone.utc)).total_seconds() / 60.0), 2)


def _module_file_payload(file_info: dict[str, Any] | None) -> dict[str, Any] | None:
    return file_info or None


def _load_sizing_runtime_state(config: dict[str, Any]) -> dict[str, Any] | None:
    try:
        raw = get_journal_state(config, SIZING_STATE_KEY)
    except Exception:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _trade_execution_price(row: dict[str, Any], key: str) -> float | None:
    value = row.get(key)
    if value is None and key == "entry_price":
        value = row.get("entry")
    number = _safe_float(value, float("nan"))
    return None if number != number else number


def _trade_execution_mark_price(row: dict[str, Any]) -> float | None:
    direct = _trade_execution_price(row, "last_price") or _trade_execution_price(row, "mark_price")
    if direct is not None:
        return direct
    for key in ("snapshot_json", "payload_json"):
        raw = row.get(key)
        if not raw:
            continue
        try:
            payload = json.loads(str(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        position = payload.get("position") if isinstance(payload, dict) else None
        if not isinstance(position, dict):
            continue
        info = position.get("info") if isinstance(position.get("info"), dict) else {}
        for price_key in ("markPrice", "lastPrice", "last"):
            number = _safe_float(position.get(price_key), float("nan"))
            if number == number:
                return number
        for price_key in ("markPx", "last"):
            number = _safe_float(info.get(price_key), float("nan"))
            if number == number:
                return number
    return None


def _trade_execution_progress(row: dict[str, Any]) -> float | None:
    side = str(row.get("side") or "").lower()
    entry = _trade_execution_price(row, "initial_entry_price") or _trade_execution_price(row, "entry_price")
    take_profit = _trade_execution_price(row, "take_profit")
    mark = _trade_execution_mark_price(row)
    if entry is None or take_profit is None or mark != mark:
        return None
    reward = take_profit - entry if side == "long" else entry - take_profit
    if reward <= 0:
        return None
    gained = mark - entry if side == "long" else entry - mark
    return round(gained / reward * 100, 2)


def _trade_execution_r_multiple(row: dict[str, Any]) -> float | None:
    side = str(row.get("side") or "").lower()
    entry = _trade_execution_price(row, "initial_entry_price") or _trade_execution_price(row, "entry_price")
    initial_sl = _trade_execution_price(row, "initial_stop_loss") or _trade_execution_price(row, "stop_loss")
    mark = _trade_execution_mark_price(row)
    if entry is None or initial_sl is None or mark != mark:
        return None
    risk = entry - initial_sl if side == "long" else initial_sl - entry
    if risk <= 0:
        return None
    gained = mark - entry if side == "long" else entry - mark
    return round(gained / risk, 4)


def _trade_execution_closed_pnl(row: dict[str, Any]) -> float | None:
    history = _json_payload(row.get("exchange_close_history_json"))
    info = history.get("info") if isinstance(history.get("info"), dict) else {}
    for key in ("realizedPnl", "realisedPnl", "netPnl", "netProfit"):
        value = _safe_float(history.get(key), float("nan"))
        if value == value:
            return round(value, 6)
    for key in ("realizedPnl", "realisedPnl", "netPnl", "netProfit"):
        value = _safe_float(info.get(key), float("nan"))
        if value == value:
            return round(value, 6)
    pnl = _safe_float(history.get("pnl"), float("nan"))
    if pnl != pnl:
        pnl = _safe_float(info.get("pnl"), float("nan"))
    if pnl != pnl:
        value = _safe_float(row.get("pnl"), float("nan"))
        return None if value != value else value
    adjustments = 0.0
    adjusted = False
    for payload in (history, info):
        for key in ("fee", "fundingFee", "funding", "settledPnl"):
            value = _safe_float(payload.get(key), float("nan"))
            if value != value:
                continue
            adjustments += value
            adjusted = True
    return round(pnl + adjustments, 6) if adjusted else pnl


def _trade_execution_summary(config: dict[str, Any]) -> dict[str, Any]:
    try:
        open_rows = list_trade_execution_rows(config, statuses=["OPEN"], limit=20, order="created_asc")
        closed_rows = list_trade_execution_rows(config, statuses=["WIN", "LOSS", "BREAKEVEN", "CLOSED", "RECONCILED"], limit=20, order="closed_desc")
        error = None
    except Exception as exc:
        open_rows = []
        closed_rows = []
        error = str(exc)
    trailing = config.get("trailing_stop", {}) if isinstance(config.get("trailing_stop"), dict) else {}
    partial = trailing.get("partial_take_profit", {}) if isinstance(trailing.get("partial_take_profit"), dict) else {}
    open_items: list[dict[str, Any]] = []
    for row in open_rows:
        open_items.append(
            {
                "id": row.get("id"),
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "entry_price": row.get("entry_price"),
                "initial_entry_price": row.get("initial_entry_price"),
                "initial_stop_loss": row.get("initial_stop_loss"),
                "stop_loss": row.get("stop_loss"),
                "take_profit": row.get("take_profit"),
                "mark_price": _trade_execution_mark_price(row),
                "pnl": row.get("pnl"),
                "pnl_pct": row.get("pnl_pct"),
                "partial_take_profit_done": bool(row.get("partial_take_profit_done")),
                "partial_take_profit_at": row.get("partial_take_profit_at"),
                "partial_take_profit_price": row.get("partial_take_profit_price"),
                "partial_take_profit_fraction": row.get("partial_take_profit_fraction"),
                "partial_take_profit_amount": row.get("partial_take_profit_amount"),
                "partial_take_profit_original_tp": row.get("partial_take_profit_original_tp"),
                "partial_take_profit_extended_tp": row.get("partial_take_profit_extended_tp"),
                "trailing_stop_updated_at": row.get("trailing_stop_updated_at"),
                "trailing_stop_r_multiple": row.get("trailing_stop_r_multiple"),
                "trailing_stop_atr": row.get("trailing_stop_atr"),
                "tp_progress_pct": _trade_execution_progress(row),
                "r_multiple": _trade_execution_r_multiple(row),
            }
        )
    closed_items = [
        {
            "id": row.get("id"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "status": row.get("status"),
            "close_reason": row.get("close_reason"),
            "closed_at": row.get("closed_at"),
            "entry_price": row.get("entry_price"),
            "stop_loss": row.get("stop_loss"),
            "take_profit": row.get("take_profit"),
            "pnl": _trade_execution_closed_pnl(row),
            "pnl_pct": row.get("pnl_pct"),
            "exchange_close_source": row.get("exchange_close_source"),
            "partial_take_profit_done": bool(row.get("partial_take_profit_done")),
        }
        for row in closed_rows
    ]
    return {
        "ok": error is None,
        "error": error,
        "open_count": len(open_items),
        "closed_count": len(closed_items),
        "partial_done_count": sum(1 for row in open_items if row.get("partial_take_profit_done")),
        "waiting_partial_count": sum(1 for row in open_items if not row.get("partial_take_profit_done")),
        "open_items": open_items,
        "recent_closed": closed_items,
        "trailing_config": {
            "enabled": bool(trailing.get("enabled")),
            "atr_timeframe": trailing.get("atr_timeframe"),
            "atr_period": trailing.get("atr_period"),
            "atr_multiplier": trailing.get("atr_multiplier"),
            "activation_r_multiple": trailing.get("activation_r_multiple"),
            "partial_enabled": bool(partial.get("enabled")),
            "partial_trigger_tp_progress": partial.get("trigger_tp_progress"),
            "partial_close_fraction": partial.get("close_fraction"),
            "remaining_sl_buffer_r": partial.get("remaining_sl_buffer_r"),
            "tp_extension_fraction": partial.get("tp_extension_fraction"),
        },
    }


def _default_automation_status(config: dict[str, Any]) -> dict[str, Any]:
    automation = config.get("automation", {})
    fallback = config.get("paper_trading", {}).get("scan_interval_seconds", 60)
    interval = max(60, int(automation.get("scan_interval_seconds", fallback) or 60))
    return {
        "enabled": bool(automation.get("enabled", True)),
        "interval_seconds": interval,
        "mode": config.get("mode", "dry_run"),
    }


def _normalize_ai_decision_range(value: Any) -> str:
    return "all" if str(value or "").strip().lower() == "all" else "current"


def _market_regime_snapshot_symbol(snapshot: dict[str, Any] | None) -> str:
    if not isinstance(snapshot, dict):
        return ""
    indicators = snapshot.get("indicators") if isinstance(snapshot.get("indicators"), dict) else {}
    return str(snapshot.get("symbol") or indicators.get("symbol") or "").strip()


def _market_regime_snapshot_scope(snapshot: dict[str, Any] | None) -> str:
    if not isinstance(snapshot, dict):
        return ""
    indicators = snapshot.get("indicators") if isinstance(snapshot.get("indicators"), dict) else {}
    return str(snapshot.get("scope") or indicators.get("scope") or "").strip().lower()


def _market_regime_history_items(
    config: dict[str, Any],
    *,
    limit: int = 30,
    symbol: str = "",
) -> list[dict[str, Any]]:
    try:
        rows = market_regime_history(config, limit=max(limit, 100) if symbol else limit)
    except Exception:
        return []
    if symbol:
        rows = [item for item in rows if _market_regime_snapshot_symbol(item) == symbol]
    return rows[:limit]


def _market_regime_history_payload(
    config: dict[str, Any],
    *,
    limit: int = 30,
    current_regime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    detail_symbols = _market_regime_top_symbols(config)
    aggregate_limit = _market_regime_aggregate_limit(config)
    read_limit = max(200, limit * max(4, len(detail_symbols) + 3))
    try:
        rows = market_regime_history(config, limit=read_limit)
    except Exception:
        rows = []
    aggregate_items: list[dict[str, Any]] = []
    by_symbol: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in detail_symbols}
    for item in rows:
        scope = _market_regime_snapshot_scope(item)
        symbol = _market_regime_snapshot_symbol(item)
        if scope == "aggregate" or symbol == "MARKET":
            aggregate_items.append(item)
            continue
        if symbol in by_symbol:
            by_symbol[symbol].append(item)
    if (
        not aggregate_items
        and isinstance(current_regime, dict)
        and (_market_regime_snapshot_scope(current_regime) == "aggregate" or _market_regime_snapshot_symbol(current_regime) == "MARKET")
        and current_regime.get("created_at")
    ):
        aggregate_items.append(current_regime)
    symbol_payload = {
        symbol: {
            "label": symbol.split("/", 1)[0],
            "items": items[:limit],
            "count": len(items[:limit]),
            "latest_created_at": items[0].get("created_at") if items else None,
        }
        for symbol, items in by_symbol.items()
    }
    fallback_symbol = _market_regime_snapshot_symbol(current_regime) if isinstance(current_regime, dict) else ""
    fallback_items = by_symbol.get(fallback_symbol, [])[:limit] if fallback_symbol in by_symbol else []
    active_items = aggregate_items[:limit] if aggregate_items else fallback_items
    latest_aggregate = aggregate_items[0] if aggregate_items else None
    aggregate_indicators = latest_aggregate.get("indicators") if isinstance(latest_aggregate, dict) and isinstance(latest_aggregate.get("indicators"), dict) else {}
    coverage_count = aggregate_indicators.get("coverage_count")
    target_count = aggregate_indicators.get("target_count") or aggregate_limit
    return {
        "items": active_items,
        "aggregate": {
            "label": "Thị trường",
            "items": aggregate_items[:limit],
            "count": len(aggregate_items[:limit]),
            "latest_created_at": latest_aggregate.get("created_at") if latest_aggregate else None,
        },
        "top_symbols": detail_symbols,
        "detail_symbols": detail_symbols,
        "aggregate_limit": target_count,
        "market_symbols": aggregate_indicators.get("market_symbols") or aggregate_indicators.get("top_symbols") or [],
        "by_symbol": symbol_payload,
        "coverage": {
            "coverage_count": coverage_count,
            "target_count": target_count,
            "covered_symbols": aggregate_indicators.get("covered_symbols") or [],
            "missing_symbols": aggregate_indicators.get("missing_symbols") or [
                symbol for symbol in detail_symbols if not by_symbol.get(symbol)
            ],
            "coverage_pct": aggregate_indicators.get("coverage_pct"),
        },
    }


def system_modules_payload(
    config: dict[str, Any],
    *,
    checked_date: str,
    checked_at_iso: str,
    ai_history: list[dict[str, Any]],
    replay: dict[str, Any],
    strategy: dict[str, Any],
    regime: dict[str, Any],
    health: dict[str, Any],
    risk_state: dict[str, Any],
    row_counts: dict[str, Any] | None = None,
    ai_range: str = "current",
    regime_history_items: list[dict[str, Any]] | None = None,
    regime_history_payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    files = _module_file_index(config)
    ai_range_key = _normalize_ai_decision_range(ai_range)
    ai_range_label = "Toàn bộ dữ liệu đang lưu" if ai_range_key == "all" else "Hôm nay"
    active_strategies = strategy.get("active") or []
    latest_ai = ai_history[-1] if ai_history else {}
    latest_ai_status = latest_ai.get("status") or latest_ai.get("result")
    latest_ai_time = latest_ai.get("created_at") or latest_ai.get("updated_at")

    try:
        if ai_range_key == "all":
            decision_created_from = None
            decision_created_to = checked_at_iso
            trade_decision_stats = ai_trade_decision_stats(config)
            ai_call_stats = ai_call_decision_stats(config)
        else:
            decision_created_from, decision_created_to = _local_calendar_day_bounds(config, checked_date)
            trade_decision_stats = ai_trade_decision_stats(
                config,
                created_from=decision_created_from,
                created_to=decision_created_to,
            )
            ai_call_stats = ai_call_decision_stats(
                config,
                created_from=decision_created_from,
                created_to=decision_created_to,
            )
        decision_stats = {
            **trade_decision_stats,
            **ai_call_stats,
            "tradeSignalRecords": trade_decision_stats.get("totalRecords"),
        }
        decision_stats["periodStart"] = decision_created_from
        decision_stats["periodEnd"] = decision_created_to
        decision_stats["range"] = ai_range_key
        decision_stats["rangeLabel"] = ai_range_label
    except Exception as exc:
        decision_stats = {"error": str(exc)}

    try:
        prompt_runtime = prompt_status(config)
    except Exception as exc:
        prompt_runtime = {"error": str(exc)}
    prompt_metrics = prompt_runtime.get("metrics") if isinstance(prompt_runtime.get("metrics"), dict) else {}

    if regime_history_items is None:
        regime_history_payload = regime_history_payload or _market_regime_history_payload(
            config,
            limit=30,
            current_regime=regime,
        )
        regime_history_items = list(regime_history_payload.get("items") or [])
    if regime_history_payload is None:
        detail_symbols = _market_regime_top_symbols(config)
        aggregate_limit = _market_regime_aggregate_limit(config)
        regime_history_payload = {
            "items": regime_history_items,
            "aggregate": {"label": "Thị trường", "items": [], "count": 0, "latest_created_at": None},
            "top_symbols": detail_symbols,
            "detail_symbols": detail_symbols,
            "aggregate_limit": aggregate_limit,
            "market_symbols": [],
            "by_symbol": {},
            "coverage": {"coverage_count": None, "target_count": aggregate_limit},
        }
    regime_counts: dict[str, int] = {}
    for item in regime_history_items:
        name = str(item.get("regime") or "UNKNOWN").upper()
        regime_counts[name] = regime_counts.get(name, 0) + 1
    regime_history_total = len(regime_history_items)

    strategy_performance = [
        item.get("performance")
        for item in active_strategies
        if isinstance(item.get("performance"), dict)
    ]
    strategy_total_trades = sum(_safe_int(item.get("totalTrades")) for item in strategy_performance)
    strategy_total_pnl = round(sum(_safe_float(item.get("totalPnl")) for item in strategy_performance), 6)
    strategy_avg_win_rate = round(
        sum(_safe_float(item.get("winRate")) for item in strategy_performance) / len(strategy_performance),
        2,
    ) if strategy_performance else None
    strategy_avg_profit_factor = round(
        sum(_safe_float(item.get("profitFactor")) for item in strategy_performance) / len(strategy_performance),
        2,
    ) if strategy_performance else None
    strategy_avg_drawdown = round(
        sum(_safe_float(item.get("drawdown")) for item in strategy_performance) / len(strategy_performance),
        2,
    ) if strategy_performance else None
    strategy_avg_confidence = round(
        sum(_safe_float(item.get("averageConfidence")) for item in strategy_performance) / len(strategy_performance),
        2,
    ) if strategy_performance else None
    strategy_avg_hold_minutes = round(
        sum(_safe_float(item.get("averageHoldMinutes")) for item in strategy_performance) / len(strategy_performance),
        2,
    ) if strategy_performance else None

    sizing_state = _load_sizing_runtime_state(config)
    open_count = _safe_int(risk_state.get("openPositionsCount"))
    max_positions = _safe_int(risk_state.get("maxConcurrentPositions"))
    slot_utilization = _module_percent(open_count, max_positions)
    paused_minutes = _module_minutes_until(risk_state.get("pausedUntil"))
    prompt_updated_at = prompt_metrics.get("updated_at") if prompt_metrics else None
    processed_keys = sizing_state.get("processed_keys") if isinstance(sizing_state, dict) else []
    row_counts = row_counts or {}
    recovery_blocked = bool(sizing_state and sizing_state.get("blocked"))
    recovery_runtime_records = sum(
        _safe_int(row_counts.get(key))
        for key in ("trade_executions", "pending_orders", "internal_pending_orders", "paper_trades", "trade_memory")
    )
    recovery_orphaned_block = recovery_blocked and open_count <= 0 and recovery_runtime_records <= 0
    recovery_status = "warn" if recovery_orphaned_block else "fail" if recovery_blocked else "ok" if sizing_state else "warn"
    try:
        capital_snapshot = latest_capital_snapshot(config) or build_capital_snapshot(config, use_cache=True)
    except Exception as exc:
        capital_snapshot = {"ok": False, "error": str(exc)}
    capital_mode = infer_capital_mode(health=health, risk_state=risk_state, sizing_state=sizing_state or {})
    try:
        capital_reserve_state = latest_capital_reserve_state(config) or calculate_capital_reserve_state(
            config,
            mode=capital_mode,
            snapshot=capital_snapshot if isinstance(capital_snapshot, dict) else None,
        )
    except Exception as exc:
        capital_reserve_state = {"ok": False, "mode": capital_mode, "reason": str(exc)}
    try:
        capital_allocation_check = check_capital_allocation(
            config,
            config.get("position_sizing", {}).get("base_margin_usdt", 0),
            mode=capital_mode,
        )
    except Exception as exc:
        capital_allocation_check = {"allowed": False, "reason": str(exc)}
    try:
        capital_position_size = calculate_position_size(
            config,
            {
                "symbol": "CHECK/USDT:USDT",
                "side": "LONG",
                "mode": capital_mode,
                "leverage": config.get("exchange", {}).get("leverage", 1),
            },
        )
    except Exception as exc:
        capital_position_size = {"allowed": False, "reason": str(exc)}
    try:
        config_impact = analyze_configuration_change(config, {})
    except Exception as exc:
        config_impact = {"risk_level": "UNKNOWN", "is_safe": False, "summary": str(exc), "warnings": [str(exc)]}
    capital_snapshot_ok = bool(capital_snapshot.get("ok", True)) and capital_snapshot.get("realized_capital") is not None
    capital_reserve_ok = bool(capital_reserve_state.get("ok", True)) and capital_reserve_state.get("realized_capital") is not None
    capital_position_ok = bool(capital_position_size.get("allowed"))
    config_impact_safe = bool(config_impact.get("is_safe")) and str(config_impact.get("risk_level") or "").upper() not in {"HIGH", "CRITICAL"}
    market_pattern = _market_pattern_engine_dashboard()
    market_pattern_latest = market_pattern.get("latest") or {}
    market_pattern_counts = market_pattern.get("counts") or {}
    market_pattern_structure = market_pattern_latest.get("market_structure") if isinstance(market_pattern_latest, dict) else {}
    market_pattern_confluence = market_pattern_latest.get("confluence") if isinstance(market_pattern_latest, dict) else {}
    market_pattern_feature = market_pattern_latest.get("feature_vector") if isinstance(market_pattern_latest, dict) else {}
    trade_execution = _trade_execution_summary(config)
    trade_config = trade_execution.get("trailing_config") or {}

    definitions = [
        {
            "number": 1,
            "name": "Bộ nhớ quyết định AI",
            "purpose": "Thống kê quyết định AI giao dịch đã được hệ thống ghi nhận thực tế.",
            "status": "fail" if "error" in str(latest_ai_status or "").lower() else "ok" if _safe_int(decision_stats.get("totalDecisions")) > 0 else "warn",
            "ai_range": ai_range_key,
            "ai_range_label": ai_range_label,
            "stats": [
                _module_row("Ngày kiểm tra", checked_date, "Ngày local của lần tổng hợp dữ liệu module."),
                _module_row("Cập nhật lúc", checked_at_iso, "Dấu thời gian UTC của payload hiện tại."),
                _module_row("Số lần gọi AI gần nhất", len(ai_history), "Số lần gọi AI đã được nhật ký lưu gần nhất."),
                _module_row("Phạm vi dữ liệu AI", ai_range_label, "Phạm vi đang được dùng để tính các biến AI trong module."),
                _module_row("total_decisions", decision_stats.get("totalDecisions"), "Tổng lần gọi AI thật theo phạm vi đang chọn: Mini + 5.5; không tính các record scan nội bộ.", attention=True),
                _module_row("Tổng log gọi AI trong phạm vi", decision_stats.get("totalRecords"), "Tổng số log gọi AI thật theo phạm vi đang kiểm tra."),
                _module_row("mini_no_trade_count", decision_stats.get("miniNoTradeCount"), "Số lần Mini được gọi nhưng trả NO_TRADE hoặc không chọn cặp nào."),
                _module_row("long_count", decision_stats.get("longCount"), "Số quyết định vào lệnh LONG đã được ghi nhận."),
                _module_row("short_count", decision_stats.get("shortCount"), "Số quyết định vào lệnh SHORT đã được ghi nhận."),
                _module_row("no_trade_count", decision_stats.get("noTradeCount"), "Số lần GPT-5.5 từ chối vào lệnh hoặc xóa setup; không tính các lần giữ setup."),
                _module_row("long_percent", decision_stats.get("longPercent"), "Tỷ trọng LONG trên tổng quyết định AI thực.", attention=True),
                _module_row("short_percent", decision_stats.get("shortPercent"), "Tỷ trọng SHORT trên tổng quyết định AI thực.", attention=True),
                _module_row("winrate_long", decision_stats.get("winrateLong"), "Tỷ lệ thắng của nhóm quyết định LONG đã đóng lệnh.", attention=True),
                _module_row("winrate_short", decision_stats.get("winrateShort"), "Tỷ lệ thắng của nhóm quyết định SHORT đã đóng lệnh.", attention=True),
                _module_row("avg_confidence_long", decision_stats.get("avgConfidenceLong"), "Độ tự tin trung bình của các quyết định LONG."),
                _module_row("avg_confidence_short", decision_stats.get("avgConfidenceShort"), "Độ tự tin trung bình của các quyết định SHORT."),
                _module_row("profit_factor_long", decision_stats.get("profitFactorLong"), "Hệ số lợi nhuận của các quyết định LONG đã chốt."),
                _module_row("profit_factor_short", decision_stats.get("profitFactorShort"), "Hệ số lợi nhuận của các quyết định SHORT đã chốt."),
                _module_row("Trạng thái AI gần nhất", latest_ai_status, "Trạng thái của lần gọi AI gần nhất được nhật ký lưu.", attention=True),
                _module_row("Ghi nhận AI lúc", latest_ai_time, "Thời điểm lần gọi AI gần nhất được ghi nhận."),
                _module_row("bias_warning", decision_stats.get("biasWarning"), "Cảnh báo lệch hướng LONG/SHORT nếu có."),
            ],
        },
        {
            "number": 2,
            "name": "Bunny Minimize Losses",
            "purpose": "Theo dõi chuỗi thua, recovery mode, pause và trạng thái slot thực tế của hệ thống.",
            "status": "fail" if risk_state.get("isPaused") else "warn" if risk_state.get("isRecoveryMode") else "ok",
            "recovery_mode": risk_state.get("recoveryMode") or ("HARD_RECOVERY" if risk_state.get("isRecoveryMode") else "NORMAL"),
            "stats": [
                _module_row("recoveryMode", risk_state.get("recoveryMode") or ("HARD_RECOVERY" if risk_state.get("isRecoveryMode") else "NORMAL"), "Mode hiện tại của Bunny Minimize Losses.", attention=True),
                _module_row("isRecoveryMode", _module_bool_percent(risk_state.get("isRecoveryMode")), "100 nghĩa là hệ thống đang ở recovery mode.", attention=True),
                _module_row("isPaused", _module_bool_percent(risk_state.get("isPaused")), "100 nghĩa là hệ thống đang pause, không nên mở lệnh mới.", attention=True),
                _module_row("globalLossStreak", risk_state.get("globalLossStreak"), "Chuỗi thua hiện tại của toàn hệ thống.", attention=True),
                _module_row("globalLossStreakThreshold", risk_state.get("globalLossStreakThreshold"), "Ngưỡng chuỗi thua để bật recovery mode."),
                _module_row("pauseTradingLossStreak", risk_state.get("pauseTradingLossStreak"), "Ngưỡng chuỗi thua để pause toàn hệ thống."),
                _module_row("openPositionsCount", risk_state.get("openPositionsCount"), "Số vị thế đang mở tại thời điểm kiểm tra.", attention=True),
                _module_row("maxConcurrentPositions", risk_state.get("maxConcurrentPositions"), "Số slot vị thế tối đa được phép chạy song song."),
                _module_row("slotUtilizationPercent", slot_utilization, "Mức sử dụng slot vị thế hiện tại theo phần trăm."),
                _module_row("pausedMinutesRemaining", paused_minutes, "Số phút còn lại trước khi trạng thái pause tự hết."),
                _module_row("normalRiskPercent", risk_state.get("normalRiskPercent"), "Phần trăm rủi ro áp dụng trong normal mode."),
                _module_row("softRecoveryRiskPercent", risk_state.get("softRecoveryRiskPercent"), "Phần trăm rủi ro áp dụng trong soft recovery."),
                _module_row("recoveryModeRiskPercent", risk_state.get("recoveryModeRiskPercent"), "Phần trăm rủi ro áp dụng trong recovery mode."),
                _module_row("currentNormalMinRuleScore", risk_state.get("currentNormalMinRuleScore"), "Ngưỡng rule score động hiện hành sau khi hệ thống tự điều chỉnh."),
                _module_row("currentNormalMinGptConfidence", risk_state.get("currentNormalMinGptConfidence"), "Ngưỡng GPT confidence động hiện hành sau khi hệ thống tự điều chỉnh."),
                _module_row("normalMinRiskReward", risk_state.get("normalMinRiskReward"), "Ngưỡng risk reward tối thiểu trong normal mode."),
                _module_row("softRecoveryMinRuleScore", risk_state.get("softRecoveryMinRuleScore"), "Ngưỡng rule score tối thiểu trong soft recovery."),
                _module_row("softRecoveryMinGptConfidence", risk_state.get("softRecoveryMinGptConfidence"), "Ngưỡng GPT confidence tối thiểu trong soft recovery."),
                _module_row("softRecoveryMinRiskReward", risk_state.get("softRecoveryMinRiskReward"), "Ngưỡng risk reward tối thiểu trong soft recovery."),
                _module_row("recoveryMinRuleScore", risk_state.get("recoveryMinRuleScore"), "Ngưỡng rule score tối thiểu trong recovery mode."),
                _module_row("recoveryMinGptConfidence", risk_state.get("recoveryMinGptConfidence"), "Ngưỡng GPT confidence tối thiểu trong recovery mode."),
                _module_row("recoveryMinRiskReward", risk_state.get("recoveryMinRiskReward"), "Ngưỡng risk reward tối thiểu trong recovery mode."),
                _module_row("strongSetupRuleScore", risk_state.get("strongSetupRuleScore"), "Ngưỡng rule score để gắn nhãn strong setup."),
                _module_row("strongSetupGptConfidence", risk_state.get("strongSetupGptConfidence"), "Ngưỡng GPT confidence để gắn nhãn strong setup."),
                _module_row("strongSetupMinRiskReward", risk_state.get("strongSetupMinRiskReward"), "Ngưỡng risk reward để gắn nhãn strong setup."),
                _module_row("enableAdaptiveThreshold", _module_bool_percent(risk_state.get("enableAdaptiveThreshold")), "100 nghĩa là adaptive threshold đang bật."),
                _module_row("weeklyTargetMinTrades", risk_state.get("weeklyTargetMinTrades"), "Số lệnh đóng tối thiểu mục tiêu trong 7 ngày."),
                _module_row("weeklyTargetMaxTrades", risk_state.get("weeklyTargetMaxTrades"), "Số lệnh đóng tối đa mục tiêu trong 7 ngày."),
                _module_row("adaptiveScoreStep", risk_state.get("adaptiveScoreStep"), "Mức tăng/giảm rule score khi adaptive threshold điều chỉnh."),
                _module_row("adaptiveConfidenceStep", risk_state.get("adaptiveConfidenceStep"), "Mức tăng/giảm GPT confidence khi adaptive threshold điều chỉnh."),
                _module_row("recoveryCyclePnlUsdt", risk_state.get("recoveryCyclePnlUsdt"), "PnL cycle dùng để phân biệt Soft Recovery và Normal.", attention=True),
                _module_row("updatedAt", risk_state.get("updatedAt"), "Thời điểm trading system state được refresh gần nhất."),
            ],
        },
        {
            "number": 3,
            "name": "Bunny Health Monitor",
            "purpose": "Theo dõi win rate, profit factor, drawdown và cảnh báo sức khỏe từ dữ liệu lệnh thực tế.",
            "status": "fail" if health.get("isCritical") else "warn" if health.get("isWarning") else "ok",
            "stats": [
                _module_row("minimumTradesForEvaluation", health.get("minimumTradesForEvaluation"), "Số lệnh đóng tối thiểu trước khi Health Monitor được phép cảnh báo hoặc pause."),
                _module_row("totalTrades", health.get("totalTrades"), "Số lệnh đã đóng được dùng để tính health."),
                _module_row("winCount", health.get("winCount"), "Số lệnh thắng trong cửa sổ health hiện tại."),
                _module_row("lossCount", health.get("lossCount"), "Số lệnh thua trong cửa sổ health hiện tại.", attention=True),
                _module_row("breakevenCount", health.get("breakevenCount"), "Số lệnh hòa vốn trong cửa sổ health hiện tại."),
                _module_row("winRate", health.get("winRate"), "Win rate thực tế đang được monitor.", attention=True),
                _module_row("profitFactor", health.get("profitFactor"), "Tỷ lệ gross profit / gross loss của tập lệnh health.", attention=True),
                _module_row("totalPnl", health.get("totalPnl"), "Tổng PnL thực tế của tập lệnh dùng để tính health.", attention=True),
                _module_row("maxDrawdownPercent", health.get("maxDrawdownPercent"), "Drawdown lớn nhất đang ghi nhận.", attention=True),
                _module_row("riskMultiplierPercent", round(_safe_float(health.get("riskMultiplier"), 0.0) * 100.0, 2), "Hệ số giảm rủi ro đang áp vào hệ thống, quy đổi sang %."),
                _module_row("scoreAdjustment", health.get("scoreAdjustment"), "Phần tăng thêm vào rule score do health monitor áp đặt."),
                _module_row("confidenceAdjustment", health.get("confidenceAdjustment"), "Phần tăng thêm vào GPT confidence do health monitor áp đặt."),
                _module_row("isPaused", _module_bool_percent(health.get("isPaused")), "100 nghĩa là health monitor đang buộc hệ thống pause.", attention=True),
                _module_row("updatedAt", health.get("updatedAt"), "Thời điểm health state được refresh gần nhất."),
                _module_row("reason", health.get("reason"), "Lý do khiến health hiện ở trạng thái hiện tại."),
            ],
        },
        {
            "number": 4,
            "name": "Replay Engine",
            "purpose": "Thống kê replay thực tế để so sánh quyết định mới với lịch sử đã lưu.",
            "status": "fail" if replay.get("error") else "ok",
            "stats": [
                _module_row("replayCount", replay.get("replayCount"), "Số lượt replay đã được lưu trong DB."),
                _module_row("decisionChangedPercent", replay.get("decisionChangedPercent"), "Tỷ lệ replay làm thay đổi quyết định ban đầu.", attention=True),
                _module_row("confidenceChangedPercent", replay.get("confidenceChangedPercent"), "Tỷ lệ replay làm thay đổi confidence ban đầu."),
                _module_row("averageLatency", replay.get("averageLatency"), "Độ trễ trung bình của các bản ghi replay."),
                _module_row("replayWinRate", replay.get("replayWinRate"), "Win rate của tập lệnh được replay."),
                _module_row("replayProfitFactor", replay.get("replayProfitFactor"), "Profit factor của tập replay đã có kết quả."),
                _module_row("replayDrawdown", replay.get("replayDrawdown"), "Drawdown của tập replay đã có kết quả.", attention=True),
            ],
        },
        {
            "number": 5,
            "name": "Market Regime",
            "purpose": "Tổng hợp trạng thái thị trường từ lịch sử regime mà hệ thống đã ghi nhận.",
            "status": "ok" if regime_history_total > 0 else "warn",
            "market_regime": regime,
            "market_regime_history": regime_history_payload,
            "stats": [
                _module_row("historySamples", regime_history_total, "Số snapshot regime gần nhất dùng để tổng hợp module này."),
                _module_row("currentConfidence", regime.get("confidence"), "Độ tin cậy của regime hiện tại.", attention=True),
                _module_row("bullPercent", _module_percent(regime_counts.get("BULL", 0), regime_history_total), "Tỷ trọng snapshot đang được phân loại là BULL."),
                _module_row("bearPercent", _module_percent(regime_counts.get("BEAR", 0), regime_history_total), "Tỷ trọng snapshot đang được phân loại là BEAR."),
                _module_row("sidewayPercent", _module_percent(regime_counts.get("SIDEWAY", 0), regime_history_total), "Tỷ trọng snapshot đang được phân loại là SIDEWAY."),
                _module_row("highVolatilityPercent", _module_percent(regime_counts.get("HIGH_VOLATILITY", 0), regime_history_total), "Tỷ trọng snapshot ở trạng thái biến động cao.", attention=True),
                _module_row("lowVolatilityPercent", _module_percent(regime_counts.get("LOW_VOLATILITY", 0), regime_history_total), "Tỷ trọng snapshot ở trạng thái biến động thấp."),
                _module_row("unknownPercent", _module_percent(regime_counts.get("UNKNOWN", 0), regime_history_total), "Tỷ trọng snapshot chưa xác định rõ regime."),
                _module_row("currentRegime", regime.get("regime"), "Regime hiện tại của thị trường."),
                _module_row("updatedAt", regime.get("created_at"), "Thời điểm snapshot regime mới nhất được ghi nhận."),
                _module_row("reason", regime.get("reason"), "Lý do phân loại regime gần nhất."),
            ],
        },
        {
            "number": 6,
            "name": "Strategy Versioning",
            "purpose": "Hiển thị hiệu suất thực tế của các strategy version đang active.",
            "status": "ok" if active_strategies else "warn",
            "stats": [
                _module_row("active_count", len(active_strategies), "Số strategy version đang active trong DB.", attention=True),
                _module_row("tracked_trades", strategy_total_trades, "Tổng số lệnh đã đóng của các strategy đang active."),
                _module_row("avg_win_rate", strategy_avg_win_rate, "Win rate trung bình của các strategy đang active.", attention=True),
                _module_row("avg_profit_factor", strategy_avg_profit_factor, "Profit factor trung bình của các strategy đang active."),
                _module_row("avg_drawdown", strategy_avg_drawdown, "Drawdown trung bình của các strategy đang active.", attention=True),
                _module_row("avg_confidence", strategy_avg_confidence, "Độ tự tin trung bình của các lệnh do strategy active tạo ra."),
                _module_row("avg_hold_minutes", strategy_avg_hold_minutes, "Thời gian giữ lệnh trung bình của các strategy đang active."),
                _module_row("total_pnl", strategy_total_pnl, "Tổng PnL thực tế của các strategy đang active.", attention=True),
                _module_row("active_versions", ", ".join(str(item.get("version") or item.get("name") or "-") for item in active_strategies) or "-", "Danh sách strategy version đang active."),
            ],
        },
        {
            "number": 7,
            "name": "Prompt Caching",
            "purpose": "Theo dõi prompt metrics thực tế đã được hệ thống ghi xuống DB.",
            "status": "ok" if prompt_metrics else "warn",
            "stats": [
                _module_row("total_requests", prompt_metrics.get("total_requests") if prompt_metrics else None, "Tổng số lần hệ thống đã ghi nhận metrics prompt."),
                _module_row("average_prompt_tokens", prompt_metrics.get("average_prompt_tokens") if prompt_metrics else None, "Số prompt tokens trung bình đã ghi nhận."),
                _module_row("average_completion_tokens", prompt_metrics.get("average_completion_tokens") if prompt_metrics else None, "Số completion tokens trung bình đã ghi nhận."),
                _module_row("average_latency", prompt_metrics.get("average_latency") if prompt_metrics else None, "Độ trễ trung bình của prompt engine."),
                _module_row("estimated_cached_tokens", prompt_metrics.get("estimated_cached_tokens") if prompt_metrics else None, "Số tokens ước tính được cache từ các lần gọi thật."),
                _module_row("estimated_dynamic_tokens", prompt_metrics.get("estimated_dynamic_tokens") if prompt_metrics else None, "Số tokens động trung bình đã ghi nhận."),
                _module_row("cache_hit_percent", prompt_metrics.get("cache_hit_percent") if prompt_metrics else None, "Tỷ lệ cache hit đang được prompt metrics ghi lại.", attention=True),
                _module_row("updated_at", prompt_updated_at or prompt_runtime.get("error"), "Thời điểm prompt metrics được cập nhật gần nhất hoặc thông báo chưa có dữ liệu.", attention=not bool(prompt_metrics)),
            ],
        },
        {
            "number": 8,
            "name": "Recovery Chain Manager",
            "purpose": "Theo dõi state gỡ lỗ thực tế của từng chuỗi recovery đã được journal lưu.",
            "status": recovery_status,
            "stats": [
                _module_row("recovery_step", sizing_state.get("recovery_step") if sizing_state else None, "Bước hiện tại của chuỗi gỡ lỗ.", attention=True),
                _module_row("cycle_pnl_usdt", sizing_state.get("cycle_pnl_usdt") if sizing_state else None, "PnL lũy kế thực tế của chu kỳ recovery hiện tại.", attention=True),
                _module_row("next_margin_usdt", sizing_state.get("next_margin_usdt") if sizing_state else None, "Margin thực tế hệ thống sẽ dùng cho bước recovery tiếp theo.", attention=True),
                _module_row("blocked", _module_bool_percent(sizing_state.get("blocked")) if sizing_state else None, "100 nghĩa là chuỗi recovery hiện đang bị chặn.", attention=True),
                _module_row("processed_keys_count", len(processed_keys) if isinstance(processed_keys, list) else None, "Số khóa lệnh đã được recovery chain manager xử lý."),
                _module_row("last_realized_net_pnl", sizing_state.get("last_realized_net_pnl") if sizing_state else None, "PnL thực tế của lần chốt gần nhất mà recovery chain đã ghi nhận."),
                _module_row("last_loss_recorded", _module_bool_percent(bool(sizing_state.get("last_loss_key"))) if sizing_state else None, "100 nghĩa là hệ thống đang có bản ghi lệnh lỗ gần nhất trong chain."),
                _module_row("updated_at", sizing_state.get("updated_at") if sizing_state else None, "Thời điểm state recovery chain được cập nhật gần nhất."),
                _module_row("block_reason", sizing_state.get("block_reason") if sizing_state else "Chưa có dữ liệu thu thập", "Lý do block recovery chain nếu có.", attention=True),
                _module_row("orphaned_block_state", _module_bool_percent(recovery_orphaned_block), "100 nghĩa là recovery đang block nhưng runtime DB không còn lệnh/pending/trade record."),
                _module_row("runtime_trade_records", recovery_runtime_records, "Số record runtime dùng để xác định recovery block còn liên quan tới lệnh thật hay chỉ là state cũ."),
                _module_row("last_loss_symbol", sizing_state.get("last_loss_symbol") if sizing_state else None, "Cặp giao dịch của lệnh lỗ gần nhất trong chain."),
                _module_row("last_loss_side", sizing_state.get("last_loss_side") if sizing_state else None, "Hướng LONG/SHORT của lệnh lỗ gần nhất trong chain."),
            ],
        },
        {
            "number": 10,
            "name": "Capital Sync",
            "purpose": "Dong bo von thuc tu OKX va tinh realized capital, khong dung PnL tha noi de tang von giao dich.",
            "status": "ok" if capital_snapshot_ok else "warn",
            "update_event": "Sau nap/rut, sau lenh dong",
            "update_schedule": "60 giay/lan hoac khi bam sync",
            "update_interval": "60 giay",
            "stats": [
                _module_row("source", capital_snapshot.get("source") or config.get("capital_sync", {}).get("capital_source", "OKX"), "Nguon dong bo von."),
                _module_row("quote_currency", capital_snapshot.get("quote_currency") or config.get("capital_sync", {}).get("quote_currency", "USDT"), "Dong tien quote dung de tinh von."),
                _module_row("wallet_balance", capital_snapshot.get("wallet_balance"), "So du vi lay tu account snapshot."),
                _module_row("unrealized_pnl", capital_snapshot.get("unrealized_pnl"), "PnL tha noi chi dung de tru ra khoi von thuc.", attention=True),
                _module_row("realized_capital", capital_snapshot.get("realized_capital"), "Von thuc = wallet_balance - unrealized_pnl.", attention=True),
                _module_row("use_realized_capital_only", _module_bool_percent(config.get("capital_sync", {}).get("use_realized_capital_only", True)), "100 nghia la chi dung realized capital."),
                _module_row("exclude_unrealized_pnl", _module_bool_percent(config.get("capital_sync", {}).get("exclude_unrealized_pnl", True)), "100 nghia la khong dung PnL tha noi de tang von."),
                _module_row("updated_at", capital_snapshot.get("created_at"), "Thoi diem snapshot von moi nhat."),
                _module_row("error", capital_snapshot.get("error") or "-", "Loi dong bo von neu co.", attention=not capital_snapshot_ok),
            ],
        },
        {
            "number": 12,
            "name": "Capital Reserve",
            "purpose": "Tach realized capital thanh reserve capital va trading capital de khong dung toan bo von tai khoan.",
            "status": "ok" if capital_reserve_ok and capital_allocation_check.get("allowed") else "warn",
            "update_event": "Sau nap/rut, sau lenh dong, khi health/recovery doi mode",
            "update_schedule": "5 phut/lan doi chieu",
            "update_interval": "5 phut",
            "stats": [
                _module_row("mode", capital_reserve_state.get("mode") or capital_mode, "Mode von suy ra tu Health, Minimize Losses va Recovery Chain.", attention=True),
                _module_row("realized_capital", capital_reserve_state.get("realized_capital"), "Von thuc dung lam nen tinh reserve."),
                _module_row("reserve_percent", capital_reserve_state.get("reserve_percent"), "Phan tram von duoc giu lai lam quy du phong.", attention=True),
                _module_row("reserve_amount", capital_reserve_state.get("reserve_amount"), "So von du phong khong dung de mo lenh moi."),
                _module_row("trading_capital", capital_reserve_state.get("trading_capital"), "Von con lai duoc phep phan bo cho giao dich."),
                _module_row("used_trading_capital", capital_reserve_state.get("used_trading_capital"), "Von trading da bi chiem boi pending/open noi bo."),
                _module_row("available_trading_capital", capital_reserve_state.get("available_trading_capital"), "Von trading con co the cap margin.", attention=True),
                _module_row("allow_reserve_usage", _module_bool_percent(capital_reserve_state.get("allow_reserve_usage")), "100 nghia la cho phep dung reserve; mac dinh phai la 0."),
                _module_row("allocation_allowed", _module_bool_percent(capital_allocation_check.get("allowed")), "100 nghia la margin co so hien tai con duoc phep cap."),
                _module_row("reason", capital_allocation_check.get("reason") or capital_reserve_state.get("reason") or "-", "Ly do cho phep/tu choi cap von.", attention=not bool(capital_allocation_check.get("allowed"))),
            ],
        },
        {
            "number": 13,
            "name": "Position Sizing",
            "purpose": "Tinh khoi luong vi the theo trading capital, reserve, risk percent, SL/TP, leverage va slot dang dung.",
            "status": "ok" if capital_position_ok else "warn",
            "update_event": "Khi chuan bi mo lenh hoac khi capital reserve thay doi",
            "update_schedule": "Theo tung setup duoc duyet",
            "update_interval": "event-driven",
            "stats": [
                _module_row("mode", capital_position_size.get("mode") or capital_mode, "Mode dung de chon risk percent."),
                _module_row("risk_percent", capital_position_size.get("risk_percent"), "Risk percent theo mode hien tai.", attention=True),
                _module_row("risk_amount", capital_position_size.get("risk_amount"), "So USDT rui ro toi da theo trading capital."),
                _module_row("available_trading_capital", capital_position_size.get("available_trading_capital"), "Trading capital con trong de cap margin."),
                _module_row("max_order_size_by_risk", capital_position_size.get("max_order_size_by_risk"), "Order size toi da theo risk."),
                _module_row("max_order_size_by_capital", capital_position_size.get("max_order_size_by_capital"), "Order size toi da theo gioi han trading capital."),
                _module_row("suggested_order_size", capital_position_size.get("suggested_order_size"), "Khoi luong vi the de xuat."),
                _module_row("required_margin", capital_position_size.get("required_margin"), "Margin can dung cho suggested order."),
                _module_row("allowed", _module_bool_percent(capital_position_size.get("allowed")), "100 nghia la sizing hien tai hop le."),
                _module_row("reason", capital_position_size.get("reason") or "-", "Ly do sizing duoc chap nhan hoac bi tu choi.", attention=not capital_position_ok),
            ],
        },
        {
            "number": 14,
            "name": "Market Structure & Pattern Engine",
            "purpose": "Bộ máy nhận diện cấu trúc thị trường, vùng giá, mô hình nến, chart pattern và Smart Money để xuất feature cho AI.",
            "status": "ok" if market_pattern.get("ok") and _safe_int(market_pattern_counts.get("market_analysis_snapshots")) > 0 else "warn",
            "update_event": "Khi scanner hoặc final re-check gửi OHLCV vào engine",
            "update_schedule": "Event-driven theo request analyze/recheck",
            "update_interval": "sự kiện",
            "market_pattern_engine": market_pattern,
            "stats": [
                _module_row("snapshots", market_pattern_counts.get("market_analysis_snapshots"), "Tổng snapshot market analysis đã lưu idempotent trong Mongo.", attention=True),
                _module_row("pattern_detections", market_pattern_counts.get("pattern_detections"), "Tổng candlestick/chart/smart-money detection đã lưu."),
                _module_row("support_resistance_zones", market_pattern_counts.get("support_resistance_zones"), "Tổng vùng support/resistance đã lưu."),
                _module_row("market_structure_events", market_pattern_counts.get("market_structure_events"), "Tổng event cấu trúc thị trường đã lưu."),
                _module_row("latest_symbol", market_pattern_latest.get("symbol"), "Symbol của snapshot mới nhất."),
                _module_row("latest_timeframe", market_pattern_latest.get("timeframe"), "Timeframe của snapshot mới nhất."),
                _module_row("trend_regime", market_pattern_structure.get("trend_regime") if isinstance(market_pattern_structure, dict) else None, "Regime cấu trúc mới nhất: bullish, bearish, range hoặc transition.", attention=True),
                _module_row("structure_state", market_pattern_structure.get("structure_state") if isinstance(market_pattern_structure, dict) else None, "Trạng thái HH/HL, LH/LL, range hoặc mixed."),
                _module_row("confluence_bias", market_pattern_confluence.get("bias") if isinstance(market_pattern_confluence, dict) else None, "Bias đồng thuận kỹ thuật, không phải xác suất thắng.", attention=True),
                _module_row("confluence_score", market_pattern_confluence.get("confluence_score") if isinstance(market_pattern_confluence, dict) else None, "Điểm đồng thuận kỹ thuật 0-1."),
                _module_row("data_quality_score", market_pattern_feature.get("data_quality_score") if isinstance(market_pattern_feature, dict) else None, "Chất lượng dữ liệu của snapshot mới nhất."),
                _module_row("updated_at", market_pattern_latest.get("updated_at") or market_pattern_latest.get("created_at"), "Thời điểm snapshot mới nhất."),
                _module_row("error", market_pattern.get("error") or "-", "Lỗi engine nếu chưa kết nối được Mongo/API.", attention=not market_pattern.get("ok")),
            ],
        },
        {
            "number": 15,
            "name": "Thực thi giao dịch & Quản lý vị thế",
            "purpose": "Thực thi giao dịch, đồng bộ OKX/Atlas, partial take-profit, trailing SL và gồng lãi cho vị thế đang mở.",
            "status": "warn" if trade_execution.get("error") else "ok",
            "update_event": "Sau khi mo lenh, partial close, amend SL/TP hoac runtime sync OKX",
            "update_schedule": "Theo chu ky runtime/trailing stop",
            "update_interval": "1 phut",
            "trade_execution": trade_execution,
            "stats": [
                _module_row("open_positions", trade_execution.get("open_count"), "So vi the live dang mo trong trade_executions.", attention=True),
                _module_row("partial_done", trade_execution.get("partial_done_count"), "So vi the da chot partial take-profit 30%.", attention=True),
                _module_row("waiting_partial", trade_execution.get("waiting_partial_count"), "So vi the dang cho dat moc partial TP."),
                _module_row("recent_closed", trade_execution.get("closed_count"), "So lenh dong gan nhat duoc nap vao module."),
                _module_row("partial_enabled", _module_bool_percent(trade_config.get("partial_enabled")), "100 nghia la co che chot 30% + gong lai dang bat.", attention=True),
                _module_row("partial_trigger_tp_progress", trade_config.get("partial_trigger_tp_progress"), "Tien do toi TP de chot partial, vi du 0.7 = 70%."),
                _module_row("partial_close_fraction", trade_config.get("partial_close_fraction"), "Ty le dong lan dau, vi du 0.3 = 30%."),
                _module_row("remaining_sl_buffer_r", trade_config.get("remaining_sl_buffer_r"), "Sau partial, SL phan con lai duoc keo len entry + buffer R."),
                _module_row("tp_extension_fraction", trade_config.get("tp_extension_fraction"), "TP moi duoc nay xa them theo reward ban dau de gong lai."),
                _module_row("atr_timeframe", trade_config.get("atr_timeframe"), "Khung ATR dung cho trailing SL sau partial."),
                _module_row("atr_multiplier", trade_config.get("atr_multiplier"), "He so ATR de dat khoang cach trailing SL."),
                _module_row("error", trade_execution.get("error") or "-", "Loi doc trade execution neu co.", attention=bool(trade_execution.get("error"))),
            ],
        },
        {
            "number": 9,
            "name": "Configuration Impact",
            "purpose": "Phan tich tac dong truoc khi ap dung thay doi cau hinh von, slot, sizing, recovery, SL/TP va leverage.",
            "status": "ok" if config_impact_safe else "warn",
            "update_event": "Khi nguoi dung de xuat doi cau hinh",
            "update_schedule": "Chay truoc khi apply config",
            "update_interval": "event-driven",
            "stats": [
                _module_row("realized_capital", config_impact.get("realized_capital"), "Von thuc dung de mo phong tac dong."),
                _module_row("trading_capital_before", config_impact.get("trading_capital_before"), "Trading capital truoc thay doi."),
                _module_row("trading_capital_after", config_impact.get("trading_capital_after"), "Trading capital sau thay doi."),
                _module_row("required_capital_before", config_impact.get("required_capital_before"), "Von can truoc thay doi."),
                _module_row("required_capital_after", config_impact.get("required_capital_after"), "Von can sau thay doi.", attention=True),
                _module_row("max_safe_order_size", config_impact.get("max_safe_order_size"), "Order size toi da con an toan theo mo phong."),
                _module_row("suggested_order_size", config_impact.get("suggested_order_size"), "Order size de xuat neu cau hinh hien tai khong an toan."),
                _module_row("safety_score", config_impact.get("safety_score"), "Diem an toan 0-100.", attention=True),
                _module_row("risk_level", config_impact.get("risk_level"), "LOW/MEDIUM/HIGH/CRITICAL.", attention=not config_impact_safe),
                _module_row("summary", config_impact.get("summary"), "Tom tat tac dong cau hinh."),
            ],
        },
    ]
    modules: list[dict[str, Any]] = []
    for definition in definitions:
        file_info = files.get(int(definition["number"]), {})
        modules.append(
            {
                **definition,
                "file": _module_file_payload(file_info),
                "has_file": bool(file_info),
            }
        )
    return modules


def _build_system_checklist_payload(
    config: dict[str, Any],
    *,
    automation: dict[str, Any] | None = None,
    ai_range: str = "current",
) -> dict[str, Any]:
    checked_at = datetime.now(timezone.utc)
    checked_at_iso = checked_at.isoformat()
    checked_at_local = checked_at.astimezone(_system_timezone(config))
    checked_date = _system_report_date(config, checked_at)
    automation = automation or _default_automation_status(config)
    ai_range_key = _normalize_ai_decision_range(ai_range)

    stats = storage_stats(config)
    row_counts = stats.get("row_counts", {})
    payload_bytes = stats.get("payload_bytes", {})
    disk = stats.get("disk", {})
    free_percent = float(disk.get("free_percent") or 0)
    disk_applicable = str(stats.get("backend") or "").lower() != "atlas"
    disk_ok = free_percent > 2 if disk_applicable else True
    disk_label = f"{free_percent}%" if disk_applicable else "N/A (MongoDB Atlas)"
    last_result = str(automation.get("last_result") or "")
    runtime_error = str(automation.get("error") or "")
    runtime_ok = disk_ok and "error" not in last_result.lower() and not runtime_error

    try:
        ai_history = recent_ai_call_history(config, limit=20)
    except Exception:
        ai_history = []
    ai_errors = [
        item for item in ai_history
        if "error" in str(item.get("status") or item.get("result") or "").lower()
    ]
    ai_enabled = bool(config.get("ai", {}).get("enabled", False))
    ai_allow = bool(config.get("ai", {}).get("allow_api_calls", False))
    ai_ok = (not ai_enabled) or not ai_errors
    latest_ai = ai_history[-1] if ai_history else {}

    try:
        replay = replay_stats(config)
        replay_ok = True
    except Exception as exc:
        replay = {"error": str(exc)}
        replay_ok = False

    try:
        strategy = current_strategy_state(config)
        active_strategies = strategy.get("active") or []
        strategy_ok = bool(active_strategies or strategy.get("selected_strategy_version") or strategy.get("version"))
    except Exception as exc:
        strategy = {"error": str(exc)}
        active_strategies = []
        strategy_ok = False

    try:
        regime = current_market_regime(config)
        regime_ok = bool(regime.get("regime"))
    except Exception as exc:
        regime = {"error": str(exc)}
        regime_ok = False
    regime_history_payload = _market_regime_history_payload(config, limit=30, current_regime=regime)
    regime_history_items = list(regime_history_payload.get("items") or [])

    try:
        health = get_bunny_health_state(config)
        health_ok = bool(health) and not bool(health.get("isCritical"))
    except Exception as exc:
        health = {"error": str(exc)}
        health_ok = False

    try:
        risk_state = get_trading_system_state(config)
        guard = market_guard_block_status(config)
        kill_switch_ok = bool(risk_state) and "error" not in risk_state
    except Exception as exc:
        risk_state = {"error": str(exc)}
        guard = {}
        kill_switch_ok = False

    try:
        paper_trades = list_paper_trades(config, limit=10)
    except Exception:
        paper_trades = []
    paper_ok = bool(paper_trades) or config.get("mode") in {"demo", "dry_run"}
    latest_paper = paper_trades[0] if paper_trades else {}

    items = [
        _check_item(
            "Khong con loi runtime",
            runtime_ok,
            f"last_result={last_result or '-'}, disk_free={disk_label}",
            evidence=[
                _evidence_line("Ngay kiem tra", checked_date),
                _evidence_line("Thoi gian local", checked_at_local.isoformat()),
                _evidence_line("Thoi gian UTC", checked_at_iso),
                _evidence_line("Nguon", "/healthz + automation_status + storage_stats"),
                _evidence_line("Ket qua runtime gan nhat", last_result or "-"),
                _evidence_line("Runtime error", runtime_error or "khong co"),
                _evidence_line(
                    "Disk con trong",
                    f"{free_percent}% ({disk.get('free_bytes', 0)} bytes)" if disk_applicable else "N/A - MongoDB Atlas",
                ),
                _evidence_line("DB path", stats.get("db_path")),
            ],
        ),
        _check_item(
            "AI quyet dinh on dinh",
            ai_ok,
            f"enabled={ai_enabled}, allow_api={ai_allow}, recent_errors={len(ai_errors)}, calls={len(ai_history)}",
            warning=not ai_history or not ai_enabled,
            evidence=[
                _evidence_line("Ngay kiem tra", checked_date),
                _evidence_line("AI enabled", ai_enabled),
                _evidence_line("Cho phep goi API", ai_allow),
                _evidence_line("So lan goi AI gan nhat duoc luu", len(ai_history)),
                _evidence_line("So loi AI gan nhat", len(ai_errors)),
                _evidence_line("Model gan nhat", latest_ai.get("model") or "-"),
                _evidence_line("Trang thai lan gan nhat", latest_ai.get("status") or "-"),
                _evidence_line("Ghi chu", "AI dang tat van duoc xem la an toan van hanh; khi bat AI tieu chi dua tren loi gan nhat."),
            ],
        ),
        _check_item(
            "Replay Engine hoat dong",
            replay_ok,
            f"records={replay.get('replayCount', '-')}",
            evidence=[
                _evidence_line("Ngay kiem tra", checked_date),
                _evidence_line("Nguon", "/api/replay/stats"),
                _evidence_line("So ban replay", replay.get("replayCount", "-")),
                _evidence_line("Ti le doi quyet dinh", replay.get("decisionChangedPercent", "-")),
                _evidence_line("Replay win rate", replay.get("replayWinRate", "-")),
                _evidence_line("Loi", replay.get("error") or "khong co"),
            ],
        ),
        _check_item(
            "Strategy Versioning hoat dong",
            strategy_ok,
            f"active={len(active_strategies)}",
            evidence=[
                _evidence_line("Ngay kiem tra", checked_date),
                _evidence_line("Nguon", "/api/strategy/current"),
                _evidence_line("So strategy active", len(active_strategies)),
                _evidence_line("Strategy active", ", ".join(str(item.get("version") or item.get("name") or "-") for item in active_strategies) or "-"),
                _evidence_line("Loi", strategy.get("error") or "khong co"),
            ],
        ),
        _check_item(
            "Market Regime hoat dong",
            regime_ok,
            f"regime={regime.get('regime', '-')}",
            evidence=[
                _evidence_line("Ngay kiem tra", checked_date),
                _evidence_line("Nguon", "/api/market-regime/current"),
                _evidence_line("Regime hien tai", regime.get("regime") or "-"),
                _evidence_line("Do tin cay", regime.get("confidence") or "-"),
                _evidence_line("Cap nhat luc", regime.get("created_at") or "-"),
                _evidence_line("Ly do", regime.get("reason") or regime.get("error") or "-"),
            ],
        ),
        _check_item(
            "Health Monitor hoat dong",
            health_ok,
            f"critical={health.get('isCritical', False)}, warning={health.get('isWarning', False)}",
            evidence=[
                _evidence_line("Ngay kiem tra", checked_date),
                _evidence_line("Nguon", "/api/bunny-health/state"),
                _evidence_line("Healthy", health.get("isHealthy")),
                _evidence_line("Warning", health.get("isWarning")),
                _evidence_line("Critical", health.get("isCritical")),
                _evidence_line("Win rate", health.get("winRate")),
                _evidence_line("Profit factor", health.get("profitFactor")),
                _evidence_line("Drawdown", health.get("maxDrawdownPercent")),
                _evidence_line("Ly do", health.get("reason") or health.get("error") or "-"),
            ],
        ),
        _check_item(
            "Kill Switch",
            kill_switch_ok,
            f"paused={risk_state.get('isPaused', False)}, guard_active={guard.get('active', False)}",
            required=True,
            evidence=[
                _evidence_line("Ngay kiem tra", checked_date),
                _evidence_line("Nguon", "trading_system_state + market_guard_block_status"),
                _evidence_line("Trading paused", risk_state.get("isPaused")),
                _evidence_line("Paused until", risk_state.get("pausedUntil") or "-"),
                _evidence_line("Market Guard active", guard.get("active")),
                _evidence_line("Guard blocked until", guard.get("blocked_until") or "-"),
                _evidence_line("Loss streak", risk_state.get("globalLossStreak") or 0),
                _evidence_line("Loi", risk_state.get("error") or "khong co"),
            ],
        ),
        _check_item(
            "Nhat ky day du",
            int(row_counts.get("journal_state", 0)) > 0,
            f"journal_state={row_counts.get('journal_state', 0)}, trades={row_counts.get('trade_executions', 0)}",
            evidence=[
                _evidence_line("Ngay kiem tra", checked_date),
                _evidence_line("Nguon", "Atlas row_counts + payload_bytes"),
                _evidence_line("journal_state rows", row_counts.get("journal_state", 0)),
                _evidence_line("trade_executions rows", row_counts.get("trade_executions", 0)),
                _evidence_line("decisions rows", row_counts.get("decisions", 0)),
                _evidence_line("pending_orders rows", row_counts.get("pending_orders", 0)),
                _evidence_line("market_scan_observations rows", row_counts.get("market_scan_observations", 0)),
                _evidence_line("decision payload bytes", payload_bytes.get("decisions", 0)),
                _evidence_line("scan payload bytes", payload_bytes.get("market_scan_observations", 0)),
            ],
        ),
        _check_item(
            "Dry Run va Paper Trading da kiem chung",
            paper_ok,
            f"paper_trades={len(paper_trades)}, mode={config.get('mode', '-')}",
            warning=not paper_trades,
            evidence=[
                _evidence_line("Ngay kiem tra", checked_date),
                _evidence_line("Nguon", "paper_trades + config mode"),
                _evidence_line("Mode", config.get("mode", "-")),
                _evidence_line("Paper trading enabled", config.get("paper_trading", {}).get("enabled")),
                _evidence_line("So paper trades gan nhat", len(paper_trades)),
                _evidence_line("Paper trade moi nhat", latest_paper.get("created_at") or "-"),
                _evidence_line("Trang thai paper moi nhat", latest_paper.get("status") or "-"),
            ],
        ),
    ]
    criteria = [items[index] for index in (0, 1, 6, 7, 8) if index < len(items)]
    modules = system_modules_payload(
        config,
        checked_date=checked_date,
        checked_at_iso=checked_at_iso,
        ai_history=ai_history,
        replay=replay,
        strategy=strategy,
        regime=regime,
        health=health,
        risk_state=risk_state,
        row_counts=row_counts,
        ai_range=ai_range_key,
        regime_history_items=regime_history_items,
        regime_history_payload=regime_history_payload,
    )
    ok_count = sum(1 for item in criteria if item["ok"])
    payload = {
        "date": checked_date,
        "created_at": checked_at_iso,
        "ok": all(item["ok"] or (item["status"] == "warn" and not item["required"]) for item in criteria),
        "ok_count": ok_count,
        "total": len(criteria),
        "items": criteria,
        "criteria": criteria,
        "modules": modules,
        "module_count": len(modules),
        "storage": stats,
        "automation": automation,
        "replay": replay,
        "strategy": strategy,
        "market_regime": regime,
        "market_regime_history": regime_history_payload,
        "health": health,
        "risk_state": risk_state,
        "ai_range": ai_range_key,
    }
    return payload


def refresh_system_checklist_snapshot(
    config: dict[str, Any],
    *,
    automation: dict[str, Any] | None = None,
    ai_range: str = "current",
) -> dict[str, Any]:
    ai_range_key = _normalize_ai_decision_range(ai_range)
    payload = _build_system_checklist_payload(config, automation=automation, ai_range=ai_range_key)
    if ai_range_key == "current":
        _persist_system_checklist_snapshot(config, payload)
    return attach_previous_system_checklist_snapshot(config, payload)


def system_checklist_payload(
    config: dict[str, Any],
    *,
    automation: dict[str, Any] | None = None,
    force_refresh: bool = False,
    max_age_seconds: int | None = SYSTEM_CHECKLIST_DEFAULT_TTL_SECONDS,
    ai_range: str = "current",
) -> dict[str, Any]:
    _ = max_age_seconds
    ai_range_key = _normalize_ai_decision_range(ai_range)
    if ai_range_key == "all":
        payload = _build_system_checklist_payload(config, automation=automation, ai_range=ai_range_key)
        return attach_previous_system_checklist_snapshot(config, payload)
    date_key = _system_report_date(config)
    if not force_refresh:
        snapshot = _preferred_system_checklist_snapshot(config, date_key)
        if snapshot is not None:
            return attach_previous_system_checklist_snapshot(config, snapshot)
        snapshot = _latest_system_checklist_snapshot(config)
        if isinstance(snapshot, dict) and str(snapshot.get("date") or "") == date_key:
            return attach_previous_system_checklist_snapshot(config, snapshot)
    return refresh_system_checklist_snapshot(config, automation=automation, ai_range=ai_range_key)


def _build_timeframe_state_dashboard(config: dict[str, Any], *, lookback_hours: int = 24) -> dict[str, Any]:
    lookback_hours = max(1, min(168, int(lookback_hours or 24)))
    configured_frames = _configured_timeframes(config)
    memory = recent_market_scan_memory(
        config,
        timeframes=configured_frames,
        lookback_hours=lookback_hours,
        per_symbol_timeframe_limit=10,
        total_limit=2000,
        include_details=False,
    )
    flat_rows = _flatten_scan_memory(memory)
    latest_scan = latest_internal_market_scan(config) or {}
    scan_context = _latest_scan_timeframe_context(latest_scan)
    grouped_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in flat_rows:
        grouped_rows[str(row.get("timeframe") or "")].append(row)
    frames_payload: list[dict[str, Any]] = []
    for frame in configured_frames:
        rows = grouped_rows.get(frame, [])
        last_row = max(rows, key=lambda item: str(item.get("created_at") or ""), default={})
        confidence_values = [_safe_float(item.get("confidence")) for item in rows if item.get("confidence") is not None]
        win_values = [
            _safe_float(item.get("win_probability_pct"))
            for item in rows
            if item.get("win_probability_pct") is not None
        ]
        top_symbols = Counter(str(item.get("symbol") or "") for item in rows if item.get("symbol"))
        frames_payload.append(
            {
                "timeframe": frame,
                "role": "primary" if frame == configured_frames[0] else "confirmation",
                "configured": True,
                "observation_count": len(rows),
                "symbol_count": len({str(item.get("symbol") or "") for item in rows if item.get("symbol")}),
                "last_observed_at": last_row.get("created_at"),
                "average_confidence": round(_avg(confidence_values), 2) if confidence_values else 0.0,
                "average_win_probability_pct": round(_avg(win_values), 2) if win_values else 0.0,
                "side_counts": dict(Counter(str(item.get("side") or "-").upper() for item in rows)),
                "latest_symbols": [symbol for symbol, _count in top_symbols.most_common(5)],
                "latest_scan_context": scan_context.get(frame, [])[:5],
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_date": _system_report_date(config),
        "timezone": str(_system_timezone(config)),
        "lookback_hours": lookback_hours,
        "primary_timeframe": configured_frames[0] if configured_frames else None,
        "configured_timeframes": configured_frames,
        "next_internal_scan_at": _iso_or_none(next_internal_market_scan_at(config)),
        "latest_internal_scan": {
            "created_at": latest_scan.get("created_at"),
            "slot_id": latest_scan.get("slot_id"),
            "approved_symbols": latest_scan.get("approved_symbols") or [],
            "selected_symbols": latest_scan.get("selected_symbols") or [],
            "selection_stale": bool(latest_scan.get("selection_stale")),
            "candidate_count": latest_scan.get("candidate_count"),
            "status": latest_scan.get("status"),
        },
        "frames": frames_payload,
    }


def timeframe_state_dashboard(
    config: dict[str, Any],
    *,
    lookback_hours: int = 24,
    force_refresh: bool = False,
    max_age_seconds: int | None = DASHBOARD_DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    lookback_hours = max(1, min(168, int(lookback_hours or 24)))
    key = _dashboard_snapshot_key(config, "timeframes", lookback_hours=lookback_hours)
    return _get_or_build_cached_payload(
        config,
        key=key,
        builder=lambda: _build_timeframe_state_dashboard(config, lookback_hours=lookback_hours),
        force_refresh=force_refresh,
        max_age_seconds=max_age_seconds,
    )


def _build_scan_memory_dashboard(
    config: dict[str, Any],
    *,
    lookback_hours: int = 24,
    per_symbol_timeframe_limit: int = 5,
) -> dict[str, Any]:
    lookback_hours = max(1, min(168, int(lookback_hours or 24)))
    per_symbol_timeframe_limit = max(1, min(20, int(per_symbol_timeframe_limit or 5)))
    configured_frames = _configured_timeframes(config)
    memory = recent_market_scan_memory(
        config,
        timeframes=configured_frames,
        lookback_hours=lookback_hours,
        per_symbol_timeframe_limit=per_symbol_timeframe_limit,
        total_limit=1200,
        include_details=False,
    )
    flat_rows = _flatten_scan_memory(memory)
    latest_scan = latest_internal_market_scan(config) or {}
    timeframe_summary: dict[str, dict[str, Any]] = {}
    for frame in configured_frames:
        rows = [row for row in flat_rows if str(row.get("timeframe") or "") == frame]
        last_seen = max((_parse_time(row.get("created_at")) for row in rows), default=None)
        timeframe_summary[frame] = {
            "timeframe": frame,
            "observation_count": len(rows),
            "symbol_count": len({str(row.get("symbol") or "") for row in rows if row.get("symbol")}),
            "last_observed_at": _iso_or_none(last_seen),
            "average_score": round(_avg([_safe_float(row.get("score")) for row in rows]), 2) if rows else 0.0,
            "average_win_probability_pct": round(
                _avg([_safe_float(row.get("win_probability_pct")) for row in rows if row.get("win_probability_pct") is not None]),
                2,
            ) if rows else 0.0,
            "recent_signals": [
                {
                    "symbol": row.get("symbol"),
                    "side": row.get("side"),
                    "created_at": row.get("created_at"),
                    "score": row.get("score"),
                    "win_probability_pct": row.get("win_probability_pct"),
                }
                for row in rows[:8]
            ],
        }
    symbol_summary = []
    for symbol, frame_map in sorted(memory.items()):
        latest_seen = max(
            (_parse_time(entry.get("created_at")) for entries in frame_map.values() for entry in entries),
            default=None,
        )
        symbol_summary.append(
            {
                "symbol": symbol,
                "timeframes": sorted(frame_map.keys(), key=_timeframe_minutes),
                "observation_count": sum(len(entries) for entries in frame_map.values()),
                "last_observed_at": _iso_or_none(latest_seen),
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_hours": lookback_hours,
        "configured_timeframes": configured_frames,
        "summary": {
            "symbol_count": len(memory),
            "observation_count": len(flat_rows),
            "timeframe_count": len(configured_frames),
            "latest_internal_scan_at": latest_scan.get("created_at"),
            "latest_internal_scan_symbols": latest_scan.get("approved_symbols") or [],
            "latest_internal_selected_symbols": latest_scan.get("selected_symbols") or [],
        },
        "by_timeframe": [timeframe_summary[frame] for frame in configured_frames],
        "by_symbol": symbol_summary[:100],
        "latest_internal_scan": latest_scan,
    }


def scan_memory_dashboard(
    config: dict[str, Any],
    *,
    lookback_hours: int = 24,
    per_symbol_timeframe_limit: int = 5,
    force_refresh: bool = False,
    max_age_seconds: int | None = DASHBOARD_DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    lookback_hours = max(1, min(168, int(lookback_hours or 24)))
    per_symbol_timeframe_limit = max(1, min(20, int(per_symbol_timeframe_limit or 5)))
    key = _dashboard_snapshot_key(
        config,
        "scan_memory",
        lookback_hours=lookback_hours,
        per_symbol_timeframe_limit=per_symbol_timeframe_limit,
    )
    return _get_or_build_cached_payload(
        config,
        key=key,
        builder=lambda: _build_scan_memory_dashboard(
            config,
            lookback_hours=lookback_hours,
            per_symbol_timeframe_limit=per_symbol_timeframe_limit,
        ),
        force_refresh=force_refresh,
        max_age_seconds=max_age_seconds,
    )


def _build_analytics_dashboard(config: dict[str, Any], *, lookback_hours: int = 24) -> dict[str, Any]:
    lookback_hours = max(1, min(168, int(lookback_hours or 24)))
    decisions = recent_ai_trade_decisions(config, limit=100, include_details=False)
    decision_stats = ai_trade_decision_stats(config)
    ai_calls = recent_ai_call_history(config, limit=50)
    prompt = prompt_status(config)
    strategy = current_strategy_state(config)
    regime = current_market_regime(config)
    health = get_bunny_health_state(config)
    risk_state = get_trading_system_state(config)
    paper_trades = list_paper_trades(config, limit=200)
    trade_memory = list_trade_memory(config, limit=200)
    pending_total = count_pending_orders(config)
    checklist = system_checklist_payload(config)
    stats = checklist.get("storage") if isinstance(checklist.get("storage"), dict) else {}
    latest_scan = latest_internal_market_scan(config) or {}
    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    guard_rows = list_market_guard_observations(config, limit=200, since=since)
    decision_days = Counter(str(row.get("created_at") or "")[:10] for row in decisions if row.get("created_at"))
    ai_call_roles = Counter(str(item.get("role") or "ai") for item in ai_calls)
    ai_call_status = Counter(str(item.get("status") or item.get("result") or "-") for item in ai_calls)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_date": _system_report_date(config),
        "lookback_hours": lookback_hours,
        "decision_stats": decision_stats,
        "decision_activity_by_day": dict(sorted(decision_days.items(), reverse=True)[:14]),
        "recent_decisions": decisions[:30],
        "ai_call_summary": {
            "count": len(ai_calls),
            "role_counts": dict(ai_call_roles),
            "status_counts": dict(ai_call_status),
            "latest_created_at": ai_calls[-1].get("created_at") if ai_calls else None,
        },
        "prompt": prompt,
        "strategy": strategy,
        "market_regime": regime,
        "health": health,
        "risk_state": risk_state,
        "pending_total": pending_total,
        "paper_trades": _paper_trade_summary(paper_trades),
        "trade_memory": _trade_memory_summary(trade_memory),
        "storage": stats,
        "market_guard": {
            "observation_count": len(guard_rows),
            "latest_observed_at": guard_rows[0].get("observed_at") if guard_rows else None,
            "severity_counts": dict(Counter(str(item.get("severity") or "-") for item in guard_rows)),
        },
        "latest_internal_scan": {
            "created_at": latest_scan.get("created_at"),
            "approved_symbols": latest_scan.get("approved_symbols") or [],
            "selected_symbols": latest_scan.get("selected_symbols") or [],
            "selection_stale": bool(latest_scan.get("selection_stale")),
            "candidate_count": latest_scan.get("candidate_count"),
            "status": latest_scan.get("status"),
        },
    }


def analytics_dashboard(
    config: dict[str, Any],
    *,
    lookback_hours: int = 24,
    force_refresh: bool = False,
    max_age_seconds: int | None = DASHBOARD_DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    lookback_hours = max(1, min(168, int(lookback_hours or 24)))
    key = _dashboard_snapshot_key(config, "analytics", lookback_hours=lookback_hours)
    return _get_or_build_cached_payload(
        config,
        key=key,
        builder=lambda: _build_analytics_dashboard(config, lookback_hours=lookback_hours),
        force_refresh=force_refresh,
        max_age_seconds=max_age_seconds,
    )


def _build_replay_dashboard_payload(config: dict[str, Any], *, limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    stats = replay_stats(config)
    payloads = list_replay_history_rows(config, limit=limit, include_trade_execution=True)
    decision_counts = Counter(str(row.get("new_decision") or "-") for row in payloads)
    strategy_counts = Counter(str(row.get("strategy_version") or "-") for row in payloads)
    model_counts = Counter(str(row.get("model_version") or "-") for row in payloads)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": stats,
        "normalized": {
            "decision_counts": dict(decision_counts),
            "strategy_counts": dict(strategy_counts),
            "model_counts": dict(model_counts),
            "recent": payloads,
        },
    }


def replay_dashboard_payload(
    config: dict[str, Any],
    *,
    limit: int = 50,
    force_refresh: bool = False,
    max_age_seconds: int | None = DASHBOARD_DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    key = _dashboard_snapshot_key(config, "replay", limit=limit)
    return _get_or_build_cached_payload(
        config,
        key=key,
        builder=lambda: _build_replay_dashboard_payload(config, limit=limit),
        force_refresh=force_refresh,
        max_age_seconds=max_age_seconds,
    )


def _build_system_health_dashboard(config: dict[str, Any], *, history_limit: int = 30) -> dict[str, Any]:
    history_limit = max(1, min(history_limit, 180))
    history = system_checklist_history(config, limit=history_limit)
    latest = system_checklist_snapshot(config, _system_report_date(config)) or (history[0] if history else None) or {}
    criterion_trends: dict[str, list[dict[str, Any]]] = defaultdict(list)
    module_trends: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for snapshot in reversed(history):
        date_key = str(snapshot.get("date") or "")
        for item in snapshot.get("criteria") or snapshot.get("items") or []:
            criterion_trends[str(item.get("name") or "-")].append(
                {"date": date_key, "status": item.get("status"), "ok": item.get("ok")}
            )
        for item in snapshot.get("modules") or []:
            module_trends[str(item.get("name") or "-")].append(
                {"date": date_key, "status": item.get("status")}
            )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_date": _system_report_date(config),
        "latest": latest,
        "history": history,
        "summary": {
            "week": system_checklist_summary(config, "week"),
            "month": system_checklist_summary(config, "month"),
            "year": system_checklist_summary(config, "year"),
        },
        "criterion_trends": dict(criterion_trends),
        "module_trends": dict(module_trends),
    }


def system_health_dashboard(
    config: dict[str, Any],
    *,
    history_limit: int = 30,
    force_refresh: bool = False,
    max_age_seconds: int | None = DASHBOARD_DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    history_limit = max(1, min(history_limit, 180))
    key = _dashboard_snapshot_key(config, "system_health", history_limit=history_limit)
    return _get_or_build_cached_payload(
        config,
        key=key,
        builder=lambda: _build_system_health_dashboard(config, history_limit=history_limit),
        force_refresh=force_refresh,
        max_age_seconds=max_age_seconds,
    )
