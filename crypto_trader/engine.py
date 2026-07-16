from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .ai_coordinator import (
    candidate_okx_review,
    internal_lc_memory,
    internal_market_shortlist,
    okx_ai_approval,
    okx_review_allows_okx_submission,
    okx_review_requests_market_entry,
    review_candidate_for_lc_okx,
    run_internal_market_scan,
    run_internal_market_scan_if_due,
    should_defer_new_vt_to_internal_lc,
)
from .config import project_path
from .codex_features import (
    candidate_from_payload,
    candidate_to_payload,
    detect_market_regime,
    record_ai_trade_decision,
    record_trade_candidates,
    select_runtime_config,
)
from .executor import execute_candidate
from .lc_pipeline import lc_pipeline_pool_rows, update_lc_internal_pipeline
from .market import fetch_market_snapshots, fetch_top_volume_symbols
from .market import prefetch_market_data
from .market_guard import market_guard_symbol_layers, market_guard_top_risk
from .models import Decision, ExecutionResult, RiskCheck, TradeCandidate, to_jsonable
from .news import collect_news
from .pending import maintain_pending_orders
from .risk import active_trades_summary, evaluate_candidate
from .sizing import apply_position_sizing
from .storage import (
    get_journal_state,
    is_retryable_storage_error,
    latest_decision_payload,
    next_global_counter,
    open_pending_symbols,
    save_decision,
    save_market_scan_observations,
    save_pending_order,
    set_journal_state,
)
from .strategy import build_candidates, enrich_quantities

LC_PIPELINE_CANDIDATE_CACHE_KEY = "lc_pipeline_candidate_cache_v1"
WAIT_SLOT_NOTIFICATION_HISTORY_KEY = "wait_slot_notification_history_v1"
WAIT_SLOT_NOTIFICATION_COUNTER_KEY = "wait_slot_notification_counter_v1"
MINI_SYSTEM_NOTIFICATION_HISTORY_KEY = "mini_system_notification_history_v1"


def _report_progress(
    progress_callback: Callable[[str, dict[str, Any] | None], None] | None,
    phase: str,
    **metadata: Any,
) -> None:
    if progress_callback is None:
        return
    progress_callback(phase, metadata or None)


def _ordered_unique(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for symbol in symbols:
        clean = str(symbol or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        unique.append(clean)
    return unique


def _engine_timezone(config: dict[str, Any]) -> ZoneInfo:
    timezone_name = (
        config.get("ai", {}).get("internal", {}).get("market_scan_timezone")
        or config.get("timezone")
        or "Asia/Ho_Chi_Minh"
    )
    try:
        return ZoneInfo(str(timezone_name))
    except Exception:
        return timezone(timedelta(hours=7))


def _parse_iso_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _wait_slot_source_label(row: dict[str, Any]) -> str:
    source_slot = row.get("source_slot") or row.get("state") or "Mini"
    source_index = row.get("source_index")
    raw_label = str(row.get("source_label") or "").strip()
    source_clock = None
    if raw_label:
        parts = raw_label.split()
        source_clock = parts[-1] if parts else None
    if not source_clock:
        source_time = _parse_iso_time(row.get("source_time"))
        source_clock = source_time.strftime("%H:%M:%S") if source_time else None
    if source_index:
        return f"{source_slot} #{source_index} ({source_clock})" if source_clock else f"{source_slot} #{source_index}"
    return str(source_slot)


def _wait_slot_source_meta(config: dict[str, Any], symbol: str) -> dict[str, Any]:
    rows = lc_pipeline_pool_rows(config, [symbol])
    row = rows[0] if rows and isinstance(rows[0], dict) else {}
    return {
        "source_slot": row.get("source_slot"),
        "source_index": row.get("source_index"),
        "source_time": row.get("source_time"),
        "source_label": row.get("source_label"),
        "source_text": _wait_slot_source_label(row) if row else "Mini scan",
    }


def _wait_slot_notification_text(
    config: dict[str, Any],
    candidate: TradeCandidate,
    *,
    wait_slot_index: Any = None,
    source_text: str,
    queued_at: Any,
) -> str:
    local_time = _parse_iso_time(queued_at) or datetime.now(timezone.utc)
    local_time = local_time.astimezone(_engine_timezone(config))
    side = str(candidate.side or "-").upper()
    wait_slot_label = (
        f"#{int(wait_slot_index)}_WS"
        if str(wait_slot_index or "").strip().isdigit()
        else "#-_WS"
    )
    lines = [
        f"🟡 WAIT_SLOT {wait_slot_label}",
        f"Cặp: {candidate.symbol} | {side}",
        f"Nguồn lọc: {source_text}",
        f"Đã chuyển vào wait_slot lúc {local_time.strftime('%H:%M:%S')}",
    ]
    return "\n".join(lines)


def _next_wait_slot_notification_index(config: dict[str, Any]) -> int | None:
    try:
        raw = get_journal_state(config, WAIT_SLOT_NOTIFICATION_COUNTER_KEY)
        current = int(str(raw or "0").strip() or "0")
        next_value = current + 1
        set_journal_state(config, WAIT_SLOT_NOTIFICATION_COUNTER_KEY, str(next_value))
        return next_value
    except Exception:
        return None


def _record_wait_slot_notification(
    config: dict[str, Any],
    candidate: TradeCandidate,
    *,
    source_text: str,
    queued_at: Any,
) -> dict[str, Any]:
    wait_slot_index = _next_wait_slot_notification_index(config)
    item = {
        "created_at": str(queued_at or datetime.now(timezone.utc).isoformat()),
        "index": wait_slot_index,
        "label": f"#{wait_slot_index}_WS" if wait_slot_index is not None else "#-_WS",
        "symbol": candidate.symbol,
        "side": str(candidate.side or "").upper(),
        "source_text": source_text,
        "queued_at": str(queued_at or ""),
    }
    item["text"] = _wait_slot_notification_text(
        config,
        candidate,
        wait_slot_index=wait_slot_index,
        source_text=source_text,
        queued_at=queued_at,
    )
    try:
        raw = get_journal_state(config, WAIT_SLOT_NOTIFICATION_HISTORY_KEY)
        history = json.loads(str(raw or "[]")) if raw else []
        if not isinstance(history, list):
            history = []
        history.append(item)
        set_journal_state(config, WAIT_SLOT_NOTIFICATION_HISTORY_KEY, json.dumps(history[-50:], ensure_ascii=False))
    except Exception:
        return item
    return item


def wait_slot_notification_timeline_messages(config: dict[str, Any], *, limit: int = 10) -> list[str]:
    try:
        raw = get_journal_state(config, WAIT_SLOT_NOTIFICATION_HISTORY_KEY)
        items = json.loads(str(raw or "[]")) if raw else []
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    texts = [str(item.get("text") or "") for item in items if isinstance(item, dict) and str(item.get("text") or "").strip()]
    return texts[-max(1, int(limit or 10)):]


def format_wait_slot_notifications_view(config: dict[str, Any], *, limit: int = 10) -> str:
    timeline_messages = wait_slot_notification_timeline_messages(config, limit=limit)
    if not timeline_messages:
        return "🟡 Wait Slot: chưa có thông báo nào."
    blocks = [
        "🟡 Thông báo Wait Slot",
        "Timeline các cặp đã được đưa vào wait_slot, mới nhất nằm dưới cùng.",
    ]
    blocks.extend(timeline_messages)
    return "\n\n".join(blocks)


def _notify_wait_slot_queued(
    config: dict[str, Any],
    candidate: TradeCandidate,
    *,
    source_text: str,
    queued_at: Any,
) -> dict[str, Any]:
    entry = _record_wait_slot_notification(
        config,
        candidate,
        source_text=source_text,
        queued_at=queued_at,
    )
    text = str(entry.get("text") or _wait_slot_notification_text(
        config,
        candidate,
        source_text=source_text,
        queued_at=queued_at,
    ))
    try:
        from .notifier import send_telegram_message
        send_telegram_message(
            config,
            text,
            with_buttons=False,
            replace_previous=False,
        )
        return entry
    except Exception:
        try:
            from .notifier import _telegram_api_request
        except Exception:
            return entry
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not chat_id:
            return entry
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:3900],
            "disable_web_page_preview": "true",
        }
        thread_id = os.getenv("TELEGRAM_MESSAGE_THREAD_ID", "").strip()
        if thread_id:
            payload["message_thread_id"] = thread_id
        try:
            _telegram_api_request(config, "sendMessage", payload)
        except Exception:
            return entry
    return entry


