from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import project_path
from .models import to_jsonable
from .storage import get_journal_state, recent_market_scan_memory, set_journal_state
from .trade_intent import _mapping_to_candidate, build_shadow_trade_intent

LOGGER = logging.getLogger(__name__)

TREND_SCAN_LOG_PREFIX = "trend_scan_log"
TREND_SCAN_LAST_SLOT_KEY = "trend_scan_last_slot"
POOL_PIPELINE_LOG_PREFIX = "pool_pipeline_log"
POOL_PIPELINE_LAST_SOURCE_KEY = "pool_pipeline_last_source"
TREND_WATCHLIST_STATE_KEY = "trend_watchlist_state"
TREND_SETUP_REVIEW_LOG_PREFIX = "trend_setup_review_log"
TREND_PENDING_PLAN_STATE_KEY = "trend_pending_plan_state"
TREND_SETUP_REVIEW_LAST_CALL_PREFIX = "trend_setup_review_last_call"


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_token(value: Any) -> str:
    return "".join(ch if ch.isalnum() or ch in "_.:-" else "_" for ch in str(value or "").strip())[:120] or "unknown"

def _clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    return max(lower, min(upper, value))


def _slot_id(config: dict[str, Any], now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    internal = config.get("ai", {}).get("internal", {}) if isinstance(config.get("ai"), dict) else {}
    interval = max(60, int(internal.get("trend_scan_log_interval_seconds", 900) or 900))
    ts = int(now.timestamp())
    slot_ts = (ts // interval) * interval
    return datetime.fromtimestamp(slot_ts, tz=timezone.utc).isoformat()


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


def _trend_direction(row: dict[str, Any]) -> str:
    indicator = row.get("indicator") if isinstance(row.get("indicator"), dict) else {}
    trend = str(indicator.get("trend") or row.get("trend") or "").strip().lower()
    if trend in {"up", "bullish", "long"}:
        return "up"
    if trend in {"down", "bearish", "short"}:
        return "down"
    ema_gap = _float(indicator.get("ema_gap_pct"))
    price_vs_ema = _float(indicator.get("price_vs_ema_slow_pct"))
    if ema_gap > 0 and price_vs_ema > 0:
        return "up"
    if ema_gap < 0 and price_vs_ema < 0:
        return "down"
    return "mixed"


def _timeframe_weight(timeframe: str) -> float:
    return {"4h": 4.0, "2h": 3.0, "1h": 2.5, "15m": 1.4, "5m": 1.0}.get(str(timeframe).lower(), 1.0)


def _row_strength(row: dict[str, Any]) -> float:
    indicator = row.get("indicator") if isinstance(row.get("indicator"), dict) else {}
    ema_gap = abs(_float(indicator.get("ema_gap_pct")))
    price_vs_ema = abs(_float(indicator.get("price_vs_ema_slow_pct")))
    volume_ratio = max(0.0, _float(indicator.get("volume_ratio")))
    score = _float(row.get("score") or row.get("win_probability_pct") or row.get("confidence"))
    strength = min(45.0, ema_gap * 6.0) + min(35.0, price_vs_ema * 4.0) + min(20.0, volume_ratio * 5.0)
    if score > 0:
        strength = (strength * 0.65) + (min(100.0, score) * 0.35)
    return round(max(0.0, min(100.0, strength)), 2)

def _frame_group(timeframe: str) -> str:
    name = str(timeframe).lower()
    if name in {"4h", "2h", "1h"}:
        return "htf"
    if name in {"15m", "5m", "1m"}:
        return "entry"
    return "other"

def _group_weight(timeframe: str, group: str) -> float:
    name = str(timeframe).lower()
    if group == "htf":
        return {"4h": 4.0, "2h": 3.0, "1h": 3.0}.get(name, 0.0)
    if group == "entry":
        return {"15m": 3.0, "5m": 2.0, "1m": 1.0}.get(name, 0.0)
    return 0.0

def _score_frames(frames: list[dict[str, Any]], group: str) -> dict[str, Any]:
    long_score = 0.0
    short_score = 0.0
    total_weight = 0.0
    aligned = 0
    mixed = 0
    used: list[dict[str, Any]] = []
    for frame in frames:
        timeframe = str(frame.get("timeframe") or "").lower()
        if _frame_group(timeframe) != group:
            continue
        weight = _group_weight(timeframe, group)
        if weight <= 0:
            continue
        direction = str(frame.get("direction") or "mixed")
        strength = _float(frame.get("strength"))
        total_weight += weight
        used.append(frame)
        if direction == "up":
            long_score += strength * weight
            aligned += 1
        elif direction == "down":
            short_score += strength * weight
            aligned += 1
        else:
            mixed += 1
    if total_weight <= 0:
        return {"side": "mixed", "score": 0.0, "long_score": 0.0, "short_score": 0.0, "frame_count": 0, "aligned_count": 0, "mixed_count": 0, "frames": []}
    long_score = round(long_score / total_weight, 2)
    short_score = round(short_score / total_weight, 2)
    if long_score >= short_score and long_score >= 45:
        side = "long"
        score = long_score
    elif short_score > long_score and short_score >= 45:
        side = "short"
        score = short_score
    else:
        side = "mixed"
        score = round(max(long_score, short_score), 2)
    return {
        "side": side,
        "score": score,
        "long_score": long_score,
        "short_score": short_score,
        "frame_count": len(used),
        "aligned_count": aligned,
        "mixed_count": mixed,
        "frames": used,
    }

def _core_trend_symbols(config: dict[str, Any]) -> list[str]:
    universe = config.get("strategy", {}).get("universe", {}) if isinstance(config.get("strategy"), dict) else {}
    raw_symbols = list(universe.get("priority_symbols") or [])
    raw_symbols.extend(["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT", "XRP/USDT:USDT", "ETC/USDT:USDT"])
    seen: set[str] = set()
    result: list[str] = []
    for symbol in raw_symbols:
        normalized = str(symbol or "").strip().upper()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result

def _trend_watch_decision(config: dict[str, Any], *, htf: dict[str, Any], entry: dict[str, Any], legacy_side: str, legacy_score: float) -> dict[str, Any]:
    internal = config.get("ai", {}).get("internal", {}) if isinstance(config.get("ai"), dict) else {}
    htf_threshold = _float(internal.get("trend_scan_htf_watch_threshold"), 60.0)
    htf_immediate = _float(internal.get("trend_scan_htf_immediate_threshold"), 75.0)
    entry_setup_threshold = _float(internal.get("trend_scan_entry_setup_threshold"), 55.0)
    entry_ai_threshold = _float(internal.get("trend_scan_entry_ai_threshold"), 65.0)
    countertrend_entry_threshold = _float(internal.get("trend_scan_countertrend_entry_threshold"), 42.0)
    required_confirmations = max(1, int(internal.get("trend_scan_htf_required_confirmations", 2) or 2))
    htf_side = str(htf.get("side") or "mixed")
    htf_score = _float(htf.get("score"))
    entry_side = str(entry.get("side") or "mixed")
    entry_score = _float(entry.get("score"))
    watch = htf_side in {"long", "short"} and htf_score >= htf_threshold
    entry_ready = watch and entry_side == htf_side and entry_score >= entry_setup_threshold
    ai_ready = entry_ready and entry_score >= entry_ai_threshold
    opposite_side = "short" if htf_side == "long" else "long" if htf_side == "short" else "mixed"
    countertrend_review = watch and entry_side == opposite_side and entry_score >= countertrend_entry_threshold
    if ai_ready:
        reason = "htf_trend_entry_ai_ready"
    elif entry_ready:
        reason = "htf_trend_entry_setup_ready"
    elif countertrend_review:
        reason = "htf_trend_overextended_countertrend_review"
    elif watch:
        reason = "htf_trend_watch_entry_not_ready"
    else:
        reason = "htf_trend_below_threshold"
    if ai_ready:
        entry_action = f"READY_{htf_side.upper()}"
    elif entry_ready:
        entry_action = f"SETUP_{htf_side.upper()}_REVIEW"
    elif countertrend_review:
        entry_action = f"REVIEW_COUNTERTREND_{opposite_side.upper()}"
    elif watch and htf_side == "long":
        entry_action = "WAIT_PULLBACK_LONG"
    elif watch and htf_side == "short":
        entry_action = "WAIT_PULLBACK_SHORT"
    else:
        entry_action = "NO_TRADE"
    return {
        "watch": watch,
        "side": htf_side if watch else legacy_side,
        "score": htf_score if watch else legacy_score,
        "reason": reason,
        "confirmation_mode": "immediate" if htf_score >= htf_immediate else f"{required_confirmations}_scan",
        "required_confirmations": required_confirmations,
        "entry_ready": entry_ready,
        "ai_ready": ai_ready,
        "countertrend_review": countertrend_review,
        "entry_action": entry_action,
        "thresholds": {
            "htf_watch": htf_threshold,
            "htf_immediate": htf_immediate,
            "entry_setup": entry_setup_threshold,
            "entry_ai": entry_ai_threshold,
            "countertrend_entry": countertrend_entry_threshold,
        },
    }


def build_trend_scan_snapshot(config: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    internal = config.get("ai", {}).get("internal", {}) if isinstance(config.get("ai"), dict) else {}
    lookback_hours = max(1, int(internal.get("trend_scan_lookback_hours", 24) or 24))
    top_limit = max(1, int(internal.get("trend_scan_top_limit", 20) or 20))
    memory = recent_market_scan_memory(
        config,
        lookback_hours=lookback_hours,
        per_symbol_timeframe_limit=1,
        total_limit=max(200, top_limit * 20),
        include_details=True,
    )
    symbols: list[dict[str, Any]] = []
    all_rows = 0
    for symbol, frame_map in memory.items():
        long_score = 0.0
        short_score = 0.0
        frames: list[dict[str, Any]] = []
        latest_at = ""
        for timeframe, rows in frame_map.items():
            if not rows:
                continue
            row = rows[0]
            all_rows += 1
            latest_at = max(latest_at, str(row.get("created_at") or ""))
            direction = _trend_direction(row)
            strength = _row_strength(row)
            weight = _timeframe_weight(str(timeframe))
            if direction == "up":
                long_score += strength * weight
            elif direction == "down":
                short_score += strength * weight
            frames.append(
                {
                    "timeframe": timeframe,
                    "direction": direction,
                    "strength": strength,
                    "score": row.get("score"),
                    "side": row.get("side"),
                    "created_at": row.get("created_at"),
                }
            )
        total_weight = sum(_timeframe_weight(str(item.get("timeframe"))) for item in frames) or 1.0
        long_score = round(long_score / total_weight, 2)
        short_score = round(short_score / total_weight, 2)
        if long_score >= short_score and long_score >= 45:
            trend_side = "long"
            trend_score = long_score
        elif short_score > long_score and short_score >= 45:
            trend_side = "short"
            trend_score = short_score
        else:
            trend_side = "mixed"
            trend_score = round(max(long_score, short_score), 2)
        htf_score = _score_frames(frames, "htf")
        entry_score = _score_frames(frames, "entry")
        watch_decision = _trend_watch_decision(
            config,
            htf=htf_score,
            entry=entry_score,
            legacy_side=trend_side,
            legacy_score=trend_score,
        )
        symbols.append(
            {
                "symbol": symbol,
                "trend_side": watch_decision["side"],
                "trend_score": round(_float(watch_decision["score"]), 2),
                "legacy_trend_side": trend_side,
                "legacy_trend_score": trend_score,
                "long_score": long_score,
                "short_score": short_score,
                "htf_trend_side": htf_score["side"],
                "htf_trend_score": htf_score["score"],
                "htf_long_score": htf_score["long_score"],
                "htf_short_score": htf_score["short_score"],
                "entry_readiness_side": entry_score["side"],
                "entry_readiness_score": entry_score["score"],
                "entry_long_score": entry_score["long_score"],
                "entry_short_score": entry_score["short_score"],
                "entry_ready": watch_decision["entry_ready"],
                "ai_ready": watch_decision["ai_ready"],
                "countertrend_review": watch_decision["countertrend_review"],
                "entry_action": watch_decision["entry_action"],
                "watchlist_eligible": watch_decision["watch"],
                "watchlist_reason": watch_decision["reason"],
                "confirmation_mode": watch_decision["confirmation_mode"],
                "required_confirmations": watch_decision["required_confirmations"],
                "trend_thresholds": watch_decision["thresholds"],
                "frame_count": len(frames),
                "latest_at": latest_at or None,
                "frames": frames,
            }
        )
    symbols.sort(key=lambda item: (_float(item.get("trend_score")), str(item.get("latest_at") or "")), reverse=True)
    symbol_by_name = {str(item.get("symbol") or "").upper(): item for item in symbols}
    core_symbols = [
        {
            **symbol_by_name.get(symbol, {"symbol": symbol}),
            "core_symbol": True,
            "core_status": "scanned" if symbol in symbol_by_name else "missing_from_market_scan",
        }
        for symbol in _core_trend_symbols(config)
    ]
    strong_threshold = _float(internal.get("trend_scan_strong_threshold"), 65.0)
    strong = [
        item
        for item in symbols
        if bool(item.get("watchlist_eligible"))
        and _float(item.get("trend_score")) >= strong_threshold
        and item.get("trend_side") in {"long", "short"}
    ]
    return {
        "created_at": now.isoformat(),
        "slot_id": _slot_id(config, now),
        "source": "market_scan_observations",
        "lookback_hours": lookback_hours,
        "observation_count": all_rows,
        "symbol_count": len(symbols),
        "strong_threshold": strong_threshold,
        "strong_count": len(strong),
        "side_counts": dict(Counter(str(item.get("trend_side") or "mixed") for item in symbols)),
        "core_symbols": core_symbols,
        "top_symbols": symbols[:top_limit],
        "strong_symbols": strong[:top_limit],
    }


def _watch_ttl_minutes(config: dict[str, Any], item: dict[str, Any]) -> int:
    internal = config.get("ai", {}).get("internal", {}) if isinstance(config.get("ai"), dict) else {}
    score = _float(item.get("trend_score"))
    frames = {str(frame.get("timeframe") or "").lower() for frame in item.get("frames") or [] if isinstance(frame, dict)}
    if score >= 82:
        default = 120
    elif "4h" in frames and score >= 70:
        default = 240
    else:
        default = 180
    return max(30, min(480, int(internal.get("trend_watchlist_ttl_minutes", default) or default)))


def update_trend_watchlist(
    config: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    raw = get_journal_state(config, TREND_WATCHLIST_STATE_KEY)
    try:
        state = json.loads(raw or "{}")
    except json.JSONDecodeError:
        state = {}
    internal = config.get("ai", {}).get("internal", {}) if isinstance(config.get("ai"), dict) else {}
    min_keep_score = _float(internal.get("trend_watchlist_min_keep_score"), 60.0)
    htf_immediate = _float(internal.get("trend_scan_htf_immediate_threshold"), 75.0)
    items = state.get("items") if isinstance(state.get("items"), dict) else {}
    pending_confirmations = state.get("pending_confirmations") if isinstance(state.get("pending_confirmations"), dict) else {}
    rejected_until = state.get("rejected_until") if isinstance(state.get("rejected_until"), dict) else {}
    next_rejected_until: dict[str, dict[str, Any]] = {}
    for key, block in rejected_until.items():
        if not isinstance(block, dict):
            continue
        until = _parse_time(block.get("until"))
        if until and until > now:
            next_rejected_until[str(key)] = block
    next_pending_confirmations: dict[str, dict[str, Any]] = {}
    next_items: dict[str, dict[str, Any]] = {}
    expired: list[dict[str, Any]] = []
    for key, item in items.items():
        if not isinstance(item, dict):
            continue
        expires_at = _parse_time(item.get("expires_at"))
        if expires_at and expires_at > now and _float(item.get("trend_score")) >= min_keep_score:
            next_items[str(key)] = item
        else:
            expired.append(
                {
                    "key": key,
                    "symbol": item.get("symbol"),
                    "side": item.get("side"),
                    "expired_at": now.isoformat(),
                    "previous_status": item.get("status"),
                    "reason": f"ttl_expired_or_trend_score_below_{min_keep_score:g}",
                }
            )
    for trend in snapshot.get("strong_symbols") or []:
        if not isinstance(trend, dict):
            continue
        symbol = str(trend.get("symbol") or "")
        side = str(trend.get("trend_side") or "")
        if not symbol or side not in {"long", "short"}:
            continue
        key = f"{symbol}|{side}"
        if key in next_rejected_until:
            continue
        existing = next_items.get(key, {})
        required_confirmations = max(1, int(trend.get("required_confirmations") or 1))
        pending = pending_confirmations.get(key) if isinstance(pending_confirmations.get(key), dict) else {}
        previous_count = int(pending.get("confirmation_count") or 0)
        confirmation_count = previous_count + 1
        confirmed = _float(trend.get("htf_trend_score"), _float(trend.get("trend_score"))) >= htf_immediate or confirmation_count >= required_confirmations
        if not confirmed and not existing:
            next_pending_confirmations[key] = {
                "symbol": symbol,
                "side": side,
                "status": "awaiting_confirmation",
                "source": "trend_scan",
                "first_seen_at": pending.get("first_seen_at") or now.isoformat(),
                "updated_at": now.isoformat(),
                "confirmation_count": confirmation_count,
                "required_confirmations": required_confirmations,
                "trend_score": trend.get("trend_score"),
                "htf_trend_score": trend.get("htf_trend_score"),
                "entry_readiness_score": trend.get("entry_readiness_score"),
                "reason": "htf_trend_needs_next_scan_confirmation",
            }
            continue
        ttl = _watch_ttl_minutes(config, trend)
        first_seen_at = existing.get("first_seen_at") or now.isoformat()
        expires_at = existing.get("expires_at") or (now + timedelta(minutes=ttl)).isoformat()
        next_items[key] = {
            **existing,
            "symbol": symbol,
            "side": side,
            "status": "watching",
            "source": "trend_scan",
            "first_seen_at": first_seen_at,
            "updated_at": now.isoformat(),
            "expires_at": expires_at,
            "ttl_minutes": ttl,
            "trend_score": trend.get("trend_score"),
            "long_score": trend.get("long_score"),
            "short_score": trend.get("short_score"),
            "legacy_trend_side": trend.get("legacy_trend_side"),
            "legacy_trend_score": trend.get("legacy_trend_score"),
            "htf_trend_side": trend.get("htf_trend_side"),
            "htf_trend_score": trend.get("htf_trend_score"),
            "htf_long_score": trend.get("htf_long_score"),
            "htf_short_score": trend.get("htf_short_score"),
            "entry_readiness_side": trend.get("entry_readiness_side"),
            "entry_readiness_score": trend.get("entry_readiness_score"),
            "entry_long_score": trend.get("entry_long_score"),
            "entry_short_score": trend.get("entry_short_score"),
            "entry_ready": trend.get("entry_ready"),
            "ai_ready": trend.get("ai_ready"),
            "countertrend_review": trend.get("countertrend_review"),
            "entry_action": trend.get("entry_action"),
            "watchlist_reason": trend.get("watchlist_reason"),
            "confirmation_mode": trend.get("confirmation_mode"),
            "required_confirmations": trend.get("required_confirmations"),
            "confirmation_count": max(confirmation_count, required_confirmations),
            "trend_thresholds": trend.get("trend_thresholds"),
            "frame_count": trend.get("frame_count"),
            "frames": trend.get("frames"),
            "last_reason": trend.get("watchlist_reason") or "trend_score_above_threshold",
        }
    payload = {
        "updated_at": now.isoformat(),
        "count": len(next_items),
        "items": next_items,
        "pending_confirmations": next_pending_confirmations,
        "rejected_until": next_rejected_until,
        "expired": expired[-50:],
    }
    set_journal_state(config, TREND_WATCHLIST_STATE_KEY, json.dumps(to_jsonable(payload), ensure_ascii=False))
    return payload


def _candidate_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
    if not isinstance(payload, dict):
        return {}
    if isinstance(row.get("indicator"), dict) and not isinstance(payload.get("indicator"), dict):
        payload = {**payload, "indicator": row.get("indicator")}
    for key in ("symbol", "side", "score", "confidence", "win_probability_pct", "risk_reward"):
        if key not in payload and key in row:
            payload = {**payload, key: row.get(key)}
    return payload


def _candidate_price(payload: dict[str, Any]) -> float:
    indicator = payload.get("indicator") if isinstance(payload.get("indicator"), dict) else {}
    indicator_summary = payload.get("indicator_summary") if isinstance(payload.get("indicator_summary"), dict) else {}
    return _float(
        payload.get("entry")
        or payload.get("price")
        or payload.get("last")
        or indicator.get("last")
        or indicator_summary.get("last")
    )


def _indicator(payload: dict[str, Any]) -> dict[str, Any]:
    indicator = payload.get("indicator_summary")
    return indicator if isinstance(indicator, dict) else {}


def _higher_timeframes(payload: dict[str, Any]) -> dict[str, Any]:
    frames = payload.get("higher_timeframes")
    return frames if isinstance(frames, dict) else {}


def _current_frame(payload: dict[str, Any]) -> dict[str, Any]:
    indicator = _indicator(payload)
    if indicator:
        return indicator
    frames = _higher_timeframes(payload)
    for key in ("15m", "5m", "1h", "4h"):
        frame = frames.get(key)
        if isinstance(frame, dict):
            return frame
    return {}

def _timeframe_alignment(payload: dict[str, Any], side: str) -> dict[str, Any]:
    frames = _higher_timeframes(payload)
    aligned = 0
    opposite = 0
    mixed = 0
    details: list[dict[str, Any]] = []
    for timeframe in ("4h", "1h", "15m", "5m"):
        frame = frames.get(timeframe)
        if not isinstance(frame, dict):
            continue
        trend = str(frame.get("trend") or "mixed").lower()
        is_aligned = (side == "long" and trend == "up") or (side == "short" and trend == "down")
        is_opposite = (side == "long" and trend == "down") or (side == "short" and trend == "up")
        if is_aligned:
            aligned += 1
        elif is_opposite:
            opposite += 1
        else:
            mixed += 1
        details.append(
            {
                "timeframe": timeframe,
                "trend": trend,
                "aligned": is_aligned,
                "opposite": is_opposite,
                "rsi": frame.get("rsi"),
                "ema_gap_pct": frame.get("ema_gap_pct"),
                "price_vs_ema_slow_pct": frame.get("price_vs_ema_slow_pct"),
            }
        )
    score = 50.0 + aligned * 14.0 - opposite * 18.0 - mixed * 4.0
    return {
        "score": round(_clamp(score), 2),
        "aligned_count": aligned,
        "opposite_count": opposite,
        "mixed_count": mixed,
        "details": details,
    }

def _support_resistance_context(payload: dict[str, Any], frame: dict[str, Any], *, entry: float = 0.0) -> dict[str, Any]:
    support_distance = _float(frame.get("support_distance_pct"), _float(payload.get("support_distance_pct"), 0.0))
    resistance_distance = _float(frame.get("resistance_distance_pct"), _float(payload.get("resistance_distance_pct"), 0.0))
    range_position = _float(frame.get("range_position"), _float(payload.get("range_position"), 0.5))
    support = _float(frame.get("support"), _float(payload.get("support"), 0.0))
    resistance = _float(frame.get("resistance"), _float(payload.get("resistance"), 0.0))
    if entry > 0 and support > 0 and support_distance <= 0:
        support_distance = ((entry - support) / entry) * 100.0
    if entry > 0 and resistance > 0 and resistance_distance <= 0:
        resistance_distance = ((resistance - entry) / entry) * 100.0
    return {
        "support": round(support, 8),
        "resistance": round(resistance, 8),
        "support_distance_pct": round(support_distance, 4),
        "resistance_distance_pct": round(resistance_distance, 4),
        "range_position": round(range_position, 4),
        "near_support": 0 <= support_distance <= 1.5,
        "near_resistance": 0 <= resistance_distance <= 1.5,
    }

def _risk_model_settings(config: dict[str, Any]) -> dict[str, float]:
    internal = config.get("ai", {}).get("internal", {}) if isinstance(config.get("ai"), dict) else {}
    return {
        "min_sl_pct": max(0.2, _float(internal.get("trend_setup_min_sl_pct"), 1.2)),
        "small_coin_min_sl_pct": max(0.5, _float(internal.get("trend_setup_small_coin_min_sl_pct"), 1.8)),
        "max_sl_pct": max(1.0, _float(internal.get("trend_setup_max_sl_pct"), 5.0)),
        "atr_pullback_mult": max(0.5, _float(internal.get("trend_setup_atr_pullback_mult"), 1.35)),
        "atr_breakout_mult": max(0.5, _float(internal.get("trend_setup_atr_breakout_mult"), 1.6)),
        "sr_buffer_atr_mult": max(0.0, _float(internal.get("trend_setup_sr_buffer_atr_mult"), 0.25)),
        "sr_buffer_min_pct": max(0.0, _float(internal.get("trend_setup_sr_buffer_min_pct"), 0.25)),
        "near_target_buffer_pct": max(0.0, _float(internal.get("trend_setup_near_target_buffer_pct"), 0.35)),
        "min_acceptable_rr": max(0.5, _float(internal.get("trend_setup_min_acceptable_rr"), 1.15)),
        "preferred_rr": max(1.0, _float(internal.get("trend_setup_preferred_rr"), 1.5)),
    }

def _is_small_or_noisy_coin(entry: float, atr_pct: float, volume_ratio: float) -> bool:
    return entry < 1.0 or atr_pct >= 1.4 or volume_ratio < 0.9

def _risk_from_market_structure(
    config: dict[str, Any],
    *,
    side: str,
    entry: float,
    entry_type: str,
    atr_pct: float,
    volume_ratio: float,
    sr_context: dict[str, Any],
    rr: float,
) -> dict[str, Any]:
    settings = _risk_model_settings(config)
    if entry <= 0 or side not in {"long", "short"}:
        return {
            "stop_loss": 0.0,
            "take_profit": 0.0,
            "invalid_price": 0.0,
            "risk_pct": 0.0,
            "tp_distance_pct": 0.0,
            "warnings": ["missing_entry_or_side"],
            "risk_model": {"method": "invalid_input"},
        }
    min_sl_pct = settings["small_coin_min_sl_pct"] if _is_small_or_noisy_coin(entry, atr_pct, volume_ratio) else settings["min_sl_pct"]
    atr_mult = settings["atr_breakout_mult"] if entry_type == "breakout" else settings["atr_pullback_mult"]
    atr_risk_pct = max(min_sl_pct, atr_pct * atr_mult)
    buffer_pct = max(settings["sr_buffer_min_pct"], atr_pct * settings["sr_buffer_atr_mult"])
    support = _float(sr_context.get("support"))
    resistance = _float(sr_context.get("resistance"))
    sr_risk_pct = 0.0
    sr_anchor = None
    if side == "long" and 0 < support < entry:
        sr_risk_pct = ((entry - support) / entry) * 100.0 + buffer_pct
        sr_anchor = "support"
    elif side == "short" and resistance > entry:
        sr_risk_pct = ((resistance - entry) / entry) * 100.0 + buffer_pct
        sr_anchor = "resistance"
    def _price_from_pct(pct: float, is_stop: bool) -> float:
        if side == "long":
            return entry * (1.0 - pct / 100.0) if is_stop else entry * (1.0 + pct / 100.0)
        return entry * (1.0 + pct / 100.0) if is_stop else entry * (1.0 - pct / 100.0)

    def _pct_to_target(target: float) -> float:
        if target <= 0:
            return 0.0
        return ((target - entry) / entry) * 100.0 if side == "long" else ((entry - target) / entry) * 100.0

    def _make_plan(method: str, risk_pct_value: float, reward_pct_value: float, *, target_anchor: str | None = None, stop_anchor: str | None = None) -> dict[str, Any]:
        risk_pct_value = min(settings["max_sl_pct"], max(0.0, risk_pct_value))
        reward_pct_value = max(0.0, reward_pct_value)
        actual_rr = reward_pct_value / risk_pct_value if risk_pct_value > 0 else 0.0
        warnings: list[str] = []
        if risk_pct_value <= 0 or reward_pct_value <= 0:
            warnings.append("invalid_sl_tp_plan")
        if risk_pct_value >= settings["max_sl_pct"] - 1e-9:
            warnings.append("sl_capped_by_max_risk")
        if risk_pct_value <= min_sl_pct + 1e-9:
            warnings.append("sl_uses_min_volatility_floor")
        if actual_rr < settings["min_acceptable_rr"]:
            warnings.append("rr_below_minimum")
        if side == "long" and 0 < resistance < _price_from_pct(reward_pct_value, False):
            warnings.append("tp_beyond_resistance")
        if side == "short" and 0 < support and _price_from_pct(reward_pct_value, False) < support:
            warnings.append("tp_beyond_support")
        score = 50.0
        score += min(25.0, actual_rr * 10.0)
        score += 10.0 if stop_anchor in {"support", "resistance", "swing"} else 0.0
        score += 8.0 if target_anchor in {"resistance", "support", "fib_extension"} else 0.0
        score -= 18.0 if "rr_below_minimum" in warnings else 0.0
        score -= 8.0 if method == "rr_minimum" else 0.0
        score -= 5.0 if "sl_capped_by_max_risk" in warnings else 0.0
        return {
            "method": method,
            "stop_loss": _price_from_pct(risk_pct_value, True),
            "take_profit": _price_from_pct(reward_pct_value, False),
            "risk_pct": risk_pct_value,
            "tp_distance_pct": reward_pct_value,
            "actual_rr": actual_rr,
            "score": round(_clamp(score), 2),
            "warnings": warnings,
            "stop_anchor": stop_anchor,
            "target_anchor": target_anchor,
        }

    plans: list[dict[str, Any]] = []
    natural_target_pct = 0.0
    if side == "long" and resistance > entry:
        natural_target_pct = _pct_to_target(resistance)
    elif side == "short" and 0 < support < entry:
        natural_target_pct = _pct_to_target(support)
    if sr_risk_pct > 0 and natural_target_pct > 0:
        plans.append(_make_plan("structure_swing_to_previous_extreme", sr_risk_pct, natural_target_pct, stop_anchor=sr_anchor or "swing", target_anchor="resistance" if side == "long" else "support"))
    plans.append(_make_plan("atr_volatility_rr", max(atr_risk_pct, sr_risk_pct * 0.65), max(atr_risk_pct, sr_risk_pct * 0.65) * rr, stop_anchor=sr_anchor or "atr", target_anchor="rr"))
    plans.append(_make_plan("rr_minimum", max(atr_risk_pct, min_sl_pct), max(atr_risk_pct, min_sl_pct) * settings["preferred_rr"], stop_anchor="atr", target_anchor="rr"))
    if support > 0 and resistance > support:
        range_pct = ((resistance - support) / entry) * 100.0
        fib_target_pct = natural_target_pct + range_pct * 0.272
        if fib_target_pct > 0:
            plans.append(_make_plan("fib_extension_1272", max(atr_risk_pct, sr_risk_pct), fib_target_pct, stop_anchor=sr_anchor or "atr", target_anchor="fib_extension"))
    viable = [plan for plan in plans if "invalid_sl_tp_plan" not in plan["warnings"]]
    structure_plans = [
        plan for plan in viable
        if plan.get("method") == "structure_swing_to_previous_extreme"
        and _float(plan.get("actual_rr")) >= settings["preferred_rr"]
    ]
    preferred = [plan for plan in viable if "rr_below_minimum" not in plan["warnings"]]
    selected = max(structure_plans or preferred or viable, key=lambda item: item["score"], default=_make_plan("invalid", 0.0, 0.0))
    warnings = list(selected.get("warnings") or [])
    return {
        "stop_loss": selected["stop_loss"],
        "take_profit": selected["take_profit"],
        "invalid_price": selected["stop_loss"],
        "risk_pct": selected["risk_pct"],
        "tp_distance_pct": selected["tp_distance_pct"],
        "warnings": warnings,
        "risk_model": {
            "method": "multi_method_sl_tp_selector",
            "selected_method": selected.get("method"),
            "atr_pct": round(atr_pct, 4),
            "atr_risk_pct": round(atr_risk_pct, 4),
            "sr_risk_pct": round(sr_risk_pct, 4),
            "sr_anchor": sr_anchor,
            "natural_target_pct": round(natural_target_pct, 4),
            "buffer_pct": round(buffer_pct, 4),
            "min_sl_pct": round(min_sl_pct, 4),
            "max_sl_pct": round(settings["max_sl_pct"], 4),
            "rr": round(rr, 4),
            "actual_rr": round(selected.get("actual_rr") or 0.0, 4),
            "tp_distance_pct": round(selected.get("tp_distance_pct") or 0.0, 4),
            "uses_small_coin_floor": _is_small_or_noisy_coin(entry, atr_pct, volume_ratio),
            "candidate_plans": [
                {
                    "method": plan.get("method"),
                    "risk_pct": round(_float(plan.get("risk_pct")), 4),
                    "tp_distance_pct": round(_float(plan.get("tp_distance_pct")), 4),
                    "actual_rr": round(_float(plan.get("actual_rr")), 4),
                    "score": plan.get("score"),
                    "warnings": plan.get("warnings"),
                    "stop_anchor": plan.get("stop_anchor"),
                    "target_anchor": plan.get("target_anchor"),
                }
                for plan in plans
            ],
        },
    }

def _fibonacci_context(*, side: str, entry: float, take_profit: float, sr_context: dict[str, Any]) -> dict[str, Any]:
    support = _float(sr_context.get("support"))
    resistance = _float(sr_context.get("resistance"))
    if entry <= 0 or support <= 0 or resistance <= support:
        return {"available": False, "reason": "missing_support_resistance"}
    range_size = resistance - support
    range_position = (entry - support) / range_size
    if side == "long":
        pullback_fib = 1.0 - range_position
        extension_1272 = resistance + range_size * 0.272
        extension_1618 = resistance + range_size * 0.618
        tp_extension = (take_profit - resistance) / range_size if take_profit > resistance else 0.0
    elif side == "short":
        pullback_fib = range_position
        extension_1272 = support - range_size * 0.272
        extension_1618 = support - range_size * 0.618
        tp_extension = (support - take_profit) / range_size if take_profit < support else 0.0
    else:
        return {"available": False, "reason": "invalid_side"}
    in_pullback_zone = 0.382 <= pullback_fib <= 0.618
    return {
        "available": True,
        "range_position": round(range_position, 4),
        "pullback_fib": round(pullback_fib, 4),
        "in_pullback_zone_382_618": in_pullback_zone,
        "extension_1272": round(extension_1272, 8),
        "extension_1618": round(extension_1618, 8),
        "tp_extension_from_range": round(tp_extension, 4),
    }


def _entry_action_from_setup_inputs(
    config: dict[str, Any],
    *,
    side: str,
    entry_type: str,
    overextended_score: float,
    rsi: float,
    price_vs_ema: float,
    sr_context: dict[str, Any],
    volume_ratio: float,
    pullback_quality: float,
    breakout_quality: float,
) -> dict[str, Any]:
    internal = config.get("ai", {}).get("internal", {}) if isinstance(config.get("ai"), dict) else {}
    overextended_threshold = _float(internal.get("trend_setup_overextended_score_threshold"), 58.0)
    hot_rsi_threshold = _float(internal.get("trend_setup_hot_rsi_threshold"), 72.0)
    ema_overextended_pct = _float(internal.get("trend_setup_price_vs_ema_overextended_pct"), 3.0)
    reasons: list[str] = []
    countertrend_side = None
    no_chase = False
    if side == "long":
        if overextended_score >= overextended_threshold:
            reasons.append(f"overextended_score>={overextended_threshold:g}")
        if rsi >= hot_rsi_threshold:
            reasons.append(f"rsi>={hot_rsi_threshold:g}")
        if price_vs_ema >= ema_overextended_pct:
            reasons.append(f"price_above_ema>={ema_overextended_pct:g}%")
        if sr_context.get("near_resistance"):
            reasons.append("near_resistance")
        no_chase = bool(reasons)
        if no_chase and volume_ratio >= 0.9 and (breakout_quality >= 55 or pullback_quality < 58):
            countertrend_side = "short"
            action = "REVIEW_COUNTERTREND_SHORT"
        elif no_chase:
            action = "WAIT_PULLBACK_LONG"
        else:
            action = "READY_LONG_PULLBACK" if entry_type == "pullback" else "READY_LONG_BREAKOUT" if entry_type == "breakout" else "READY_LONG_CONTINUATION"
    elif side == "short":
        if overextended_score >= overextended_threshold:
            reasons.append(f"overextended_score>={overextended_threshold:g}")
        if rsi <= 100.0 - hot_rsi_threshold:
            reasons.append(f"rsi<={100.0 - hot_rsi_threshold:g}")
        if price_vs_ema <= -ema_overextended_pct:
            reasons.append(f"price_below_ema>={ema_overextended_pct:g}%")
        if sr_context.get("near_support"):
            reasons.append("near_support")
        no_chase = bool(reasons)
        if no_chase and volume_ratio >= 0.9 and (breakout_quality >= 55 or pullback_quality < 58):
            countertrend_side = "long"
            action = "REVIEW_COUNTERTREND_LONG"
        elif no_chase:
            action = "WAIT_PULLBACK_SHORT"
        else:
            action = "READY_SHORT_PULLBACK" if entry_type == "pullback" else "READY_SHORT_BREAKOUT" if entry_type == "breakout" else "READY_SHORT_CONTINUATION"
    else:
        action = "NO_TRADE"
        reasons.append("missing_side")
    return {
        "entry_action": action,
        "countertrend_side": countertrend_side,
        "no_chase": no_chase,
        "entry_action_reason": reasons or ["entry_not_overextended"],
        "thresholds": {
            "overextended_score": overextended_threshold,
            "hot_rsi": hot_rsi_threshold,
            "price_vs_ema_pct": ema_overextended_pct,
        },
    }


def build_entry_proposal(config: dict[str, Any], row: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    payload = _candidate_payload(row)
    symbol = str(payload.get("symbol") or row.get("symbol") or "")
    side = str(payload.get("side") or row.get("side") or "").lower()
    entry = _candidate_price(payload)
    frame = _current_frame(payload)
    frames = _higher_timeframes(payload)
    atr_pct = max(0.2, _float(frame.get("atr_pct"), 1.0))
    rsi = _float(frame.get("rsi"), 50.0)
    volume_ratio = _float(frame.get("volume_ratio"), _float(payload.get("volume_ratio"), 1.0))
    price_vs_ema = _float(frame.get("price_vs_ema_slow_pct"))
    alignment = _timeframe_alignment(payload, side)
    sr_context = _support_resistance_context(payload, frame, entry=entry)
    overextended_score = _clamp(abs(price_vs_ema) * 14.0 + max(0.0, rsi - 68.0 if side == "long" else 32.0 - rsi) * 3.0)
    overextended = overextended_score >= 70.0
    pullback_quality = _clamp(
        100.0
        - abs(rsi - 50.0) * 2.0
        - max(0.0, abs(price_vs_ema) - 1.5) * 8.0
        + (8.0 if sr_context["near_support"] and side == "long" else 0.0)
        + (8.0 if sr_context["near_resistance"] and side == "short" else 0.0)
        + (alignment["score"] - 50.0) * 0.25
    )
    breakout_quality = _clamp(
        50.0
        + min(30.0, volume_ratio * 10.0)
        + min(20.0, abs(price_vs_ema) * 4.0)
        + (alignment["score"] - 50.0) * 0.2
        - (20.0 if overextended else 0.0)
    )
    entry_type = "pullback" if pullback_quality >= breakout_quality else "breakout"
    if 55 <= pullback_quality < 72 and not overextended:
        entry_type = "continuation"
    entry_action = _entry_action_from_setup_inputs(
        config,
        side=side,
        entry_type=entry_type,
        overextended_score=overextended_score,
        rsi=rsi,
        price_vs_ema=price_vs_ema,
        sr_context=sr_context,
        volume_ratio=volume_ratio,
        pullback_quality=pullback_quality,
        breakout_quality=breakout_quality,
    )
    rr = 1.75 if entry_type in {"pullback", "continuation"} else 1.5
    risk_model = _risk_from_market_structure(
        config,
        side=side,
        entry=entry,
        entry_type=entry_type,
        atr_pct=atr_pct,
        volume_ratio=volume_ratio,
        sr_context=sr_context,
        rr=rr,
    )
    stop_loss = _float(risk_model.get("stop_loss"))
    take_profit = _float(risk_model.get("take_profit"))
    invalid_price = _float(risk_model.get("invalid_price"))
    risk_pct = _float(risk_model.get("risk_pct"))
    fibonacci = _fibonacci_context(side=side, entry=entry, take_profit=take_profit, sr_context=sr_context)
    warnings: list[str] = []
    warnings.extend(str(item) for item in risk_model.get("warnings") or [])
    if overextended:
        warnings.append("overextended_risk")
    if entry_action["no_chase"]:
        warnings.append("no_chase_entry")
    if volume_ratio < 0.7:
        warnings.append("weak_volume")
    if alignment["opposite_count"] >= 2:
        warnings.append("lower_timeframe_misalignment")
    if fibonacci.get("available") and entry_type in {"pullback", "continuation"} and not fibonacci.get("in_pullback_zone_382_618"):
        warnings.append("fib_pullback_zone_mismatch")
    if _float(payload.get("risk_reward"), rr) < 1.3:
        warnings.append("risk_reward_too_low")
    if entry <= 0 or side not in {"long", "short"}:
        warnings.append("missing_entry_or_side")
    hard_warnings = {"missing_entry_or_side", "risk_reward_too_low"}
    setup_state = "blocked" if any(item in hard_warnings for item in warnings) else "review_only" if entry_action["no_chase"] else "ready_for_ai_review"
    return {
        "created_at": now.isoformat(),
        "symbol": symbol,
        "side": side,
        "strategy": "TrendFollowing",
        "entry_type": entry_type,
        "entry_price": round(entry, 8),
        "stop_loss": round(stop_loss, 8),
        "take_profit": round(take_profit, 8),
        "risk_reward": round(rr, 4),
        "risk_pct": round(risk_pct, 4),
        "tp_distance_pct": round(_float(risk_model.get("tp_distance_pct")), 4),
        "invalid_price": round(invalid_price, 8),
        "risk_model": risk_model.get("risk_model"),
        "fibonacci_context": fibonacci,
        "overextended": overextended,
        "overextended_score": round(overextended_score, 2),
        "entry_action": entry_action["entry_action"],
        "countertrend_side": entry_action["countertrend_side"],
        "no_chase": entry_action["no_chase"],
        "entry_action_reason": entry_action["entry_action_reason"],
        "entry_action_thresholds": entry_action["thresholds"],
        "pullback_quality": round(pullback_quality, 2),
        "breakout_quality": round(breakout_quality, 2),
        "volume_confirmation": volume_ratio >= 1.0,
        "timeframe_alignment": alignment,
        "support_resistance": sr_context,
        "rsi": round(rsi, 2),
        "price_vs_ema_slow_pct": round(price_vs_ema, 4),
        "warnings": warnings,
        "setup_state": setup_state,
        "timeframes": {
            key: {
                "trend": value.get("trend"),
                "rsi": value.get("rsi"),
                "ema_gap_pct": value.get("ema_gap_pct"),
                "price_vs_ema_slow_pct": value.get("price_vs_ema_slow_pct"),
                "volume_ratio": value.get("volume_ratio"),
            }
            for key, value in frames.items()
            if isinstance(value, dict) and key in {"5m", "15m", "1h", "4h"}
        },
    }


def _review_prompt_package(setup: dict[str, Any], source_payload: dict[str, Any]) -> dict[str, Any]:
    system = (
        "You are Mini Setup Review for a crypto trading bot. "
        "The code has already calculated side, entry, stop loss, take profit and RR. "
        "Do not invent new entry/SL/TP. Review the proposed setup only. "
        "Return strict JSON with decision APPROVE, REJECT, or REVIEW."
    )
    user = {
        "task": "review_code_based_trend_setup",
        "allowed_decisions": ["APPROVE", "REJECT", "REVIEW"],
        "rules": [
            "APPROVE only if trend is still valid, entry is not overextended, RR is acceptable, and evidence supports continuation.",
            "REVIEW if trend is valid but entry should wait for pullback or confirmation.",
            "REJECT if trend is broken, setup is overextended, volume is too weak, or RR is poor.",
            "For REJECT, classify whether only the current setup is bad or the watchlist item should be removed.",
            "Use reject_scope=SETUP_ONLY when the trend can remain on watchlist but this entry is bad.",
            "Use reject_scope=WATCHLIST_REMOVE when trend is invalid, RR cannot be fixed, liquidity/risk is unacceptable, or market context conflicts.",
            "Do not change entry_price, stop_loss, take_profit, or risk_reward.",
        ],
        "expected_json": {
            "decision": "APPROVE|REJECT|REVIEW",
            "setup_grade": "S|A|B|C|D",
            "entry_quality": "number 0-100",
            "continuation_score": "number 0-100",
            "pending_order_allowed": "boolean",
            "reject_scope": "SETUP_ONLY|WATCHLIST_REMOVE",
            "reject_reason_type": "BAD_ENTRY|TREND_INVALID|RR_BAD|LIQUIDITY_RISK|MARKET_CONFLICT|SYSTEM_RISK|OTHER",
            "allow_recheck_if_setup_changes": "boolean",
            "reason": "short reason",
            "evidence": ["short evidence strings"],
            "warnings": ["short warning strings"],
        },
        "setup_proposal": setup,
        "source_candidate": source_payload,
    }
    return {
        "prompt_version": "trend-setup-review-v1",
        "prompt_hash": "trend-setup-review-v1",
        "estimated_static_tokens": 900,
        "estimated_dynamic_tokens": max(1, len(json.dumps(user, ensure_ascii=False)) // 4),
        "estimated_cache_hit": 0.0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ],
    }


def review_setup_with_mini(
    config: dict[str, Any],
    setup: dict[str, Any],
    source_payload: dict[str, Any],
    *,
    notify_telegram: bool = False,
) -> dict[str, Any]:
    from .codex_features import call_openai_json

    internal_config = config.get("ai", {}).get("internal", {}) if isinstance(config.get("ai"), dict) else {}
    if not bool(internal_config.get("trend_setup_review_ai_enabled", False)):
        raise RuntimeError("Trend setup AI review is disabled: ai.internal.trend_setup_review_ai_enabled=false")
    symbol = str(setup.get("symbol") or "unknown")
    cooldown_seconds = max(60, int(internal_config.get("trend_setup_review_ai_cooldown_seconds", 900) or 900))
    last_key = f"{TREND_SETUP_REVIEW_LAST_CALL_PREFIX}:{_safe_token(symbol)}"
    last_raw = get_journal_state(config, last_key)
    last_at = _parse_time(last_raw)
    now = datetime.now(timezone.utc)
    if last_at is not None and (now - last_at).total_seconds() < cooldown_seconds:
        remaining = int(cooldown_seconds - (now - last_at).total_seconds())
        raise RuntimeError(f"Trend setup AI review cooldown active for {symbol}: wait {remaining}s")
    set_journal_state(config, last_key, now.isoformat())
    response = call_openai_json(
        config,
        internal_config,
        _review_prompt_package(setup, source_payload),
        model_name=str(internal_config.get("model", "gpt-5.4-mini")),
        purpose="mini_market_scan",
        route="trend_setup_review",
        record_history=True,
        notify_telegram=False,
    )
    parsed = dict(response.get("parsed") or {})
    parsed.update(
        {
            "model_version": internal_config.get("model", "gpt-5.4-mini"),
            "prompt_version": "trend-setup-review-v1",
            "latency_ms": response.get("latency_ms"),
        }
    )
    normalized = normalize_ai_setup_review(parsed, setup)
    if notify_telegram:
        try:
            from .codex_features import record_ai_call_event

            record_ai_call_event(
                config,
                {
                    "role": "mini",
                    "model": normalized.get("model_version") or internal_config.get("model", "gpt-5.4-mini"),
                    "symbols": [symbol],
                    "status": normalized.get("decision"),
                    "approved": normalized.get("decision") == "APPROVE",
                    "decision": normalized.get("decision"),
                    "reason": normalized.get("reason"),
                    "sl_tp_method": ((setup.get("risk_model") or {}).get("selected_method") if isinstance(setup.get("risk_model"), dict) else None),
                    "prompt_version": "trend-setup-review-v1",
                    "prompt_hash": "trend-setup-review-v1",
                    "latency_ms": response.get("latency_ms"),
                },
                notify_telegram=True,
            )
        except Exception:
            LOGGER.warning("Skipping normalized trend setup review telegram notification", exc_info=True)
    return normalized

def normalize_ai_setup_review(review: dict[str, Any], setup: dict[str, Any]) -> dict[str, Any]:
    decision = str(review.get("decision") or "").upper().strip()
    if decision not in {"APPROVE", "REJECT", "REVIEW"}:
        decision = "REVIEW" if setup.get("setup_state") == "ready_for_ai_review" else "REJECT"
    grade = str(review.get("setup_grade") or "").upper().strip()
    if grade not in {"S", "A", "B", "C", "D"}:
        entry_quality = _float(review.get("entry_quality"), _float(setup.get("pullback_quality"), 0.0))
        grade = "A" if entry_quality >= 84 else "B" if entry_quality >= 74 else "C" if entry_quality >= 62 else "D"
    warnings = review.get("warnings") if isinstance(review.get("warnings"), list) else []
    evidence = review.get("evidence") if isinstance(review.get("evidence"), list) else []
    hard_setup_block = setup.get("setup_state") == "blocked" or bool(set(setup.get("warnings") or []) & {"missing_entry_or_side", "risk_reward_too_low"})
    if hard_setup_block and decision == "APPROVE":
        decision = "REJECT"
        warnings = [*warnings, "code_hard_block_overrode_ai_approve"]
    if setup.get("overextended") and decision == "APPROVE":
        decision = "REVIEW"
        warnings = [*warnings, "overextended_requires_review"]
    if setup.get("no_chase") and decision == "APPROVE":
        decision = "REVIEW"
        warnings = [*warnings, "no_chase_requires_review"]
    pending_allowed = bool(review.get("pending_order_allowed"))
    if decision == "APPROVE":
        pending_allowed = False
    if decision == "REJECT":
        pending_allowed = False
    reject_scope = str(review.get("reject_scope") or "").upper().strip()
    reject_reason_type = str(review.get("reject_reason_type") or "").upper().strip()
    if decision == "REJECT":
        if reject_scope not in {"SETUP_ONLY", "WATCHLIST_REMOVE"}:
            reject_scope = "SETUP_ONLY"
        if reject_reason_type not in {"BAD_ENTRY", "TREND_INVALID", "RR_BAD", "LIQUIDITY_RISK", "MARKET_CONFLICT", "SYSTEM_RISK", "OTHER"}:
            reject_reason_type = "OTHER"
    else:
        reject_scope = ""
        reject_reason_type = ""
    allow_recheck = bool(review.get("allow_recheck_if_setup_changes"))
    if decision == "REJECT" and "TREND_INVALID" == reject_reason_type:
        allow_recheck = False
    return {
        **review,
        "decision": decision,
        "setup_grade": grade,
        "entry_quality": round(_clamp(_float(review.get("entry_quality"), _float(setup.get("pullback_quality"), 0.0))), 2),
        "continuation_score": round(_clamp(_float(review.get("continuation_score"), _float(setup.get("breakout_quality"), 0.0))), 2),
        "pending_order_allowed": pending_allowed,
        "reject_scope": reject_scope,
        "reject_reason_type": reject_reason_type,
        "allow_recheck_if_setup_changes": allow_recheck,
        "reason": str(review.get("reason") or "AI review normalized without explicit reason."),
        "evidence": [str(item) for item in evidence[:8]],
        "warnings": [str(item) for item in warnings[:8]],
        "normalized": True,
    }


def activate_trend_trade_intent(setup: dict[str, Any], ai_review: dict[str, Any]) -> dict[str, Any]:
    decision = str(ai_review.get("decision") or "").upper()
    status = "approved_for_risk" if decision == "APPROVE" else "pending_review" if decision == "REVIEW" else "rejected"
    pending_allowed = decision == "REVIEW" and bool(ai_review.get("pending_order_allowed"))
    ttl_minutes = 60
    if pending_allowed and setup.get("entry_type") == "breakout":
        ttl_minutes = 30
    elif pending_allowed:
        ttl_minutes = 90
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat() if pending_allowed else None
    cancel_checks = evaluate_pending_cancel_rules(setup, ai_review)
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "enabled_for_execution": False,
        "status": "canceled" if cancel_checks["cancel"] else status,
        "trade_intent": {
            "symbol": setup.get("symbol"),
            "side": setup.get("side"),
            "strategy": setup.get("strategy") or "TrendFollowing",
            "entry_type": setup.get("entry_type"),
            "entry_price": setup.get("entry_price"),
            "stop_loss": setup.get("stop_loss"),
            "take_profit": setup.get("take_profit"),
            "risk_reward": setup.get("risk_reward"),
            "setup_grade": ai_review.get("setup_grade"),
            "source": "trend_watchlist",
            "status": "canceled" if cancel_checks["cancel"] else status,
        },
        "pending_plan": {
            "allowed": pending_allowed and not cancel_checks["cancel"],
            "ttl_minutes": ttl_minutes if pending_allowed else 0,
            "expires_at": expires_at,
            "cancel_now": cancel_checks["cancel"],
            "cancel_reasons": cancel_checks["reasons"],
            "cancel_if": [
                "trend_score < 60",
                "risk_reward_below_threshold",
                "price_moves_too_far_from_entry",
                "spread_worsens",
                "slot_full",
                "daily_trade_limit_reached",
            ],
        },
    }

def evaluate_pending_cancel_rules(setup: dict[str, Any], ai_review: dict[str, Any], *, live_context: dict[str, Any] | None = None) -> dict[str, Any]:
    live_context = live_context if isinstance(live_context, dict) else {}
    reasons: list[str] = []
    if setup.get("setup_state") == "blocked":
        reasons.append("setup_blocked")
    if _float(setup.get("risk_reward")) < 1.3:
        reasons.append("risk_reward_below_threshold")
    if bool(setup.get("overextended")) and str(ai_review.get("decision") or "").upper() != "REVIEW":
        reasons.append("overextended_without_review")
    trend_score = _float(live_context.get("trend_score"), 100.0)
    if trend_score < 60:
        reasons.append("trend_score_below_60")
    spread_pct = _float(live_context.get("spread_pct"), 0.0)
    if spread_pct >= 0.25:
        reasons.append("spread_worsens")
    return {"cancel": bool(reasons), "reasons": reasons}

def _setup_to_candidate(config: dict[str, Any], setup: dict[str, Any], ai_review: dict[str, Any]) -> Any | None:
    leverage = max(1.0, _float(config.get("exchange", {}).get("leverage"), 1.0))
    margin_usdt = _float(config.get("position_sizing", {}).get("base_margin_usdt"), 0.0)
    if margin_usdt <= 0:
        margin_usdt = _float(config.get("risk", {}).get("order_usdt"), 0.0) / leverage
    order_usdt = max(0.0, margin_usdt * leverage)
    entry_quality = _float(ai_review.get("entry_quality"), _float(setup.get("pullback_quality"), 0.0))
    continuation = _float(ai_review.get("continuation_score"), _float(setup.get("breakout_quality"), 0.0))
    confidence = round(_clamp((entry_quality * 0.55) + (continuation * 0.45)), 2)
    candidate = _mapping_to_candidate(
        {
            "symbol": setup.get("symbol"),
            "side": setup.get("side"),
            "payload": {
                "symbol": setup.get("symbol"),
                "side": setup.get("side"),
                "entry": setup.get("entry_price"),
                "stop_loss": setup.get("stop_loss"),
                "take_profit": setup.get("take_profit"),
                "risk_reward": setup.get("risk_reward"),
                "order_usdt": order_usdt,
                "margin_usdt": margin_usdt,
                "confidence": confidence,
                "win_probability_pct": max(entry_quality, continuation),
                "indicator_summary": {
                    "rsi": setup.get("rsi"),
                    "volume_ratio": 1.0 if setup.get("volume_confirmation") else 0.0,
                    "atr_pct": setup.get("risk_pct"),
                    "price_vs_ema_slow_pct": setup.get("price_vs_ema_slow_pct"),
                },
                "higher_timeframes": setup.get("timeframes") or {},
                "warnings": setup.get("warnings") or [],
                "reasons": [ai_review.get("reason") or "trend setup review"],
            },
        }
    )
    if candidate is not None:
        candidate.margin_usdt = margin_usdt
    return candidate

def evaluate_trade_intent_risk_capital_shadow(config: dict[str, Any], setup: dict[str, Any], ai_review: dict[str, Any], activation: dict[str, Any]) -> dict[str, Any]:
    candidate = _setup_to_candidate(config, setup, ai_review)
    if candidate is None:
        return {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "shadow_mode": True,
            "phase": "phase_5_risk_capital_reads_trade_intent",
            "risk": {"approved": False, "reasons": ["candidate_unavailable_from_trade_intent"], "warnings": []},
            "capital": {"approved": False, "reason": "candidate_unavailable"},
        }
    try:
        from .risk import evaluate_candidate

        check = evaluate_candidate(config, candidate, enforce_active_limit=True, check_active_trades=True, check_order_limits=True)
        risk_payload = {"approved": bool(check.passed), "reasons": list(check.reasons or []), "warnings": list(check.warnings or [])}
    except Exception as exc:
        risk_payload = {"approved": False, "reasons": [f"risk_shadow_error: {exc}"], "warnings": []}
    leverage = max(1.0, _float(config.get("exchange", {}).get("leverage"), 1.0))
    margin_usdt = _float(getattr(candidate, "margin_usdt", None), 0.0)
    if margin_usdt <= 0:
        margin_usdt = _float(candidate.order_usdt) / leverage
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "shadow_mode": True,
        "phase": "phase_5_risk_capital_reads_trade_intent",
        "trade_intent_status": (activation.get("trade_intent") or {}).get("status"),
        "risk": risk_payload,
        "capital": {
            "approved": bool(risk_payload.get("approved")),
            "margin_usdt": round(margin_usdt, 4),
            "leverage": leverage,
            "notional_usdt": round(_float(candidate.order_usdt), 4),
            "risk_profile": (activation.get("trade_intent") or {}).get("risk_profile") or "Normal",
            "source": "trade_intent_shadow",
        },
    }

def build_position_review_shadow(setup: dict[str, Any], ai_review: dict[str, Any], activation: dict[str, Any]) -> dict[str, Any]:
    decision = str(ai_review.get("decision") or "").upper()
    setup_state = str(setup.get("setup_state") or "")
    entry_quality = _float(ai_review.get("entry_quality"), _float(setup.get("pullback_quality"), 0.0))
    continuation = _float(ai_review.get("continuation_score"), _float(setup.get("breakout_quality"), 0.0))
    warnings = set(str(item) for item in (setup.get("warnings") or []))
    if decision == "REJECT" or setup_state == "blocked":
        review_decision = "NO_POSITION"
        good_exit_rule = "do_not_enter"
    elif continuation < 45 or "overextended_risk" in warnings:
        review_decision = "GOOD_EXIT_IF_ALREADY_OPEN"
        good_exit_rule = "exit_if_continuation_breaks_or_price_rejects"
    elif entry_quality >= 70 and continuation >= 60:
        review_decision = "HOLD_WITH_TRAILING_PLAN"
        good_exit_rule = "trail_after_partial_profit"
    else:
        review_decision = "WAIT_FOR_ENTRY_CONFIRMATION"
        good_exit_rule = "cancel_if_entry_readiness_stays_weak"
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "shadow_mode": True,
        "phase": "phase_6_position_review_good_exit",
        "review_interval_minutes": 15 if setup.get("strategy") == "TrendFollowing" else 10,
        "decision": review_decision,
        "good_exit_rule": good_exit_rule,
        "entry_quality": round(entry_quality, 2),
        "continuation_score": round(continuation, 2),
        "activation_status": activation.get("status"),
    }

def build_trade_memory_plan_shadow(setup: dict[str, Any], activation: dict[str, Any]) -> dict[str, Any]:
    intent = activation.get("trade_intent") if isinstance(activation.get("trade_intent"), dict) else {}
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "shadow_mode": True,
        "phase": "phase_8_trade_memory_backtest_loop",
        "record_when": "after_position_fully_closed",
        "dedupe_key_fields": ["symbol", "side", "entry_price", "opened_at", "closed_at"],
        "fields": {
            "symbol": intent.get("symbol") or setup.get("symbol"),
            "side": intent.get("side") or setup.get("side"),
            "strategy": intent.get("strategy") or setup.get("strategy"),
            "entry_type": intent.get("entry_type") or setup.get("entry_type"),
            "setup_grade": intent.get("setup_grade"),
            "risk_reward": intent.get("risk_reward") or setup.get("risk_reward"),
            "exit_type": "TP|SL|GOOD_EXIT|MANUAL|EXPIRED",
            "result_r": "computed_after_close",
            "net_pnl_usdt": "okx_realized_pnl_after_fees_funding",
        },
    }

def build_pool_reduction_plan_shadow() -> dict[str, Any]:
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "shadow_mode": True,
        "phase": "phase_7_pool_filter_reduction_plan",
        "current_action": "observe_only",
        "safe_to_reduce_after": "7-14_days_of_shadow_logs",
        "rules": [
            "Keep old 1h/2h/4h pool as Market Context Cache.",
            "Do not remove Mini/5.5 gate until Risk/Capital and Good Exit are stable.",
            "Compare pool decisions against Trade Intent before reducing filters.",
        ],
    }

