from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from .market import fetch_market_snapshots
from .market_guard import market_guard_symbol_layers
from .models import TradeCandidate, to_jsonable
from .news import collect_news
from .risk import active_trades_summary
from .storage import (
    clear_dashboard_snapshot_cache,
    get_journal_state,
    open_pending_symbols,
    purge_deprecated_journal_state,
    set_journal_state,
)
from .strategy import build_candidates, enrich_quantities


LC_PIPELINE_STATE_KEY = "lc_internal_pipeline_state"
LC_PIPELINE_STATE_VERSION = 3
DEFAULT_TWO_HOUR_ICON = "🟡"
ONE_HOUR_ICON = "🔵"
FOUR_HOUR_ICON = "🔴"
MINI_ICON = "🟣"
ONE_HOUR_HISTORY_KEEP_DAYS = 3
TWO_HOUR_HISTORY_KEEP_DAYS = 3
FOUR_HOUR_HISTORY_KEEP_DAYS = 7
RECHECK_STABLE_DELTA_PCT = 1.0
_LC_PIPELINE_UPDATE_LOCK = threading.RLock()


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _pipeline_config(config: dict[str, Any]) -> dict[str, Any]:
    internal = config.get("ai", {}).get("internal", {})
    shared_min_win = float(internal.get("lc_pipeline_min_win_probability_pct", 62) or 62)
    promote_after_hours = max(1.0, float(internal.get("lc_pipeline_promote_after_hours", 6) or 6))
    undecided_max_age_hours = max(
        promote_after_hours,
        float(
            internal.get(
                "lc_pipeline_undecided_max_age_hours",
                max(12.0, promote_after_hours * 2),
            )
            or max(12.0, promote_after_hours * 2)
        ),
    )
    return {
        "enabled": bool(internal.get("lc_pipeline_enabled", True)),
        "top_limit": max(1, min(3, int(internal.get("lc_pipeline_top_limit", 3) or 3))),
        "undecided_max": max(3, int(internal.get("lc_pipeline_undecided_max", 6) or 6)),
        "undecided_prune_floor": max(3, int(internal.get("lc_pipeline_undecided_prune_floor", 6) or 6)),
        "undecided_prune_drop": max(1, int(internal.get("lc_pipeline_undecided_prune_drop", 3) or 3)),
        "internal_lc_max": max(1, min(3, int(internal.get("lc_pipeline_internal_lc_max", 3) or 3))),
        "promote_after_hours": promote_after_hours,
        "undecided_max_age_hours": undecided_max_age_hours,
        "recheck_interval_minutes": max(15, int(internal.get("lc_pipeline_recheck_interval_minutes", 90) or 90)),
        "slot_tolerance_minutes": max(1, min(10, int(internal.get("lc_pipeline_slot_tolerance_minutes", 3) or 3))),
        "min_win_probability_pct": shared_min_win,
        "one_hour_min_win_probability_pct": float(
            internal.get("lc_pipeline_one_hour_min_win_probability_pct", 61) or 61
        ),
        "two_hour_min_win_probability_pct": float(
            internal.get("lc_pipeline_two_hour_min_win_probability_pct", shared_min_win) or shared_min_win
        ),
        "four_hour_min_win_probability_pct": float(
            internal.get("lc_pipeline_four_hour_min_win_probability_pct", 63) or 63
        ),
        "relaxed_min_win_probability_pct": float(internal.get("lc_pipeline_relaxed_min_win_probability_pct", 55) or 55),
        "relaxed_min_confidence": float(internal.get("lc_pipeline_relaxed_min_confidence", 70) or 70),
        "relaxed_min_risk_reward": float(internal.get("lc_pipeline_relaxed_min_risk_reward", 1.5) or 1.5),
        "notify_one_hour_summary": bool(internal.get("lc_pipeline_notify_one_hour_summary", True)),
        "notify_two_hour_summary": bool(internal.get("lc_pipeline_notify_two_hour_summary", False)),
        "notify_undecided_recheck_summary": bool(internal.get("lc_pipeline_notify_undecided_recheck_summary", True)),
        "notify_mini_pool_summary": bool(internal.get("lc_pipeline_notify_mini_pool_summary", False)),
        "promote_survivors": bool(
            internal.get("lc_pipeline_promote_survivors", internal.get("lc_pipeline_promote_to_pending", True))
        ),
    }


def _two_hour_icon(config: dict[str, Any]) -> str:
    internal = config.get("ai", {}).get("internal", {})
    icon = str(internal.get("lc_pipeline_two_hour_icon") or DEFAULT_TWO_HOUR_ICON).strip()
    return icon or DEFAULT_TWO_HOUR_ICON


def _frame_icon(config: dict[str, Any], frame: str) -> str:
    frame_name = str(frame or "").lower()
    if frame_name == "1h":
        return ONE_HOUR_ICON
    if frame_name == "2h":
        return _two_hour_icon(config)
    if frame_name == "4h":
        return FOUR_HOUR_ICON
    if frame_name == "mini":
        return MINI_ICON
    return "🔹"


def _local_time(config: dict[str, Any], now: datetime) -> datetime:
    name = str(
        config.get("ai", {}).get("internal", {}).get("market_scan_timezone")
        or config.get("timezone")
        or "Asia/Ho_Chi_Minh"
    )
    if name in {"Asia/Ho_Chi_Minh", "Asia/Saigon", "UTC+7", "+07:00"}:
        return now.astimezone(timezone(timedelta(hours=7)))
    return now.astimezone(timezone.utc)


def _day_key(config: dict[str, Any], now: datetime) -> str:
    return _local_time(config, now).date().isoformat()


