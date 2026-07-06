from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import TradeCandidate, to_jsonable
from .storage import get_journal_state, open_pending_symbols, set_journal_state


LC_PIPELINE_STATE_KEY = "lc_internal_pipeline_state"
DEFAULT_TWO_HOUR_ICON = "🕑"
ONE_HOUR_ICON = "🕐"
FOUR_HOUR_ICON = "🕓"


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _pipeline_config(config: dict[str, Any]) -> dict[str, Any]:
    internal = config.get("ai", {}).get("internal", {})
    return {
        "enabled": bool(internal.get("lc_pipeline_enabled", True)),
        "top_limit": max(1, min(3, int(internal.get("lc_pipeline_top_limit", 3) or 3))),
        "undecided_max": max(3, int(internal.get("lc_pipeline_undecided_max", 6) or 6)),
        "undecided_prune_floor": max(3, int(internal.get("lc_pipeline_undecided_prune_floor", 6) or 6)),
        "undecided_prune_drop": max(1, int(internal.get("lc_pipeline_undecided_prune_drop", 3) or 3)),
        "internal_lc_max": max(1, min(3, int(internal.get("lc_pipeline_internal_lc_max", 3) or 3))),
        "promote_after_hours": max(1.0, float(internal.get("lc_pipeline_promote_after_hours", 6) or 6)),
        "recheck_interval_minutes": max(15, int(internal.get("lc_pipeline_recheck_interval_minutes", 90) or 90)),
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


def _rank_candidates(candidates: list[TradeCandidate], limit: int) -> list[TradeCandidate]:
    clear = [candidate for candidate in candidates if _has_clear_candlestick(candidate)]
    ranked = sorted(clear or candidates, key=_candidate_score, reverse=True)
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


def _candidate_record(candidate: TradeCandidate, *, state: str, first_seen_at: str | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    payload = to_jsonable(candidate)
    indicator = payload.get("indicator_summary") if isinstance(payload, dict) else {}
    return {
        "symbol": candidate.symbol,
        "base": candidate.base,
        "side": candidate.side,
        "state": state,
        "first_seen_at": first_seen_at or now,
        "last_seen_at": now,
        "entry": candidate.entry,
        "price": candidate.entry,
        "confidence": candidate.confidence,
        "win_probability_pct": candidate.win_probability_pct,
        "risk_reward": candidate.risk_reward,
        "volume_ratio": (indicator or {}).get("volume_ratio"),
        "payload": payload,
    }


def _load_state(config: dict[str, Any], now: datetime) -> dict[str, Any]:
    raw = get_journal_state(config, LC_PIPELINE_STATE_KEY)
    if raw:
        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            state = {}
    else:
        state = {}
    day = _day_key(config, now)
    if state.get("day_key") != day:
        state["day_key"] = day
        state["daily_two_hour_counter"] = 0
        state["hourly_windows"] = []
        state["two_hour_windows"] = []
        state["telegram_events"] = []
        state["internal_notifications"] = []
    state.setdefault("hourly_windows", [])
    state.setdefault("two_hour_windows", [])
    state.setdefault("undecided", [])
    state.setdefault("internal_lc", [])
    state.setdefault("telegram_events", [])
    state.setdefault("internal_notifications", [])
    state.setdefault("daily_two_hour_counter", 0)
    return state


def _save_state(config: dict[str, Any], state: dict[str, Any]) -> None:
    set_journal_state(config, LC_PIPELINE_STATE_KEY, json.dumps(state, ensure_ascii=False))


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
    floor = int(settings["undecided_prune_floor"])
    if len(rows) <= floor:
        return sorted(rows, key=_candidate_score, reverse=True)
    drop_count = min(int(settings["undecided_prune_drop"]), len(rows) - floor)
    ranked_low_first = sorted(rows, key=_candidate_score)
    dropped_keys = {_setup_key(row) for row in ranked_low_first[:drop_count]}
    kept = [row for row in rows if _setup_key(row) not in dropped_keys]
    return sorted(kept, key=_candidate_score, reverse=True)


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
    compact_details = " | ".join(part for part in details[:3] if part)
    if compact_details:
        return f"{item.get('icon', '🔔')} {item.get('title', '-')} | {created_label} | {compact_details}"
    return f"{item.get('icon', '🔔')} {item.get('title', '-')} | {created_label}"


def format_internal_notifications_view(config: dict[str, Any], *, limit_per_frame: int = 5) -> str:
    state = _load_state(config, datetime.now(timezone.utc))
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
    state = _load_state(config, now)
    settings = _pipeline_config(config)
    undecided = sorted(
        [_row_age_payload(row, now) for row in state.get("undecided") or []],
        key=_candidate_score,
        reverse=True,
    )
    internal_lc = sorted(
        [_row_age_payload(row, now) for row in state.get("internal_lc") or []],
        key=_candidate_score,
        reverse=True,
    )
    two_hour_windows = state.get("two_hour_windows") or []
    latest_two_hour = two_hour_windows[-1] if two_hour_windows else None
    return {
        "enabled": settings["enabled"],
        "created_at": now.isoformat(),
        "day_key": state.get("day_key"),
        "daily_two_hour_counter": state.get("daily_two_hour_counter", 0),
        "last_hourly_slot": state.get("last_hourly_slot"),
        "last_two_hour_slot": state.get("last_two_hour_slot"),
        "last_recheck_at": state.get("last_recheck_at"),
        "settings": settings,
        "counts": {
            "undecided": len(undecided),
            "internal_lc": len(internal_lc),
            "two_hour_windows": len(two_hour_windows),
        },
        "undecided": undecided,
        "internal_lc": internal_lc,
        "latest_two_hour": latest_two_hour,
    }


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
    if source_index:
        return f"{source_slot} #{source_index}"
    return str(source_slot)


def notify_mini_pool_summary(config: dict[str, Any], rows: list[dict[str, Any]], *, slot_id: str | None = None) -> None:
    if not rows:
        return
    now = datetime.now(timezone.utc)
    local_now = _local_time(config, now)
    settings = _pipeline_config(config)
    lines = [
        "3 cặp gửi lên mini:",
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
        win_probability >= float(settings["relaxed_min_win_probability_pct"])
        and confidence >= float(settings["relaxed_min_confidence"])
        and float(candidate.risk_reward or 0) >= 1.5
    )


def _promote_survivors(
    config: dict[str, Any],
    state: dict[str, Any],
    candidates_by_symbol: dict[str, TradeCandidate],
    settings: dict[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    if not settings["promote_survivors"]:
        return []
    promoted: list[dict[str, Any]] = []
    active_symbols = open_pending_symbols(config)
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
            and _candidate_is_relaxed_valid(candidate, settings)
        ):
            record = {
                **_candidate_record(candidate, state="LC_NOI_BO", first_seen_at=row.get("first_seen_at")),
                "source_slot": "HS",
                "source_index": row.get("source_index"),
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
    state["internal_lc"] = sorted(internal_lc, key=_candidate_score, reverse=True)[: int(settings["internal_lc_max"])]
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
    top_limit = int(settings["top_limit"])
    candidates_by_symbol = {candidate.symbol: candidate for candidate in candidates}
    hourly_slot = _slot_key(config, now, 1)
    two_hour_slot = _slot_key(config, now, 2)
    result: dict[str, Any] = {
        "enabled": True,
        "hourly_slot": hourly_slot,
        "two_hour_slot": two_hour_slot,
        "created_hourly": False,
        "created_two_hour": False,
        "promoted": [],
    }

    if candidates and state.get("last_hourly_slot") != hourly_slot:
        top = [_candidate_record(candidate, state="HOUR_1") for candidate in _rank_candidates(candidates, top_limit)]
        state["hourly_windows"].append({"slot": hourly_slot, "created_at": now.isoformat(), "top": top})
        state["hourly_windows"] = state["hourly_windows"][-2:]
        state["last_hourly_slot"] = hourly_slot
        result["created_hourly"] = True
        _append_internal_notification(
            state,
            frame="1h",
            icon=ONE_HOUR_ICON,
            title=f"1h top {len(top)} setup",
            lines=[_format_pair_line(index, row) for index, row in enumerate(top[:3], 1)],
            created_at=now,
        )

    hourly_windows = state.get("hourly_windows") or []
    if len(hourly_windows) >= 2 and state.get("last_two_hour_slot") != two_hour_slot:
        combined: list[dict[str, Any]] = []
        for window in hourly_windows[-2:]:
            combined.extend(window.get("top") or [])
        ranked = sorted(combined, key=_candidate_score, reverse=True)
        next_daily_index = int(state.get("daily_two_hour_counter") or 0) + 1
        local_now = _local_time(config, now)
        approved: list[dict[str, Any]] = []
        seen: set[str] = set()
        approved_keys: set[tuple[str, str]] = set()
        for row in ranked:
            symbol = str(row.get("symbol") or "")
            if not symbol or symbol in seen:
                continue
            approved_row = {
                **row,
                "state": "LC_NOI_BO",
                "source_slot": "2h",
                "source_index": next_daily_index,
                "source_time": now.isoformat(),
                "source_label": local_now.strftime("%d/%m/%y %H:%M:%S"),
            }
            approved.append(approved_row)
            approved_keys.add(_setup_key(approved_row))
            seen.add(symbol)
            if len(approved) >= top_limit:
                break
        rejected = [
            {
                **row,
                "state": "CHUA_DUYET",
                "source_slot": "2h",
                "source_index": next_daily_index,
                "source_time": now.isoformat(),
                "source_label": local_now.strftime("%d/%m/%y %H:%M:%S"),
            }
            for row in ranked
            if _setup_key(row) not in approved_keys
        ]
        existing = list(state.get("undecided") or [])
        existing_by_setup = {_setup_key(row): row for row in existing if row.get("symbol")}
        for row in rejected:
            previous = existing_by_setup.get(_setup_key(row))
            first_seen = previous.get("first_seen_at") if previous else row.get("first_seen_at")
            existing = _upsert_by_setup(existing, {**row, "first_seen_at": first_seen, "last_seen_at": now.isoformat()})
        state["undecided"] = _trim_undecided(existing, settings)
        state["internal_lc"] = sorted(approved, key=_candidate_score, reverse=True)[: int(settings["internal_lc_max"])]
        state["daily_two_hour_counter"] = next_daily_index
        event = {
            "slot": two_hour_slot,
            "created_at": now.isoformat(),
            "daily_index": state["daily_two_hour_counter"],
            "date": local_now.strftime("%d/%m/%y"),
            "time": local_now.strftime("%H:%M:%S"),
            "approved": approved[:top_limit],
            "rejected": rejected,
        }
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
        if settings["notify_two_hour_summary"]:
            _notify_two_hour_summary(config, event)

    last_recheck_at = _parse_time(state.get("last_recheck_at"))
    recheck_due = (
        last_recheck_at is None
        or (now - last_recheck_at).total_seconds() >= int(settings["recheck_interval_minutes"]) * 60
    )
    if recheck_due:
        state["last_recheck_at"] = now.isoformat()
        result["promoted"] = _promote_survivors(config, state, candidates_by_symbol, settings, now)

    result["undecided"] = sorted(state.get("undecided", []), key=_candidate_score, reverse=True)[
        : int(settings["undecided_max"])
    ]
    result["internal_lc"] = sorted(state.get("internal_lc", []), key=_candidate_score, reverse=True)[
        : int(settings["internal_lc_max"])
    ]
    _save_state(config, state)
    return result


def lc_pipeline_mini_pool(config: dict[str, Any], candidates: list[TradeCandidate], *, limit: int = 3) -> list[TradeCandidate]:
    settings = _pipeline_config(config)
    if not settings["enabled"]:
        return _rank_candidates(candidates, limit)
    state = _load_state(config, datetime.now(timezone.utc))
    candidates_by_symbol = {candidate.symbol: candidate for candidate in candidates}
    desired_symbols: list[str] = []
    for row in state.get("internal_lc") or []:
        symbol = str(row.get("symbol") or "")
        if symbol and symbol not in desired_symbols:
            desired_symbols.append(symbol)
    pool = [candidates_by_symbol[symbol] for symbol in desired_symbols if symbol in candidates_by_symbol]
    return _rank_candidates(pool, limit)


def lc_pipeline_pool_rows(config: dict[str, Any], symbols: list[str]) -> list[dict[str, Any]]:
    state = _load_state(config, datetime.now(timezone.utc))
    rows_by_symbol = {
        str(row.get("symbol") or ""): row
        for row in state.get("internal_lc") or []
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