def _latest_market_scan_row_for_symbol_side(config: dict[str, Any], symbol: str, side: str) -> dict[str, Any] | None:
    memory = recent_market_scan_memory(
        config,
        lookback_hours=max(1, int(config.get("ai", {}).get("internal", {}).get("trend_scan_lookback_hours", 24) or 24)),
        per_symbol_timeframe_limit=5,
        total_limit=1000,
        include_details=True,
    )
    frame_map = memory.get(symbol) if isinstance(memory, dict) else None
    if not isinstance(frame_map, dict):
        return None
    rows: list[dict[str, Any]] = []
    for timeframe_rows in frame_map.values():
        if not isinstance(timeframe_rows, list):
            continue
        for row in timeframe_rows:
            if not isinstance(row, dict):
                continue
            row_side = str(row.get("side") or (row.get("payload") or {}).get("side") or "").lower()
            if row_side and row_side != side:
                continue
            rows.append(row)
    rows.sort(key=lambda item: (str(item.get("created_at") or ""), 1 if str(item.get("timeframe") or "").lower() == "1m" else 0), reverse=True)
    return rows[0] if rows else None

def _trend_setup_signature(setup: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": setup.get("symbol"),
        "side": setup.get("side"),
        "entry_action": setup.get("entry_action"),
        "setup_state": setup.get("setup_state"),
        "entry_type": setup.get("entry_type"),
        "entry_price": setup.get("entry_price"),
        "stop_loss": setup.get("stop_loss"),
        "take_profit": setup.get("take_profit"),
        "risk_reward": setup.get("risk_reward"),
        "trend_score": item.get("trend_score"),
        "entry_readiness_score": item.get("entry_readiness_score"),
        "warnings": sorted(str(value) for value in (setup.get("warnings") or [])),
    }