def _mini_system_notification_key(
    *,
    stage: str,
    reason: str,
    scan: dict[str, Any] | None,
    candidate: TradeCandidate | None,
) -> str:
    symbol = str(getattr(candidate, "symbol", "") or "")
    side = str(getattr(candidate, "side", "") or "")
    slot_id = str((scan or {}).get("slot_id") or (scan or {}).get("created_at") or "unknown")
    return "|".join([slot_id, stage, symbol, side, str(reason or "")[:180]])


def _mini_system_notification_text(
    config: dict[str, Any],
    *,
    stage: str,
    reason: str,
    scan: dict[str, Any] | None,
    candidate: TradeCandidate | None,
) -> str:
    now = datetime.now(timezone.utc)
    local_now = now.astimezone(_engine_timezone(config))
    selected = [str(symbol) for symbol in (scan or {}).get("selected_symbols") or [] if str(symbol)]
    pool = [str(symbol) for symbol in (scan or {}).get("pool_symbols") or [] if str(symbol)]
    lines = [
        "⚠️ Thông báo hệ thống",
        f"Thời gian: {local_now.strftime('%d/%m/%y %H:%M:%S')}",
        f"Bị chặn ở bước: {stage}",
        f"Slot Mini: {(scan or {}).get('slot_id') or '-'}",
        f"Trạng thái Mini: {(scan or {}).get('status') or '-'}",
    ]
    if candidate is not None:
        lines.append(f"Cặp: {candidate.symbol} | {str(candidate.side or '-').upper()}")
    if selected:
        lines.append("Mini đã chọn: " + ", ".join(selected[:3]))
    elif pool:
        lines.append("Pool Mini: " + ", ".join(pool[:3]))
    lines.append("Lý do chặn: " + (str(reason or "").strip() or "-"))
    lines.append("Hành động: không tạo LC_OKX/không gọi bước sau cho setup này.")
    return "\n".join(lines)


def _notify_mini_system_block(
    config: dict[str, Any],
    *,
    stage: str,
    reason: str,
    scan: dict[str, Any] | None,
    candidate: TradeCandidate | None = None,
) -> dict[str, Any] | None:
    reason = str(reason or "").strip()
    if not reason:
        return None
    fingerprint = _mini_system_notification_key(stage=stage, reason=reason, scan=scan, candidate=candidate)
    try:
        raw = get_journal_state(config, MINI_SYSTEM_NOTIFICATION_HISTORY_KEY)
        history = json.loads(str(raw or "[]")) if raw else []
    except Exception:
        history = []
    if not isinstance(history, list):
        history = []
    if any(isinstance(item, dict) and item.get("fingerprint") == fingerprint for item in history[-80:]):
        return None
    text = _mini_system_notification_text(config, stage=stage, reason=reason, scan=scan, candidate=candidate)
    entry = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "fingerprint": fingerprint,
        "stage": stage,
        "reason": reason,
        "symbol": getattr(candidate, "symbol", None),
        "side": getattr(candidate, "side", None),
        "slot_id": (scan or {}).get("slot_id"),
        "text": text,
    }
    history.append(entry)
    try:
        set_journal_state(config, MINI_SYSTEM_NOTIFICATION_HISTORY_KEY, json.dumps(history[-80:], ensure_ascii=False))
    except Exception:
        pass
    try:
        from .notifier import send_telegram_message

        send_telegram_message(config, text, with_buttons=False, replace_previous=False)
    except Exception:
        pass
    return entry


def _storage_warning(label: str, exc: Exception) -> str:
    return f"{label} unavailable: {exc}"


def _is_degradable_storage_error(exc: Exception) -> bool:
    if not is_retryable_storage_error(exc):
        return False
    message = str(exc).lower()
    if exc.__class__.__name__ in {
        "AutoReconnect",
        "ConnectionFailure",
        "ExecutionTimeout",
        "NetworkTimeout",
        "ServerSelectionTimeoutError",
        "WaitQueueTimeoutError",
    }:
        return True
    return any(
        token in message
        for token in (
            "mongodb",
            ".net:27017",
            "journal_state",
            "connection pool paused",
            "wait queue timeout",
        )
    )


def _best_effort_storage(
    operation: Callable[[], Any],
    *,
    default: Any,
    label: str,
    warnings: list[str],
) -> Any:
    try:
        return operation()
    except Exception as exc:
        if not _is_degradable_storage_error(exc):
            raise
        warnings.append(_storage_warning(label, exc))
        return default


def _block_candidates_for_storage_hold(
    candidates: list[TradeCandidate],
    *,
    reason: str,
) -> None:
    for candidate in candidates:
        candidate.margin_usdt = 0.0
        candidate.order_usdt = 0.0
        candidate.recovery_margin_usdt = None
        candidate.warnings.append(reason)


def _read_report_payload(config: dict[str, Any]) -> dict[str, Any] | None:
    path = project_path(config, config.get("report_path", "reports/latest_decision.json"))
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _load_previous_payload(config: dict[str, Any], warnings: list[str]) -> dict[str, Any] | None:
    try:
        return latest_decision_payload(config)
    except Exception as exc:
        if not _is_degradable_storage_error(exc):
            raise
        fallback = _read_report_payload(config)
        source = "local report fallback" if fallback else "no local fallback available"
        warnings.append(_storage_warning(f"Previous decision memory ({source})", exc))
        return fallback


def _universe_uses_top_volume(config: dict[str, Any]) -> bool:
    universe = config.get("strategy", {}).get("universe", {})
    mode = str(universe.get("mode", "configured") or "configured")
    enabled = bool(universe.get("enabled", mode == "top_volume_24h"))
    return enabled and mode == "top_volume_24h"


def _resolve_strategy_symbols(
    config: dict[str, Any],
    *,
    market_data: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, Any], list[str]]:
    strategy_config = config.get("strategy", {})
    configured_symbols = _ordered_unique(strategy_config.get("symbols", []))
    shortlist_symbols, shortlist_state = internal_market_shortlist(config)
    internal_config = config.get("ai", {}).get("internal", {})
    use_shortlist_as_universe = not bool(internal_config.get("market_scan_to_pending", True))
    if use_shortlist_as_universe and shortlist_state and not shortlist_state.get("stale"):
        return (
            _ordered_unique(shortlist_symbols),
            {
                "enabled": True,
                "mode": "ai_internal_market_scan",
                "source": "gpt-mini-4h-shortlist",
                "max_symbols": len(shortlist_symbols),
                "symbols": _ordered_unique(shortlist_symbols),
                "scan_created_at": shortlist_state.get("created_at"),
                "model": shortlist_state.get("model"),
                "provider": shortlist_state.get("provider"),
                "candidate_count": shortlist_state.get("candidate_count"),
            },
            [],
        )
    universe = strategy_config.get("universe", {})
    mode = str(universe.get("mode", "configured") or "configured")
    enabled = bool(universe.get("enabled", mode == "top_volume_24h"))
    if not enabled or mode != "top_volume_24h":
        return configured_symbols, {"enabled": False, "mode": "configured", "symbols": configured_symbols}, []

    max_symbols = max(1, min(30, int(universe.get("max_symbols", 30) or 30)))
    volume_symbols, warnings = fetch_top_volume_symbols(config, market_data=market_data)
    if volume_symbols:
        selected = _ordered_unique(volume_symbols)[:max_symbols]
        return (
            selected,
            {
                "enabled": True,
                "mode": "top_volume_24h",
                "source": "okx_24h_volume",
                "max_symbols": max_symbols,
                "quote": str(universe.get("quote", "USDT") or "USDT"),
                "symbols": selected,
            },
            warnings,
        )

    return (
        configured_symbols,
        {
            "enabled": True,
            "mode": "top_volume_24h",
            "source": "configured_fallback",
            "max_symbols": max_symbols,
            "quote": str(universe.get("quote", "USDT") or "USDT"),
            "symbols": configured_symbols,
            "warnings": warnings,
        },
        warnings,
    )


