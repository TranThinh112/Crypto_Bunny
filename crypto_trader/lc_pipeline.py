from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .market import fetch_market_snapshots
from .market_guard import market_guard_symbol_layers
from .models import TradeCandidate, to_jsonable
from .news import collect_news
from .risk import active_trades_summary
from .sizing import apply_position_sizing
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
DEFAULT_TWO_HOUR_ICON = "🕑"
ONE_HOUR_ICON = "🕐"
FOUR_HOUR_ICON = "🕓"
ONE_HOUR_HISTORY_KEEP_DAYS = 3
TWO_HOUR_HISTORY_KEEP_DAYS = 3
FOUR_HOUR_HISTORY_KEEP_DAYS = 7
RECHECK_STABLE_DELTA_PCT = 1.0


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
    return {
        "enabled": bool(internal.get("lc_pipeline_enabled", True)),
        "top_limit": max(1, min(3, int(internal.get("lc_pipeline_top_limit", 3) or 3))),
        "undecided_max": max(3, int(internal.get("lc_pipeline_undecided_max", 6) or 6)),
        "undecided_prune_floor": max(3, int(internal.get("lc_pipeline_undecided_prune_floor", 6) or 6)),
        "undecided_prune_drop": max(1, int(internal.get("lc_pipeline_undecided_prune_drop", 3) or 3)),
        "internal_lc_max": max(1, min(3, int(internal.get("lc_pipeline_internal_lc_max", 3) or 3))),
        "promote_after_hours": max(1.0, float(internal.get("lc_pipeline_promote_after_hours", 6) or 6)),
        "recheck_interval_minutes": max(15, int(internal.get("lc_pipeline_recheck_interval_minutes", 90) or 90)),
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
        "relaxed_min_win_probability_pct": float(internal.get("lc_pipeline_relaxed_min_win_probability_pct", 50) or 50),
        "relaxed_min_confidence": float(internal.get("lc_pipeline_relaxed_min_confidence", 70) or 70),
        "notify_two_hour_summary": bool(internal.get("lc_pipeline_notify_two_hour_summary", False)),
        "notify_mini_pool_summary": bool(internal.get("lc_pipeline_notify_mini_pool_summary", False)),
        "promote_survivors": bool(
            internal.get("lc_pipeline_promote_survivors", internal.get("lc_pipeline_promote_to_pending", True))
        ),
    }


def _two_hour_icon(config: dict[str, Any]) -> str:
    internal = config.get("ai", {}).get("internal", {})
    icon = str(internal.get("lc_pipeline_two_hour_icon") or DEFAULT_TWO_HOUR_ICON).strip()
    return icon or DEFAULT_TWO_HOUR_ICON


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


def _prune_blocked_state(state: dict[str, Any], blocked_symbols: set[str]) -> None:
    if not blocked_symbols:
        return
    state["internal_lc"] = _strip_blocked_symbols(list(state.get("internal_lc") or []), blocked_symbols)
    state["undecided"] = _strip_blocked_symbols(list(state.get("undecided") or []), blocked_symbols)
    hourly_windows = []
    for window in state.get("hourly_windows") or []:
        hourly_windows.append({**window, "top": _strip_blocked_symbols(list(window.get("top") or []), blocked_symbols)})
    state["hourly_windows"] = hourly_windows