def _pct_delta(current: Any, previous: Any) -> float:
    current_value = _float(current)
    previous_value = _float(previous)
    if previous_value == 0:
        return 100.0 if current_value != 0 else 0.0
    return abs((current_value - previous_value) / previous_value) * 100.0

def _trend_setup_changed_enough(config: dict[str, Any], setup: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    internal = config.get("ai", {}).get("internal", {}) if isinstance(config.get("ai"), dict) else {}
    price_delta_threshold = _float(internal.get("trend_setup_review_price_change_pct", 0.35), 0.35)
    score_delta_threshold = _float(internal.get("trend_setup_review_score_change", 8.0), 8.0)
    previous = item.get("last_ai_setup_signature") if isinstance(item.get("last_ai_setup_signature"), dict) else {}
    current = _trend_setup_signature(setup, item)
    if not previous:
        return {"changed": True, "reason": "first_ai_review_for_watch_item", "signature": current}
    reasons: list[str] = []
    for key in ("entry_action", "setup_state", "entry_type"):
        if str(current.get(key)) != str(previous.get(key)):
            reasons.append(f"{key}_changed")
    if _pct_delta(current.get("entry_price"), previous.get("entry_price")) >= price_delta_threshold:
        reasons.append("entry_price_changed")
    if _pct_delta(current.get("stop_loss"), previous.get("stop_loss")) >= price_delta_threshold:
        reasons.append("stop_loss_changed")
    if _pct_delta(current.get("take_profit"), previous.get("take_profit")) >= price_delta_threshold:
        reasons.append("take_profit_changed")
    if abs(_float(current.get("trend_score")) - _float(previous.get("trend_score"))) >= score_delta_threshold:
        reasons.append("trend_score_changed")
    if abs(_float(current.get("entry_readiness_score")) - _float(previous.get("entry_readiness_score"))) >= score_delta_threshold:
        reasons.append("entry_readiness_changed")
    if current.get("warnings") != previous.get("warnings"):
        reasons.append("warnings_changed")
    return {
        "changed": bool(reasons),
        "reason": ",".join(reasons) if reasons else "setup_unchanged",
        "signature": current,
    }

def _save_watchlist_ai_review_state(
    config: dict[str, Any],
    symbol: str,
    side: str,
    *,
    signature: dict[str, Any],
    ai_review: dict[str, Any],
    setup: dict[str, Any],
) -> None:
    raw = get_journal_state(config, TREND_WATCHLIST_STATE_KEY)
    try:
        state = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return
    items = state.get("items") if isinstance(state.get("items"), dict) else {}
    key = f"{symbol}|{side}"
    item = items.get(key) if isinstance(items.get(key), dict) else None
    if item is None:
        return
    previous_reject_count = int(item.get("reject_count") or 0)
    decision = str(ai_review.get("decision") or "").upper()
    reject_scope = str(ai_review.get("reject_scope") or "").upper()
    reject_reason_type = str(ai_review.get("reject_reason_type") or "").upper()
    remove_reasons = {"TREND_INVALID", "RR_BAD", "LIQUIDITY_RISK", "MARKET_CONFLICT", "SYSTEM_RISK"}
    should_remove = decision == "REJECT" and (reject_scope == "WATCHLIST_REMOVE" or reject_reason_type in remove_reasons or previous_reject_count >= 1)
    if should_remove:
        internal = config.get("ai", {}).get("internal", {}) if isinstance(config.get("ai"), dict) else {}
        reject_cooldown_minutes = max(15, int(internal.get("trend_watchlist_reject_cooldown_minutes", 120) or 120))
        expired = state.get("expired") if isinstance(state.get("expired"), list) else []
        rejected_until = state.get("rejected_until") if isinstance(state.get("rejected_until"), dict) else {}
        expired.append(
            {
                "key": key,
                "symbol": symbol,
                "side": side,
                "expired_at": datetime.now(timezone.utc).isoformat(),
                "previous_status": item.get("status"),
                "reason": "ai_reject_watchlist_remove" if reject_scope == "WATCHLIST_REMOVE" else "ai_reject_repeated_or_structural",
                "reject_scope": reject_scope,
                "reject_reason_type": reject_reason_type,
                "reject_count": previous_reject_count + 1,
            }
        )
        rejected_until[key] = {
            "symbol": symbol,
            "side": side,
            "until": (datetime.now(timezone.utc) + timedelta(minutes=reject_cooldown_minutes)).isoformat(),
            "reason": "ai_reject_watchlist_remove",
            "reject_scope": reject_scope,
            "reject_reason_type": reject_reason_type,
        }
        items.pop(key, None)
        state["items"] = items
        state["expired"] = expired[-50:]
        state["rejected_until"] = rejected_until
        set_journal_state(config, TREND_WATCHLIST_STATE_KEY, json.dumps(to_jsonable(state), ensure_ascii=False))
        return
    item["last_ai_review_at"] = datetime.now(timezone.utc).isoformat()
    item["last_ai_decision"] = ai_review.get("decision")
    item["last_ai_setup_signature"] = signature
    item["last_ai_entry_action"] = setup.get("entry_action")
    item["last_ai_setup_state"] = setup.get("setup_state")
    item["last_reject_scope"] = reject_scope
    item["last_reject_reason_type"] = reject_reason_type
    item["allow_recheck_if_setup_changes"] = bool(ai_review.get("allow_recheck_if_setup_changes"))
    if decision == "REJECT":
        item["status"] = "rejected_wait_new_setup"
        item["reject_count"] = previous_reject_count + 1
    elif decision in {"APPROVE", "REVIEW"}:
        item["reject_count"] = 0
    items[key] = item
    state["items"] = items
    set_journal_state(config, TREND_WATCHLIST_STATE_KEY, json.dumps(to_jsonable(state), ensure_ascii=False))

def run_trend_auto_shadow_reviews(config: dict[str, Any], watchlist: dict[str, Any]) -> dict[str, Any]:
    internal = config.get("ai", {}).get("internal", {}) if isinstance(config.get("ai"), dict) else {}
    if not bool(internal.get("trend_auto_shadow_review_enabled", True)):
        return {"enabled": False, "created": 0, "items": []}
    limit = max(1, int(internal.get("trend_auto_shadow_review_limit", 5) or 5))
    call_ai = bool(internal.get("trend_setup_review_ai_enabled", False))
    items = watchlist.get("items") if isinstance(watchlist.get("items"), dict) else {}
    reviewed: list[dict[str, Any]] = []
    for item in sorted(items.values(), key=lambda row: _float(row.get("trend_score")), reverse=True):
        if len(reviewed) >= limit:
            break
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "")
        side = str(item.get("side") or "").lower()
        if not symbol or side not in {"long", "short"}:
            continue
        row = _latest_market_scan_row_for_symbol_side(config, symbol, side)
        if row is None:
            reviewed.append({"symbol": symbol, "side": side, "status": "skipped", "reason": "source_row_unavailable"})
            continue
        try:
            setup_preview = build_entry_proposal(config, row)
            setup_change = _trend_setup_changed_enough(config, setup_preview, item)
        except Exception as exc:
            reviewed.append({"symbol": symbol, "side": side, "status": "error", "reason": f"setup_preview_error: {exc}"})
            continue
        if call_ai and not bool(setup_change.get("changed")):
            reviewed.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "status": "skipped",
                    "reason": setup_change.get("reason") or "setup_unchanged",
                    "ai_called": False,
                    "entry_price": setup_preview.get("entry_price"),
                    "setup_state": setup_preview.get("setup_state"),
                    "entry_action": setup_preview.get("entry_action"),
                }
            )
            continue
        try:
            result = build_trend_setup_review_flow(config, row, call_ai=call_ai, notify_telegram=False)
            setup = result.get("setup_proposal") if isinstance(result.get("setup_proposal"), dict) else {}
            ai_review = result.get("ai_review") if isinstance(result.get("ai_review"), dict) else {}
            if call_ai:
                _save_watchlist_ai_review_state(
                    config,
                    symbol,
                    side,
                    signature=setup_change.get("signature") if isinstance(setup_change.get("signature"), dict) else _trend_setup_signature(setup, item),
                    ai_review=ai_review,
                    setup=setup,
                )
            reviewed.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "status": "logged",
                    "ai_called": call_ai,
                    "ai_decision": ai_review.get("decision"),
                    "review_reason": setup_change.get("reason"),
                    "entry_price": setup.get("entry_price"),
                    "entry_action": setup.get("entry_action"),
                    "setup_state": setup.get("setup_state"),
                    "risk_approved": ((result.get("risk_capital_shadow") or {}).get("risk") or {}).get("approved"),
                    "position_review": (result.get("position_review_shadow") or {}).get("decision"),
                }
            )
        except Exception as exc:
            reviewed.append({"symbol": symbol, "side": side, "status": "error", "reason": str(exc)})
    return {"enabled": True, "created": len([item for item in reviewed if item.get("status") == "logged"]), "items": reviewed}