def _collect_realtime_scan_inputs(
    config: dict[str, Any],
    *,
    previous_payload: dict[str, Any] | None = None,
    storage_warnings: list[str] | None = None,
    progress_callback: Callable[[str, dict[str, Any] | None], None] | None = None,
) -> dict[str, Any]:
    storage_warnings = storage_warnings if storage_warnings is not None else []
    old_symbols = _previous_symbols(previous_payload)
    _report_progress(progress_callback, "collect_realtime_scan_inputs.pending_symbols")
    pending_symbols_before_scan = _ordered_unique(
        list(
            _best_effort_storage(
                lambda: open_pending_symbols(config),
                default=set(),
                label="Pending order memory",
                warnings=storage_warnings,
            )
        )
    )
    prefetched_market_data: dict[str, Any] | None = None
    if _universe_uses_top_volume(config):
        _report_progress(progress_callback, "collect_realtime_scan_inputs.prefetch_top_volume_market_data")
        prefetched_market_data = prefetch_market_data(config, require_all_tickers=True)
    _report_progress(progress_callback, "collect_realtime_scan_inputs.resolve_strategy_symbols")
    strategy_symbols, universe_context, universe_warnings = _resolve_strategy_symbols(
        config,
        market_data=prefetched_market_data,
    )
    fetch_symbols = _ordered_unique(strategy_symbols + old_symbols + pending_symbols_before_scan)
    if prefetched_market_data is None:
        _report_progress(progress_callback, "collect_realtime_scan_inputs.prefetch_snapshot_market_data", symbol_count=len(fetch_symbols))
        prefetched_market_data = prefetch_market_data(config, symbols=fetch_symbols)
    _report_progress(progress_callback, "collect_realtime_scan_inputs.collect_news")
    digest = collect_news(config)
    _report_progress(progress_callback, "collect_realtime_scan_inputs.fetch_market_snapshots", symbol_count=len(fetch_symbols))
    snapshots, market_warnings = fetch_market_snapshots(config, fetch_symbols, market_data=prefetched_market_data)
    market_warnings = storage_warnings + universe_warnings + market_warnings
    snapshots_by_symbol = {snapshot.symbol: snapshot for snapshot in snapshots}
    market_layers: dict[str, dict[str, Any]] = {}
    market_layer_warnings: list[str] = []
    if config.get("market_guard", {}).get("use_memory_in_strategy", True):
        try:
            _report_progress(progress_callback, "collect_realtime_scan_inputs.load_market_guard_memory", symbol_count=len(fetch_symbols))
            market_layers = market_guard_symbol_layers(config, fetch_symbols)
        except Exception as exc:
            market_layer_warnings.append(f"Market guard memory unavailable: {exc}")

    new_scan_symbols = _ordered_unique(strategy_symbols + pending_symbols_before_scan)
    new_scan_snapshots = [
        snapshots_by_symbol[symbol] for symbol in new_scan_symbols if symbol in snapshots_by_symbol
    ]
    old_scan_snapshots = [
        snapshots_by_symbol[symbol] for symbol in old_symbols if symbol in snapshots_by_symbol
    ]

    _report_progress(progress_callback, "collect_realtime_scan_inputs.build_candidates", snapshot_count=len(new_scan_snapshots))
    all_new_candidates = build_candidates(
        config,
        new_scan_snapshots,
        digest,
        limit=None,
        market_layers=market_layers,
    )
    _report_progress(progress_callback, "collect_realtime_scan_inputs.apply_position_sizing", candidate_count=len(all_new_candidates))
    try:
        apply_position_sizing(config, all_new_candidates)
    except Exception as exc:
        if not _is_degradable_storage_error(exc):
            raise
        storage_warnings.append(_storage_warning("Position sizing state", exc))
        _block_candidates_for_storage_hold(
            all_new_candidates,
            reason="Position sizing state unavailable; holding new entries until storage recovers",
        )
    _report_progress(progress_callback, "collect_realtime_scan_inputs.enrich_quantities", candidate_count=len(all_new_candidates))
    enrich_quantities(config, all_new_candidates)
    _report_progress(progress_callback, "collect_realtime_scan_inputs.detect_market_regime")
    market_regime = detect_market_regime(config, new_scan_snapshots or snapshots)
    for candidate in all_new_candidates:
        candidate.market_regime = market_regime.get("regime")
        candidate.regime_confidence = market_regime.get("confidence")

    return {
        "strategy_symbols": strategy_symbols,
        "universe_context": universe_context,
        "universe_warnings": universe_warnings,
        "old_symbols": old_symbols,
        "pending_symbols_before_scan": pending_symbols_before_scan,
        "fetch_symbols": fetch_symbols,
        "digest": digest,
        "snapshots": snapshots,
        "snapshots_by_symbol": snapshots_by_symbol,
        "market_warnings": market_warnings,
        "market_layers": market_layers,
        "market_layer_warnings": market_layer_warnings,
        "new_scan_symbols": new_scan_symbols,
        "new_scan_snapshots": new_scan_snapshots,
        "old_scan_snapshots": old_scan_snapshots,
        "all_new_candidates": all_new_candidates,
        "market_regime": market_regime,
    }


def _serialize_lc_pipeline_candidate_cache(snapshot: dict[str, Any]) -> str:
    payload = {
        "enabled": bool(snapshot.get("enabled", True)),
        "started_at": snapshot.get("started_at"),
        "created_at": snapshot.get("created_at"),
        "candidate_count": int(snapshot.get("candidate_count") or 0),
        "source_symbol_count": int(snapshot.get("source_symbol_count") or 0),
        "source_symbols": list(snapshot.get("source_symbols") or []),
        "market_warnings": list(snapshot.get("market_warnings") or []),
        "market_layer_warnings": list(snapshot.get("market_layer_warnings") or []),
        "universe": dict(snapshot.get("universe") or {}),
        "market_regime": dict(snapshot.get("market_regime") or {}),
        "candidates": [candidate_to_payload(candidate) for candidate in list(snapshot.get("candidates") or [])],
    }
    return json.dumps(payload, ensure_ascii=False)


def save_lc_pipeline_candidate_cache(config: dict[str, Any], snapshot: dict[str, Any]) -> None:
    set_journal_state(config, LC_PIPELINE_CANDIDATE_CACHE_KEY, _serialize_lc_pipeline_candidate_cache(snapshot))


def load_lc_pipeline_candidate_cache(config: dict[str, Any]) -> dict[str, Any] | None:
    raw = get_journal_state(config, LC_PIPELINE_CANDIDATE_CACHE_KEY)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    candidate_payloads = payload.get("candidates") or []
    candidates: list[TradeCandidate] = []
    if isinstance(candidate_payloads, list):
        for item in candidate_payloads:
            if not isinstance(item, dict):
                continue
            try:
                candidates.append(candidate_from_payload(item))
            except Exception:
                continue
    return {
        "enabled": bool(payload.get("enabled", True)),
        "started_at": payload.get("started_at"),
        "created_at": payload.get("created_at"),
        "candidate_count": int(payload.get("candidate_count") or len(candidates)),
        "source_symbol_count": int(payload.get("source_symbol_count") or 0),
        "source_symbols": list(payload.get("source_symbols") or []),
        "market_warnings": list(payload.get("market_warnings") or []),
        "market_layer_warnings": list(payload.get("market_layer_warnings") or []),
        "universe": dict(payload.get("universe") or {}),
        "market_regime": dict(payload.get("market_regime") or {}),
        "candidates": candidates,
    }


def collect_lc_pipeline_candidates(config: dict[str, Any]) -> dict[str, Any]:
    config = select_runtime_config(config)
    started_at = datetime.now(timezone.utc)
    scan_inputs = _collect_realtime_scan_inputs(config)
    snapshot = {
        "enabled": True,
        "started_at": started_at.isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "candidate_count": len(scan_inputs["all_new_candidates"]),
        "source_symbol_count": len(scan_inputs["new_scan_symbols"]),
        "source_symbols": list(scan_inputs["new_scan_symbols"]),
        "market_warnings": list(scan_inputs["market_warnings"]),
        "market_layer_warnings": list(scan_inputs["market_layer_warnings"]),
        "universe": scan_inputs["universe_context"],
        "market_regime": scan_inputs["market_regime"],
        "candidates": list(scan_inputs["all_new_candidates"]),
    }
    save_lc_pipeline_candidate_cache(config, snapshot)
    return snapshot