def _prune_low_win_state(state: dict[str, Any], settings: dict[str, Any]) -> None:
    state["internal_lc"] = [
        row for row in list(state.get("internal_lc") or []) if _candidate_passes_lc_threshold(row, settings, phase="2h")
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
        "mini_index",
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
    apply_position_sizing(config, refreshed_candidates)
    enrich_quantities(config, refreshed_candidates)
    candidates_by_key = {(candidate.symbol, candidate.side.lower()): candidate for candidate in refreshed_candidates}
    candidates_by_symbol = {candidate.symbol: candidate for candidate in refreshed_candidates}

    refreshed_rows: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for row in valid_rows:
        symbol = str(row.get("symbol") or "")
        side = str(row.get("side") or "").lower()
        candidate = candidates_by_key.get((symbol, side))
        if candidate is not None:
            refreshed_rows.append(_refresh_row_from_candidate(row, candidate, now=now))
            continue
        replacement = candidates_by_symbol.get(symbol)
        if replacement is not None:
            dropped.append(
                {
                    "symbol": symbol,
                    "old_side": side,
                    "reason": f"setup hiện tại đã đổi sang {replacement.side.upper()}",
                    "current_side": replacement.side,
                    "current_win_probability_pct": replacement.win_probability_pct,
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
    state.setdefault("four_hour_counter", 0)
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
    _backfill_one_hour_source_metadata(state)
    _prune_low_win_state(state, _pipeline_config(config))
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
        "undecided_status",
        "undecided_reason",
        "last_recheck_at",
        "mini_index",
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


def latest_lc_pipeline_mini_scan(config: dict[str, Any]) -> dict[str, Any] | None:
    state = _load_state(config, datetime.now(timezone.utc), reset_for_new_day=False)
    scan = state.get("latest_mini_scan") if isinstance(state.get("latest_mini_scan"), dict) else {}
    if not scan:
        return None
    current_symbols = lc_pipeline_internal_symbols(config, limit=10)
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


def _format_pair_line(index: int, row: dict[str, Any]) -> str:
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
    return f"{index}. {row.get('symbol', '-')} | {side_text} | Win {win_text} | Giá {price_text} | KLGD {volume_text}"


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


def format_internal_notifications_view(config: dict[str, Any], *, limit_per_frame: int = 5) -> str:
    state = _load_state(config, datetime.now(timezone.utc), reset_for_new_day=False)
    items = state.get("internal_notifications") if isinstance(state.get("internal_notifications"), list) else []
    if not items:
        return "🔔 Thông báo nội bộ: chưa có dữ liệu 1h/2h/4h."
    timeline_limit = max(3, int(limit_per_frame) * 3)
    timeline = sorted(items, key=lambda row: str(row.get("created_at") or ""))[-timeline_limit:]
    lines = [
        "🔔 Thông báo nội bộ",
        "Timeline 1h/2h/4h, mỗi dòng là 1 thông báo, mới nhất nằm dưới cùng.",
    ]
    for item in timeline:
        lines.append(_internal_notification_summary_line(item, config))
    return "\n".join(lines)


def _notify_two_hour_summary(config: dict[str, Any], event: dict[str, Any]) -> None:
    try:
        from .notifier import send_telegram_message
    except Exception:
        return
    rows = event.get("approved") or []
    icon = _two_hour_icon(config)
    lines = [
        f"{icon} #{event.get('daily_index', '-')} LC nội bộ tổng hợp 2h",
        f"{event.get('date', '-')} {event.get('time', '-')}",
        "3 cặp duyệt lượt này:",
    ]
    lines.extend(_format_pair_line(index, row) for index, row in enumerate(rows[:3], 1))
    send_telegram_message(config, "\n".join(lines), with_buttons=False, replace_previous=False)


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
    undecided = _sort_saved_rows([_row_age_payload(row, now) for row in state.get("undecided") or []], settings, reverse=True)
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
        source_label = _source_label(row)
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


def notify_mini_pool_summary(
    config: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    slot_id: str | None = None,
    now: datetime | None = None,
) -> None:
    if not rows:
        return
    now = now or datetime.now(timezone.utc)
    local_now = _local_time(config, now)
    settings = _pipeline_config(config)
    pool_count = len(rows[:3])
    lines = [
        f"Pool mini 4h: {pool_count}/3 cặp",
    ]
    for index, row in enumerate(rows[:3], 1):
        side = str(row.get("side") or "-").upper()
        lines.append(f"{index}. {row.get('symbol', '-')} | {side} | {_source_label(row)}")
    if slot_id:
        lines.append(f"Slot: {slot_id}")
    state = _load_state(config, now)
    _append_internal_notification(
        state,
        frame="4h",
        icon=FOUR_HOUR_ICON,
        title=f"Mini pool 4h {local_now.strftime('%d/%m/%y %H:%M:%S')}",
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
        "\n".join([f"{FOUR_HOUR_ICON} Mini pool 4h {local_now.strftime('%d/%m/%y %H:%M:%S')}", *lines]),
        with_buttons=False,
        replace_previous=False,
    )


def _candidate_is_relaxed_valid(candidate: TradeCandidate, settings: dict[str, Any]) -> bool:
    win_probability = float(candidate.win_probability_pct or 0)
    confidence = float(candidate.confidence or 0)
    return (
        win_probability > float(settings["relaxed_min_win_probability_pct"])
        and confidence >= float(settings["relaxed_min_confidence"])
        and float(candidate.risk_reward or 0) >= 1.5
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
        win_probability > float(settings["relaxed_min_win_probability_pct"])
        and confidence >= float(settings["relaxed_min_confidence"])
        and risk_reward >= 1.5
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


def _recheck_undecided_pool(
    config: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    now: datetime,
    settings: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    undecided_rows = [row for row in rows if isinstance(row, dict) and str(row.get("symbol") or "")]
    if not undecided_rows:
        return [], {"input_count": 0, "refreshed_count": 0, "dropped_count": 0, "kept_count": 0, "warnings": []}
    refreshed_rows, recheck_meta = _recheck_rows_with_latest_market_data(config, undecided_rows, now=now)
    refreshed_by_key = {_setup_key(row): row for row in refreshed_rows if isinstance(row, dict)}
    dropped_by_key = {
        (str(item.get("symbol") or ""), str(item.get("old_side") or "").lower()): item
        for item in list(recheck_meta.get("dropped") or [])
        if isinstance(item, dict)
    }
    output: list[dict[str, Any]] = []
    for row in undecided_rows:
        key = _setup_key(row)
        if key in refreshed_by_key:
            merged = _merge_rechecked_row(row, refreshed_by_key[key])
            status = "soft_valid" if _row_is_relaxed_valid(merged, settings) else "soft_invalid"
            output.append(_mark_undecided_row(merged, now=now, settings=settings, status=status))
            continue
        dropped_item = dropped_by_key.get(key)
        if dropped_item:
            output.append(
                _mark_missing_undecided_row(
                    row,
                    now=now,
                    reason=str(dropped_item.get("reason") or "khong con setup hop le trong du lieu moi nhat"),
                )
            )
            continue
        stale_row = {
            **row,
            "recheck_state": "invalid",
            "win_rate_trend": "down",
        }
        output.append(_mark_undecided_row(stale_row, now=now, settings=settings, status="stale"))
    kept_rows = list(output)
    trimmed = False
    if len(kept_rows) > int(settings["undecided_max"]):
        kept_rows = _trim_undecided(kept_rows, settings)
        trimmed = len(kept_rows) < len(output)
    return kept_rows, {
        "input_count": len(undecided_rows),
        "refreshed_count": len(refreshed_rows),
        "dropped_count": len(dropped_by_key),
        "kept_count": len(kept_rows),
        "trimmed": trimmed,
        "warnings": list(recheck_meta.get("warnings") or []),
        "dropped": list(recheck_meta.get("dropped") or []),
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
    survivor_symbols = {str(row.get("symbol") or "") for row in state["internal_lc"]}
    for record, candidate, age_hours in revive_candidates:
        symbol = str(record.get("symbol") or "")
        if symbol not in survivor_symbols:
            continue
        promoted.append(record)
        active_symbols.add(symbol)
        _notify_promoted_lc(config, record, age_hours=age_hours, remaining_count=len(state["undecided"]))
    return promoted


def update_lc_internal_pipeline(
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
    _prune_low_win_state(state, settings)
    top_limit = int(settings["top_limit"])
    candidates_by_symbol = {candidate.symbol: candidate for candidate in candidates}
    hourly_slot = _slot_key(config, now, 1)
    two_hour_slot = _slot_key(config, now, 2)
    four_hour_slot = _slot_key(config, now, 4)
    result: dict[str, Any] = {
        "enabled": True,
        "hourly_slot": hourly_slot,
        "two_hour_slot": two_hour_slot,
        "four_hour_slot": four_hour_slot,
        "created_hourly": False,
        "created_two_hour": False,
        "created_four_hour": False,
        "promoted": [],
        "blocked_symbols": sorted(blocked_symbols),
    }

    if candidates and state.get("last_hourly_slot") != hourly_slot:
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
            lines=[_format_pair_line(index, row) for index, row in enumerate(top[:3], 1)],
            created_at=now,
        )

    hourly_windows = state.get("hourly_windows") or []
    recent_hourly_windows = hourly_windows[-2:] if len(hourly_windows) >= 2 else []
    has_full_two_hour_inputs = (
        len(recent_hourly_windows) >= 2
        and all(list(window.get("top") or []) for window in recent_hourly_windows)
    )
    if has_full_two_hour_inputs and state.get("last_two_hour_slot") != two_hour_slot:
        combined: list[dict[str, Any]] = []
        for window in recent_hourly_windows:
            combined.extend(window.get("top") or [])
        refreshed_combined, two_hour_recheck = _recheck_rows_with_latest_market_data(config, combined, now=now)
        _sync_state_after_recheck(
            state,
            hourly_slots=[str(window.get("slot") or "") for window in recent_hourly_windows],
            refreshed_rows=refreshed_combined,
            dropped=list(two_hour_recheck.get("dropped") or []),
        )
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
        rejected = [
            _row_with_source_metadata(
                row,
                state_label="CHUA_DUYET",
                source_slot="2h",
                source_index=next_daily_index,
                now=now,
                local_now=local_now,
            )
            for row in ranked
            if _setup_key(row) not in approved_keys and str(row.get("symbol") or "") not in blocked_symbols
        ]
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
        }
        state["two_hour_history"].append(event)
        state["two_hour_windows"].append(event)
        state["two_hour_windows"] = state["two_hour_windows"][-2:]
        state["last_two_hour_slot"] = two_hour_slot
        state["telegram_events"].append(event)
        state["telegram_events"] = state["telegram_events"][-20:]
        _append_internal_notification(
            state,
            frame="2h",
            icon=_two_hour_icon(config),
            title=f"#{state['daily_two_hour_counter']} LC nội bộ tổng hợp 2h",
            lines=[_format_pair_line(index, row) for index, row in enumerate(approved[:top_limit], 1)],
            created_at=now,
        )
        result["created_two_hour"] = True
        result["two_hour_event"] = event
        result["two_hour_recheck"] = two_hour_recheck
        if settings["notify_two_hour_summary"]:
            _notify_two_hour_summary(config, event)

    two_hour_windows = state.get("two_hour_windows") or []
    recent_two_hour_windows = two_hour_windows[-2:] if len(two_hour_windows) >= 2 else []
    has_full_four_hour_inputs = (
        len(recent_two_hour_windows) >= 2
        and all(list(window.get("approved") or []) for window in recent_two_hour_windows)
    )
    if has_full_four_hour_inputs and state.get("last_four_hour_slot") != four_hour_slot:
        combined_two_hour: list[dict[str, Any]] = []
        for window in recent_two_hour_windows:
            combined_two_hour.extend(window.get("approved") or [])
        refreshed_two_hour, four_hour_recheck = _recheck_rows_with_latest_market_data(config, combined_two_hour, now=now)
        _sync_state_after_recheck(
            state,
            two_hour_slots=[str(window.get("slot") or "") for window in recent_two_hour_windows],
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
        rejected_four_hour = [
            _row_with_source_metadata(
                row,
                state_label="CHUA_DUYET",
                source_slot="4h",
                source_index=next_four_hour_index,
                now=now,
                local_now=local_now,
            )
            for row in ranked_two_hour
            if _setup_key(row) not in approved_four_hour_keys and str(row.get("symbol") or "") not in blocked_symbols
        ]
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
        }
        state["four_hour_history"].append(four_hour_event)
        state["four_hour_counter"] = next_four_hour_index
        state["last_four_hour_slot"] = four_hour_slot
        result["created_four_hour"] = True
        result["four_hour_event"] = four_hour_event
        result["four_hour_recheck"] = four_hour_recheck

    last_recheck_at = _parse_time(state.get("last_recheck_at"))
    recheck_due = (
        last_recheck_at is None
        or (now - last_recheck_at).total_seconds() >= int(settings["recheck_interval_minutes"]) * 60
    )
    if recheck_due:
        state["last_recheck_at"] = now.isoformat()
        state["undecided"], result["undecided_recheck"] = _recheck_undecided_pool(
            config,
            list(state.get("undecided") or []),
            now=now,
            settings=settings,
        )
        result["promoted"] = _promote_survivors(config, state, candidates_by_symbol, settings, now, blocked_symbols)

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