def build_trend_setup_review_flow(
    config: dict[str, Any],
    row: dict[str, Any],
    *,
    call_ai: bool = False,
    notify_telegram: bool = False,
) -> dict[str, Any]:
    payload = _candidate_payload(row)
    setup = build_entry_proposal(config, row)
    shadow_intent = None
    try:
        candidate = _mapping_to_candidate(row)
        shadow_intent = build_shadow_trade_intent(candidate) if candidate is not None else None
    except Exception:
        shadow_intent = None
    if call_ai:
        ai_review = review_setup_with_mini(config, setup, payload, notify_telegram=notify_telegram)
    else:
        ai_review = normalize_ai_setup_review({
            "decision": "REVIEW" if setup.get("warnings") else "APPROVE",
            "setup_grade": "B",
            "entry_quality": setup.get("pullback_quality"),
            "continuation_score": setup.get("breakout_quality"),
            "pending_order_allowed": bool(setup.get("warnings")),
            "reason": "local dry review; AI not called",
            "evidence": [],
            "warnings": setup.get("warnings") or [],
        }, setup)
    activation = activate_trend_trade_intent(setup, ai_review)
    risk_capital = evaluate_trade_intent_risk_capital_shadow(config, setup, ai_review, activation)
    position_review = build_position_review_shadow(setup, ai_review, activation)
    trade_memory_plan = build_trade_memory_plan_shadow(setup, activation)
    pool_reduction_plan = build_pool_reduction_plan_shadow()
    pending_state = upsert_trend_pending_plan(config, activation)
    result = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "phase_completion": {
            "phase_5_trend_watchlist": 100,
            "phase_6_entry_builder": 100,
            "phase_7_ai_setup_review": 100 if call_ai else 50,
            "phase_8_intent_activation": 100,
            "remaining_phase_5_risk_capital_reads_intent": 100,
            "remaining_phase_6_position_review_good_exit": 100,
            "remaining_phase_7_pool_reduction_plan": 100,
            "remaining_phase_8_trade_memory_plan": 100,
        },
        "setup_proposal": setup,
        "ai_review": ai_review,
        "activation": activation,
        "risk_capital_shadow": risk_capital,
        "position_review_shadow": position_review,
        "pool_reduction_plan_shadow": pool_reduction_plan,
        "trade_memory_plan_shadow": trade_memory_plan,
        "pending_state": pending_state,
        "shadow_intent_context": shadow_intent,
    }
    key = f"{TREND_SETUP_REVIEW_LOG_PREFIX}:{_safe_token(setup.get('symbol'))}:{_safe_token(result['created_at'])}"
    body = json.dumps(to_jsonable(result), ensure_ascii=False, separators=(",", ":"))
    try:
        set_journal_state(config, key, body)
    except Exception as exc:
        LOGGER.warning("Skipping trend setup review journal log after storage error: %s", exc)
    try:
        _write_jsonl(config, "logs/trend_setup_review.jsonl", result)
    except Exception as exc:
        LOGGER.warning("Skipping trend setup review file log after filesystem error: %s", exc)
    return result