def run_lc_pipeline_cycle(config: dict[str, Any]) -> dict[str, Any]:
    snapshot = collect_lc_pipeline_candidates(config)
    pipeline_now = datetime.now(timezone.utc)
    lc_pipeline_state = update_lc_internal_pipeline(
        config,
        list(snapshot.get("candidates") or []),
        now=pipeline_now,
    )
    return {
        **snapshot,
        "created_at": pipeline_now.isoformat(),
        "lc_internal_pipeline": lc_pipeline_state,
    }


def _previous_candidates(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not payload:
        return []
    candidates = payload.get("candidates")
    return candidates if isinstance(candidates, list) else []


def _previous_symbols(payload: dict[str, Any] | None) -> list[str]:
    return _ordered_unique(
        [
            str(candidate.get("symbol", ""))
            for candidate in _previous_candidates(payload)
            if isinstance(candidate, dict)
        ]
    )[:5]


def _candidate_sort_key(candidate: Any) -> tuple[float, float]:
    return (float(candidate.win_probability_pct or 0), float(candidate.confidence or 0))


def _annotate_scan_context(
    candidate: Any,
    previous_by_symbol: dict[str, dict[str, Any]],
    source: str,
) -> None:
    previous = previous_by_symbol.get(candidate.symbol)
    candidate.scan_source = source
    if not previous:
        candidate.previous_win_probability_pct = None
        candidate.win_delta_pct = None
        return
    previous_win = previous.get("win_probability_pct")
    if previous_win is None:
        candidate.previous_win_probability_pct = None
        candidate.win_delta_pct = None
        return
    candidate.previous_win_probability_pct = round(float(previous_win), 2)
    if candidate.win_probability_pct is not None:
        candidate.win_delta_pct = round(float(candidate.win_probability_pct) - float(previous_win), 2)


def _merge_cycle_candidates(
    new_top: list[Any],
    refreshed_previous: list[Any],
    previous_payload: dict[str, Any] | None,
) -> tuple[list[Any], dict[str, Any]]:
    previous_by_symbol = {
        str(candidate.get("symbol")): candidate
        for candidate in _previous_candidates(previous_payload)
        if isinstance(candidate, dict) and candidate.get("symbol")
    }
    by_symbol: dict[str, Any] = {}
    sources: dict[str, set[str]] = {}

    def add(candidate: Any, source: str) -> None:
        sources.setdefault(candidate.symbol, set()).add(source)
        existing = by_symbol.get(candidate.symbol)
        if existing is None or _candidate_sort_key(candidate) > _candidate_sort_key(existing):
            by_symbol[candidate.symbol] = candidate

    for candidate in new_top:
        add(candidate, "new_scan")
    for candidate in refreshed_previous:
        add(candidate, "old_rescan")

    for symbol, candidate in by_symbol.items():
        source_set = sources.get(symbol, set())
        if source_set == {"new_scan", "old_rescan"}:
            source = "new_and_old_rescan"
        elif "old_rescan" in source_set:
            source = "old_rescan"
        else:
            source = "new_scan"
        _annotate_scan_context(candidate, previous_by_symbol, source)

    ranked = sorted(by_symbol.values(), key=_candidate_sort_key, reverse=True)
    kept = ranked[:5]
    dropped = ranked[5:]
    return kept, {
        "enabled": True,
        "logic": "new top 5 plus refreshed previous top 5, then keep highest current win-rate",
        "previous_symbols": list(previous_by_symbol.keys())[:5],
        "new_top_symbols": [candidate.symbol for candidate in new_top],
        "refreshed_previous_symbols": [candidate.symbol for candidate in refreshed_previous],
        "kept_symbols": [candidate.symbol for candidate in kept],
        "dropped_symbols": [candidate.symbol for candidate in dropped],
    }


def _internal_scan_to_pending_enabled(config: dict[str, Any]) -> bool:
    internal_config = config.get("ai", {}).get("internal", {})
    return bool(internal_config.get("market_scan_to_pending", True))


def _internal_scan_allows_pending(config: dict[str, Any], scan: dict[str, Any] | None) -> tuple[bool, str]:
    if not scan:
        return False, "No internal mini scan is available"
    internal_config = config.get("ai", {}).get("internal", {})
    if not bool(internal_config.get("market_scan_to_pending", True)):
        return False, "Mini scan pending queue is disabled"
    selected_symbols = [str(symbol) for symbol in scan.get("selected_symbols") or [] if str(symbol)]
    if not selected_symbols:
        skip_reason = str(scan.get("skip_reason") or "").strip()
        if skip_reason:
            return False, skip_reason
        if scan.get("selection_stale"):
            return False, "Mini selection is stale because a newer LC noi bo 4h slot is available"
        return False, "Mini scan has no selected symbols"
    if bool(internal_config.get("market_scan_require_ai_for_pending", True)):
        if scan.get("fallback") or scan.get("ai_review_error"):
            return False, "Mini scan did not finish with external AI approval"
        if str(scan.get("provider") or "") == "openai" and not scan.get("ai_review"):
            return False, "Mini scan has not returned an OpenAI review yet"
    return True, "Mini scan can create pending setups"


def _mini_pending_risk_config(config: dict[str, Any]) -> dict[str, Any]:
    pending_config = config.get("pending_orders", {})
    review_config = pending_config.get("review", {})
    risk_config = deepcopy(config)
    risk_config.setdefault("strategy", {})
    risk_config.setdefault("news", {})
    risk_config["strategy"]["min_confidence"] = float(
        review_config.get("min_confidence", risk_config["strategy"].get("min_confidence", 75)) or 75
    )
    risk_config["strategy"]["min_win_probability_pct"] = float(
        review_config.get("min_win_probability_pct", 50) or 50
    )
    risk_config["strategy"]["min_risk_reward"] = float(
        review_config.get("min_risk_reward", risk_config["strategy"].get("min_risk_reward", 1.5)) or 1.5
    )
    risk_config["news"]["require_symbol_news"] = bool(
        pending_config.get("require_symbol_news_for_mini_lc", False)
    )
    return risk_config


def _candidate_from_payload(payload: dict[str, Any]) -> TradeCandidate | None:
    if not isinstance(payload, dict):
        return None
    symbol = str(payload.get("symbol") or "")
    side = str(payload.get("side") or "").lower()
    if not symbol or side not in {"long", "short"}:
        return None
    fields = set(TradeCandidate.__dataclass_fields__.keys())
    clean = {key: payload.get(key) for key in fields if key in payload}
    clean.setdefault("symbol", symbol)
    clean.setdefault("base", str(payload.get("base") or symbol.split("/")[0]))
    clean.setdefault("side", side)
    clean.setdefault("confidence", float(payload.get("confidence") or 0))
    clean.setdefault("entry", float(payload.get("entry") or 0))
    clean.setdefault("stop_loss", float(payload.get("stop_loss") or 0))
    clean.setdefault("take_profit", float(payload.get("take_profit") or 0))
    clean.setdefault("risk_reward", float(payload.get("risk_reward") or 0))
    clean.setdefault("order_usdt", float(payload.get("order_usdt") or 0))
    clean.setdefault("quantity", payload.get("quantity"))
    clean.setdefault("spread_pct", payload.get("spread_pct"))
    clean.setdefault("news_score", float(payload.get("news_score") or 0))
    clean.setdefault("news_count", int(payload.get("news_count") or 0))
    clean.setdefault("higher_timeframes", payload.get("higher_timeframes") or {})
    clean.setdefault("indicator_summary", payload.get("indicator_summary") or {})
    clean.setdefault("candlestick_patterns", payload.get("candlestick_patterns") or {})
    clean.setdefault("reasons", payload.get("reasons") or [])
    clean.setdefault("warnings", payload.get("warnings") or [])
    return TradeCandidate(**clean)


def _internal_lc_candidate_cache(
    config: dict[str, Any],
    symbols: list[str],
) -> dict[str, TradeCandidate]:
    cached_rows: list[TradeCandidate] = []
    for row in lc_pipeline_pool_rows(config, symbols):
        candidate = _candidate_from_payload((row or {}).get("payload") or {})
        if candidate is None:
            continue
        cached_rows.append(candidate)
    if cached_rows:
        apply_position_sizing(config, cached_rows)
        enrich_quantities(config, cached_rows)
    return {candidate.symbol: candidate for candidate in cached_rows}


def _internal_scan_candidate_cache(
    config: dict[str, Any],
    internal_scan: dict[str, Any] | None,
    symbols: list[str],
) -> dict[str, TradeCandidate]:
    wanted = {str(symbol) for symbol in symbols if str(symbol)}
    if not wanted:
        return {}
    cached_rows: list[TradeCandidate] = []
    for item in (internal_scan or {}).get("candidates") or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "")
        if symbol not in wanted:
            continue
        candidate = _candidate_from_payload(item)
        if candidate is None:
            continue
        candidate.scan_source = "mini_scan_snapshot"
        cached_rows.append(candidate)
    if cached_rows:
        apply_position_sizing(config, cached_rows)
        enrich_quantities(config, cached_rows)
    return {candidate.symbol: candidate for candidate in cached_rows}