def _slot_key(config: dict[str, Any], now: datetime, hours: int) -> str:
    local = _local_time(config, now)
    slot_hour = (local.hour // hours) * hours
    return local.replace(hour=slot_hour, minute=0, second=0, microsecond=0).isoformat()


def _aligned_source_slots(config: dict[str, Any], target_slot: str, *, parent_hours: int, child_hours: int) -> list[str]:
    target_time = _parse_time(target_slot)
    if target_time is None:
        return []
    local_target = _local_time(config, target_time)
    offsets = range(parent_hours - child_hours, -1, -child_hours)
    return [(local_target - timedelta(hours=offset)).isoformat() for offset in offsets]


def _slot_is_open(config: dict[str, Any], now: datetime, *, hours: int, tolerance_minutes: int) -> bool:
    local_now = _local_time(config, now)
    slot_hour = (local_now.hour // hours) * hours
    slot_start = local_now.replace(hour=slot_hour, minute=0, second=0, microsecond=0)
    elapsed = (local_now - slot_start).total_seconds()
    return 0 <= elapsed <= max(1, int(tolerance_minutes)) * 60


def _fixed_interval_slot_key(config: dict[str, Any], now: datetime, *, minutes: int) -> str:
    interval_minutes = max(1, int(minutes))
    local_now = _local_time(config, now)
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes_since_midnight = local_now.hour * 60 + local_now.minute
    slot_minutes = (minutes_since_midnight // interval_minutes) * interval_minutes
    slot_start = midnight + timedelta(minutes=slot_minutes)
    return slot_start.isoformat()


def _fixed_interval_slot_is_exact(config: dict[str, Any], now: datetime, *, minutes: int) -> bool:
    interval_minutes = max(1, int(minutes))
    local_now = _local_time(config, now)
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes_since_midnight = local_now.hour * 60 + local_now.minute
    slot_minutes = (minutes_since_midnight // interval_minutes) * interval_minutes
    slot_start = midnight + timedelta(minutes=slot_minutes)
    elapsed = (local_now - slot_start).total_seconds()
    tolerance_seconds = max(1, int(_pipeline_config(config)["slot_tolerance_minutes"])) * 60
    return 0 <= elapsed <= tolerance_seconds


def _latest_events_for_slots(events: list[dict[str, Any]], slots: list[str]) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for slot in slots:
        event = next(
            (
                item
                for item in reversed(events)
                if isinstance(item, dict) and str(item.get("slot") or "") == slot
            ),
            None,
        )
        if event is not None:
            matched.append(event)
    return matched


def _event_source_slots(event: dict[str, Any]) -> list[str]:
    source_windows = event.get("source_windows") if isinstance(event.get("source_windows"), list) else []
    slots: list[str] = []
    for item in source_windows:
        if not isinstance(item, dict):
            continue
        slot = str(item.get("slot") or "").strip()
        if slot and slot not in slots:
            slots.append(slot)
    return slots


def _event_expected_source_slots(config: dict[str, Any], event: dict[str, Any]) -> list[str]:
    frame = str(event.get("frame") or "").lower()
    target_slot = str(event.get("slot") or "")
    if frame == "2h":
        return _aligned_source_slots(config, target_slot, parent_hours=2, child_hours=1)
    if frame == "4h":
        return _aligned_source_slots(config, target_slot, parent_hours=4, child_hours=2)
    return []


def _event_has_aligned_sources(config: dict[str, Any], event: dict[str, Any]) -> bool:
    frame = str(event.get("frame") or "").lower()
    if frame not in {"2h", "4h"}:
        return True
    actual_slots = _event_source_slots(event)
    expected_slots = _event_expected_source_slots(config, event)
    if not actual_slots or not expected_slots:
        return False
    return all(slot in expected_slots for slot in actual_slots)


def _latest_event_for_slot(
    events: list[dict[str, Any]],
    slot: str,
    *,
    config: dict[str, Any] | None = None,
    require_aligned_sources: bool = False,
) -> dict[str, Any] | None:
    for event in reversed(events):
        if not isinstance(event, dict) or str(event.get("slot") or "") != slot:
            continue
        if require_aligned_sources and config is not None and not _event_has_aligned_sources(config, event):
            continue
        return event
    return None


def _latest_events_for_slots_aligned(
    config: dict[str, Any],
    events: list[dict[str, Any]],
    slots: list[str],
) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for slot in slots:
        event = _latest_event_for_slot(events, slot, config=config, require_aligned_sources=True)
        if event is not None:
            matched.append(event)
    return matched


def _replace_event_for_slot(events: list[dict[str, Any]], replacement: dict[str, Any]) -> list[dict[str, Any]]:
    slot = str(replacement.get("slot") or "")
    frame = str(replacement.get("frame") or "")
    output: list[dict[str, Any]] = []
    replaced = False
    for event in events:
        if (
            isinstance(event, dict)
            and str(event.get("slot") or "") == slot
            and str(event.get("frame") or "") == frame
        ):
            if not replaced:
                output.append(replacement)
                replaced = True
            continue
        output.append(event)
    if not replaced:
        output.append(replacement)
    return output


def _latest_slot_from_events(config: dict[str, Any], events: list[dict[str, Any]], frame: str) -> str | None:
    slots: list[datetime] = []
    for event in events:
        if not isinstance(event, dict) or str(event.get("frame") or "").lower() != frame:
            continue
        event_time = _parse_time(event.get("slot"))
        if event_time is None:
            continue
        slots.append(event_time)
    if not slots:
        return None
    latest = max(slots)
    return _local_time(config, latest).isoformat()


def _sanitize_current_day_aggregate_state(config: dict[str, Any], state: dict[str, Any], now: datetime) -> None:
    current_day = _day_key(config, now)

    def _keep_event(event: dict[str, Any]) -> bool:
        if not isinstance(event, dict):
            return False
        frame = str(event.get("frame") or "").lower()
        if frame not in {"2h", "4h"}:
            return True
        event_time = _parse_time(event.get("slot") or event.get("created_at"))
        if event_time is None or _day_key(config, event_time) != current_day:
            return True
        return _event_has_aligned_sources(config, event)

    state["two_hour_history"] = [
        event for event in state.get("two_hour_history") or [] if isinstance(event, dict) and _keep_event(event)
    ]
    state["four_hour_history"] = [
        event for event in state.get("four_hour_history") or [] if isinstance(event, dict) and _keep_event(event)
    ]
    state["two_hour_windows"] = [
        event for event in state.get("two_hour_windows") or [] if isinstance(event, dict) and _keep_event(event)
    ]
    state["telegram_events"] = [
        event for event in state.get("telegram_events") or [] if isinstance(event, dict) and _keep_event(event)
    ]
    state["last_two_hour_slot"] = _latest_slot_from_events(config, state.get("two_hour_history") or [], "2h")
    state["last_four_hour_slot"] = _latest_slot_from_events(config, state.get("four_hour_history") or [], "4h")


def _is_same_local_day(config: dict[str, Any], value: Any, current_day: str) -> bool:
    event_time = _parse_time(value)
    if event_time is None:
        return False
    return _day_key(config, event_time) == current_day


def _candidate_score(candidate: TradeCandidate | dict[str, Any]) -> tuple[float, float, float]:
    if isinstance(candidate, dict):
        win_probability = candidate.get("win_probability_pct")
        confidence = candidate.get("confidence")
        volume_ratio = ((candidate.get("indicator_summary") or {}).get("volume_ratio"))
    else:
        win_probability = candidate.win_probability_pct
        confidence = candidate.confidence
        volume_ratio = (candidate.indicator_summary or {}).get("volume_ratio")
    try:
        win_value = float(win_probability or 0)
    except (TypeError, ValueError):
        win_value = 0.0
    try:
        confidence_value = float(confidence or 0)
    except (TypeError, ValueError):
        confidence_value = 0.0
    try:
        volume_value = float(volume_ratio or 0)
    except (TypeError, ValueError):
        volume_value = 0.0
    return (win_value, confidence_value, volume_value)


def _candidate_win_probability(candidate: TradeCandidate | dict[str, Any]) -> float:
    raw = candidate.get("win_probability_pct") if isinstance(candidate, dict) else candidate.win_probability_pct
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _peak_win_probability(*values: Any) -> float:
    valid = [_safe_float(value, float("-inf")) for value in values]
    valid = [value for value in valid if value != float("-inf")]
    if not valid:
        return 0.0
    return max(valid)


def _row_age_hours(row: dict[str, Any], now: datetime) -> float | None:
    first_seen = _parse_time(row.get("first_seen_at"))
    if first_seen is None:
        return None
    return max(0.0, (now - first_seen).total_seconds() / 3600)


def _recheck_strength(previous_win_probability: Any, current_win_probability: Any) -> tuple[str, str]:
    previous = _safe_float(previous_win_probability)
    current = _safe_float(current_win_probability)
    delta = current - previous
    if abs(delta) <= RECHECK_STABLE_DELTA_PCT:
        return "stable", "->"
    if delta > 0:
        return "stronger", "up"
    return "weaker", "down"


def _phase_min_win_probability(settings: dict[str, Any], phase: str) -> float:
    phase_key = str(phase or "2h").lower()
    if phase_key == "1h":
        return float(settings.get("one_hour_min_win_probability_pct", settings.get("min_win_probability_pct", 61)) or 61)
    if phase_key == "4h":
        return float(settings.get("four_hour_min_win_probability_pct", settings.get("min_win_probability_pct", 63)) or 63)
    return float(settings.get("two_hour_min_win_probability_pct", settings.get("min_win_probability_pct", 62)) or 62)


def _phase_settings(settings: dict[str, Any], phase: str) -> dict[str, Any]:
    output = dict(settings)
    output["min_win_probability_pct"] = _phase_min_win_probability(settings, phase)
    return output


def _candidate_passes_lc_threshold(
    candidate: TradeCandidate | dict[str, Any],
    settings: dict[str, Any],
    *,
    phase: str = "2h",
) -> bool:
    raw = candidate.get("win_probability_pct") if isinstance(candidate, dict) else candidate.win_probability_pct
    if raw in (None, ""):
        return True
    try:
        return float(raw) >= _phase_min_win_probability(settings, phase)
    except (TypeError, ValueError):
        return True


def _sort_saved_rows(rows: list[dict[str, Any]], settings: dict[str, Any], *, reverse: bool = True) -> list[dict[str, Any]]:
    _ = settings
    ranked = [row for row in rows if isinstance(row, dict)]
    return sorted(ranked, key=_candidate_score, reverse=reverse)


def _has_clear_candlestick(candidate: TradeCandidate) -> bool:
    frames: list[Any] = []
    frames.extend((candidate.candlestick_patterns or {}).values())
    for frame_data in (candidate.higher_timeframes or {}).values():
        if isinstance(frame_data, dict):
            frames.append(frame_data.get("candlestick_patterns"))
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        direction = str(frame.get("direction") or "").lower()
        strongest = frame.get("strongest_pattern")
        patterns = frame.get("patterns")
        if direction in {"bullish", "bearish"} and (strongest or patterns):
            return True
    return False


def _active_symbol_blocklist(config: dict[str, Any]) -> set[str]:
    _count, active_symbols, _warnings = active_trades_summary(config)
    return {str(symbol) for symbol in active_symbols if str(symbol)}


def _strip_blocked_symbols(rows: list[dict[str, Any]], blocked_symbols: set[str]) -> list[dict[str, Any]]:
    if not blocked_symbols:
        return list(rows)
    return [row for row in rows if str(row.get("symbol") or "") not in blocked_symbols]


def _undecided_drop_reason(row: dict[str, Any], settings: dict[str, Any], now: datetime) -> str:
    age_hours = _row_age_hours(row, now)
    if age_hours is not None and age_hours > float(settings["undecided_max_age_hours"]):
        return f"qua han Chua duyet ({_age_label(age_hours)} > {float(settings['undecided_max_age_hours']):.0f}h)"
    try:
        win_probability = float(row.get("win_probability_pct") or 0)
    except (TypeError, ValueError):
        win_probability = 0.0
    if win_probability < float(settings["relaxed_min_win_probability_pct"]):
        return f"win {win_probability:.2f}% < relaxed {float(settings['relaxed_min_win_probability_pct']):.0f}%"
    try:
        confidence = float(row.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < float(settings["relaxed_min_confidence"]):
        return f"confidence {confidence:.2f} < relaxed {float(settings['relaxed_min_confidence']):.0f}"
    try:
        risk_reward = float(row.get("risk_reward") or 0)
    except (TypeError, ValueError):
        risk_reward = 0.0
    if risk_reward < float(settings["relaxed_min_risk_reward"]):
        return f"RR {risk_reward:.2f} < relaxed {float(settings['relaxed_min_risk_reward']):.2f}"
    return "khong con hop le trong Chua duyet"


def _undecided_row_is_active(row: dict[str, Any], settings: dict[str, Any], now: datetime) -> bool:
    if not _row_is_relaxed_valid(row, settings):
        return False
    age_hours = _row_age_hours(row, now)
    if age_hours is None:
        return True
    return age_hours <= float(settings["undecided_max_age_hours"])


def _prune_blocked_state(state: dict[str, Any], blocked_symbols: set[str]) -> None:
    if not blocked_symbols:
        return
    state["internal_lc"] = _strip_blocked_symbols(list(state.get("internal_lc") or []), blocked_symbols)
    state["undecided"] = _strip_blocked_symbols(list(state.get("undecided") or []), blocked_symbols)
    hourly_windows = []
    for window in state.get("hourly_windows") or []:
        hourly_windows.append({**window, "top": _strip_blocked_symbols(list(window.get("top") or []), blocked_symbols)})
    state["hourly_windows"] = hourly_windows


def _prune_low_win_state(state: dict[str, Any], settings: dict[str, Any], now: datetime) -> None:
    state["internal_lc"] = [
        row for row in list(state.get("internal_lc") or []) if _candidate_passes_lc_threshold(row, settings, phase="2h")
    ]
    state["undecided"] = [
        row
        for row in list(state.get("undecided") or [])
        if isinstance(row, dict) and _undecided_row_is_active(row, settings, now)
    ]
    hourly_windows = []
    for window in state.get("hourly_windows") or []:
        top = [row for row in list(window.get("top") or []) if _candidate_passes_lc_threshold(row, settings, phase="1h")]
        hourly_windows.append({**window, "top": top})
    state["hourly_windows"] = hourly_windows
    two_hour_windows = []
    for event in state.get("two_hour_windows") or []:
        if not isinstance(event, dict):
            continue
        approved = [
            row for row in list(event.get("approved") or []) if _candidate_passes_lc_threshold(row, settings, phase="2h")
        ]
        rejected = [
            row for row in list(event.get("rejected") or []) if _candidate_passes_lc_threshold(row, settings, phase="2h")
        ]
        two_hour_windows.append({**event, "approved": approved, "rejected": rejected})
    state["two_hour_windows"] = two_hour_windows
    four_hour_history = []
    for event in state.get("four_hour_history") or []:
        if not isinstance(event, dict):
            continue
        approved = [
            row for row in list(event.get("approved") or []) if _candidate_passes_lc_threshold(row, settings, phase="4h")
        ]
        rejected = [
            row for row in list(event.get("rejected") or []) if _candidate_passes_lc_threshold(row, settings, phase="4h")
        ]
        four_hour_history.append({**event, "approved": approved, "rejected": rejected})
    state["four_hour_history"] = four_hour_history
    telegram_events = []
    for event in state.get("telegram_events") or []:
        if not isinstance(event, dict):
            continue
        approved = [
            row for row in list(event.get("approved") or []) if _candidate_passes_lc_threshold(row, settings, phase="2h")
        ]
        rejected = [
            row for row in list(event.get("rejected") or []) if _candidate_passes_lc_threshold(row, settings, phase="2h")
        ]
        telegram_events.append({**event, "approved": approved, "rejected": rejected})
    state["telegram_events"] = telegram_events


def _rank_candidates(
    candidates: list[TradeCandidate],
    limit: int,
    *,
    blocked_symbols: set[str] | None = None,
    settings: dict[str, Any] | None = None,
    phase: str = "1h",
) -> list[TradeCandidate]:
    settings = settings or {"min_win_probability_pct": 62}
    blocked_symbols = {str(symbol) for symbol in (blocked_symbols or set()) if str(symbol)}
    eligible = [
        candidate
        for candidate in candidates
        if candidate.symbol not in blocked_symbols and _candidate_passes_lc_threshold(candidate, settings, phase=phase)
    ]
    clear = [candidate for candidate in eligible if _has_clear_candlestick(candidate)]
    ranked = sorted(
        clear or eligible,
        key=lambda item: _candidate_score(
            {
                "win_probability_pct": item.win_probability_pct,
                "confidence": item.confidence,
                "indicator_summary": item.indicator_summary or {},
            }
        ),
        reverse=True,
    )
    clean: list[TradeCandidate] = []
    seen: set[str] = set()
    for candidate in ranked:
        if candidate.symbol in seen:
            continue
        clean.append(candidate)
        seen.add(candidate.symbol)
        if len(clean) >= limit:
            break
    return clean


def _candidate_record(
    candidate: TradeCandidate,
    *,
    state: str,
    first_seen_at: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    payload = _candidate_payload(candidate)
    indicator = payload.get("indicator_summary") if isinstance(payload, dict) else {}
    return {
        "symbol": candidate.symbol,
        "base": candidate.base,
        "side": candidate.side,
        "state": state,
        "first_seen_at": first_seen_at or timestamp,
        "last_seen_at": timestamp,
        "entry": candidate.entry,
        "price": candidate.entry,
        "confidence": candidate.confidence,
        "win_probability_pct": candidate.win_probability_pct,
        "current_win_probability_pct": candidate.win_probability_pct,
        "peak_win_probability_pct": candidate.win_probability_pct,
        "win_rate_trend": "->",
        "recheck_state": "initial",
        "risk_reward": candidate.risk_reward,
        "volume_ratio": (indicator or {}).get("volume_ratio"),
        "payload": payload,
    }


def _refresh_row_from_candidate(
    row: dict[str, Any],
    candidate: TradeCandidate,
    *,
    now: datetime,
) -> dict[str, Any]:
    previous_win_probability = row.get("current_win_probability_pct", row.get("win_probability_pct"))
    current_win_probability = candidate.win_probability_pct
    recheck_state, win_rate_trend = _recheck_strength(previous_win_probability, current_win_probability)
    refreshed = _candidate_record(
        candidate,
        state=str(row.get("state") or "HOUR_1"),
        first_seen_at=row.get("first_seen_at"),
        now=now,
    )
    for key in (
        "source_slot",
        "source_index",
        "source_time",
        "source_label",
        "origin_source_slot",
        "origin_source_index",
        "origin_source_time",
        "origin_source_label",
        "revived_at",
        "revived_label",
        "revived_age_hours",
        "revived_age_label",
        "revived_target_rank",
        "mini_index",
        "recheck_daily_index",
        "recheck_slot",
        "recheck_time",
        "recheck_label",
    ):
        if key in row:
            refreshed[key] = row.get(key)
    try:
        refreshed["previous_scan_win_probability_pct"] = float(row.get("win_probability_pct") or 0)
    except (TypeError, ValueError):
        refreshed["previous_scan_win_probability_pct"] = row.get("win_probability_pct")
    refreshed["current_win_probability_pct"] = _safe_float(current_win_probability)
    refreshed["peak_win_probability_pct"] = _peak_win_probability(
        row.get("peak_win_probability_pct"),
        row.get("current_win_probability_pct"),
        row.get("win_probability_pct"),
        current_win_probability,
    )
    refreshed["win_rate_trend"] = win_rate_trend
    refreshed["recheck_state"] = recheck_state
    refreshed["last_recheck_at"] = now.isoformat()
    return refreshed


def _dropped_setup_keys(dropped: list[dict[str, Any]]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for item in dropped:
        if not isinstance(item, dict):
            continue
        keys.add((str(item.get("symbol") or ""), str(item.get("old_side") or "").lower()))
    return keys


def _merge_rechecked_row(existing_row: dict[str, Any], refreshed_row: dict[str, Any]) -> dict[str, Any]:
    merged = {**existing_row, **refreshed_row}
    previous_win_probability = existing_row.get("current_win_probability_pct", existing_row.get("win_probability_pct"))
    current_win_probability = refreshed_row.get(
        "current_win_probability_pct",
        refreshed_row.get("win_probability_pct", merged.get("current_win_probability_pct", merged.get("win_probability_pct"))),
    )
    if "previous_scan_win_probability_pct" not in refreshed_row:
        if "win_probability_pct" in existing_row:
            merged["previous_scan_win_probability_pct"] = existing_row.get("win_probability_pct")
        elif "previous_scan_win_probability_pct" in existing_row:
            merged["previous_scan_win_probability_pct"] = existing_row.get("previous_scan_win_probability_pct")
    merged["current_win_probability_pct"] = _safe_float(current_win_probability)
    merged["peak_win_probability_pct"] = _peak_win_probability(
        refreshed_row.get("peak_win_probability_pct"),
        existing_row.get("peak_win_probability_pct"),
        existing_row.get("current_win_probability_pct"),
        existing_row.get("win_probability_pct"),
        current_win_probability,
    )
    if "win_rate_trend" not in refreshed_row or "recheck_state" not in refreshed_row:
        recheck_state, win_rate_trend = _recheck_strength(previous_win_probability, current_win_probability)
        merged["win_rate_trend"] = win_rate_trend
        merged["recheck_state"] = recheck_state
    merged.setdefault("last_recheck_at", refreshed_row.get("last_seen_at", existing_row.get("last_recheck_at")))
    return merged


def _sync_rows_with_recheck(
    rows: list[dict[str, Any]],
    refreshed_rows: list[dict[str, Any]],
    dropped: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    refreshed_by_key = {_setup_key(row): row for row in refreshed_rows if isinstance(row, dict)}
    dropped_keys = _dropped_setup_keys(dropped)
    synced: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _setup_key(row)
        if key in refreshed_by_key:
            synced.append(_merge_rechecked_row(row, refreshed_by_key[key]))
            continue
        if key in dropped_keys:
            continue
        synced.append(row)
    return synced


def _merge_undecided_rows(
    existing_rows: list[dict[str, Any]],
    incoming_rows: list[dict[str, Any]],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    merged = list(existing_rows)
    existing_by_setup = {_setup_key(row): row for row in merged if isinstance(row, dict) and row.get("symbol")}
    for row in incoming_rows:
        if not isinstance(row, dict):
            continue
        previous = existing_by_setup.get(_setup_key(row))
        first_seen = row.get("first_seen_at")
        if previous and previous.get("first_seen_at"):
            first_seen = previous.get("first_seen_at")
        if not first_seen:
            first_seen = now.isoformat()
        updated = {**row, "first_seen_at": first_seen, "last_seen_at": now.isoformat()}
        merged = _upsert_by_setup(merged, updated)
        existing_by_setup[_setup_key(updated)] = updated
    return merged


def _soft_undecided_rows(
    rows: list[dict[str, Any]],
    *,
    settings: dict[str, Any],
    approved_keys: set[tuple[str, str]],
    blocked_symbols: set[str],
    source_slot: str,
    source_index: int | None,
    now: datetime,
    local_now: datetime,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in _sort_saved_rows(list(rows), settings, reverse=True):
        if not isinstance(row, dict):
            continue
        key = _setup_key(row)
        symbol = str(row.get("symbol") or "")
        if not symbol or symbol in blocked_symbols or key in approved_keys or key in seen:
            continue
        if not _row_is_relaxed_valid(row, settings):
            continue
        output.append(
            _row_with_source_metadata(
                row,
                state_label="CHUA_DUYET",
                source_slot=source_slot,
                source_index=source_index,
                now=now,
                local_now=local_now,
            )
        )
        seen.add(key)
    return output


def _sync_events_with_recheck(
    events: list[dict[str, Any]],
    *,
    slots: list[str],
    row_field: str,
    refreshed_rows: list[dict[str, Any]],
    dropped: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    slot_keys = {str(slot) for slot in slots if str(slot)}
    synced_events: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if str(event.get("slot") or "") not in slot_keys:
            synced_events.append(event)
            continue
        synced_events.append(
            {
                **event,
                row_field: _sync_rows_with_recheck(list(event.get(row_field) or []), refreshed_rows, dropped),
            }
        )
    return synced_events


def _sync_state_after_recheck(
    state: dict[str, Any],
    *,
    hourly_slots: list[str] | None = None,
    two_hour_slots: list[str] | None = None,
    refreshed_rows: list[dict[str, Any]],
    dropped: list[dict[str, Any]],
) -> None:
    state["internal_lc"] = _sync_rows_with_recheck(list(state.get("internal_lc") or []), refreshed_rows, dropped)
    state["undecided"] = _sync_rows_with_recheck(list(state.get("undecided") or []), refreshed_rows, dropped)
    if hourly_slots:
        state["hourly_windows"] = _sync_events_with_recheck(
            list(state.get("hourly_windows") or []),
            slots=hourly_slots,
            row_field="top",
            refreshed_rows=refreshed_rows,
            dropped=dropped,
        )
        state["one_hour_history"] = _sync_events_with_recheck(
            list(state.get("one_hour_history") or []),
            slots=hourly_slots,
            row_field="approved",
            refreshed_rows=refreshed_rows,
            dropped=dropped,
        )
    if two_hour_slots:
        state["two_hour_windows"] = _sync_events_with_recheck(
            list(state.get("two_hour_windows") or []),
            slots=two_hour_slots,
            row_field="approved",
            refreshed_rows=refreshed_rows,
            dropped=dropped,
        )
        state["two_hour_history"] = _sync_events_with_recheck(
            list(state.get("two_hour_history") or []),
            slots=two_hour_slots,
            row_field="approved",
            refreshed_rows=refreshed_rows,
            dropped=dropped,
        )


def _recheck_rows_with_latest_market_data(
    config: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    valid_rows = [row for row in rows if isinstance(row, dict) and str(row.get("symbol") or "")]
    symbols: list[str] = []
    for row in valid_rows:
        symbol = str(row.get("symbol") or "")
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    if not symbols:
        return [], {"refreshed_count": 0, "dropped": [], "warnings": []}

    warnings: list[str] = []
    digest = collect_news(config)
    snapshots, market_warnings = fetch_market_snapshots(config, symbols)
    warnings.extend(market_warnings)
    market_layers: dict[str, dict[str, Any]] = {}
    if config.get("market_guard", {}).get("use_memory_in_strategy", True):
        try:
            market_layers = market_guard_symbol_layers(config, symbols)
        except Exception as exc:
            warnings.append(f"Market guard memory unavailable during LC recheck: {exc}")
    refreshed_candidates = build_candidates(
        config,
        snapshots,
        digest,
        limit=None,
        market_layers=market_layers,
    )
    enrich_quantities(config, refreshed_candidates)
    candidates_by_key = {(candidate.symbol, candidate.side.lower()): candidate for candidate in refreshed_candidates}
    candidate_sides_by_symbol: dict[str, set[str]] = {}
    for candidate in refreshed_candidates:
        candidate_sides_by_symbol.setdefault(candidate.symbol, set()).add(candidate.side.lower())

    refreshed_rows: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for row in valid_rows:
        symbol = str(row.get("symbol") or "")
        side = str(row.get("side") or "").lower()
        candidate = candidates_by_key.get((symbol, side))
        if candidate is not None:
            refreshed_rows.append(_refresh_row_from_candidate(row, candidate, now=now))
            continue
        available_sides = candidate_sides_by_symbol.get(symbol) or set()
        if available_sides:
            opposite_sides = sorted(side_name.upper() for side_name in available_sides if side_name and side_name != side)
            opposite_text = f"; thi truong hien nghieng {', '.join(opposite_sides)}" if opposite_sides else ""
            dropped.append(
                {
                    "symbol": symbol,
                    "old_side": side,
                    "reason": (
                        f"setup goc {side.upper()} khong con hop le trong du lieu moi nhat"
                        f"{opposite_text}; re-check giu nguyen setup goc, khong doi chieu"
                    ),
                }
            )
            continue
        dropped.append(
            {
                "symbol": symbol,
                "old_side": side,
                "reason": "không còn setup hợp lệ trong dữ liệu mới nhất",
            }
        )
    return refreshed_rows, {"refreshed_count": len(refreshed_rows), "dropped": dropped, "warnings": warnings, "fallback": False}


def _compact_patterns(frame: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(frame, dict):
        return {}
    patterns = frame.get("candlestick_patterns") or {}
    if not isinstance(patterns, dict):
        patterns = {}
    return {
        "direction": patterns.get("direction"),
        "strongest_pattern": patterns.get("strongest_pattern"),
        "signal_summary": patterns.get("signal_summary"),
        "patterns": list(patterns.get("patterns") or [])[:4],
    }


def _compact_higher_timeframes(frames: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    if not isinstance(frames, dict):
        return output
    for frame, data in frames.items():
        if not isinstance(data, dict):
            continue
        output[str(frame)] = {
            "timeframe": data.get("timeframe") or frame,
            "last": data.get("last"),
            "ema_gap_pct": data.get("ema_gap_pct"),
            "price_vs_ema_slow_pct": data.get("price_vs_ema_slow_pct"),
            "rsi": data.get("rsi"),
            "atr_pct": data.get("atr_pct"),
            "volume_ratio": data.get("volume_ratio"),
            "range_position": data.get("range_position"),
            "trend": data.get("trend"),
            "candlestick_patterns": _compact_patterns(data),
        }
    return output


def _compact_payload_dict(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {
        "symbol": payload.get("symbol"),
        "base": payload.get("base"),
        "side": payload.get("side"),
        "confidence": payload.get("confidence"),
        "entry": payload.get("entry"),
        "stop_loss": payload.get("stop_loss"),
        "take_profit": payload.get("take_profit"),
        "risk_reward": payload.get("risk_reward"),
        "order_usdt": payload.get("order_usdt"),
        "quantity": payload.get("quantity"),
        "spread_pct": payload.get("spread_pct"),
        "news_score": payload.get("news_score"),
        "news_count": payload.get("news_count"),
        "higher_timeframes": _compact_higher_timeframes(payload.get("higher_timeframes") or {}),
        "indicator_summary": to_jsonable(payload.get("indicator_summary") or {}),
        "candlestick_patterns": to_jsonable(payload.get("candlestick_patterns") or {}),
        "rule_score": payload.get("rule_score"),
        "margin_usdt": payload.get("margin_usdt"),
        "recovery_margin_usdt": payload.get("recovery_margin_usdt"),
        "recovery_source_key": payload.get("recovery_source_key"),
        "win_probability_pct": payload.get("win_probability_pct"),
        "target_mode": payload.get("target_mode"),
        "take_profit_pct": payload.get("take_profit_pct"),
        "stop_loss_pct": payload.get("stop_loss_pct"),
        "price_take_profit_pct": payload.get("price_take_profit_pct"),
        "price_stop_loss_pct": payload.get("price_stop_loss_pct"),
        "reasons": list(payload.get("reasons") or [])[:6],
        "warnings": list(payload.get("warnings") or [])[:4],
        "scan_source": payload.get("scan_source"),
        "setup_quality": payload.get("setup_quality"),
        "position_slot": payload.get("position_slot"),
        "risk_percent": payload.get("risk_percent"),
        "market_regime": payload.get("market_regime"),
        "regime_confidence": payload.get("regime_confidence"),
    }


def _candidate_payload(candidate: TradeCandidate) -> dict[str, Any]:
    return {
        "symbol": candidate.symbol,
        "base": candidate.base,
        "side": candidate.side,
        "confidence": candidate.confidence,
        "entry": candidate.entry,
        "stop_loss": candidate.stop_loss,
        "take_profit": candidate.take_profit,
        "risk_reward": candidate.risk_reward,
        "order_usdt": candidate.order_usdt,
        "quantity": candidate.quantity,
        "spread_pct": candidate.spread_pct,
        "news_score": candidate.news_score,
        "news_count": candidate.news_count,
        "higher_timeframes": _compact_higher_timeframes(candidate.higher_timeframes),
        "indicator_summary": to_jsonable(candidate.indicator_summary or {}),
        "candlestick_patterns": to_jsonable(candidate.candlestick_patterns or {}),
        "rule_score": candidate.rule_score,
        "margin_usdt": candidate.margin_usdt,
        "recovery_margin_usdt": candidate.recovery_margin_usdt,
        "recovery_source_key": candidate.recovery_source_key,
        "win_probability_pct": candidate.win_probability_pct,
        "target_mode": candidate.target_mode,
        "take_profit_pct": candidate.take_profit_pct,
        "stop_loss_pct": candidate.stop_loss_pct,
        "price_take_profit_pct": candidate.price_take_profit_pct,
        "price_stop_loss_pct": candidate.price_stop_loss_pct,
        "reasons": list(candidate.reasons or [])[:6],
        "warnings": list(candidate.warnings or [])[:4],
        "scan_source": candidate.scan_source,
        "setup_quality": candidate.setup_quality,
        "position_slot": candidate.position_slot,
        "risk_percent": candidate.risk_percent,
        "market_regime": candidate.market_regime,
        "regime_confidence": candidate.regime_confidence,
    }


def _load_state(config: dict[str, Any], now: datetime, *, reset_for_new_day: bool = True) -> dict[str, Any]:
    purge_deprecated_journal_state(config)
    raw = get_journal_state(config, LC_PIPELINE_STATE_KEY)
    if raw:
        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            state = {}
    else:
        state = {}
    if state.get("state_version") != LC_PIPELINE_STATE_VERSION:
        state["latest_mini_scan"] = {}
        state["state_version"] = LC_PIPELINE_STATE_VERSION
    day = _day_key(config, now)
    if reset_for_new_day and state.get("day_key") != day:
        state["day_key"] = day
        state["daily_one_hour_counter"] = 0
        state["daily_two_hour_counter"] = 0
        state["daily_undecided_recheck_counter"] = 0
        state["daily_mini_counter"] = 0
        state["last_undecided_recheck_slot"] = None
        state["hourly_windows"] = []
        state["two_hour_windows"] = []
        state["telegram_events"] = []
        state["internal_notifications"] = []
    state.setdefault("one_hour_history", [])
    state.setdefault("two_hour_history", [])
    state.setdefault("four_hour_history", [])
    state.setdefault("hourly_windows", [])
    state.setdefault("two_hour_windows", [])
    state.setdefault("undecided", [])
    state.setdefault("internal_lc", [])
    state.setdefault("telegram_events", [])
    state.setdefault("internal_notifications", [])
    state.setdefault("daily_one_hour_counter", 0)
    state.setdefault("daily_two_hour_counter", 0)
    state.setdefault("daily_undecided_recheck_counter", 0)
    state.setdefault("four_hour_counter", 0)
    state.setdefault("daily_mini_counter", 0)
    state.setdefault("last_undecided_recheck_slot", None)
    state.setdefault("latest_mini_scan", {})
    state.setdefault("state_version", LC_PIPELINE_STATE_VERSION)
    state["one_hour_history"] = _prune_history(
        state.get("one_hour_history") or [],
        now=now,
        keep_days=ONE_HOUR_HISTORY_KEEP_DAYS,
    )
    state["two_hour_history"] = _prune_history(
        state.get("two_hour_history") or [],
        now=now,
        keep_days=TWO_HOUR_HISTORY_KEEP_DAYS,
    )
    state["four_hour_history"] = _prune_history(
        state.get("four_hour_history") or [],
        now=now,
        keep_days=FOUR_HOUR_HISTORY_KEEP_DAYS,
    )
    _sanitize_current_day_aggregate_state(config, state, now)
    _backfill_one_hour_source_metadata(state)
    _prune_low_win_state(state, _pipeline_config(config), now)
    return state


def _compact_saved_row(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    keep = {
        "symbol",
        "base",
        "side",
        "state",
        "first_seen_at",
        "last_seen_at",
        "entry",
        "price",
        "confidence",
        "win_probability_pct",
        "current_win_probability_pct",
        "peak_win_probability_pct",
        "previous_scan_win_probability_pct",
        "win_rate_trend",
        "recheck_state",
        "risk_reward",
        "volume_ratio",
        "source_slot",
        "source_index",
        "source_time",
        "source_label",
        "origin_source_slot",
        "origin_source_index",
        "origin_source_time",
        "origin_source_label",
        "revived_at",
        "revived_label",
        "revived_age_hours",
        "revived_age_label",
        "revived_target_rank",
        "undecided_status",
        "undecided_reason",
        "last_recheck_at",
        "mini_index",
        "recheck_daily_index",
        "recheck_slot",
        "recheck_time",
        "recheck_label",
    }
    compact = {key: row.get(key) for key in keep if key in row}
    payload = row.get("payload")
    if isinstance(payload, dict):
        compact["payload"] = _compact_payload_dict(payload)
    elif payload is not None:
        compact["payload"] = payload
    return compact


def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(event, dict):
        return {}
    compact = {
        "frame": event.get("frame"),
        "slot": event.get("slot"),
        "created_at": event.get("created_at"),
        "index": event.get("index"),
        "sequence_index": event.get("sequence_index"),
        "daily_index": event.get("daily_index"),
        "date": event.get("date"),
        "time": event.get("time"),
        "recheck": event.get("recheck"),
        "source_windows": event.get("source_windows"),
    }
    compact["approved"] = [_compact_saved_row(row) for row in event.get("approved") or [] if isinstance(row, dict)]
    compact["rejected"] = [_compact_saved_row(row) for row in event.get("rejected") or [] if isinstance(row, dict)]
    return compact


def _prune_history(events: list[dict[str, Any]], *, now: datetime, keep_days: int) -> list[dict[str, Any]]:
    cutoff = now - timedelta(days=max(1, int(keep_days)))
    kept: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        created_at = _parse_time(event.get("created_at"))
        if created_at is not None and created_at < cutoff:
            continue
        kept.append(event)
    return kept


def _compact_mini_candidate(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    keep = {
        "symbol",
        "side",
        "confidence",
        "win_probability_pct",
        "risk_reward",
        "entry",
        "stop_loss",
        "take_profit",
        "spread_pct",
        "news_score",
        "news_count",
        "indicator_summary",
        "code_timeframe_analysis",
        "mini_context_4h",
        "reasons",
        "warnings",
    }
    return {key: item.get(key) for key in keep if key in item}


def _symbol_list(values: Any, *, limit: int = 3) -> list[str]:
    symbols: list[str] = []
    if not isinstance(values, list):
        return symbols
    for item in values:
        symbol = str(item or "").strip()
        if not symbol or symbol in symbols:
            continue
        symbols.append(symbol)
        if len(symbols) >= max(1, int(limit)):
            break
    return symbols


def _compact_local_policy(policy: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(policy, dict):
        return {}
    return {
        "provider": policy.get("provider"),
        "decision": policy.get("decision"),
        "threshold_win_probability_pct": policy.get("threshold_win_probability_pct"),
        "qualified_symbols": _symbol_list(policy.get("qualified_symbols"), limit=3),
        "approved_symbols": _symbol_list(policy.get("approved_symbols"), limit=3),
        "approved_count": policy.get("approved_count"),
        "candidate_count": policy.get("candidate_count"),
        "selection_source": policy.get("selection_source"),
        "skip_reason": policy.get("skip_reason"),
        "warnings": list(policy.get("warnings") or [])[:8],
    }


def _compact_ai_review(review: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(review, dict):
        return {}
    return {
        "approved_symbols": _symbol_list(review.get("approved_symbols"), limit=3),
        "decision": review.get("decision"),
        "confidence": review.get("confidence"),
        "setup_scores": review.get("setup_scores") if isinstance(review.get("setup_scores"), dict) else {},
        "reason": review.get("reason"),
        "model_version": review.get("model_version") or review.get("model"),
        "prompt_version": review.get("prompt_version"),
        "prompt_hash": review.get("prompt_hash"),
        "prompt_tokens": review.get("prompt_tokens"),
        "completion_tokens": review.get("completion_tokens"),
        "latency_ms": review.get("latency_ms"),
    }


def _compact_mini_scan(scan: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(scan, dict) or not scan:
        return {}
    pool_symbols = _symbol_list(scan.get("pool_symbols"), limit=3)
    if not pool_symbols:
        pool_symbols = _symbol_list(((scan.get("local_policy") or {}).get("approved_symbols")), limit=3)
    selected_symbols = _symbol_list(scan.get("selected_symbols"), limit=3)
    if not selected_symbols:
        selected_symbols = _symbol_list(scan.get("approved_symbols"), limit=3)
    compact = {
        "enabled": bool(scan.get("enabled", True)),
        "agent": scan.get("agent"),
        "created_at": scan.get("created_at"),
        "mini_index": scan.get("mini_index"),
        "slot_id": scan.get("slot_id"),
        "slot_start": scan.get("slot_start"),
        "status": scan.get("status"),
        "interval_seconds": scan.get("interval_seconds"),
        "provider": scan.get("provider"),
        "model": scan.get("model"),
        "source": scan.get("source"),
        "source_symbols": list(scan.get("source_symbols") or [])[:20],
        "candidate_count": scan.get("candidate_count"),
        "local_policy": _compact_local_policy(scan.get("local_policy") or {}),
        "pool_symbols": pool_symbols,
        "selected_symbols": selected_symbols,
        "compact_ai_payload": bool(scan.get("compact_ai_payload", True)),
        "warnings": list(scan.get("warnings") or [])[:20],
        "ai_review": _compact_ai_review(scan.get("ai_review") or {}),
        "ai_review_error": scan.get("ai_review_error"),
        "fallback": scan.get("fallback"),
        "skip_reason": scan.get("skip_reason"),
        "decision_reason_vi": scan.get("decision_reason_vi"),
    }
    compact["candidates"] = [
        _compact_mini_candidate(item)
        for item in scan.get("candidates") or []
        if isinstance(item, dict)
    ][:3]
    return compact


def _save_state(config: dict[str, Any], state: dict[str, Any]) -> None:
    state["state_version"] = LC_PIPELINE_STATE_VERSION
    state["one_hour_history"] = [
        _compact_event(event) for event in state.get("one_hour_history") or [] if isinstance(event, dict)
    ]
    state["two_hour_history"] = [
        _compact_event(event) for event in state.get("two_hour_history") or [] if isinstance(event, dict)
    ]
    state["four_hour_history"] = [
        _compact_event(event) for event in state.get("four_hour_history") or [] if isinstance(event, dict)
    ]
    state["internal_lc"] = [_compact_saved_row(row) for row in state.get("internal_lc") or [] if isinstance(row, dict)]
    state["undecided"] = [_compact_saved_row(row) for row in state.get("undecided") or [] if isinstance(row, dict)]
    state["hourly_windows"] = [
        {
            **window,
            "top": [_compact_saved_row(row) for row in window.get("top") or [] if isinstance(row, dict)],
        }
        for window in state.get("hourly_windows") or []
        if isinstance(window, dict)
    ]
    state["two_hour_windows"] = [_compact_event(event) for event in state.get("two_hour_windows") or [] if isinstance(event, dict)]
    state["telegram_events"] = [_compact_event(event) for event in state.get("telegram_events") or [] if isinstance(event, dict)]
    state["latest_mini_scan"] = _compact_mini_scan(state.get("latest_mini_scan") or {})
    set_journal_state(config, LC_PIPELINE_STATE_KEY, json.dumps(state, ensure_ascii=False))
    clear_dashboard_snapshot_cache(config)


def lc_pipeline_internal_symbols(config: dict[str, Any], *, limit: int | None = None) -> list[str]:
    state = _load_state(config, datetime.now(timezone.utc), reset_for_new_day=False)
    symbols: list[str] = []
    for row in state.get("internal_lc") or []:
        symbol = str(row.get("symbol") or "")
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    if limit is None:
        return symbols
    return symbols[: max(1, int(limit))]


def lc_pipeline_four_hour_symbols(config: dict[str, Any], *, limit: int | None = None) -> list[str]:
    state = _load_state(config, datetime.now(timezone.utc), reset_for_new_day=False)
    symbols: list[str] = []
    for row in _latest_four_hour_rows(state):
        symbol = str(row.get("symbol") or "")
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    if limit is None:
        return symbols
    return symbols[: max(1, int(limit))]


def latest_lc_pipeline_four_hour_event(config: dict[str, Any]) -> dict[str, Any] | None:
    state = _load_state(config, datetime.now(timezone.utc), reset_for_new_day=False)
    four_hour_history = state.get("four_hour_history") or []
    if not four_hour_history:
        return None
    latest_event = four_hour_history[-1]
    return latest_event if isinstance(latest_event, dict) else None


def latest_lc_pipeline_mini_scan(config: dict[str, Any]) -> dict[str, Any] | None:
    state = _load_state(config, datetime.now(timezone.utc), reset_for_new_day=False)
    scan = state.get("latest_mini_scan") if isinstance(state.get("latest_mini_scan"), dict) else {}
    if not scan:
        return None
    current_symbols = lc_pipeline_four_hour_symbols(config, limit=10)
    original_selected_symbols = _symbol_list(
        scan.get("selected_symbols") if isinstance(scan.get("selected_symbols"), list) else scan.get("approved_symbols"),
        limit=10,
    )
    selected_symbols = [symbol for symbol in original_selected_symbols if symbol in current_symbols]
    selection_stale = bool(original_selected_symbols and selected_symbols != original_selected_symbols)
    status = "stale_selection" if selection_stale else scan.get("status")
    skip_reason = scan.get("skip_reason")
    if selection_stale and not selected_symbols:
        skip_reason = "Mini selection is stale because the current LC noi bo pool has changed"
    return {
        **scan,
        "status": status,
        "pool_symbols": current_symbols,
        "pool_count": len(current_symbols),
        "approved_symbols": current_symbols,
        "approved_count": len(current_symbols),
        "selected_symbols": selected_symbols,
        "selected_count": len(selected_symbols),
        "selected_original_symbols": original_selected_symbols,
        "selection_stale": selection_stale,
        "skip_reason": skip_reason,
    }


def save_lc_pipeline_mini_scan(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    state = _load_state(config, datetime.now(timezone.utc))
    latest_scan = state.get("latest_mini_scan") if isinstance(state.get("latest_mini_scan"), dict) else {}
    if payload.get("mini_index") in (None, ""):
        same_slot = (
            latest_scan
            and str(latest_scan.get("slot_id") or "") == str(payload.get("slot_id") or "")
            and str(latest_scan.get("created_at") or "") == str(payload.get("created_at") or "")
        )
        if same_slot and latest_scan.get("mini_index") not in (None, ""):
            payload = {**payload, "mini_index": latest_scan.get("mini_index")}
        else:
            payload = {**payload, "mini_index": int(state.get("daily_mini_counter") or 0) + 1}
    state["daily_mini_counter"] = max(int(state.get("daily_mini_counter") or 0), int(payload.get("mini_index") or 0))
    state["latest_mini_scan"] = _compact_mini_scan(payload)
    _save_state(config, state)
    return latest_lc_pipeline_mini_scan(config) or {}


def _upsert_by_symbol(rows: list[dict[str, Any]], record: dict[str, Any]) -> list[dict[str, Any]]:
    output = [row for row in rows if row.get("symbol") != record.get("symbol")]
    output.append(record)
    return output


def _setup_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("symbol") or ""), str(row.get("side") or "").lower())


def _upsert_by_setup(rows: list[dict[str, Any]], record: dict[str, Any]) -> list[dict[str, Any]]:
    record_key = _setup_key(record)
    output = [row for row in rows if _setup_key(row) != record_key]
    output.append(record)
    return output


def _prune_undecided(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    ranked = sorted(rows, key=_candidate_score, reverse=True)
    return ranked[:limit]


def _trim_undecided(rows: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    max_rows = int(settings["undecided_max"])
    kept = _sort_saved_rows(list(rows), settings, reverse=True)
    if len(kept) <= max_rows:
        return kept
    return kept[:max_rows]


def _row_with_source_metadata(
    row: dict[str, Any],
    *,
    state_label: str,
    source_slot: str,
    source_index: int | None,
    now: datetime,
    local_now: datetime,
) -> dict[str, Any]:
    source_time = now.isoformat()
    source_label = local_now.strftime("%d/%m/%y %H:%M:%S")
    record = {
        **row,
        "state": state_label,
        "source_slot": source_slot,
        "source_index": source_index,
        "source_time": source_time,
        "source_label": source_label,
    }
    if source_slot == "1h":
        return record
    origin_slot = row.get("origin_source_slot")
    origin_index = row.get("origin_source_index")
    origin_time = row.get("origin_source_time")
    origin_label = row.get("origin_source_label")
    if not origin_slot and row.get("source_index") is not None:
        origin_slot = row.get("source_slot")
        origin_index = row.get("source_index")
        origin_time = row.get("source_time")
        origin_label = row.get("source_label")
    if not origin_slot and source_index is not None:
        origin_slot = source_slot
        origin_index = source_index
        origin_time = source_time
        origin_label = source_label
    if origin_slot:
        record["origin_source_slot"] = origin_slot
        record["origin_source_index"] = origin_index
        record["origin_source_time"] = origin_time
        record["origin_source_label"] = origin_label
    return record


def _backfill_one_hour_source_metadata(state: dict[str, Any]) -> None:
    metadata_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for event in state.get("one_hour_history") or []:
        if not isinstance(event, dict):
            continue
        source_slot = str(event.get("frame") or "1h")
        source_index = event.get("daily_index")
        source_time = event.get("created_at")
        source_label = None
        created_at = _parse_time(source_time)
        if created_at is not None:
            source_label = _local_time({"timezone": "Asia/Ho_Chi_Minh"}, created_at).strftime("%d/%m/%y %H:%M:%S")
        elif event.get("date") and event.get("time"):
            source_label = f"{event.get('date')} {event.get('time')}"
        for row in event.get("approved") or []:
            if not isinstance(row, dict):
                continue
            metadata_by_key[_setup_key(row)] = {
                "source_slot": source_slot,
                "source_index": source_index,
                "source_time": source_time,
                "source_label": source_label,
            }

    def _apply(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("source_slot") or "") != "1h" or row.get("source_index") is not None:
                output.append(row)
                continue
            metadata = metadata_by_key.get(_setup_key(row))
            if not metadata:
                output.append(row)
                continue
            output.append(
                {
                    **row,
                    "source_slot": metadata.get("source_slot"),
                    "source_index": metadata.get("source_index"),
                    "source_time": metadata.get("source_time"),
                    "source_label": metadata.get("source_label"),
                }
            )
        return output

    state["internal_lc"] = _apply(list(state.get("internal_lc") or []))
    state["undecided"] = _apply(list(state.get("undecided") or []))
    state["hourly_windows"] = [
        {**window, "top": _apply(list(window.get("top") or []))}
        for window in state.get("hourly_windows") or []
        if isinstance(window, dict)
    ]
    state["one_hour_history"] = [
        {**event, "approved": _apply(list(event.get("approved") or []))}
        for event in state.get("one_hour_history") or []
        if isinstance(event, dict)
    ]


def _latest_four_hour_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    four_hour_history = state.get("four_hour_history") or []
    if not four_hour_history:
        return []
    latest_event = four_hour_history[-1]
    if not isinstance(latest_event, dict):
        return []
    return [row for row in latest_event.get("approved") or [] if isinstance(row, dict)]


def _row_origin_label(row: dict[str, Any], *, config: dict[str, Any]) -> str | None:
    origin_slot = str(row.get("origin_source_slot") or "").strip()
    origin_index = row.get("origin_source_index")
    origin_time = row.get("origin_source_time")
    if not origin_slot:
        source_slot = str(row.get("source_slot") or "").strip()
        if source_slot == "1h":
            origin_slot = source_slot
            origin_index = row.get("source_index")
            origin_time = row.get("source_time")
    if origin_slot != "1h":
        return None
    origin_time_label = _event_clock_label(origin_time, config=config)
    origin_icon = _frame_icon(config, origin_slot)
    if origin_index in (None, ""):
        return f"Gốc {origin_icon} {origin_slot} ({origin_time_label})"
    return f"Gốc {origin_icon} {origin_slot} #{origin_index} ({origin_time_label})"


def _base_source_label(row: dict[str, Any]) -> str:
    source_slot = row.get("source_slot") or row.get("state") or "-"
    source_index = row.get("source_index")
    raw_label = str(row.get("source_label") or "").strip()
    source_clock = None
    if raw_label:
        parts = raw_label.split()
        source_clock = parts[-1] if parts else None
    if not source_clock:
        source_time = _parse_time(row.get("source_time"))
        source_clock = source_time.strftime("%H:%M:%S") if source_time else None
    if source_index:
        return f"{source_slot} #{source_index} ({source_clock})" if source_clock else f"{source_slot} #{source_index}"
    return str(source_slot)


def _revived_target_label(row: dict[str, Any]) -> str:
    rank = row.get("revived_target_rank")
    if rank in (None, ""):
        return "LC nội bộ hiện tại"
    return f"LC nội bộ #{rank}"


def _revived_time_label(row: dict[str, Any]) -> str | None:
    raw_label = str(row.get("revived_label") or "").strip()
    if raw_label:
        parts = raw_label.split()
        return parts[-1] if parts else raw_label
    revived_at = _parse_time(row.get("revived_at"))
    return revived_at.strftime("%H:%M:%S") if revived_at else None


def _format_pair_line(
    index: int,
    row: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    include_origin: bool = False,
) -> str:
    price = row.get("price") or row.get("entry") or "-"
    volume = row.get("volume_ratio")
    win_probability = row.get("win_probability_pct")
    try:
        volume_text = f"x{float(volume):.2f}"
    except (TypeError, ValueError):
        volume_text = "-"
    try:
        win_text = f"{float(win_probability):.2f}%"
    except (TypeError, ValueError):
        win_text = "-"
    try:
        price_text = f"{float(price):.6g}"
    except (TypeError, ValueError):
        price_text = str(price)
    side_text = str(row.get("side") or "-").upper()
    parts = [
        f"{index}. {row.get('symbol', '-')}",
        side_text,
        f"Win {win_text}",
        f"Giá {price_text}",
        f"KLGD {volume_text}",
    ]
    if include_origin and config is not None:
        origin_label = _row_origin_label(row, config=config)
        if origin_label:
            parts.append(origin_label)
    if row.get("revived_at"):
        parts.append("HS")
    return " | ".join(parts)


def _event_clock_label(value: Any, *, config: dict[str, Any] | None = None) -> str:
    event_time = _parse_time(value)
    if event_time is None:
        return "-"
    return _local_time(config or {}, event_time).strftime("%H:%M")


def _source_window_payload(frame: str, event: dict[str, Any], *, config: dict[str, Any]) -> dict[str, Any]:
    return {
        "frame": frame,
        "index": event.get("daily_index", event.get("index")),
        "time": _event_clock_label(event.get("created_at") or event.get("slot"), config=config),
        "slot": event.get("slot"),
        "created_at": event.get("created_at"),
    }


def _source_window_label(item: dict[str, Any], *, config: dict[str, Any]) -> str:
    frame = str(item.get("frame") or "-")
    index = item.get("index")
    time_label = str(item.get("time") or "-")
    icon = _frame_icon(config, frame)
    if index in (None, ""):
        return f"{icon} {frame} ({time_label})"
    return f"{icon} #{index} {frame} ({time_label})"


def _raw_pipeline_state(config: dict[str, Any]) -> dict[str, Any]:
    raw = get_journal_state(config, LC_PIPELINE_STATE_KEY)
    if not raw:
        return {}
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return state if isinstance(state, dict) else {}


def _infer_source_windows(config: dict[str, Any], event: dict[str, Any]) -> list[dict[str, Any]]:
    frame = str(event.get("frame") or "").lower()
    history_key = ""
    source_frame = ""
    if frame == "2h":
        history_key = "one_hour_history"
        source_frame = "1h"
    elif frame == "4h":
        history_key = "two_hour_history"
        source_frame = "2h"
    else:
        return []

    state = _raw_pipeline_state(config)
    history = state.get(history_key) if isinstance(state.get(history_key), list) else []
    if not history:
        return []
    target_time = _parse_time(event.get("created_at") or event.get("slot"))
    eligible: list[dict[str, Any]] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        item_time = _parse_time(item.get("created_at") or item.get("slot"))
        if target_time is not None and item_time is not None and item_time > target_time:
            continue
        eligible.append(item)
    inferred = eligible[-2:]
    return [_source_window_payload(source_frame, item, config=config) for item in inferred if isinstance(item, dict)]


def _event_source_windows(config: dict[str, Any], event: dict[str, Any]) -> list[dict[str, Any]]:
    source_windows = event.get("source_windows") if isinstance(event.get("source_windows"), list) else []
    source_windows = [item for item in source_windows if isinstance(item, dict)]
    if source_windows:
        return source_windows
    return _infer_source_windows(config, event)


def _event_source_lines(event: dict[str, Any], *, config: dict[str, Any]) -> list[str]:
    frame = str(event.get("frame") or "-")
    frame_index = event.get("daily_index")
    if frame_index in (None, ""):
        frame_index = event.get("index")
    frame_time = _event_clock_label(event.get("created_at") or event.get("slot"), config=config)
    frame_icon = _frame_icon(config, frame)
    if frame_index in (None, ""):
        lines = [f"Khung {frame_icon} {frame}: ({frame_time})"]
    else:
        lines = [f"Khung {frame_icon} #{frame_index} {frame} ({frame_time})"]
    source_windows = _event_source_windows(config, event)
    source_labels = [_source_window_label(item, config=config) for item in source_windows if isinstance(item, dict)]
    if source_labels:
        lines.append("Gộp từ: " + ", ".join(source_labels))
    return lines


def _mini_reason_vi(scan: dict[str, Any]) -> str:
    selected_symbols = _symbol_list(scan.get("selected_symbols"), limit=3)
    ai_review = scan.get("ai_review") if isinstance(scan.get("ai_review"), dict) else {}
    local_policy = scan.get("local_policy") if isinstance(scan.get("local_policy"), dict) else {}
    selection_source = str(local_policy.get("selection_source") or "")
    status = str(scan.get("status") or "")
    if selected_symbols:
        if ai_review:
            return (
                "Mini đã đối chiếu nhóm LC 4h với đánh giá AI nội bộ, rồi giữ lại các cặp còn mạnh về Win Rate, "
                "chất lượng setup, xu hướng và chỉ báo."
            )
        if selection_source == "lc_internal_pipeline":
            return (
                "Mini chọn trực tiếp từ nhóm LC 4h hiện tại vì các cặp này đang nổi bật nhất sau khi so sánh "
                "Win Rate, chất lượng setup, xu hướng và chỉ báo."
            )
        return "Mini giữ lại các cặp đang phù hợp nhất trong nhóm LC 4h hiện tại."
    if status == "waiting_lc":
        return "Mini chưa chọn cặp nào vì hiện chưa có đủ LC 4h phù hợp để duyệt."
    if status == "stale_selection":
        return "Mini chưa chốt cặp vì dữ liệu LC 4h đã thay đổi, cần chờ lượt Mini mới."
    skip_reason = str(scan.get("skip_reason") or "").strip()
    if skip_reason:
        return f"Mini chưa chọn cặp nào. Lý do: {skip_reason}."
    return "Mini chưa chọn cặp nào sau khi rà lại nhóm LC 4h."


def _build_internal_lc_rows(
    rows: list[dict[str, Any]],
    *,
    source_slot: str,
    source_index: int | None,
    now: datetime,
    local_now: datetime,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        output.append(
            _row_with_source_metadata(
                row,
                state_label="LC_NOI_BO",
                source_slot=source_slot,
                source_index=source_index,
                now=now,
                local_now=local_now,
            )
        )
    return output


def _append_internal_notification(
    state: dict[str, Any],
    *,
    frame: str,
    icon: str,
    title: str,
    lines: list[str],
    created_at: datetime,
) -> None:
    state.setdefault("internal_notifications", [])
    state["internal_notifications"].append(
        {
            "frame": frame,
            "icon": icon,
            "title": title,
            "created_at": created_at.isoformat(),
            "lines": lines,
        }
    )
    state["internal_notifications"] = sorted(
        state["internal_notifications"],
        key=lambda item: str(item.get("created_at") or ""),
    )[-80:]


def _internal_notification_text(item: dict[str, Any], config: dict[str, Any]) -> str:
    created_at = _parse_time(item.get("created_at"))
    created_label = _local_time(config, created_at).strftime("%d/%m/%y %H:%M:%S") if created_at else "-"
    lines = [
        f"{item.get('icon', '🔔')} {item.get('title', '-')}",
        created_label,
    ]
    lines.extend(str(line) for line in item.get("lines") or [])
    return "\n".join(lines)


def _internal_notification_summary_line(item: dict[str, Any], config: dict[str, Any]) -> str:
    created_at = _parse_time(item.get("created_at"))
    created_label = _local_time(config, created_at).strftime("%d/%m/%y %H:%M:%S") if created_at else "-"
    details = [str(line).strip() for line in item.get("lines") or [] if str(line).strip()]
    header = f"{item.get('icon', '🔔')} {item.get('title', '-')} | {created_label}"
    if not details:
        return header
    return " | ".join([header, *details])


def _one_hour_notification_text(config: dict[str, Any], event: dict[str, Any]) -> str:
    rows = [row for row in event.get("approved") or [] if isinstance(row, dict)]
    daily_index = event.get("daily_index", event.get("index", "-"))
    lines = [
        f"{ONE_HOUR_ICON} #{daily_index} 1h top {len(rows)} setup",
        f"{event.get('date', '-')} {event.get('time', '-')}",
        f"Khung {ONE_HOUR_ICON} #{daily_index} 1h ({_event_clock_label(event.get('created_at') or event.get('slot'), config=config)})",
    ]
    lines.extend(_format_pair_line(index, row, config=config) for index, row in enumerate(rows[:3], 1))
    return "\n".join(lines)


def _two_hour_notification_text(config: dict[str, Any], event: dict[str, Any]) -> str:
    rows = [row for row in event.get("approved") or [] if isinstance(row, dict)]
    icon = _two_hour_icon(config)
    lines = [
        f"{icon} #{event.get('daily_index', '-')} LC nội bộ tổng hợp 2h",
        f"{event.get('date', '-')} {event.get('time', '-')}",
    ]
    lines.extend(_event_source_lines(event, config=config))
    lines.append("3 cặp duyệt lượt này:")
    lines.extend(_format_pair_line(index, row, config=config, include_origin=True) for index, row in enumerate(rows[:3], 1))
    return "\n".join(lines)


def _four_hour_notification_text(config: dict[str, Any], event: dict[str, Any]) -> str:
    rows = [row for row in event.get("approved") or [] if isinstance(row, dict)]
    lines = [f"{FOUR_HOUR_ICON} #{event.get('index', '-')} LC nội bộ tổng hợp 4h"]
    if event.get("date") or event.get("time"):
        lines.append(f"{event.get('date', '-')} {event.get('time', '-')}".strip())
    lines.extend(_event_source_lines(event, config=config))
    lines.append("Các cặp giữ lại ở 4h:")
    if rows:
        lines.extend(_format_pair_line(index, row, config=config, include_origin=True) for index, row in enumerate(rows[:3], 1))
    else:
        lines.append("Không có cặp nào đủ điều kiện giữ lại ở 4h.")
    return "\n".join(lines)


def _mini_notification_text(
    config: dict[str, Any],
    scan: dict[str, Any],
    latest_four_hour: dict[str, Any] | None,
    *,
    rows: list[dict[str, Any]] | None = None,
) -> str:
    created_at = _parse_time(scan.get("created_at")) or datetime.now(timezone.utc)
    local_now = _local_time(config, created_at)
    selected_symbols = _symbol_list(scan.get("selected_symbols"), limit=3)
    selected_count = len(selected_symbols)
    mini_index = scan.get("mini_index") or "-"
    display_rows = [row for row in (rows or []) if isinstance(row, dict)]
    if not display_rows:
        display_rows = lc_pipeline_pool_rows(config, list(selected_symbols))
    lines = [
        f"{MINI_ICON} Mini #{mini_index}" if mini_index != "-" else f"{MINI_ICON} Mini",
        local_now.strftime("%d/%m/%y %H:%M:%S"),
        f"Lần gọi Mini: #{mini_index} ({local_now.strftime('%H:%M')})" if mini_index != "-" else f"Lần gọi Mini: {local_now.strftime('%H:%M')}",
        f"Mini chọn: {selected_count}/3 cặp",
    ]
    if isinstance(latest_four_hour, dict):
        lines.extend(_event_source_lines(latest_four_hour, config=config))
    if display_rows:
        for index, row in enumerate(display_rows[:3], 1):
            side = str(row.get("side") or "-").upper()
            lines.append(f"{index}. {row.get('symbol', '-')} | {side} | {_source_label_v2(row)}")
    else:
        lines.append("Mini chưa chọn được cặp nào.")
    lines.append("Lý do: " + str(scan.get("decision_reason_vi") or _mini_reason_vi(scan)))
    if scan.get("slot_id"):
        lines.append(f"Slot: {scan.get('slot_id')}")
    return "\n".join(lines)


def _rc_recheck_row_line(index: int, row: dict[str, Any], *, dropped: bool = False) -> str:
    side = str(row.get("side") or "-").upper()
    try:
        win_text = f"{float(row.get('win_probability_pct') or 0):.2f}%"
    except (TypeError, ValueError):
        win_text = "-"
    prefix = "Win trước" if dropped else "Win"
    return f"{index}. {row.get('symbol', '-')} | {side} | {prefix} {win_text}"


def _undecided_recheck_notification_text(
    config: dict[str, Any],
    meta: dict[str, Any],
    *,
    kept_rows: list[dict[str, Any]],
    promoted_rows: list[dict[str, Any]],
) -> str:
    created_at = _parse_time(meta.get("recheck_time")) or datetime.now(timezone.utc)
    local_now = _local_time(config, created_at)
    recheck_index = meta.get("daily_index", "-")
    dropped_rows = [row for row in meta.get("dropped_rows") or [] if isinstance(row, dict)]
    lines = [
        f"📋 RC #{recheck_index} Chưa Duyệt",
        local_now.strftime("%d/%m/%y %H:%M:%S"),
        f"Lần recheck: #{recheck_index} ({local_now.strftime('%H:%M:%S')})",
        (
            f"Kept {len(kept_rows)} | Dropped {len(dropped_rows)} | "
            f"Promoted {len(promoted_rows)}"
        ),
    ]
    if kept_rows:
        lines.append("Giữ lại:")
        lines.extend(_rc_recheck_row_line(index, row) for index, row in enumerate(kept_rows[:3], 1))
    else:
        lines.append("Giữ lại: không còn cặp nào.")
    if dropped_rows:
        lines.append("Bị loại:")
        lines.extend(_rc_recheck_row_line(index, row, dropped=True) for index, row in enumerate(dropped_rows[:3], 1))
    else:
        lines.append("Bị loại: 0")
    if promoted_rows:
        lines.append("Đẩy lên LC nội bộ:")
        lines.extend(_rc_recheck_row_line(index, row) for index, row in enumerate(promoted_rows[:3], 1))
    else:
        lines.append("Đẩy lên LC nội bộ: 0")
    return "\n".join(lines)


def _rc_row_age_label(row: dict[str, Any], now: datetime) -> str:
    age_hours = _row_age_hours(row, now)
    if age_hours is None:
        return str(row.get("age_label") or "-")
    return _age_label(age_hours)


def _rc_row_source_label(config: dict[str, Any], row: dict[str, Any]) -> str:
    origin_label = _row_origin_label(row, config=config)
    if origin_label:
        return origin_label
    return f"Nguồn {_base_source_label(row)}"


def _rc_recheck_row_line_v2(
    config: dict[str, Any],
    index: int,
    row: dict[str, Any],
    *,
    now: datetime,
    dropped: bool = False,
) -> str:
    side = str(row.get("side") or "-").upper()
    try:
        win_text = f"{float(row.get('win_probability_pct') or 0):.2f}%"
    except (TypeError, ValueError):
        win_text = "-"
    prefix = "Win trước" if dropped else "Win"
    parts = [
        f"{index}. {row.get('symbol', '-')}",
        side,
        f"{prefix} {win_text}",
        _rc_row_source_label(config, row),
        f"sống {_rc_row_age_label(row, now)}",
    ]
    if row.get("revived_at"):
        parts.append(_revived_target_label(row))
        revived_time = _revived_time_label(row)
        if revived_time:
            parts.append(f"Hồi sinh {revived_time}")
        parts.append("HS")
    return " | ".join(parts)


def _undecided_recheck_notification_text_v2(
    config: dict[str, Any],
    meta: dict[str, Any],
    *,
    kept_rows: list[dict[str, Any]],
    promoted_rows: list[dict[str, Any]],
) -> str:
    created_at = _parse_time(meta.get("recheck_time")) or datetime.now(timezone.utc)
    local_now = _local_time(config, created_at)
    recheck_index = meta.get("daily_index", "-")
    dropped_rows = [row for row in meta.get("dropped_rows") or [] if isinstance(row, dict)]
    lines = [
        f"📋 RC #{recheck_index} Chưa Duyệt",
        local_now.strftime("%d/%m/%y %H:%M:%S"),
        f"Lần recheck: #{recheck_index} ({local_now.strftime('%H:%M:%S')})",
        f"Kept {len(kept_rows)} | Dropped {len(dropped_rows)} | Promoted {len(promoted_rows)}",
    ]
    if kept_rows:
        lines.append("🟢 Giữ lại:")
        lines.extend(_rc_recheck_row_line_v2(config, index, row, now=created_at) for index, row in enumerate(kept_rows[:3], 1))
    else:
        lines.append("🟢 Giữ lại: không còn cặp nào.")
    if dropped_rows:
        lines.append("🔴 Bị loại:")
        lines.extend(
            _rc_recheck_row_line_v2(config, index, row, now=created_at, dropped=True)
            for index, row in enumerate(dropped_rows[:3], 1)
        )
    else:
        lines.append("🔴 Bị loại: 0")
    if promoted_rows:
        lines.append("♻️ Đẩy lên LC nội bộ:")
        lines.extend(
            _rc_recheck_row_line_v2(config, index, row, now=created_at)
            for index, row in enumerate(promoted_rows[:3], 1)
        )
        top = promoted_rows[0]
        revived_time = _revived_time_label(top)
        lines.append(
            f"Thông báo hồi sinh: {top.get('symbol', '-')} -> {_revived_target_label(top)} | "
            f"từ {_base_source_label(top)} | lúc {revived_time or '-'}"
        )
    else:
        lines.append("♻️ Đẩy lên LC nội bộ: 0")
    return "\n".join(lines)


def internal_notification_timeline_messages(config: dict[str, Any], *, limit_per_frame: int = 5) -> list[str]:
    now = datetime.now(timezone.utc)
    current_day = _day_key(config, now)
    state = _load_state(config, now, reset_for_new_day=False)
    items = state.get("internal_notifications") if isinstance(state.get("internal_notifications"), list) else []
    entries: list[tuple[str, str]] = []
    for event in state.get("one_hour_history") or []:
        if isinstance(event, dict) and _is_same_local_day(config, event.get("created_at") or event.get("slot"), current_day):
            entries.append((str(event.get("created_at") or ""), _one_hour_notification_text(config, event)))
    for event in state.get("two_hour_history") or []:
        if isinstance(event, dict) and _is_same_local_day(config, event.get("created_at") or event.get("slot"), current_day):
            entries.append((str(event.get("created_at") or ""), _two_hour_notification_text(config, event)))
    for event in state.get("four_hour_history") or []:
        if isinstance(event, dict) and _is_same_local_day(config, event.get("created_at") or event.get("slot"), current_day):
            entries.append((str(event.get("created_at") or ""), _four_hour_notification_text(config, event)))
    latest_scan = latest_lc_pipeline_mini_scan(config)
    latest_four_hour = state.get("four_hour_history")[-1] if state.get("four_hour_history") else None
    mini_created_at_keys: set[str] = set()
    if (
        isinstance(latest_scan, dict)
        and latest_scan.get("created_at")
        and _is_same_local_day(config, latest_scan.get("created_at"), current_day)
    ):
        created_at_key = str(latest_scan.get("created_at") or "")
        entries.append((created_at_key, _mini_notification_text(config, latest_scan, latest_four_hour)))
        mini_created_at_keys.add(created_at_key)
    for item in items:
        if not isinstance(item, dict):
            continue
        frame = str(item.get("frame") or "")
        if frame in {"rc", "1h", "2h", "4h"}:
            continue
        created_at_key = str(item.get("created_at") or "")
        if not created_at_key or not _is_same_local_day(config, created_at_key, current_day):
            continue
        if frame == "mini" and created_at_key in mini_created_at_keys:
            continue
        entries.append((created_at_key, _internal_notification_text(item, config)))
    if not entries:
        return []
    timeline_limit = max(4, int(limit_per_frame) * 4)
    timeline = sorted(entries, key=lambda item: item[0])[-timeline_limit:]
    return [text for _, text in timeline]


def undecided_notification_timeline_messages(config: dict[str, Any], *, limit_per_frame: int = 5) -> list[str]:
    now = datetime.now(timezone.utc)
    current_day = _day_key(config, now)
    state = _load_state(config, now, reset_for_new_day=False)
    items = state.get("internal_notifications") if isinstance(state.get("internal_notifications"), list) else []
    entries: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("frame") or "") != "rc":
            continue
        created_at_key = str(item.get("created_at") or "")
        if not created_at_key or not _is_same_local_day(config, created_at_key, current_day):
            continue
        entries.append((created_at_key, _internal_notification_text(item, config)))
    if not entries:
        return []
    timeline_limit = max(3, int(limit_per_frame) * 3)
    timeline = sorted(entries, key=lambda item: item[0])[-timeline_limit:]
    return [text for _, text in timeline]


def format_internal_notifications_view(config: dict[str, Any], *, limit_per_frame: int = 5) -> str:
    timeline_messages = internal_notification_timeline_messages(config, limit_per_frame=limit_per_frame)
    if not timeline_messages:
        return "🔔 Thông báo nội bộ: chưa có dữ liệu 1h/2h/4h/Mini."
    blocks = [
        "🔔 Thông báo nội bộ",
        "Timeline 1h/2h/4h/Mini, mỗi khối là 1 thông báo đầy đủ, mới nhất nằm dưới cùng.",
    ]
    blocks.extend(timeline_messages)
    return "\n\n".join(blocks)


def _notify_two_hour_summary(config: dict[str, Any], event: dict[str, Any]) -> None:
    try:
        from .notifier import send_telegram_message
    except Exception:
        return
    send_telegram_message(config, _two_hour_notification_text(config, event), with_buttons=False, replace_previous=False)


def _notify_one_hour_summary(config: dict[str, Any], event: dict[str, Any]) -> None:
    try:
        from .notifier import send_telegram_message
    except Exception:
        return
    send_telegram_message(config, _one_hour_notification_text(config, event), with_buttons=False, replace_previous=False)


def _notify_four_hour_summary(config: dict[str, Any], event: dict[str, Any]) -> None:
    try:
        from .notifier import send_telegram_message
    except Exception:
        return
    send_telegram_message(config, _four_hour_notification_text(config, event), with_buttons=False, replace_previous=False)


def _notify_undecided_recheck_summary(
    config: dict[str, Any],
    state: dict[str, Any],
    meta: dict[str, Any],
    *,
    kept_rows: list[dict[str, Any]],
    promoted_rows: list[dict[str, Any]],
    now: datetime,
) -> None:
    if int(meta.get("input_count") or 0) <= 0:
        return
    text = _undecided_recheck_notification_text_v2(
        config,
        {**meta, "recheck_time": now.isoformat()},
        kept_rows=kept_rows,
        promoted_rows=promoted_rows,
    )
    lines = text.splitlines()[2:]
    _append_internal_notification(
        state,
        frame="rc",
        icon="📋",
        title=f"RC #{meta.get('daily_index', '-')}",
        lines=lines,
        created_at=now,
    )
    if not _pipeline_config(config)["notify_undecided_recheck_summary"]:
        return
    try:
        from .notifier import send_telegram_message
    except Exception:
        return
    send_telegram_message(config, text, with_buttons=False, replace_previous=False)


def _age_label(hours: float) -> str:
    total_minutes = max(0, int(hours * 60))
    whole_hours = total_minutes // 60
    minutes = total_minutes % 60
    if whole_hours:
        return f"{whole_hours}h{minutes:02d}m"
    return f"{minutes}m"


def _row_age_payload(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    first_seen = _parse_time(row.get("first_seen_at"))
    last_seen = _parse_time(row.get("last_seen_at"))
    age_hours = (now - first_seen).total_seconds() / 3600 if first_seen else None
    return {
        **row,
        "first_seen_at": first_seen.isoformat() if first_seen else row.get("first_seen_at"),
        "last_seen_at": last_seen.isoformat() if last_seen else row.get("last_seen_at"),
        "age_hours": round(age_hours, 3) if age_hours is not None else None,
        "age_label": _age_label(age_hours or 0) if age_hours is not None else "-",
    }


def lc_pipeline_dashboard_payload(config: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    state = _load_state(config, now, reset_for_new_day=False)
    settings = _pipeline_config(config)
    undecided = _sort_saved_rows(
        [
            _row_age_payload(row, now)
            for row in state.get("undecided") or []
            if isinstance(row, dict) and _undecided_row_is_active(row, settings, now)
        ],
        settings,
        reverse=True,
    )
    internal_lc = _sort_saved_rows([_row_age_payload(row, now) for row in state.get("internal_lc") or []], settings, reverse=True)
    two_hour_windows = state.get("two_hour_windows") or []
    latest_two_hour = two_hour_windows[-1] if two_hour_windows else None
    one_hour_history = state.get("one_hour_history") or []
    two_hour_history = state.get("two_hour_history") or []
    four_hour_history = state.get("four_hour_history") or []
    latest_four_hour = four_hour_history[-1] if four_hour_history else None
    return {
        "enabled": settings["enabled"],
        "created_at": now.isoformat(),
        "day_key": state.get("day_key"),
        "daily_one_hour_counter": state.get("daily_one_hour_counter", 0),
        "daily_two_hour_counter": state.get("daily_two_hour_counter", 0),
        "four_hour_counter": state.get("four_hour_counter", 0),
        "last_hourly_slot": state.get("last_hourly_slot"),
        "last_two_hour_slot": state.get("last_two_hour_slot"),
        "last_four_hour_slot": state.get("last_four_hour_slot"),
        "last_recheck_at": state.get("last_recheck_at"),
        "settings": settings,
        "counts": {
            "undecided": len(undecided),
            "internal_lc": len(internal_lc),
            "two_hour_windows": len(two_hour_windows),
            "one_hour_history": len(one_hour_history),
            "two_hour_history": len(two_hour_history),
            "four_hour_history": len(four_hour_history),
        },
        "undecided": undecided,
        "internal_lc": internal_lc,
        "latest_two_hour": latest_two_hour,
        "latest_four_hour": latest_four_hour,
    }


def format_internal_lc_view(config: dict[str, Any], *, limit: int = 10) -> str:
    now = datetime.now(timezone.utc)
    state = _load_state(config, now, reset_for_new_day=False)
    settings = _pipeline_config(config)
    rows = _sort_saved_rows([_row_age_payload(row, now) for row in state.get("internal_lc") or []], settings, reverse=True)[
        : max(1, int(limit))
    ]
    lines = [f"🟡 LC nội bộ { _local_time(config, now).strftime('%d/%m/%Y %H:%M') }", f"📊 Tổng LC: {len(rows)}"]
    if not rows:
        lines.append("⚪ Chưa có LC nội bộ")
        return "\n".join(lines)
    for index, row in enumerate(rows, 1):
        side = str(row.get("side") or "-").upper()
        source_label = _source_label_v2(row)
        try:
            win_label = f"{float(row.get('win_probability_pct') or 0):.2f}%"
        except (TypeError, ValueError):
            win_label = "-"
        lines.append(
            f"{index}. {row.get('symbol', '-')} | {side} | Win {win_label} | {source_label}"
        )
    return "\n".join(lines)


def _notify_promoted_lc(config: dict[str, Any], record: dict[str, Any], *, age_hours: float, remaining_count: int) -> None:
    try:
        from .notifier import send_telegram_message
    except Exception:
        return
    side = str(record.get("side") or "-").upper()
    lines = [
        "♻️ Chưa Duyệt hồi sinh thành LC nội bộ",
        f"{record.get('symbol', '-')} | {side} | sống {_age_label(age_hours)}",
        f"Chưa Duyệt còn: {remaining_count}",
    ]
    send_telegram_message(config, "\n".join(lines), with_buttons=False, replace_previous=False)


def _source_label(row: dict[str, Any]) -> str:
    if row.get("revived_at"):
        return f"HS {row.get('revived_label') or row.get('revived_at')}"
    source_slot = row.get("source_slot") or row.get("state") or "-"
    source_index = row.get("source_index")
    raw_label = str(row.get("source_label") or "").strip()
    source_clock = None
    if raw_label:
        parts = raw_label.split()
        source_clock = parts[-1] if parts else None
    if not source_clock:
        source_time = _parse_time(row.get("source_time"))
        source_clock = source_time.strftime("%H:%M:%S") if source_time else None
    if source_index:
        return f"{source_slot} #{source_index} ({source_clock})" if source_clock else f"{source_slot} #{source_index}"
    return str(source_slot)


def _source_label_v2(row: dict[str, Any]) -> str:
    if row.get("revived_at"):
        return f"{_revived_target_label(row)} | HS {row.get('revived_label') or row.get('revived_at')}"
    return _source_label(row)


def _notify_promoted_lc_v2(config: dict[str, Any], record: dict[str, Any], *, age_hours: float, remaining_count: int) -> None:
    try:
        from .notifier import send_telegram_message
    except Exception:
        return
    side = str(record.get("side") or "-").upper()
    revived_time = _revived_time_label(record) or "-"
    lines = [
        "♻️ Chưa Duyệt hồi sinh thành LC nội bộ",
        f"{record.get('symbol', '-')} | {side} | sống {_age_label(age_hours)}",
        f"Vào: {_revived_target_label(record)} | từ {_base_source_label(record)}",
        f"Hồi sinh lúc: {revived_time}",
        f"Chưa Duyệt còn: {remaining_count}",
    ]
    send_telegram_message(config, "\n".join(lines), with_buttons=False, replace_previous=False)


def notify_mini_pool_summary(
    config: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    scan: dict[str, Any] | None = None,
    slot_id: str | None = None,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(timezone.utc)
    settings = _pipeline_config(config)
    state = _load_state(config, now)
    latest_four_hour = state.get("four_hour_history")[-1] if state.get("four_hour_history") else None
    scan = scan or latest_lc_pipeline_mini_scan(config) or {}
    mini_index = scan.get("mini_index") or int(state.get("daily_mini_counter") or 0) or "-"
    if slot_id and not scan.get("slot_id"):
        scan = {**scan, "slot_id": slot_id}
    local_now = _local_time(config, now)
    selected_symbols = _symbol_list(scan.get("selected_symbols"), limit=3)
    selected_count = len(selected_symbols)
    lines = [
        f"Lần gọi Mini: #{mini_index} ({local_now.strftime('%H:%M')})" if mini_index != "-" else f"Lần gọi Mini: {local_now.strftime('%H:%M')}",
        f"Mini chọn: {selected_count}/3 cặp",
    ]
    if isinstance(latest_four_hour, dict):
        lines.extend(_event_source_lines(latest_four_hour, config=config))
    if rows:
        for index, row in enumerate(rows[:3], 1):
            side = str(row.get("side") or "-").upper()
            lines.append(f"{index}. {row.get('symbol', '-')} | {side} | {_source_label_v2(row)}")
    else:
        lines.append("Mini chưa chọn được cặp nào.")
    lines.append("Lý do: " + str(scan.get("decision_reason_vi") or _mini_reason_vi(scan)))
    if scan.get("slot_id"):
        lines.append(f"Slot: {scan.get('slot_id')}")
    _append_internal_notification(
        state,
        frame="mini",
        icon=MINI_ICON,
        title=f"Mini #{mini_index}" if mini_index != "-" else f"Mini {local_now.strftime('%d/%m/%y %H:%M:%S')}",
        lines=lines,
        created_at=now,
    )
    _save_state(config, state)
    if not settings["notify_mini_pool_summary"]:
        return
    try:
        from .notifier import send_telegram_message
    except Exception:
        return
    send_telegram_message(
        config,
        _mini_notification_text(config, scan, latest_four_hour, rows=rows),
        with_buttons=False,
        replace_previous=False,
    )


def _candidate_is_relaxed_valid(candidate: TradeCandidate, settings: dict[str, Any]) -> bool:
    win_probability = float(candidate.win_probability_pct or 0)
    confidence = float(candidate.confidence or 0)
    return (
        win_probability >= float(settings["relaxed_min_win_probability_pct"])
        and confidence >= float(settings["relaxed_min_confidence"])
        and float(candidate.risk_reward or 0) >= float(settings["relaxed_min_risk_reward"])
    )


def _row_is_relaxed_valid(row: dict[str, Any], settings: dict[str, Any]) -> bool:
    try:
        win_probability = float(row.get("win_probability_pct") or 0)
    except (TypeError, ValueError):
        win_probability = 0.0
    try:
        confidence = float(row.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    try:
        risk_reward = float(row.get("risk_reward") or 0)
    except (TypeError, ValueError):
        risk_reward = 0.0
    return (
        win_probability >= float(settings["relaxed_min_win_probability_pct"])
        and confidence >= float(settings["relaxed_min_confidence"])
        and risk_reward >= float(settings["relaxed_min_risk_reward"])
    )


def _mark_undecided_row(
    row: dict[str, Any],
    *,
    now: datetime,
    settings: dict[str, Any],
    status: str,
    reason: str | None = None,
) -> dict[str, Any]:
    record = {
        **row,
        "state": "CHUA_DUYET",
        "last_seen_at": now.isoformat(),
        "last_recheck_at": now.isoformat(),
        "undecided_status": status,
    }
    record.setdefault("current_win_probability_pct", row.get("win_probability_pct"))
    record["peak_win_probability_pct"] = _peak_win_probability(
        row.get("peak_win_probability_pct"),
        row.get("current_win_probability_pct"),
        row.get("win_probability_pct"),
    )
    if reason:
        record["undecided_reason"] = reason
    elif "undecided_reason" in record:
        record.pop("undecided_reason", None)
    return record


def _mark_missing_undecided_row(
    row: dict[str, Any],
    *,
    now: datetime,
    reason: str,
) -> dict[str, Any]:
    record = {**row}
    if "previous_scan_win_probability_pct" not in record:
        record["previous_scan_win_probability_pct"] = record.get("win_probability_pct")
    record["win_probability_pct"] = 0.0
    record["current_win_probability_pct"] = 0.0
    record["peak_win_probability_pct"] = _peak_win_probability(
        row.get("peak_win_probability_pct"),
        row.get("current_win_probability_pct"),
        row.get("win_probability_pct"),
    )
    record["win_rate_trend"] = "down"
    record["recheck_state"] = "invalid"
    return _mark_undecided_row(record, now=now, settings={}, status="missing_setup", reason=reason)


def _row_with_recheck_metadata(
    row: dict[str, Any],
    *,
    recheck_daily_index: int,
    recheck_slot: str,
    now: datetime,
    local_now: datetime,
) -> dict[str, Any]:
    return {
        **row,
        "recheck_daily_index": recheck_daily_index,
        "recheck_slot": recheck_slot,
        "recheck_time": now.isoformat(),
        "recheck_label": local_now.strftime("%d/%m/%y %H:%M:%S"),
    }


def _recheck_undecided_pool(
    config: dict[str, Any],
    state: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    now: datetime,
    settings: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    undecided_rows = [row for row in rows if isinstance(row, dict) and str(row.get("symbol") or "")]
    if not undecided_rows:
        return [], {"input_count": 0, "refreshed_count": 0, "dropped_count": 0, "kept_count": 0, "warnings": []}
    local_now = _local_time(config, now)
    recheck_daily_index = int(state.get("daily_undecided_recheck_counter") or 0) + 1
    state["daily_undecided_recheck_counter"] = recheck_daily_index
    recheck_slot = _fixed_interval_slot_key(config, now, minutes=int(settings["recheck_interval_minutes"]))
    refreshed_rows, recheck_meta = _recheck_rows_with_latest_market_data(config, undecided_rows, now=now)
    refreshed_by_key = {_setup_key(row): row for row in refreshed_rows if isinstance(row, dict)}
    dropped_by_key = {
        (str(item.get("symbol") or ""), str(item.get("old_side") or "").lower()): item
        for item in list(recheck_meta.get("dropped") or [])
        if isinstance(item, dict)
    }
    dropped_rows_summary: list[dict[str, Any]] = []
    output: list[dict[str, Any]] = []
    for row in undecided_rows:
        key = _setup_key(row)
        if key in refreshed_by_key:
            merged = _merge_rechecked_row(row, refreshed_by_key[key])
            merged = _row_with_recheck_metadata(
                merged,
                recheck_daily_index=recheck_daily_index,
                recheck_slot=recheck_slot,
                now=now,
                local_now=local_now,
            )
            if not _undecided_row_is_active(merged, settings, now):
                dropped_rows_summary.append(
                    {
                        "symbol": merged.get("symbol"),
                        "side": merged.get("side"),
                        "win_probability_pct": merged.get("win_probability_pct"),
                        "reason": _undecided_drop_reason(merged, settings, now),
                    }
                )
                continue
            output.append(_mark_undecided_row(merged, now=now, settings=settings, status="soft_valid"))
            continue
        dropped_item = dropped_by_key.get(key)
        if dropped_item:
            dropped_rows_summary.append(
                {
                    "symbol": row.get("symbol"),
                    "side": row.get("side"),
                    "win_probability_pct": row.get("current_win_probability_pct", row.get("win_probability_pct")),
                    "reason": dropped_item.get("reason"),
                }
            )
            marked_row = _row_with_recheck_metadata(
                row,
                recheck_daily_index=recheck_daily_index,
                recheck_slot=recheck_slot,
                now=now,
                local_now=local_now,
            )
            dropped_rows_summary.append(
                {
                    "symbol": marked_row.get("symbol"),
                    "side": marked_row.get("side"),
                    "win_probability_pct": marked_row.get(
                        "current_win_probability_pct",
                        marked_row.get("win_probability_pct"),
                    ),
                    "reason": str(dropped_item.get("reason") or "khong con setup hop le trong du lieu moi nhat"),
                }
            )
            continue
        dropped_rows_summary.append(
            {
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "win_probability_pct": row.get("current_win_probability_pct", row.get("win_probability_pct")),
                "reason": "khong tim thay setup hop le trong du lieu recheck moi nhat",
            }
        )
    kept_rows = list(output)
    trimmed = False
    if len(kept_rows) > int(settings["undecided_max"]):
        kept_rows = _trim_undecided(kept_rows, settings)
        trimmed = len(kept_rows) < len(output)
    return kept_rows, {
        "input_count": len(undecided_rows),
        "refreshed_count": len(refreshed_rows),
        "dropped_count": len(dropped_rows_summary),
        "kept_count": len(kept_rows),
        "trimmed": trimmed,
        "warnings": list(recheck_meta.get("warnings") or []),
        "dropped": list(recheck_meta.get("dropped") or []),
        "dropped_rows": dropped_rows_summary,
        "daily_index": recheck_daily_index,
        "recheck_label": local_now.strftime("%d/%m/%y %H:%M:%S"),
        "slot": recheck_slot,
    }


def _promote_survivors(
    config: dict[str, Any],
    state: dict[str, Any],
    candidates_by_symbol: dict[str, TradeCandidate],
    settings: dict[str, Any],
    now: datetime,
    blocked_symbols: set[str],
) -> list[dict[str, Any]]:
    if not settings["promote_survivors"]:
        return []
    promoted: list[dict[str, Any]] = []
    active_symbols = open_pending_symbols(config) | set(blocked_symbols)
    internal_lc = list(state.get("internal_lc") or [])
    undecided: list[dict[str, Any]] = []
    local_now = _local_time(config, now)
    revive_candidates: list[tuple[dict[str, Any], TradeCandidate, float]] = []
    for row in state.get("undecided") or []:
        symbol = str(row.get("symbol") or "")
        first_seen = _parse_time(row.get("first_seen_at"))
        candidate = candidates_by_symbol.get(symbol)
        if not first_seen or not candidate:
            undecided.append(row)
            continue
        age_hours = (now - first_seen).total_seconds() / 3600
        if (
            age_hours >= float(settings["promote_after_hours"])
            and symbol not in active_symbols
            and symbol not in blocked_symbols
            and _candidate_is_relaxed_valid(candidate, settings)
        ):
            base_record = {
                **_candidate_record(candidate, state="LC_NOI_BO", first_seen_at=row.get("first_seen_at"), now=now),
                "origin_source_slot": row.get("origin_source_slot"),
                "origin_source_index": row.get("origin_source_index"),
                "origin_source_time": row.get("origin_source_time"),
                "origin_source_label": row.get("origin_source_label"),
                "source_slot": row.get("source_slot"),
                "source_index": row.get("source_index"),
                "source_time": row.get("source_time"),
                "source_label": row.get("source_label"),
            }
            record = {
                **_row_with_source_metadata(
                    base_record,
                    state_label="LC_NOI_BO",
                    source_slot="HS",
                    source_index=row.get("source_index"),
                    now=now,
                    local_now=local_now,
                ),
                "revived_at": now.isoformat(),
                "revived_label": local_now.strftime("%d/%m/%y %H:%M:%S"),
                "revived_age_hours": round(age_hours, 3),
                "revived_age_label": _age_label(age_hours),
            }
            internal_lc = _upsert_by_symbol(internal_lc, record)
            revive_candidates.append((record, candidate, age_hours))
            continue
        undecided.append(row)
    state["undecided"] = _trim_undecided(undecided, settings)
    state["internal_lc"] = _sort_saved_rows(internal_lc, settings, reverse=True)[: int(settings["internal_lc_max"])]
    for position, lc_row in enumerate(state["internal_lc"], 1):
        if isinstance(lc_row, dict) and lc_row.get("revived_at"):
            lc_row["revived_target_rank"] = position
    survivor_symbols = {str(row.get("symbol") or "") for row in state["internal_lc"]}
    for record, candidate, age_hours in revive_candidates:
        symbol = str(record.get("symbol") or "")
        if symbol not in survivor_symbols:
            continue
        for lc_row in state["internal_lc"]:
            if isinstance(lc_row, dict) and str(lc_row.get("symbol") or "") == symbol:
                record["revived_target_rank"] = lc_row.get("revived_target_rank")
                break
        promoted.append(record)
        active_symbols.add(symbol)
        _notify_promoted_lc_v2(config, record, age_hours=age_hours, remaining_count=len(state["undecided"]))
    return promoted


def update_lc_internal_pipeline(
    config: dict[str, Any],
    candidates: list[TradeCandidate],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    with _LC_PIPELINE_UPDATE_LOCK:
        return _update_lc_internal_pipeline_impl(config, candidates, now=now)


def _update_lc_internal_pipeline_impl(
    config: dict[str, Any],
    candidates: list[TradeCandidate],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    settings = _pipeline_config(config)
    if not settings["enabled"]:
        return {"enabled": False}
    state = _load_state(config, now)
    blocked_symbols = _active_symbol_blocklist(config)
    _prune_blocked_state(state, blocked_symbols)
    _prune_low_win_state(state, settings, now)
    top_limit = int(settings["top_limit"])
    candidates_by_symbol = {candidate.symbol: candidate for candidate in candidates}
    hourly_slot = _slot_key(config, now, 1)
    two_hour_slot = _slot_key(config, now, 2)
    four_hour_slot = _slot_key(config, now, 4)
    undecided_recheck_slot = _fixed_interval_slot_key(
        config,
        now,
        minutes=int(settings["recheck_interval_minutes"]),
    )
    slot_tolerance_minutes = int(settings["slot_tolerance_minutes"])
    hourly_slot_open = _slot_is_open(config, now, hours=1, tolerance_minutes=slot_tolerance_minutes)
    two_hour_slot_open = _slot_is_open(config, now, hours=2, tolerance_minutes=slot_tolerance_minutes)
    four_hour_slot_open = _slot_is_open(config, now, hours=4, tolerance_minutes=slot_tolerance_minutes)
    undecided_recheck_due = _fixed_interval_slot_is_exact(
        config,
        now,
        minutes=int(settings["recheck_interval_minutes"]),
    ) and state.get("last_undecided_recheck_slot") != undecided_recheck_slot
    result: dict[str, Any] = {
        "enabled": True,
        "hourly_slot": hourly_slot,
        "two_hour_slot": two_hour_slot,
        "four_hour_slot": four_hour_slot,
        "undecided_recheck_slot": undecided_recheck_slot,
        "slot_tolerance_minutes": slot_tolerance_minutes,
        "hourly_slot_open": hourly_slot_open,
        "two_hour_slot_open": two_hour_slot_open,
        "four_hour_slot_open": four_hour_slot_open,
        "undecided_recheck_due": undecided_recheck_due,
        "created_hourly": False,
        "created_two_hour": False,
        "created_four_hour": False,
        "promoted": [],
        "blocked_symbols": sorted(blocked_symbols),
    }

    if candidates and hourly_slot_open and state.get("last_hourly_slot") != hourly_slot:
        local_now = _local_time(config, now)
        next_hourly_index = int(state.get("daily_one_hour_counter") or 0) + 1
        top = [
            _row_with_source_metadata(
                _candidate_record(candidate, state="HOUR_1", now=now),
                state_label="HOUR_1",
                source_slot="1h",
                source_index=next_hourly_index,
                now=now,
                local_now=local_now,
            )
            for candidate in _rank_candidates(
                candidates,
                top_limit,
                blocked_symbols=blocked_symbols,
                settings=settings,
                phase="1h",
            )
        ]
        one_hour_event = {
            "frame": "1h",
            "slot": hourly_slot,
            "created_at": now.isoformat(),
            "index": next_hourly_index,
            "daily_index": next_hourly_index,
            "date": local_now.strftime("%d/%m/%y"),
            "time": local_now.strftime("%H:%M:%S"),
            "approved": top[:top_limit],
            "rejected": [],
            "source_windows": [],
        }
        state["one_hour_history"].append(one_hour_event)
        state["hourly_windows"].append(
            {
                "slot": hourly_slot,
                "created_at": now.isoformat(),
                "daily_index": next_hourly_index,
                "top": top,
            }
        )
        state["hourly_windows"] = state["hourly_windows"][-2:]
        state["daily_one_hour_counter"] = next_hourly_index
        state["last_hourly_slot"] = hourly_slot
        if top:
            hourly_internal_lc = _build_internal_lc_rows(
                top[:top_limit],
                source_slot="1h",
                source_index=next_hourly_index,
                now=now,
                local_now=local_now,
            )
            state["internal_lc"] = _sort_saved_rows(hourly_internal_lc, settings, reverse=True)[
                : int(settings["internal_lc_max"])
            ]
        result["created_hourly"] = True
        result["one_hour_event"] = one_hour_event
        _append_internal_notification(
            state,
            frame="1h",
            icon=ONE_HOUR_ICON,
            title=f"1h top {len(top)} setup",
            lines=[f"Khung {ONE_HOUR_ICON} 1h: #{next_hourly_index} ({local_now.strftime('%H:%M')})"]
            + [_format_pair_line(index, row, config=config) for index, row in enumerate(top[:3], 1)],
            created_at=now,
        )
        if settings["notify_one_hour_summary"]:
            _notify_one_hour_summary(config, one_hour_event)

    expected_hourly_slots = _aligned_source_slots(config, two_hour_slot, parent_hours=2, child_hours=1)
    aligned_hourly_events = _latest_events_for_slots(list(state.get("one_hour_history") or []), expected_hourly_slots)
    has_two_hour_inputs = any(list(window.get("approved") or []) for window in aligned_hourly_events)
    current_two_hour_event = _latest_event_for_slot(
        list(state.get("two_hour_history") or []),
        two_hour_slot,
        config=config,
        require_aligned_sources=True,
    )
    if two_hour_slot_open and has_two_hour_inputs and current_two_hour_event is None:
        combined: list[dict[str, Any]] = []
        for window in aligned_hourly_events:
            combined.extend(window.get("approved") or [])
        refreshed_combined, two_hour_recheck = _recheck_rows_with_latest_market_data(config, combined, now=now)
        result["two_hour_recheck"] = two_hour_recheck
        eligible_two_hour = [
            row for row in refreshed_combined if _candidate_passes_lc_threshold(row, settings, phase="2h")
        ]
        ranked = _sort_saved_rows(eligible_two_hour, settings, reverse=True)
        next_daily_index = int(state.get("daily_two_hour_counter") or 0) + 1
        local_now = _local_time(config, now)
        approved: list[dict[str, Any]] = []
        seen: set[str] = set()
        approved_keys: set[tuple[str, str]] = set()
        for row in ranked:
            symbol = str(row.get("symbol") or "")
            if not symbol or symbol in seen or symbol in blocked_symbols:
                continue
            approved_row = _build_internal_lc_rows(
                [row],
                source_slot="2h",
                source_index=next_daily_index,
                now=now,
                local_now=local_now,
            )[0]
            approved.append(approved_row)
            approved_keys.add(_setup_key(approved_row))
            seen.add(symbol)
            if len(approved) >= top_limit:
                break
        rejected = _soft_undecided_rows(
            refreshed_combined,
            settings=settings,
            approved_keys=approved_keys,
            blocked_symbols=blocked_symbols,
            source_slot="2h",
            source_index=next_daily_index,
            now=now,
            local_now=local_now,
        )
        existing = list(state.get("undecided") or [])
        existing = _merge_undecided_rows(existing, rejected, now=now)
        state["undecided"] = _sort_saved_rows(existing, settings, reverse=True)
        state["internal_lc"] = _sort_saved_rows(approved, settings, reverse=True)[: int(settings["internal_lc_max"])]
        state["daily_two_hour_counter"] = next_daily_index
        event = {
            "frame": "2h",
            "slot": two_hour_slot,
            "created_at": now.isoformat(),
            "index": state["daily_two_hour_counter"],
            "daily_index": state["daily_two_hour_counter"],
            "date": local_now.strftime("%d/%m/%y"),
            "time": local_now.strftime("%H:%M:%S"),
            "approved": approved[:top_limit],
            "rejected": rejected,
            "recheck": two_hour_recheck,
            "source_windows": [
                _source_window_payload("1h", window, config=config) for window in aligned_hourly_events if isinstance(window, dict)
            ],
        }
        state["two_hour_history"] = _replace_event_for_slot(list(state.get("two_hour_history") or []), event)
        state["two_hour_windows"] = _replace_event_for_slot(list(state.get("two_hour_windows") or []), event)
        state["two_hour_windows"] = state["two_hour_windows"][-2:]
        state["last_two_hour_slot"] = two_hour_slot
        state["telegram_events"] = _replace_event_for_slot(list(state.get("telegram_events") or []), event)
        state["telegram_events"] = state["telegram_events"][-20:]
        _append_internal_notification(
            state,
            frame="2h",
            icon=_two_hour_icon(config),
            title=f"#{state['daily_two_hour_counter']} LC nội bộ tổng hợp 2h",
            lines=_event_source_lines(event, config=config)
            + [_format_pair_line(index, row, config=config, include_origin=True) for index, row in enumerate(approved[:top_limit], 1)],
            created_at=now,
        )
        result["created_two_hour"] = True
        result["two_hour_event"] = event
        result["two_hour_recheck"] = two_hour_recheck
        if settings["notify_two_hour_summary"]:
            _notify_two_hour_summary(config, event)

    expected_two_hour_slots = _aligned_source_slots(config, four_hour_slot, parent_hours=4, child_hours=2)
    aligned_two_hour_events = _latest_events_for_slots_aligned(
        config,
        list(state.get("two_hour_history") or []),
        expected_two_hour_slots,
    )
    has_four_hour_inputs = any(list(window.get("approved") or []) for window in aligned_two_hour_events)
    current_four_hour_event = _latest_event_for_slot(
        list(state.get("four_hour_history") or []),
        four_hour_slot,
        config=config,
        require_aligned_sources=True,
    )
    if four_hour_slot_open and has_four_hour_inputs and current_four_hour_event is None:
        combined_two_hour: list[dict[str, Any]] = []
        for window in aligned_two_hour_events:
            combined_two_hour.extend(window.get("approved") or [])
        refreshed_two_hour, four_hour_recheck = _recheck_rows_with_latest_market_data(config, combined_two_hour, now=now)
        _sync_state_after_recheck(
            state,
            two_hour_slots=[str(window.get("slot") or "") for window in aligned_two_hour_events],
            refreshed_rows=refreshed_two_hour,
            dropped=list(four_hour_recheck.get("dropped") or []),
        )
        result["four_hour_recheck"] = four_hour_recheck
        eligible_four_hour = [
            row for row in refreshed_two_hour if _candidate_passes_lc_threshold(row, settings, phase="4h")
        ]
        ranked_two_hour = _sort_saved_rows(eligible_four_hour, settings, reverse=True)
        next_four_hour_index = int(state.get("four_hour_counter") or 0) + 1
        local_now = _local_time(config, now)
        approved_four_hour: list[dict[str, Any]] = []
        approved_four_hour_keys: set[tuple[str, str]] = set()
        seen_four_hour: set[str] = set()
        for row in ranked_two_hour:
            symbol = str(row.get("symbol") or "")
            if not symbol or symbol in seen_four_hour or symbol in blocked_symbols:
                continue
            approved_row = _build_internal_lc_rows(
                [row],
                source_slot="4h",
                source_index=next_four_hour_index,
                now=now,
                local_now=local_now,
            )[0]
            approved_four_hour.append(approved_row)
            approved_four_hour_keys.add(_setup_key(approved_row))
            seen_four_hour.add(symbol)
            if len(approved_four_hour) >= top_limit:
                break
        rejected_four_hour = _soft_undecided_rows(
            refreshed_two_hour,
            settings=settings,
            approved_keys=approved_four_hour_keys,
            blocked_symbols=blocked_symbols,
            source_slot="4h",
            source_index=next_four_hour_index,
            now=now,
            local_now=local_now,
        )
        existing = list(state.get("undecided") or [])
        existing = _merge_undecided_rows(existing, rejected_four_hour, now=now)
        state["undecided"] = _sort_saved_rows(existing, settings, reverse=True)
        four_hour_event = {
            "frame": "4h",
            "slot": four_hour_slot,
            "created_at": now.isoformat(),
            "index": next_four_hour_index,
            "sequence_index": next_four_hour_index,
            "date": local_now.strftime("%d/%m/%y"),
            "time": local_now.strftime("%H:%M:%S"),
            "approved": approved_four_hour[:top_limit],
            "rejected": rejected_four_hour,
            "recheck": four_hour_recheck,
            "source_windows": [
                _source_window_payload("2h", window, config=config) for window in aligned_two_hour_events if isinstance(window, dict)
            ],
        }
        state["four_hour_history"] = _replace_event_for_slot(list(state.get("four_hour_history") or []), four_hour_event)
        state["four_hour_counter"] = next_four_hour_index
        state["last_four_hour_slot"] = four_hour_slot
        _append_internal_notification(
            state,
            frame="4h",
            icon=FOUR_HOUR_ICON,
            title=f"#{next_four_hour_index} LC nội bộ tổng hợp 4h",
            lines=_event_source_lines(four_hour_event, config=config)
            + (
                [_format_pair_line(index, row, config=config, include_origin=True) for index, row in enumerate(approved_four_hour[:top_limit], 1)]
                if approved_four_hour
                else ["Không có cặp nào đủ điều kiện giữ lại ở 4h."]
            ),
            created_at=now,
        )
        result["created_four_hour"] = True
        result["four_hour_event"] = four_hour_event
        result["four_hour_recheck"] = four_hour_recheck
        if settings["notify_mini_pool_summary"]:
            _notify_four_hour_summary(config, four_hour_event)

    if undecided_recheck_due:
        state["last_recheck_at"] = now.isoformat()
        state["last_undecided_recheck_slot"] = undecided_recheck_slot
        state["undecided"], result["undecided_recheck"] = _recheck_undecided_pool(
            config,
            state,
            list(state.get("undecided") or []),
            now=now,
            settings=settings,
        )
        result["promoted"] = _promote_survivors(config, state, candidates_by_symbol, settings, now, blocked_symbols)
        _notify_undecided_recheck_summary(
            config,
            state,
            result["undecided_recheck"],
            kept_rows=_sort_saved_rows(state.get("undecided", []), settings, reverse=True)[: int(settings["undecided_max"])],
            promoted_rows=result["promoted"],
            now=now,
        )

    result["undecided"] = _sort_saved_rows(state.get("undecided", []), settings, reverse=True)[
        : int(settings["undecided_max"])
    ]
    result["internal_lc"] = _sort_saved_rows(state.get("internal_lc", []), settings, reverse=True)[
        : int(settings["internal_lc_max"])
    ]
    _save_state(config, state)
    return result


def lc_pipeline_mini_pool(config: dict[str, Any], candidates: list[TradeCandidate], *, limit: int = 3) -> list[TradeCandidate]:
    settings = _pipeline_config(config)
    if not settings["enabled"]:
        return _rank_candidates(candidates, limit, settings=_phase_settings(settings, "4h"), phase="4h")
    state = _load_state(config, datetime.now(timezone.utc), reset_for_new_day=False)
    blocked_symbols = _active_symbol_blocklist(config)
    candidates_by_symbol = {candidate.symbol: candidate for candidate in candidates}
    desired_symbols: list[str] = []
    for row in _latest_four_hour_rows(state):
        symbol = str(row.get("symbol") or "")
        if symbol and symbol not in desired_symbols and symbol not in blocked_symbols:
            desired_symbols.append(symbol)
    pool = [candidates_by_symbol[symbol] for symbol in desired_symbols if symbol in candidates_by_symbol]
    return _rank_candidates(pool, limit, blocked_symbols=blocked_symbols, settings=_phase_settings(settings, "4h"), phase="4h")


def lc_pipeline_pool_rows(config: dict[str, Any], symbols: list[str]) -> list[dict[str, Any]]:
    state = _load_state(config, datetime.now(timezone.utc), reset_for_new_day=False)
    rows_by_symbol = {
        str(row.get("symbol") or ""): row
        for row in _latest_four_hour_rows(state)
        if row.get("symbol")
    }
    output: list[dict[str, Any]] = []
    for index, symbol in enumerate(symbols, 1):
        row = rows_by_symbol.get(symbol)
        if row:
            output.append({**row, "mini_index": index})
        else:
            output.append({"symbol": symbol, "source_slot": "scan", "mini_index": index})
    return output