def upsert_trend_pending_plan(config: dict[str, Any], activation: dict[str, Any]) -> dict[str, Any]:
    raw = get_journal_state(config, TREND_PENDING_PLAN_STATE_KEY)
    try:
        state = json.loads(raw or "{}")
    except json.JSONDecodeError:
        state = {}
    plans = state.get("plans") if isinstance(state.get("plans"), dict) else {}
    intent = activation.get("trade_intent") if isinstance(activation.get("trade_intent"), dict) else {}
    pending = activation.get("pending_plan") if isinstance(activation.get("pending_plan"), dict) else {}
    symbol = str(intent.get("symbol") or "")
    side = str(intent.get("side") or "")
    if symbol and side:
        key = f"{symbol}|{side}"
        plans[key] = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "side": side,
            "status": activation.get("status"),
            "enabled_for_execution": bool(activation.get("enabled_for_execution")),
            "trade_intent": intent,
            "pending_plan": pending,
        }
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(plans),
        "plans": plans,
    }
    set_journal_state(config, TREND_PENDING_PLAN_STATE_KEY, json.dumps(to_jsonable(payload), ensure_ascii=False))
    return payload


def _write_jsonl(config: dict[str, Any], relative_path: str, payload: dict[str, Any]) -> None:
    path = project_path(config, relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(to_jsonable(payload), ensure_ascii=False, separators=(",", ":")) + "\n")