def _is_wait_slot_only_rejection(reasons: list[str]) -> bool:
    normalized = [str(reason or "").strip() for reason in reasons if str(reason or "").strip()]
    if not normalized:
        return False
    allowed_prefixes = (
        "Da het slot:",
        "Slot ",
        "Active trade limit reached:",
    )
    return all(any(reason.startswith(prefix) for prefix in allowed_prefixes) for reason in normalized)


def _with_wait_slot_metadata(
    candidate: TradeCandidate,
    *,
    reason: str,
    internal_scan: dict[str, Any] | None,
    source_meta: dict[str, Any] | None = None,
) -> TradeCandidate:
    queued = deepcopy(candidate)
    queued_at = datetime.now(timezone.utc).isoformat()
    queued.decision_metadata = {
        **(queued.decision_metadata or {}),
        "wait_slot_queue": {
            "queued_at": queued_at,
            "reason": reason,
            "scan_created_at": (internal_scan or {}).get("created_at"),
            "scan_slot_id": (internal_scan or {}).get("slot_id"),
            "pool_symbols": list((internal_scan or {}).get("pool_symbols") or []),
            "selected_symbols": list((internal_scan or {}).get("selected_symbols") or []),
            "source_slot": (source_meta or {}).get("source_slot"),
            "source_index": (source_meta or {}).get("source_index"),
            "source_time": (source_meta or {}).get("source_time"),
            "source_label": (source_meta or {}).get("source_label"),
            "source_text": (source_meta or {}).get("source_text"),
        },
    }
    return queued


def _create_pending_from_internal_scan(
    config: dict[str, Any],
    candidates: list[Any],
    internal_scan: dict[str, Any] | None,
    active_summary: Any,
    pending_symbols: set[str],
) -> dict[str, Any]:
    allowed, reason = _internal_scan_allows_pending(config, internal_scan)
    internal_config = config.get("ai", {}).get("internal", {})
    configured_pending_limit = max(1, min(3, int(internal_config.get("market_scan_pending_limit", 3) or 3)))
    # Mini flow only promotes the single best symbol into the final LC_OKX step.
    pending_limit = 1
    result: dict[str, Any] = {
        "enabled": _internal_scan_to_pending_enabled(config),
        "allowed": allowed,
        "reason": reason,
        "limit": pending_limit,
        "configured_limit": configured_pending_limit,
        "created": 0,
        "created_orders": [],
        "market": 0,
        "market_orders": [],
        "wait_slot": 0,
        "wait_slot_orders": [],
        "skipped": [],
        "system_notifications": [],
    }
    if not result["enabled"] or not allowed:
        notification = _notify_mini_system_block(
            config,
            stage="Mini -> LC_OKX",
            reason=reason,
            scan=internal_scan,
        )
        if notification:
            result["system_notifications"].append(notification)
        return result

    approved = [str(symbol) for symbol in (internal_scan or {}).get("selected_symbols") or [] if str(symbol)]
    approved = approved[:pending_limit]
    current_candidates_by_symbol = {candidate.symbol: candidate for candidate in candidates}
    cached_candidates_by_symbol = _internal_lc_candidate_cache(config, approved)
    missing_cached_symbols = [symbol for symbol in approved if symbol not in cached_candidates_by_symbol]
    scan_candidates_by_symbol = _internal_scan_candidate_cache(config, internal_scan, missing_cached_symbols)
    created_symbols = set(pending_symbols)
    for symbol in approved:
        if result["created"] >= pending_limit:
            break
        candidate = (
            cached_candidates_by_symbol.get(symbol)
            or scan_candidates_by_symbol.get(symbol)
            or current_candidates_by_symbol.get(symbol)
        )
        if candidate is None:
            skip_reason = "approved symbol not available in saved internal LC setup"
            result["skipped"].append(
                {
                    "symbol": symbol,
                    "reason": skip_reason,
                }
            )
            notification = _notify_mini_system_block(
                config,
                stage="Mini -> lấy setup đã lưu",
                reason=skip_reason,
                scan=internal_scan,
            )
            if notification:
                result["system_notifications"].append(notification)
            continue
        if candidate.symbol in created_symbols:
            skip_reason = "already pending or active in LC memory"
            result["skipped"].append({"symbol": candidate.symbol, "reason": skip_reason})
            notification = _notify_mini_system_block(
                config,
                stage="Mini -> kiểm tra trùng lệnh",
                reason=skip_reason,
                scan=internal_scan,
                candidate=candidate,
            )
            if notification:
                result["system_notifications"].append(notification)
            continue
        pending_risk_config = _mini_pending_risk_config(config)
        check = evaluate_candidate(
            pending_risk_config,
            candidate,
            active_summary=active_summary,
            enforce_active_limit=False,
            extra_active_symbols=created_symbols,
        )
        if not check.passed:
            check_reason = "; ".join(check.reasons[:3]) or "risk check failed"
            if _is_wait_slot_only_rejection(check.reasons):
                source_meta = _wait_slot_source_meta(config, candidate.symbol)
                journal_id = next_global_counter(config, "LC") if config.get("mode") != "dry_run" else None
                queued_candidate = _with_wait_slot_metadata(
                    candidate,
                    reason=check_reason,
                    internal_scan=internal_scan,
                    source_meta=source_meta,
                )
                record = save_pending_order(
                    config,
                    queued_candidate,
                    None,
                    status="WAIT_SLOT",
                    max_age_hours=float(config.get("pending_orders", {}).get("local_max_age_hours", 6) or 6),
                    journal_id=journal_id,
                )
                result["wait_slot"] += 1
                created_symbols.add(candidate.symbol)
                notification_entry = _notify_wait_slot_queued(
                    config,
                    candidate,
                    source_text=str(source_meta.get("source_text") or "Mini scan"),
                    queued_at=(queued_candidate.decision_metadata or {}).get("wait_slot_queue", {}).get("queued_at"),
                )
                result["wait_slot_orders"].append(
                    {
                        "id": record.get("id"),
                        "lc_id": journal_id or record.get("id"),
                        "status": "WAIT_SLOT",
                        "symbol": candidate.symbol,
                        "side": candidate.side,
                        "reason": check_reason,
                        "win_probability_pct": candidate.win_probability_pct,
                        "confidence": candidate.confidence,
                        "source": source_meta.get("source_text"),
                        "queued_at": (
                            (queued_candidate.decision_metadata or {}).get("wait_slot_queue", {}).get("queued_at")
                        ),
                        "wait_slot_id": notification_entry.get("label"),
                    }
                )
                continue
            result["skipped"].append(
                {
                    "symbol": candidate.symbol,
                    "side": candidate.side,
                    "reason": check_reason,
                }
            )
            notification = _notify_mini_system_block(
                config,
                stage="Mini -> risk gate",
                reason=check_reason,
                scan=internal_scan,
                candidate=candidate,
            )
            if notification:
                result["system_notifications"].append(notification)
            continue
        journal_id = next_global_counter(config, "LC") if config.get("mode") != "dry_run" else None
        reviewed_candidate = candidate
        if config.get("mode") != "dry_run":
            reviewed_candidate, okx_review = review_candidate_for_lc_okx(
                config,
                candidate,
                check,
                context={
                    "route": "lc_okx_setup_review",
                    "lc_id": journal_id,
                    "source": "mini_lc_okx",
                    "from_status": "MINI_APPROVED",
                },
            )
            if okx_review_requests_market_entry(okx_review):
                vt_id = next_global_counter(config, "VT") if config.get("mode") != "dry_run" else None
                execution = execute_candidate(
                    config,
                    reviewed_candidate,
                    order_type_override="market",
                    entry_type="mini_lc_market",
                    journal_type="VT",
                    journal_id=vt_id,
                )
                if not execution.submitted:
                    skip_reason = f"OKX market entry failed: {execution.message}"
                    result["skipped"].append(
                        {
                            "symbol": candidate.symbol,
                            "side": candidate.side,
                            "reason": skip_reason,
                        }
                    )
                    notification = _notify_mini_system_block(
                        config,
                        stage="Mini -> Market",
                        reason=skip_reason,
                        scan=internal_scan,
                        candidate=reviewed_candidate,
                    )
                    if notification:
                        result["system_notifications"].append(notification)
                    continue
                result["market"] += 1
                created_symbols.add(candidate.symbol)
                result["market_orders"].append(
                    {
                        "vt_id": vt_id,
                        "symbol": reviewed_candidate.symbol,
                        "side": reviewed_candidate.side,
                        "exchange_order_id": execution.order_id,
                        "win_probability_pct": reviewed_candidate.win_probability_pct,
                        "confidence": reviewed_candidate.confidence,
                        "okx_review": candidate_okx_review(reviewed_candidate, route="lc_okx_setup_review"),
                        "execution": to_jsonable(execution),
                    }
                )
                continue
            if not okx_review_allows_okx_submission(okx_review):
                reason = str(okx_review.get("reason") or okx_review.get("decision") or "GPT-5.5 rejected LC_OKX setup")
                cache_reason = str(okx_review.get("cache_reason") or "").strip()
                notify_reason = f"{cache_reason}: {reason}" if cache_reason else reason
                result["skipped"].append(
                    {
                        "symbol": candidate.symbol,
                        "side": candidate.side,
                        "reason": reason,
                    }
                )
                notification = _notify_mini_system_block(
                    config,
                    stage="Mini -> 5.5/LC_OKX",
                    reason=notify_reason,
                    scan=internal_scan,
                    candidate=candidate,
                )
                if notification:
                    result["system_notifications"].append(notification)
                continue
        exchange_order_id: str | None = None
        order_status = "OPEN"
        if config.get("mode") != "dry_run":
            order_type = str(config.get("pending_orders", {}).get("order_type", "limit") or "limit")
            execution = execute_candidate(
                config,
                reviewed_candidate,
                order_type_override=order_type,
                entry_type="mini_lc_okx",
                journal_type="LC",
                journal_id=journal_id,
            )
            if not execution.submitted or not execution.order_id:
                skip_reason = f"OKX LC submit failed: {execution.message}"
                result["skipped"].append(
                    {
                        "symbol": candidate.symbol,
                        "side": candidate.side,
                        "reason": skip_reason,
                    }
                )
                notification = _notify_mini_system_block(
                    config,
                    stage="Mini -> gửi lệnh chờ OKX",
                    reason=skip_reason,
                    scan=internal_scan,
                    candidate=reviewed_candidate,
                )
                if notification:
                    result["system_notifications"].append(notification)
                continue
            exchange_order_id = execution.order_id
            order_status = "LC_OKX"
        record = save_pending_order(
            config,
            reviewed_candidate,
            exchange_order_id,
            max_age_days=float(config.get("pending_orders", {}).get("exchange_max_age_days", 1.5) or 1.5)
            if exchange_order_id
            else float(config.get("pending_orders", {}).get("max_age_days", 3) or 3),
            max_age_hours=None if exchange_order_id else float(config.get("pending_orders", {}).get("local_max_age_hours", 6) or 6),
            journal_id=journal_id,
        )
        result["created"] += 1
        created_symbols.add(candidate.symbol)
        result["created_orders"].append(
            {
                "id": record.get("id"),
                "lc_id": journal_id or record.get("id"),
                "status": order_status,
                "symbol": reviewed_candidate.symbol,
                "side": reviewed_candidate.side,
                "exchange_order_id": exchange_order_id,
                "win_probability_pct": reviewed_candidate.win_probability_pct,
                "confidence": reviewed_candidate.confidence,
                "okx_review": candidate_okx_review(reviewed_candidate, route="lc_okx_setup_review"),
            }
        )
    return result