def persist_trend_scan_log(config: dict[str, Any], *, now: datetime | None = None, force: bool = False) -> dict[str, Any]:
    snapshot = build_trend_scan_snapshot(config, now=now)
    slot_id = str(snapshot.get("slot_id") or "")
    if not force:
        try:
            if get_journal_state(config, TREND_SCAN_LAST_SLOT_KEY) == slot_id:
                return {"ok": True, "skipped": True, "reason": "slot_already_logged", "slot_id": slot_id}
        except Exception as exc:
            LOGGER.warning("Skipping trend scan last-slot read after storage error: %s", exc)
    try:
        watchlist = update_trend_watchlist(config, snapshot, now=now)
    except Exception as exc:
        LOGGER.warning("Skipping trend watchlist update after storage error: %s", exc)
        watchlist = {"error": str(exc)}
    snapshot["watchlist"] = {
        "count": watchlist.get("count"),
        "updated_at": watchlist.get("updated_at"),
        "expired_count": len(watchlist.get("expired") or []) if isinstance(watchlist.get("expired"), list) else None,
        "pending_confirmation_count": len(watchlist.get("pending_confirmations") or {}) if isinstance(watchlist.get("pending_confirmations"), dict) else 0,
    }
    try:
        snapshot["auto_shadow_reviews"] = run_trend_auto_shadow_reviews(config, watchlist)
    except Exception as exc:
        snapshot["auto_shadow_reviews"] = {"enabled": True, "error": str(exc)}
        LOGGER.warning("Skipping trend auto shadow reviews after error: %s", exc)
    body = json.dumps(to_jsonable(snapshot), ensure_ascii=False, separators=(",", ":"))
    try:
        set_journal_state(config, f"{TREND_SCAN_LOG_PREFIX}:{_safe_token(slot_id)}", body)
        set_journal_state(config, TREND_SCAN_LAST_SLOT_KEY, slot_id)
    except Exception as exc:
        LOGGER.warning("Skipping trend scan journal log after storage error: %s", exc)
    try:
        _write_jsonl(config, "logs/trend_scan.jsonl", snapshot)
    except Exception as exc:
        LOGGER.warning("Skipping trend scan file log after filesystem error: %s", exc)
    return {"ok": True, "skipped": False, **snapshot}