def write_report(config: dict[str, Any], decision: Decision) -> Path:
    path = project_path(config, config.get("report_path", "reports/latest_decision.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(to_jsonable(decision), handle, ensure_ascii=False, indent=2)
    return path


def force_mini_scan_from_latest_four_hour(config: dict[str, Any]) -> dict[str, Any]:
    internal_market_scan = run_internal_market_scan(config, force=True)
    mini_pending_queue = _create_pending_from_internal_scan(
        config,
        [],
        internal_market_scan,
        active_trades_summary(config),
        open_pending_symbols(config),
    )
    return {
        "internal_market_scan": internal_market_scan,
        "mini_pending_queue": mini_pending_queue,
    }


def run_once(
    config: dict[str, Any],
    execute: bool,
    *,
    progress_callback: Callable[[str, dict[str, Any] | None], None] | None = None,
) -> Decision:
    config = select_runtime_config(config)
    storage_warnings: list[str] = []
    _report_progress(progress_callback, "load_previous_payload")
    previous_payload = _load_previous_payload(config, storage_warnings)
    _report_progress(progress_callback, "collect_realtime_scan_inputs")
    scan_inputs = _collect_realtime_scan_inputs(
        config,
        previous_payload=previous_payload,
        storage_warnings=storage_warnings,
        progress_callback=progress_callback,
    )
    strategy_symbols = list(scan_inputs["strategy_symbols"])
    universe_context = dict(scan_inputs["universe_context"])
    digest = scan_inputs["digest"]
    old_symbols = list(scan_inputs["old_symbols"])
    pending_symbols_before_scan = list(scan_inputs["pending_symbols_before_scan"])
    snapshots = list(scan_inputs["snapshots"])
    market_warnings = list(scan_inputs["market_warnings"])
    snapshots_by_symbol = dict(scan_inputs["snapshots_by_symbol"])
    market_layers = dict(scan_inputs["market_layers"])
    market_layer_warnings = list(scan_inputs["market_layer_warnings"])
    new_scan_snapshots = list(scan_inputs["new_scan_snapshots"])
    old_scan_snapshots = list(scan_inputs["old_scan_snapshots"])
    all_new_candidates = list(scan_inputs["all_new_candidates"])
    market_regime = dict(scan_inputs["market_regime"])
    _report_progress(progress_callback, "record_trade_candidates", candidate_count=len(all_new_candidates))
    _best_effort_storage(
        lambda: record_trade_candidates(config, all_new_candidates),
        default=0,
        label="Trade candidate history",
        warnings=storage_warnings,
    )
    # Refresh the LC pipeline before Mini so the same cycle can advance
    # 1h -> 2h -> 4h before Mini snapshots the latest 4h pool.
    _report_progress(progress_callback, "update_lc_internal_pipeline")
    lc_pipeline_state = _best_effort_storage(
        lambda: update_lc_internal_pipeline(config, all_new_candidates),
        default={"enabled": False, "warnings": ["LC pipeline update skipped because storage is unavailable"]},
        label="LC internal pipeline",
        warnings=storage_warnings,
    )
    _report_progress(progress_callback, "run_internal_market_scan")
    internal_market_scan = _best_effort_storage(
        lambda: run_internal_market_scan_if_due(config),
        default=None,
        label="Internal mini scan",
        warnings=storage_warnings,
    )
    _report_progress(progress_callback, "save_market_scan_observations")
    market_scan_storage = {
        "saved_rows": _best_effort_storage(
            lambda: save_market_scan_observations(
                config,
                all_new_candidates,
                source="continuous_1m_5m_1h_scan",
                limit=int(config.get("market_scan_memory", {}).get("max_saved_candidates_per_scan", 20) or 20),
            ),
            default=0,
            label="Market scan memory",
            warnings=storage_warnings,
        ),
        "timeframes": [
            str(config.get("strategy", {}).get("timeframe", "1m")),
            *[
                str(frame)
                for frame in config.get("strategy", {}).get("confirmation_timeframes", {}).get("frames", [])
            ],
        ],
    }
    if storage_warnings:
        market_scan_storage["warnings"] = list(storage_warnings)
    if internal_market_scan:
        universe_context["internal_market_scan"] = {
            "created_at": internal_market_scan.get("created_at"),
            "provider": internal_market_scan.get("provider"),
            "model": internal_market_scan.get("model"),
            "approved_symbols": internal_market_scan.get("approved_symbols"),
            "candidate_count": internal_market_scan.get("candidate_count"),
            "fallback": internal_market_scan.get("fallback"),
            "ai_review_error": internal_market_scan.get("ai_review_error"),
        }
    _report_progress(progress_callback, "merge_cycle_candidates")
    new_top = all_new_candidates[:5]
    refreshed_previous = (
        build_candidates(
            config,
            old_scan_snapshots,
            digest,
            limit=None,
            market_layers=market_layers,
        )
        if old_symbols
        else []
    )
    candidates, scan_comparison = _merge_cycle_candidates(new_top, refreshed_previous, previous_payload)
    scan_comparison["market_scan_storage"] = market_scan_storage
    scan_comparison["lc_internal_pipeline"] = lc_pipeline_state
    scan_comparison["universe"] = universe_context
    scan_comparison["market_regime"] = market_regime
    if internal_market_scan:
        scan_comparison["internal_market_scan"] = internal_market_scan
    if market_layers:
        selected_symbols = _ordered_unique([candidate.symbol for candidate in candidates] + pending_symbols_before_scan)
        scan_comparison["market_guard_layers"] = {
            "enabled": True,
            "top_risk": market_guard_top_risk(market_layers, limit=5),
            "symbols": {
                symbol: market_layers.get(symbol)
                for symbol in selected_symbols
                if symbol in market_layers
            },
        }
    elif market_layer_warnings:
        scan_comparison["market_guard_layers"] = {
            "enabled": False,
            "warnings": market_layer_warnings,
        }
    _report_progress(progress_callback, "apply_position_sizing")
    try:
        scan_comparison["position_sizing"] = apply_position_sizing(config, candidates)
    except Exception as exc:
        if not _is_degradable_storage_error(exc):
            raise
        storage_warnings.append(_storage_warning("Position sizing state", exc))
        hold_reason = "Position sizing state unavailable; holding new entries until storage recovers"
        _block_candidates_for_storage_hold(candidates, reason=hold_reason)
        scan_comparison["position_sizing"] = {
            "enabled": False,
            "blocked": True,
            "block_reason": hold_reason,
            "warnings": [hold_reason],
        }

    _report_progress(progress_callback, "enrich_quantities")
    quantity_warnings = enrich_quantities(config, candidates)
    if candidates and (market_warnings or quantity_warnings or market_layer_warnings):
        candidates[0].warnings.extend(market_warnings + quantity_warnings + market_layer_warnings)

    _report_progress(progress_callback, "maintain_pending_orders")
    ai_internal_before_pending = _best_effort_storage(
        lambda: internal_lc_memory(config),
        default={},
        label="Internal LC memory",
        warnings=storage_warnings,
    )
    scan_comparison["ai_internal_before_pending"] = ai_internal_before_pending
    review_candidates_by_key = {
        (candidate.symbol, candidate.side): candidate
        for candidate in [*all_new_candidates, *refreshed_previous, *candidates]
    }
    pending_review = _best_effort_storage(
        lambda: maintain_pending_orders(
            config,
            list(review_candidates_by_key.values()),
            allow_release=execute,
            market_layers=market_layers,
        ),
        default={
            "enabled": bool(config.get("pending_orders", {}).get("enabled", True)),
            "warnings": ["Pending order maintenance skipped because storage is unavailable"],
        },
        label="Pending order maintenance",
        warnings=storage_warnings,
    )
    ai_internal_after_pending = _best_effort_storage(
        lambda: internal_lc_memory(config),
        default={},
        label="Internal LC memory",
        warnings=storage_warnings,
    )
    scan_comparison["ai_internal_after_pending"] = ai_internal_after_pending
    defer_new_vt_to_internal_lc = execute and should_defer_new_vt_to_internal_lc(config, ai_internal_before_pending)
    scan_comparison["ai_router"] = {
        "enabled": True,
        "defer_new_vt_to_internal_lc": defer_new_vt_to_internal_lc,
        "priority": ["LC_OKX", "OPEN", "new_scan_candidate"],
    }
    pending_symbols = _best_effort_storage(
        lambda: open_pending_symbols(config),
        default=set(),
        label="Pending order memory",
        warnings=storage_warnings,
    )
    active_summary = active_trades_summary(config)
    active_count, _active_symbols, active_warnings = active_summary
    max_active = int(config.get("risk", {}).get("max_active_trades", 1))
    mini_pending_queue = None
    if (
        execute
        and _internal_scan_to_pending_enabled(config)
        and not defer_new_vt_to_internal_lc
        and config.get("pending_orders", {}).get("enabled", True)
    ):
        _report_progress(progress_callback, "create_pending_from_mini")
        mini_pending_queue = _create_pending_from_internal_scan(
            config,
            candidates,
            internal_market_scan,
            active_summary,
            pending_symbols,
        )
        scan_comparison["mini_pending_queue"] = mini_pending_queue

    selected = None
    risk_check = RiskCheck(False, ["No candidate passed risk checks"], market_warnings + quantity_warnings)
    if defer_new_vt_to_internal_lc:
        preferred = ai_internal_before_pending.get("preferred") or {}
        risk_check = RiskCheck(
            False,
            [
                "OKX AI deferred new VT because internal LC memory has priority: "
                f"{preferred.get('status') or 'LC'} #{preferred.get('lc_id') or '-'}"
            ],
            market_warnings + quantity_warnings,
        )
    else:
        _report_progress(progress_callback, "evaluate_candidates")
        for candidate in candidates:
            current_check = evaluate_candidate(
                config,
                candidate,
                active_summary=active_summary,
                extra_active_symbols=pending_symbols,
            )
            if current_check.passed:
                selected = candidate
                risk_check = current_check
                break
            if selected is None and candidate is candidates[0]:
                risk_check = current_check

    market_from_mini = bool((mini_pending_queue or {}).get("market"))
    queued_from_mini = bool((mini_pending_queue or {}).get("created"))
    waiting_slot_from_mini = bool((mini_pending_queue or {}).get("wait_slot"))
    if market_from_mini:
        first_market = (mini_pending_queue or {}).get("market_orders", [{}])[0]
        selected = next(
            (
                candidate
                for candidate in candidates
                if candidate.symbol == first_market.get("symbol") and candidate.side == first_market.get("side")
            ),
            selected,
        )
        risk_check = RiskCheck(True, [], market_warnings + quantity_warnings)
    elif queued_from_mini:
        first_created = (mini_pending_queue or {}).get("created_orders", [{}])[0]
        selected = next(
            (
                candidate
                for candidate in candidates
                if candidate.symbol == first_created.get("symbol") and candidate.side == first_created.get("side")
            ),
            selected,
        )
        risk_check = RiskCheck(True, [], market_warnings + quantity_warnings)
    elif waiting_slot_from_mini:
        first_waiting = (mini_pending_queue or {}).get("wait_slot_orders", [{}])[0]
        selected = next(
            (
                candidate
                for candidate in candidates
                if candidate.symbol == first_waiting.get("symbol") and candidate.side == first_waiting.get("side")
            ),
            selected,
        )
        risk_check = RiskCheck(
            False,
            [str(first_waiting.get("reason") or "Mini setup duoc dua vao hang tai kiem vi da het slot")],
            market_warnings + quantity_warnings,
        )
    execution_result: ExecutionResult | None = None
    action = "hold"
    if market_from_mini and selected:
        first_market = (mini_pending_queue or {}).get("market_orders", [{}])[0]
        execution_payload = first_market.get("execution") if isinstance(first_market.get("execution"), dict) else {}
        action = f"{selected.side}_{selected.symbol}"
        execution_result = ExecutionResult(
            mode=str(execution_payload.get("mode") or config.get("mode", "dry_run")),
            submitted=bool(execution_payload.get("submitted", True)),
            order_id=execution_payload.get("order_id"),
            message=str(execution_payload.get("message") or "GPT 5.5 selected immediate market entry from Mini review"),
            raw={"mini_pending_queue": mini_pending_queue, "mini_market_entry": True},
            journal_type=str(execution_payload.get("journal_type") or "VT"),
            journal_id=execution_payload.get("journal_id") or first_market.get("vt_id"),
        )
    elif queued_from_mini and selected:
        action = f"pending_{selected.side}_{selected.symbol}"
        execution_result = ExecutionResult(
            mode=config.get("mode", "dry_run"),
            submitted=True,
            order_id=None,
            message=(
                f"{int((mini_pending_queue or {}).get('created') or 0)} GPT mini setup(s) submitted as LC_OKX; "
                "GPT 5.5 da duyet setup truoc khi dua vao pipeline cho buoc kiem tra cuoi"
            ),
            raw={"lc_okx_pending": True, "mini_pending_queue": mini_pending_queue},
            journal_type="LC",
            journal_id=first_created.get("lc_id"),
        )
    elif waiting_slot_from_mini and selected:
        first_waiting = (mini_pending_queue or {}).get("wait_slot_orders", [{}])[0]
        action = "hold"
        execution_result = ExecutionResult(
            mode=config.get("mode", "dry_run"),
            submitted=False,
            order_id=None,
            message=(
                "GPT mini da chon setup nhung he thong dang day slot; "
                "setup duoc dua vao hang tai kiem va se doi cap nhat setup/win rate truoc khi gui tiep"
            ),
            raw={"mini_pending_queue": mini_pending_queue, "wait_slot_recheck": True},
            journal_type="LC",
            journal_id=first_waiting.get("lc_id"),
        )
    elif selected and risk_check.passed:
        action = f"{selected.side}_{selected.symbol}"
        if execute and _internal_scan_to_pending_enabled(config):
            action = "hold"
            reason = (
                ((mini_pending_queue or {}).get("reason") if mini_pending_queue else None)
                or "Mini pending queue did not create a setup"
            )
            risk_check = RiskCheck(False, [str(reason)], market_warnings + quantity_warnings)
            execution_result = ExecutionResult(
                mode=config.get("mode", "dry_run"),
                submitted=False,
                order_id=None,
                message="new VT blocked because entries must come from mini pending queue: " + str(reason),
                raw={"mini_pending_queue": mini_pending_queue},
            )
        elif execute:
            _report_progress(progress_callback, "pre_entry_check")
            pre_entry_pending_symbols = _best_effort_storage(
                lambda: open_pending_symbols(config),
                default=set(),
                label="Pending order memory",
                warnings=storage_warnings,
            )
            pre_entry_check = evaluate_candidate(
                config,
                selected,
                active_summary=active_trades_summary(config),
                extra_active_symbols=pre_entry_pending_symbols,
            )
            scan_comparison["pre_entry_check"] = {
                "enabled": True,
                "passed": pre_entry_check.passed,
                "reasons": pre_entry_check.reasons,
                "warnings": pre_entry_check.warnings,
            }
            if pre_entry_check.passed:
                _report_progress(progress_callback, "okx_ai_approval")
                ai_decision = okx_ai_approval(
                    config,
                    selected,
                    pre_entry_check,
                    context={"route": "new_vt", "source": "scan_top_win_rate"},
                    pending_memory=_best_effort_storage(
                        lambda: internal_lc_memory(config),
                        default={},
                        label="Internal LC memory",
                        warnings=storage_warnings,
                    ),
                )
                scan_comparison["ai_okx_approval"] = ai_decision
                if ai_decision.get("approved"):
                    final_validator_pending_symbols = _best_effort_storage(
                        lambda: open_pending_symbols(config),
                        default=set(),
                        label="Pending order memory",
                        warnings=storage_warnings,
                    )
                    final_validator = evaluate_candidate(
                        config,
                        selected,
                        active_summary=active_trades_summary(config),
                        extra_active_symbols=final_validator_pending_symbols,
                    )
                    scan_comparison["final_validator"] = {
                        "enabled": True,
                        "passed": final_validator.passed,
                        "reasons": final_validator.reasons,
                        "warnings": final_validator.warnings,
                    }
                    if not final_validator.passed:
                        action = "hold"
                        risk_check = final_validator
                        execution_result = ExecutionResult(
                            mode=config.get("mode", "dry_run"),
                            submitted=False,
                            order_id=None,
                            message="final validator blocked after OKX AI approval: "
                            + "; ".join(final_validator.reasons[:3]),
                        )
                    else:
                        _report_progress(progress_callback, "execute_candidate")
                        journal_id = next_global_counter(config, "VT") if config.get("mode") != "dry_run" else None
                        execution_result = execute_candidate(
                            config,
                            selected,
                            journal_type="VT",
                            journal_id=journal_id,
                        )
                else:
                    action = "hold"
                    risk_check = RiskCheck(False, [str(ai_decision.get("reason") or ai_decision.get("decision"))])
                    execution_result = ExecutionResult(
                        mode=config.get("mode", "dry_run"),
                        submitted=False,
                        order_id=None,
                        message="OKX AI approval blocked new VT: "
                        + str(ai_decision.get("reason") or ai_decision.get("decision")),
                    )
            else:
                action = "hold"
                risk_check = pre_entry_check
                execution_result = ExecutionResult(
                    mode=config.get("mode", "dry_run"),
                    submitted=False,
                    order_id=None,
                    message="pre-entry check blocked: " + "; ".join(pre_entry_check.reasons[:3]),
                )
        else:
            execution_result = ExecutionResult(
                mode=config.get("mode", "dry_run"),
                submitted=False,
                order_id=None,
                message="analysis only: order was not submitted",
            )
    elif (
        execute
        and not defer_new_vt_to_internal_lc
        and not _internal_scan_to_pending_enabled(config)
        and config.get("pending_orders", {}).get("enabled", True)
        and active_count is not None
        and active_count >= max_active
    ):
        for candidate in candidates:
            current_check = evaluate_candidate(
                config,
                candidate,
                active_summary=active_summary,
                enforce_active_limit=False,
                extra_active_symbols=pending_symbols,
            )
            if not current_check.passed:
                if candidate is candidates[0]:
                    risk_check = current_check
                continue
            selected = candidate
            risk_check = current_check
            action = f"pending_{selected.side}_{selected.symbol}"
            journal_id = next_global_counter(config, "LC") if config.get("mode") != "dry_run" else None
            save_pending_order(
                config,
                selected,
                None,
                max_age_hours=float(config.get("pending_orders", {}).get("local_max_age_hours", 6) or 6),
                journal_id=journal_id,
            )
            execution_result = ExecutionResult(
                mode=config.get("mode", "dry_run"),
                submitted=True,
                order_id=None,
                message="local pending order created; it will be submitted only when an active slot is available",
                raw={"local_pending": True, "max_active_trades": max_active, "active_count": active_count},
                journal_type="LC",
                journal_id=journal_id,
            )
            break
    elif active_count is None and active_warnings:
        risk_check.warnings.extend(active_warnings)

    decision = Decision(
        created_at=datetime.now(timezone.utc),
        mode=config.get("mode", "dry_run"),
        action=action,
        selected=selected,
        candidates=candidates,
        risk_check=risk_check,
        execution=execution_result,
        news_items=digest.items,
        scan_comparison={**scan_comparison, "pending_orders": pending_review},
    )
    if storage_warnings:
        decision.scan_comparison["storage_warnings"] = list(dict.fromkeys(storage_warnings))
    _report_progress(progress_callback, "save_decision")
    _best_effort_storage(
        lambda: save_decision(config, decision),
        default=None,
        label="Decision history",
        warnings=storage_warnings,
    )
    _report_progress(progress_callback, "record_ai_trade_decision")
    _best_effort_storage(
        lambda: record_ai_trade_decision(config, decision),
        default=None,
        label="AI trade history",
        warnings=storage_warnings,
    )
    if storage_warnings:
        decision.scan_comparison["storage_warnings"] = list(dict.fromkeys(storage_warnings))
    _report_progress(progress_callback, "write_report")
    write_report(config, decision)
    _report_progress(progress_callback, "completed", action=action)
    return decision