def persist_pool_pipeline_log(config: dict[str, Any], pipeline: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    source_key = "|".join(
        [
            str(pipeline.get("hourly_slot") or ""),
            str(pipeline.get("two_hour_slot") or ""),
            str(pipeline.get("four_hour_slot") or ""),
            str(pipeline.get("candidate_count") or 0),
        ]
    )
    try:
        if get_journal_state(config, POOL_PIPELINE_LAST_SOURCE_KEY) == source_key:
            return {"ok": True, "skipped": True, "reason": "source_already_logged", "source_key": source_key}
    except Exception as exc:
        LOGGER.warning("Skipping pool pipeline last-source read after storage error: %s", exc)
    payload = {
        "created_at": now.isoformat(),
        "source_key": source_key,
        "pipeline": to_jsonable(pipeline),
    }
    body = json.dumps(to_jsonable(payload), ensure_ascii=False, separators=(",", ":"))
    try:
        key = f"{POOL_PIPELINE_LOG_PREFIX}:{_safe_token(now.isoformat())}"
        set_journal_state(config, key, body)
        set_journal_state(config, POOL_PIPELINE_LAST_SOURCE_KEY, source_key)
    except Exception as exc:
        LOGGER.warning("Skipping pool pipeline journal log after storage error: %s", exc)
    try:
        _write_jsonl(config, "logs/pool_pipeline.jsonl", payload)
    except Exception as exc:
        LOGGER.warning("Skipping pool pipeline file log after filesystem error: %s", exc)
    return {"ok": True, "skipped": False, **payload}
