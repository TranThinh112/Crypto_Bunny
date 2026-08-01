from __future__ import annotations

import hashlib
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
TREND_APPROVED_HOLD_QUEUE_STATE_KEY = "trend_approved_hold_queue_state"
TREND_SETUP_REVIEW_LAST_CALL_PREFIX = "trend_setup_review_last_call"
TREND_SETUP_CLASS_MEMORY_PREFIX = "trend_setup_class_memory"


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

def _post_move_watch_decision(
    config: dict[str, Any],
    *,
    htf: dict[str, Any],
    entry: dict[str, Any],
    legacy_side: str,
    legacy_score: float,
    frames: list[dict[str, Any]],
    trend_watch: dict[str, Any],
) -> dict[str, Any]:
    internal = config.get("ai", {}).get("internal", {}) if isinstance(config.get("ai"), dict) else {}
    enabled = bool(internal.get("post_move_watch_enabled", True))
    if not enabled or bool(trend_watch.get("watch")):
        return {"watch": False, "reason": "disabled_or_regular_trend_watch"}
    htf_threshold = _float(internal.get("trend_scan_htf_watch_threshold"), 60.0)
    htf_min = _float(internal.get("post_move_watch_htf_min_score"), 48.0)
    near_gap = _float(internal.get("post_move_watch_near_threshold_gap"), 12.0)
    entry_max = _float(internal.get("post_move_watch_entry_max_score"), 35.0)
    retest_entry_score = _float(internal.get("post_move_watch_retest_entry_score"), 45.0)
    htf_side = str(htf.get("side") or "mixed")
    htf_score = _float(htf.get("score"))
    entry_side = str(entry.get("side") or "mixed")
    entry_score = _float(entry.get("score"))
    side = htf_side if htf_side in {"long", "short"} else legacy_side
    if side not in {"long", "short"}:
        return {"watch": False, "reason": "no_direction_for_post_move"}

    latest_frames = {str(frame.get("timeframe") or "").lower(): frame for frame in frames if isinstance(frame, dict)}
    entry_frame = latest_frames.get("15m") or latest_frames.get("5m") or latest_frames.get("1m") or {}
    htf_frame = latest_frames.get("4h") or latest_frames.get("1h") or {}
    rsi = _float(entry_frame.get("rsi"), _float(htf_frame.get("rsi"), 50.0))
    price_vs_ema = _float(entry_frame.get("price_vs_ema_slow_pct"), _float(htf_frame.get("price_vs_ema_slow_pct")))
    volume_ratio = _float(entry_frame.get("volume_ratio"), _float(htf_frame.get("volume_ratio"), 1.0))
    near_htf_threshold = htf_score >= max(htf_min, htf_threshold - near_gap)
    entry_not_ready = entry_score <= entry_max or entry_side not in {side, "long", "short"}
    hot_top = side == "long" and (rsi >= 68.0 or price_vs_ema >= 2.0)
    cold_bottom = side == "short" and (rsi <= 32.0 or price_vs_ema <= -2.0)
    large_move = htf_score >= htf_min or _float(legacy_score) >= htf_min
    watch = large_move and near_htf_threshold and entry_not_ready
    if not watch:
        return {
            "watch": False,
            "reason": "post_move_conditions_not_met",
            "htf_score": round(htf_score, 2),
            "entry_score": round(entry_score, 2),
        }

    opposite_side = "short" if side == "long" else "long"
    if side == "long" and hot_top:
        action = "WAIT_REVERSAL_SHORT"
        watch_reason = "overextended_top_wait_short_confirmation"
        trigger_side = "short"
    elif side == "short" and cold_bottom:
        action = "WAIT_REVERSAL_LONG"
        watch_reason = "overextended_bottom_wait_long_confirmation"
        trigger_side = "long"
    elif side == "long":
        action = "WAIT_RETEST_LONG"
        watch_reason = "post_move_wait_pullback_or_retest_long"
        trigger_side = "long"
    else:
        action = "WAIT_RETEST_SHORT"
        watch_reason = "post_move_wait_pullback_or_retest_short"
        trigger_side = "short"
    trigger_ready = entry_side == trigger_side and entry_score >= retest_entry_score
    return {
        "watch": True,
        "watch_type": "post_move",
        "side": trigger_side if trigger_ready else side,
        "move_side": side,
        "counter_side": opposite_side,
        "score": round(max(htf_score, _float(legacy_score)), 2),
        "reason": watch_reason,
        "entry_action": action,
        "trigger_ready": trigger_ready,
        "ai_ready": trigger_ready,
        "entry_ready": trigger_ready,
        "ttl_minutes": max(30, min(
            int(internal.get("post_move_watch_max_ttl_minutes", 120) or 120),
            int(internal.get("post_move_watch_default_ttl_minutes", 60) or 60),
        )),
        "recheck_minutes": 15,
        "rsi": round(rsi, 2),
        "price_vs_ema_slow_pct": round(price_vs_ema, 4),
        "volume_ratio": round(volume_ratio, 4),
        "next_conditions": [
            "Không vào market ngay sau cú move mạnh.",
            "Chỉ xét tiếp khi retest/pullback hoặc reversal có xác nhận 5m/15m.",
            "Gọi AI khi entry_score hồi phục và RR mới đủ tốt.",
        ],
        "thresholds": {
            "htf_min": htf_min,
            "entry_max": entry_max,
            "retest_entry_score": retest_entry_score,
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
            indicator = row.get("indicator") if isinstance(row.get("indicator"), dict) else {}
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
                    "rsi": indicator.get("rsi"),
                    "ema_gap_pct": indicator.get("ema_gap_pct"),
                    "price_vs_ema_slow_pct": indicator.get("price_vs_ema_slow_pct"),
                    "volume_ratio": indicator.get("volume_ratio"),
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
        post_move_decision = _post_move_watch_decision(
            config,
            htf=htf_score,
            entry=entry_score,
            legacy_side=trend_side,
            legacy_score=trend_score,
            frames=frames,
            trend_watch=watch_decision,
        )
        symbols.append(
            {
                "symbol": symbol,
                "trend_side": post_move_decision.get("side") if post_move_decision.get("watch") else watch_decision["side"],
                "trend_score": round(_float(post_move_decision.get("score") if post_move_decision.get("watch") else watch_decision["score"]), 2),
                "watch_type": "post_move" if post_move_decision.get("watch") else "trend",
                "post_move_watch": post_move_decision,
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
                "entry_ready": post_move_decision.get("entry_ready", watch_decision["entry_ready"]) if post_move_decision.get("watch") else watch_decision["entry_ready"],
                "ai_ready": post_move_decision.get("ai_ready", watch_decision["ai_ready"]) if post_move_decision.get("watch") else watch_decision["ai_ready"],
                "countertrend_review": watch_decision["countertrend_review"],
                "entry_action": post_move_decision.get("entry_action") if post_move_decision.get("watch") else watch_decision["entry_action"],
                "watchlist_eligible": watch_decision["watch"],
                "post_move_eligible": bool(post_move_decision.get("watch")),
                "watchlist_reason": post_move_decision.get("reason") if post_move_decision.get("watch") else watch_decision["reason"],
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
    post_move = [
        item
        for item in symbols
        if bool(item.get("post_move_eligible")) and item.get("trend_side") in {"long", "short"}
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
        "post_move_count": len(post_move),
        "side_counts": dict(Counter(str(item.get("trend_side") or "mixed") for item in symbols)),
        "core_symbols": core_symbols,
        "top_symbols": symbols[:top_limit],
        "strong_symbols": strong[:top_limit],
        "post_move_symbols": post_move[:top_limit],
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
        is_post_move = str(item.get("watch_type") or item.get("source") or "") == "post_move"
        keep_by_score = is_post_move or _float(item.get("trend_score")) >= min_keep_score
        if expires_at and expires_at > now and keep_by_score:
            next_items[str(key)] = item
        else:
            expired.append(
                {
                    "key": key,
                    "symbol": item.get("symbol"),
                    "side": item.get("side"),
                    "expired_at": now.isoformat(),
                    "previous_status": item.get("status"),
                    "reason": "post_move_ttl_expired" if is_post_move else f"ttl_expired_or_trend_score_below_{min_keep_score:g}",
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
    for trend in snapshot.get("post_move_symbols") or []:
        if not isinstance(trend, dict):
            continue
        symbol = str(trend.get("symbol") or "")
        post_move = trend.get("post_move_watch") if isinstance(trend.get("post_move_watch"), dict) else {}
        side = str(post_move.get("side") or trend.get("trend_side") or "")
        if not symbol or side not in {"long", "short"}:
            continue
        key = f"{symbol}|{side}|post_move"
        if key in next_rejected_until:
            continue
        existing = next_items.get(key, {})
        ttl = max(30, min(
            int(internal.get("post_move_watch_max_ttl_minutes", 120) or 120),
            int(post_move.get("ttl_minutes") or internal.get("post_move_watch_default_ttl_minutes", 60) or 60),
        ))
        first_seen_at = existing.get("first_seen_at") or now.isoformat()
        expires_at = existing.get("expires_at") or (now + timedelta(minutes=ttl)).isoformat()
        next_items[key] = {
            **existing,
            "symbol": symbol,
            "side": side,
            "status": "post_move_watching",
            "source": "post_move_watch",
            "watch_type": "post_move",
            "first_seen_at": first_seen_at,
            "updated_at": now.isoformat(),
            "expires_at": expires_at,
            "ttl_minutes": ttl,
            "trend_score": trend.get("trend_score"),
            "long_score": trend.get("long_score"),
            "short_score": trend.get("short_score"),
            "legacy_trend_side": trend.get("legacy_trend_side"),
            "legacy_trend_score": trend.get("legacy_trend_score"),
            "move_side": post_move.get("move_side"),
            "counter_side": post_move.get("counter_side"),
            "htf_trend_side": trend.get("htf_trend_side"),
            "htf_trend_score": trend.get("htf_trend_score"),
            "htf_long_score": trend.get("htf_long_score"),
            "htf_short_score": trend.get("htf_short_score"),
            "entry_readiness_side": trend.get("entry_readiness_side"),
            "entry_readiness_score": trend.get("entry_readiness_score"),
            "entry_long_score": trend.get("entry_long_score"),
            "entry_short_score": trend.get("entry_short_score"),
            "entry_ready": bool(post_move.get("entry_ready")),
            "ai_ready": bool(post_move.get("ai_ready")),
            "countertrend_review": trend.get("countertrend_review"),
            "entry_action": trend.get("entry_action"),
            "watchlist_reason": trend.get("watchlist_reason"),
            "confirmation_mode": "post_move_recheck",
            "required_confirmations": 1,
            "confirmation_count": 1,
            "trend_thresholds": {
                **(trend.get("trend_thresholds") if isinstance(trend.get("trend_thresholds"), dict) else {}),
                "post_move": post_move.get("thresholds"),
            },
            "frame_count": trend.get("frame_count"),
            "frames": trend.get("frames"),
            "post_move_watch": post_move,
            "last_reason": trend.get("watchlist_reason") or "post_move_wait_second_chance",
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
    market_pattern = frame.get("market_pattern") if isinstance(frame.get("market_pattern"), dict) else payload.get("market_pattern")
    if isinstance(market_pattern, dict):
        nearest_support = market_pattern.get("nearest_support") if isinstance(market_pattern.get("nearest_support"), dict) else {}
        nearest_resistance = market_pattern.get("nearest_resistance") if isinstance(market_pattern.get("nearest_resistance"), dict) else {}
        if support <= 0:
            support = _float(nearest_support.get("center_price"))
        if resistance <= 0:
            resistance = _float(nearest_resistance.get("center_price"))
    if entry > 0 and support > 0 and support_distance <= 0:
        support_distance = ((entry - support) / entry) * 100.0
    if entry > 0 and resistance > 0 and resistance_distance <= 0:
        resistance_distance = ((resistance - entry) / entry) * 100.0
    if entry > 0 and support > 0 and resistance > support:
        range_position = (entry - support) / max(resistance - support, 1e-12)
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
            "gpt_confidence": "required number 0-100; AI confidence that this setup is valid now",
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
    notification_context: dict[str, Any] | None = None,
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
        record_history=not notify_telegram,
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

            event_context = dict(notification_context or {})
            if not _ai_review_should_extend_watchlist(normalized):
                event_context["watchlist_ai_review_extend_minutes"] = 0
            if _ai_review_removes_watchlist_pair(normalized):
                event_context["watchlist_reject_cooldown_minutes"] = max(
                    15,
                    int(internal_config.get("trend_watchlist_reject_cooldown_minutes", 120) or 120),
                )
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
                    "reject_scope": normalized.get("reject_scope"),
                    "reject_reason_type": normalized.get("reject_reason_type"),
                    "sl_tp_method": ((setup.get("risk_model") or {}).get("selected_method") if isinstance(setup.get("risk_model"), dict) else None),
                    **event_context,
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
    gpt_confidence_raw = review.get("gpt_confidence", review.get("gptConfidence"))
    gpt_confidence = _float(gpt_confidence_raw, float("nan"))
    if gpt_confidence != gpt_confidence or gpt_confidence < 0 or gpt_confidence > 100:
        gpt_confidence = None
        warnings = [*warnings, "gpt_confidence_missing_or_invalid"]
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
        "gpt_confidence": None if gpt_confidence is None else round(gpt_confidence, 2),
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
            "risk_profile": "Reduced" if ai_review.get("setup_grade") in {"C", "D"} or _float(ai_review.get("entry_quality")) < 70 else "Normal",
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
    gpt_confidence = ai_review.get("gpt_confidence", ai_review.get("gptConfidence"))
    confidence = round(_clamp(_float(gpt_confidence, 0.0)), 2)
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
    intent = activation.get("trade_intent") if isinstance(activation.get("trade_intent"), dict) else {}
    try:
        from .capital import calculate_trade_intent_position_size

        capital_plan = calculate_trade_intent_position_size(config, intent, setup, ai_review)
    except Exception as exc:
        capital_plan = {
            "allowed": False,
            "reason": f"capital_sizing_error: {exc}",
            "source": "trade_intent_capital",
        }
    candidate = _setup_to_candidate(config, setup, ai_review)
    if candidate is None:
        return {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "shadow_mode": True,
            "phase": "phase_5_risk_capital_reads_trade_intent",
            "risk": {"approved": False, "reasons": ["candidate_unavailable_from_trade_intent"], "warnings": []},
            "capital": {**capital_plan, "approved": False, "reason": "candidate_unavailable"},
        }
    leverage = max(1.0, _float(capital_plan.get("leverage"), _float(config.get("exchange", {}).get("leverage"), 1.0)))
    margin_usdt = _float(capital_plan.get("margin_usdt") or capital_plan.get("required_margin"))
    notional_usdt = _float(capital_plan.get("notional_usdt") or capital_plan.get("suggested_order_size"))
    if margin_usdt > 0:
        candidate.margin_usdt = margin_usdt
    if notional_usdt > 0:
        candidate.order_usdt = notional_usdt
    try:
        from .risk import evaluate_candidate

        check = evaluate_candidate(config, candidate, enforce_active_limit=True, check_active_trades=True, check_order_limits=True)
        risk_payload = {"approved": bool(check.passed), "reasons": list(check.reasons or []), "warnings": list(check.warnings or [])}
    except Exception as exc:
        risk_payload = {"approved": False, "reasons": [f"risk_shadow_error: {exc}"], "warnings": []}
    capital_approved = bool(capital_plan.get("allowed")) and bool(risk_payload.get("approved"))
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "shadow_mode": True,
        "phase": "phase_5_risk_capital_reads_trade_intent",
        "trade_intent_status": intent.get("status"),
        "risk": risk_payload,
        "capital": {
            **capital_plan,
            "approved": capital_approved,
            "margin_usdt": round(margin_usdt, 4),
            "leverage": leverage,
            "notional_usdt": round(_float(candidate.order_usdt), 4),
            "risk_profile": intent.get("risk_profile") or capital_plan.get("risk_profile") or "Normal",
            "source": "trade_intent_shadow",
        },
    }

def _approved_hold_settings(config: dict[str, Any]) -> dict[str, Any]:
    internal = config.get("ai", {}).get("internal", {}) if isinstance(config.get("ai"), dict) else {}
    return {
        "ttl_minutes": max(5, int(internal.get("trend_approved_hold_ttl_minutes", 30) or 30)),
        "recheck_seconds": max(30, int(internal.get("trend_approved_hold_recheck_seconds", 120) or 120)),
        "priority_rewatch_ttl_minutes": max(5, int(internal.get("trend_priority_rewatch_ttl_minutes", 30) or 30)),
        "max_entry_drift_pct": max(0.1, _float(internal.get("trend_approved_hold_max_entry_drift_pct"), 1.2)),
        "stale_minutes": max(5, int(internal.get("trend_approved_hold_stale_minutes", 45) or 45)),
        "reject_cooldown_minutes": max(15, int(internal.get("trend_watchlist_reject_cooldown_minutes", 120) or 120)),
    }

def _approved_hold_block_type(reasons: list[str], warnings: list[str] | None = None) -> str:
    text = " | ".join(str(item).lower() for item in [*reasons, *(warnings or [])])
    remove_tokens = (
        "trend_invalid",
        "liquidity",
        "market_conflict",
        "symbol is not tradable",
        "not tradable",
        "delisted",
    )
    rewatch_tokens = (
        "risk/reward",
        "risk_reward",
        "volume",
        "confidence",
        "win probability",
        "entry",
        "spread",
        "stop distance",
        "news",
    )
    temporary_tokens = (
        "health monitor",
        "pause",
        "recovery",
        "order size is not positive",
        "active trade limit",
        "already exists",
        "cooldown active",
        "daily order limit",
        "capital",
        "automation",
        "okx",
        "api",
        "cannot verify",
        "lock",
    )
    if any(token in text for token in remove_tokens):
        return "remove_pair"
    if any(token in text for token in temporary_tokens):
        return "temporary_block"
    if any(token in text for token in rewatch_tokens):
        return "priority_rewatch"
    return "temporary_block"

def _format_hold_minutes(value: Any) -> str:
    minutes = int(max(0, _float(value)))
    return f"{minutes}p"

def _brief_hold_block_reason_vi(reason: str) -> str:
    text = str(reason or "").strip()
    lower = text.lower()
    if not text:
        return "-"
    if "health monitor" in lower or "pause" in lower:
        return "Health Monitor đang pause"
    if "order size is not positive" in lower or "suggested order size is below minimum" in lower:
        return "Vốn vào lệnh đang bằng 0/quá nhỏ"
    if "confidence" in lower:
        return "Confidence chưa đạt"
    if "win probability" in lower:
        return "Xác suất thắng chưa đạt"
    if "risk/reward" in lower or "risk_reward" in lower:
        return "RR chưa đạt"
    if "volume" in lower:
        return "Volume chưa xác nhận"
    if "active trade limit" in lower:
        return "Đã đủ số lệnh đang mở"
    if "already exists" in lower:
        return "Đã có lệnh/position cùng cặp"
    if "cooldown active" in lower:
        return "Cooldown lệnh chưa hết"
    if "capital" in lower or "margin" in lower:
        return "Vốn khả dụng chưa đủ"
    if "market guard" in lower:
        return "Market Guard đang chặn"
    if "spread" in lower:
        return "Spread chưa đạt"
    if "stop distance" in lower:
        return "Khoảng cách SL chưa hợp lệ"
    if "news" in lower:
        return "News/context chưa xác nhận"
    if "trend_invalid" in lower or "trend invalid" in lower:
        return "Trend đã gãy"
    if "liquidity" in lower:
        return "Thanh khoản rủi ro"
    if "market_conflict" in lower or "market conflict" in lower:
        return "Bối cảnh thị trường xung đột"
    if "okx" in lower or "api" in lower:
        return "OKX/API lỗi tạm thời"
    if "cannot verify" in lower:
        return "Chưa xác minh được trạng thái OKX"
    return text[:90]

def _format_hold_block_reasons(reasons: list[str]) -> str:
    compact = []
    seen: set[str] = set()
    for reason in reasons:
        label = _brief_hold_block_reason_vi(reason)
        if label in seen or label == "-":
            continue
        seen.add(label)
        compact.append(label)
    if not compact:
        compact = ["-"]
    return "\n".join(f"  - {item}" for item in compact[:5])

def _compact_hold_block_reason_labels(reasons: list[str]) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        label = _brief_hold_block_reason_vi(reason)
        if label in seen or label == "-":
            continue
        seen.add(label)
        labels.append(label)
    return labels

def _approved_hold_event_message(event: str, item: dict[str, Any], settings: dict[str, Any]) -> str:
    symbol = str(item.get("symbol") or "-")
    side = str(item.get("side") or "-").upper()
    block_type = str(item.get("block_type") or "-")
    reasons = [str(reason) for reason in item.get("block_reasons") or [] if reason]
    reason_text = _format_hold_block_reasons(reasons)
    resolved_reasons = [str(reason) for reason in item.get("block_resolved_reasons") or [] if reason]
    resolved_text = _format_hold_block_reasons(resolved_reasons) if resolved_reasons else ""
    ttl = settings.get("ttl_minutes")
    if event == "entered":
        title = "⏳ Trend APPROVED HOLD QUEUE"
        status = "ENTERED_QUEUE"
        meaning = "Mini đã duyệt nhưng đang bị block; giữ trong queue để recheck."
        extra = f"Thời gian trong queue: {_format_hold_minutes(ttl)}"
    elif event == "priority_rewatch":
        title = "↩️ Trend PRIORITY REWATCH"
        status = "BACK_TO_WATCHLIST"
        meaning = "Setup cần làm mới; quay lại watchlist ưu tiên, không đi lại từ đầu."
        extra = f"TTL rewatch: {_format_hold_minutes(settings.get('priority_rewatch_ttl_minutes'))}"
    elif event == "remove_pair":
        title = "🧹 Trend REMOVE PAIR"
        status = "REMOVE_PAIR"
        meaning = "Block/risk nặng; xóa khỏi watchlist và cooldown cặp/side này."
        extra = f"Cooldown: {_format_hold_minutes(settings.get('reject_cooldown_minutes'))}"
    elif event == "ready":
        title = "✅ Trend QUEUE CLEARED"
        status = "READY_FOR_ORDER"
        meaning = "Block đã hết; setup có thể đi tiếp tới bước đặt lệnh."
        extra = "Hướng đi: Risk/Capital clear → OKX order"
    elif event == "update":
        title = "📌 Trend APPROVED HOLD UPDATE"
        status = "QUEUE_UPDATED"
        meaning = "Một số block đã được gỡ; tiếp tục giữ queue nếu vẫn còn block."
        extra = f"Thời gian trong queue: {_format_hold_minutes(ttl)}"
    else:
        title = "📌 Trend APPROVED HOLD UPDATE"
        status = str(event or "UPDATE").upper()
        meaning = "Trạng thái queue đã thay đổi."
        extra = "-"
    return "\n".join(
        [
            title,
            f"Cặp: {symbol} | {side}",
            f"Trạng thái queue: {status}",
            f"Ý nghĩa: {meaning}",
            f"Loại block: {block_type}",
            "Lý do block:",
            reason_text,
            "Block đã gỡ:",
            resolved_text,
            extra,
        ]
    )

def _notify_approved_hold_event(config: dict[str, Any], event: str, item: dict[str, Any], settings: dict[str, Any]) -> None:
    internal = config.get("ai", {}).get("internal", {}) if isinstance(config.get("ai"), dict) else {}
    if not bool(internal.get("trend_approved_hold_notify_enabled", True)):
        return
    telegram_config = config.get("notifications", {}).get("telegram", {})
    if not bool(telegram_config.get("notify_ai_api_calls", True)):
        return
    try:
        from .notifier import send_telegram_message

        send_telegram_message(config, _approved_hold_event_message(event, item, settings), with_buttons=False, replace_previous=False)
    except Exception:
        LOGGER.warning("Skipping approved hold queue telegram notification", exc_info=True)

def upsert_trend_approved_hold_queue(
    config: dict[str, Any],
    *,
    setup: dict[str, Any],
    ai_review: dict[str, Any],
    activation: dict[str, Any],
    risk_capital: dict[str, Any],
) -> dict[str, Any]:
    settings = _approved_hold_settings(config)
    now = datetime.now(timezone.utc)
    raw = get_journal_state(config, TREND_APPROVED_HOLD_QUEUE_STATE_KEY)
    try:
        state = json.loads(raw or "{}")
    except json.JSONDecodeError:
        state = {}
    items = state.get("items") if isinstance(state.get("items"), dict) else {}
    symbol = str(setup.get("symbol") or "")
    side = str(setup.get("side") or "").lower()
    if not symbol or side not in {"long", "short"}:
        return {"updated_at": now.isoformat(), "count": len(items), "items": items, "error": "missing_symbol_or_side"}
    key = f"{symbol}|{side}"
    existing = items.get(key) if isinstance(items.get(key), dict) else {}
    risk = risk_capital.get("risk") if isinstance(risk_capital.get("risk"), dict) else {}
    capital = risk_capital.get("capital") if isinstance(risk_capital.get("capital"), dict) else {}
    reasons = [str(item) for item in [*(risk.get("reasons") or []), capital.get("reason")] if item]
    warnings = [str(item) for item in risk.get("warnings") or []]
    block_type = _approved_hold_block_type(reasons, warnings)
    previous_labels = set(_compact_hold_block_reason_labels([str(reason) for reason in existing.get("block_reasons") or []]))
    current_labels = set(_compact_hold_block_reason_labels(reasons))
    resolved_reasons = sorted(previous_labels - current_labels)
    item = {
        "created_at": existing.get("created_at") or now.isoformat(),
        "updated_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=settings["ttl_minutes"])).isoformat(),
        "next_recheck_at": (now + timedelta(seconds=settings["recheck_seconds"])).isoformat(),
        "symbol": symbol,
        "side": side,
        "status": "approved_hold",
        "block_type": block_type,
        "block_reasons": reasons[:10],
        "block_resolved_reasons": resolved_reasons,
        "block_warnings": warnings[:10],
        "setup": setup,
        "ai_review": ai_review,
        "activation": activation,
        "risk_capital": risk_capital,
        "priority_rewatch_ttl_minutes": settings["priority_rewatch_ttl_minutes"],
        "max_entry_drift_pct": settings["max_entry_drift_pct"],
        "stale_minutes": settings["stale_minutes"],
    }
    items[key] = item
    payload = {"updated_at": now.isoformat(), "count": len(items), "items": items}
    set_journal_state(config, TREND_APPROVED_HOLD_QUEUE_STATE_KEY, json.dumps(to_jsonable(payload), ensure_ascii=False))
    if not existing or str(existing.get("block_type") or "") != block_type:
        _notify_approved_hold_event(config, "entered", item, settings)
    elif resolved_reasons:
        _notify_approved_hold_event(config, "update", item, settings)
    return payload

def _add_priority_rewatch_item(
    config: dict[str, Any],
    queue_item: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    settings = _approved_hold_settings(config)
    raw = get_journal_state(config, TREND_WATCHLIST_STATE_KEY)
    try:
        state = json.loads(raw or "{}")
    except json.JSONDecodeError:
        state = {}
    items = state.get("items") if isinstance(state.get("items"), dict) else {}
    symbol = str(queue_item.get("symbol") or "")
    side = str(queue_item.get("side") or "").lower()
    key = f"{symbol}|{side}"
    setup = queue_item.get("setup") if isinstance(queue_item.get("setup"), dict) else {}
    existing = items.get(key) if isinstance(items.get(key), dict) else {}
    items[key] = {
        **existing,
        "symbol": symbol,
        "side": side,
        "status": "priority_rewatch",
        "source": "approved_hold_returned",
        "priority": 100,
        "first_seen_at": existing.get("first_seen_at") or queue_item.get("created_at") or now.isoformat(),
        "updated_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=settings["priority_rewatch_ttl_minutes"])).isoformat(),
        "ttl_minutes": settings["priority_rewatch_ttl_minutes"],
        "last_reason": "approved_hold_returned_to_priority_rewatch",
        "entry_price": setup.get("entry_price"),
        "stop_loss": setup.get("stop_loss"),
        "take_profit": setup.get("take_profit"),
        "risk_reward": setup.get("risk_reward"),
        "last_ai_decision": (queue_item.get("ai_review") or {}).get("decision") if isinstance(queue_item.get("ai_review"), dict) else None,
        "last_ai_review_at": queue_item.get("updated_at"),
    }
    state["items"] = items
    state["updated_at"] = now.isoformat()
    state["count"] = len(items)
    set_journal_state(config, TREND_WATCHLIST_STATE_KEY, json.dumps(to_jsonable(state), ensure_ascii=False))
    return state

def _cooldown_removed_approved_hold_pair(
    config: dict[str, Any],
    queue_item: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    settings = _approved_hold_settings(config)
    raw = get_journal_state(config, TREND_WATCHLIST_STATE_KEY)
    try:
        state = json.loads(raw or "{}")
    except json.JSONDecodeError:
        state = {}
    items = state.get("items") if isinstance(state.get("items"), dict) else {}
    rejected_until = state.get("rejected_until") if isinstance(state.get("rejected_until"), dict) else {}
    expired = state.get("expired") if isinstance(state.get("expired"), list) else []
    symbol = str(queue_item.get("symbol") or "")
    side = str(queue_item.get("side") or "").lower()
    key = f"{symbol}|{side}"
    items.pop(key, None)
    rejected_until[key] = {
        "symbol": symbol,
        "side": side,
        "until": (now + timedelta(minutes=settings["reject_cooldown_minutes"])).isoformat(),
        "reason": "approved_hold_remove_pair",
        "block_reasons": queue_item.get("block_reasons") or [],
    }
    expired.append(
        {
            "key": key,
            "symbol": symbol,
            "side": side,
            "expired_at": now.isoformat(),
            "previous_status": "approved_hold",
            "reason": "approved_hold_remove_pair",
        }
    )
    state["items"] = items
    state["rejected_until"] = rejected_until
    state["expired"] = expired[-50:]
    state["updated_at"] = now.isoformat()
    state["count"] = len(items)
    set_journal_state(config, TREND_WATCHLIST_STATE_KEY, json.dumps(to_jsonable(state), ensure_ascii=False))
    return state

def process_trend_approved_hold_queue(config: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    raw = get_journal_state(config, TREND_APPROVED_HOLD_QUEUE_STATE_KEY)
    try:
        state = json.loads(raw or "{}")
    except json.JSONDecodeError:
        state = {}
    items = state.get("items") if isinstance(state.get("items"), dict) else {}
    next_items: dict[str, dict[str, Any]] = {}
    processed: list[dict[str, Any]] = []
    for key, item in items.items():
        if not isinstance(item, dict):
            continue
        expires_at = _parse_time(item.get("expires_at"))
        if expires_at and expires_at > now:
            next_items[str(key)] = item
            continue
        block_type = str(item.get("block_type") or "temporary_block")
        if block_type == "remove_pair":
            _cooldown_removed_approved_hold_pair(config, item, now=now)
            action = "remove_pair_cooldown"
            _notify_approved_hold_event(config, "remove_pair", item, _approved_hold_settings(config))
        else:
            _add_priority_rewatch_item(config, item, now=now)
            action = "priority_rewatch"
            _notify_approved_hold_event(config, "priority_rewatch", item, _approved_hold_settings(config))
        processed.append({"key": key, "symbol": item.get("symbol"), "side": item.get("side"), "action": action})
    payload = {"updated_at": now.isoformat(), "count": len(next_items), "items": next_items, "processed": processed[-50:]}
    set_journal_state(config, TREND_APPROVED_HOLD_QUEUE_STATE_KEY, json.dumps(to_jsonable(payload), ensure_ascii=False))
    return payload

def recheck_trend_approved_hold_queue(config: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Re-evaluate held approved setups and mark them ready when all blocks clear.

    This worker intentionally stops at READY_FOR_ORDER. The live order executor can
    consume that state in a separate, explicit execution step.
    """
    now = now or datetime.now(timezone.utc)
    raw = get_journal_state(config, TREND_APPROVED_HOLD_QUEUE_STATE_KEY)
    try:
        state = json.loads(raw or "{}")
    except json.JSONDecodeError:
        state = {}
    items = state.get("items") if isinstance(state.get("items"), dict) else {}
    next_items: dict[str, dict[str, Any]] = {}
    checked: list[dict[str, Any]] = []
    for key, item in items.items():
        if not isinstance(item, dict):
            continue
        next_recheck_at = _parse_time(item.get("next_recheck_at"))
        if next_recheck_at and next_recheck_at > now:
            next_items[str(key)] = item
            continue
        setup = item.get("setup") if isinstance(item.get("setup"), dict) else {}
        ai_review = item.get("ai_review") if isinstance(item.get("ai_review"), dict) else {}
        activation = item.get("activation") if isinstance(item.get("activation"), dict) else {}
        risk_capital = evaluate_trade_intent_risk_capital_shadow(config, setup, ai_review, activation)
        risk = risk_capital.get("risk") if isinstance(risk_capital.get("risk"), dict) else {}
        capital = risk_capital.get("capital") if isinstance(risk_capital.get("capital"), dict) else {}
        reasons = [str(value) for value in [*(risk.get("reasons") or []), capital.get("reason")] if value and value != "OK"]
        previous_labels = set(_compact_hold_block_reason_labels([str(reason) for reason in item.get("block_reasons") or []]))
        current_labels = set(_compact_hold_block_reason_labels(reasons))
        resolved_reasons = sorted(previous_labels - current_labels)
        if bool(capital.get("approved")) and bool(risk.get("approved")):
            ready_item = {
                **item,
                "updated_at": now.isoformat(),
                "status": "ready_for_order",
                "block_type": "cleared",
                "block_reasons": [],
                "block_resolved_reasons": sorted(previous_labels),
                "risk_capital": risk_capital,
            }
            _notify_approved_hold_event(config, "ready", ready_item, _approved_hold_settings(config))
            checked.append({"key": key, "symbol": item.get("symbol"), "side": item.get("side"), "action": "ready_for_order"})
            continue
        settings = _approved_hold_settings(config)
        updated_item = {
            **item,
            "updated_at": now.isoformat(),
            "next_recheck_at": (now + timedelta(seconds=settings["recheck_seconds"])).isoformat(),
            "block_type": _approved_hold_block_type(reasons, risk.get("warnings") or []),
            "block_reasons": reasons[:10],
            "block_resolved_reasons": resolved_reasons,
            "risk_capital": risk_capital,
        }
        if resolved_reasons or set(_compact_hold_block_reason_labels([str(reason) for reason in item.get("block_reasons") or []])) != current_labels:
            _notify_approved_hold_event(config, "update", updated_item, settings)
        next_items[str(key)] = updated_item
        checked.append({"key": key, "symbol": item.get("symbol"), "side": item.get("side"), "action": "still_blocked"})
    payload = {"updated_at": now.isoformat(), "count": len(next_items), "items": next_items, "checked": checked[-50:]}
    set_journal_state(config, TREND_APPROVED_HOLD_QUEUE_STATE_KEY, json.dumps(to_jsonable(payload), ensure_ascii=False))
    return payload

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
        "entry_price": round(_float(setup.get("entry_price")), 8),
        "stop_loss": round(_float(setup.get("stop_loss")), 8),
        "take_profit": round(_float(setup.get("take_profit")), 8),
        "risk_reward": round(_float(setup.get("risk_reward")), 4),
        "trend_score": round(_float(item.get("trend_score")), 2),
        "entry_readiness_score": round(_float(item.get("entry_readiness_score")), 2),
        "pullback_quality": round(_float(setup.get("pullback_quality")), 2),
        "breakout_quality": round(_float(setup.get("breakout_quality")), 2),
        "rsi": round(_float(setup.get("rsi")), 2),
        "price_vs_ema_slow_pct": round(_float(setup.get("price_vs_ema_slow_pct")), 4),
        "volume_confirmation": bool(setup.get("volume_confirmation")),
        "watch_type": item.get("watch_type"),
        "warnings": sorted(str(value) for value in (setup.get("warnings") or [])),
    }

def _trend_setup_fingerprint(setup: dict[str, Any], item: dict[str, Any]) -> str:
    payload = json.dumps(_trend_setup_signature(setup, item), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()

def _trend_setup_class(setup: dict[str, Any], item: dict[str, Any] | None = None) -> str:
    warnings = {str(value) for value in (setup.get("warnings") or [])}
    reasons = {str(value) for value in (setup.get("entry_action_reason") or [])}
    entry_action = str(setup.get("entry_action") or "").upper()
    setup_state = str(setup.get("setup_state") or "").lower()
    volume_confirmed = bool(setup.get("volume_confirmation"))
    support_resistance = setup.get("support_resistance") if isinstance(setup.get("support_resistance"), dict) else {}
    near_resistance = "near_resistance" in reasons or bool(support_resistance.get("near_resistance"))
    near_support = "near_support" in reasons or bool(support_resistance.get("near_support"))
    no_chase = "no_chase_entry" in warnings or "NO_CHASE" in entry_action or entry_action.startswith("REVIEW_COUNTERTREND")
    method = str((setup.get("risk_model") or {}).get("selected_method") or setup.get("sl_tp_method") or "")
    if setup_state in {"invalid", "rejected"}:
        return "trend_invalid"
    if not volume_confirmed:
        return "volume_weak"
    if near_resistance and no_chase:
        return "near_resistance_no_chase"
    if near_support and no_chase:
        return "near_support_no_chase"
    if "BREAKOUT" in entry_action:
        return "breakout_confirmation"
    if setup_state in {"ready", "approved", "trade_ready"} or entry_action.startswith("READY"):
        return "ready_to_review"
    if method in {"structure_swing_to_previous_extreme", "fib_extension_1272"} and not no_chase:
        return "clean_pullback"
    if "PULLBACK" in entry_action or str(setup.get("entry_type") or "") == "pullback":
        return "wait_pullback"
    return "setup_review"

def _trend_setup_quality_rank(setup_class: str) -> int:
    ranks = {
        "trend_invalid": 0,
        "volume_weak": 1,
        "near_resistance_no_chase": 2,
        "near_support_no_chase": 2,
        "wait_pullback": 3,
        "breakout_confirmation": 4,
        "clean_pullback": 5,
        "ready_to_review": 6,
    }
    return ranks.get(str(setup_class or ""), 3)

def _trend_setup_memory_date(config: dict[str, Any]) -> str:
    timezone_name = str(config.get("timezone") or config.get("ai", {}).get("internal", {}).get("market_scan_timezone") or "Asia/Ho_Chi_Minh")
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = timezone(timedelta(hours=7))
    return datetime.now(tz).date().isoformat()

def _trend_setup_class_memory_key(config: dict[str, Any], symbol: str, side: str, setup_class: str) -> str:
    return (
        f"{TREND_SETUP_CLASS_MEMORY_PREFIX}:"
        f"{_trend_setup_memory_date(config)}:"
        f"{_safe_token(symbol)}:"
        f"{_safe_token(side)}:"
        f"{_safe_token(setup_class)}"
    )

def _load_trend_setup_class_memory(config: dict[str, Any], symbol: str, side: str, setup_class: str) -> dict[str, Any]:
    raw = get_journal_state(config, _trend_setup_class_memory_key(config, symbol, side, setup_class))
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        payload = {}
    return payload if isinstance(payload, dict) else {}

def _save_trend_setup_class_memory(
    config: dict[str, Any],
    symbol: str,
    side: str,
    setup_class: str,
    payload: dict[str, Any],
) -> None:
    set_journal_state(
        config,
        _trend_setup_class_memory_key(config, symbol, side, setup_class),
        json.dumps(to_jsonable(payload), ensure_ascii=False, sort_keys=True),
    )

def _pct_delta(current: Any, previous: Any) -> float:
    current_value = _float(current)
    previous_value = _float(previous)
    if previous_value == 0:
        return 100.0 if current_value != 0 else 0.0
    return abs((current_value - previous_value) / previous_value) * 100.0

def _trend_setup_changed_enough(config: dict[str, Any], setup: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    internal = config.get("ai", {}).get("internal", {}) if isinstance(config.get("ai"), dict) else {}
    price_delta_threshold = _float(internal.get("trend_setup_review_price_change_pct", 3.0), 3.0)
    score_delta_threshold = _float(internal.get("trend_setup_review_score_change", 15.0), 15.0)
    trend_score_delta_threshold = _float(internal.get("trend_setup_review_trend_score_change", 12.0), 12.0)
    same_fingerprint_block_enabled = bool(internal.get("trend_setup_review_same_fingerprint_block_enabled", True))
    review_cooldown_minutes = max(
        5,
        int(internal.get("trend_setup_review_review_cooldown_minutes", 45) or 45),
    )
    reject_setup_cooldown_minutes = max(
        15,
        int(internal.get("trend_setup_review_reject_setup_only_cooldown_minutes", 120) or 120),
    )
    reject_remove_cooldown_minutes = max(
        60,
        int(internal.get("trend_setup_review_reject_watchlist_remove_cooldown_minutes", 720) or 720),
    )
    approve_cooldown_minutes = max(
        5,
        int(internal.get("trend_setup_review_approve_cooldown_minutes", 15) or 15),
    )
    previous = item.get("last_ai_setup_signature") if isinstance(item.get("last_ai_setup_signature"), dict) else {}
    current = _trend_setup_signature(setup, item)
    current_setup_class = _trend_setup_class(setup, item)
    previous_setup_class = str(item.get("last_ai_setup_class") or previous.get("setup_class") or "")
    current["setup_class"] = current_setup_class
    current_fingerprint = _trend_setup_fingerprint(setup, item)
    previous_fingerprint = str(item.get("last_ai_setup_fingerprint") or previous.get("fingerprint") or "")
    last_verdict = str(item.get("last_ai_verdict") or "").upper().strip()
    last_reject_scope = str(item.get("last_ai_reject_scope") or "").upper().strip()
    last_reject_reason_type = str(item.get("last_ai_reject_reason_type") or "").upper().strip()
    last_reviewed_at = _parse_time(item.get("last_ai_review_at") or item.get("last_ai_reviewed_at"))
    same_verdict_count = max(0, int(item.get("last_ai_same_verdict_count") or 0))
    if not previous:
        symbol = str(setup.get("symbol") or item.get("symbol") or "")
        side = str(setup.get("side") or item.get("side") or "").lower()
        memory = _load_trend_setup_class_memory(config, symbol, side, current_setup_class) if symbol and side else {}
        memory_last_reviewed_at = _parse_time(memory.get("last_review_at"))
        memory_verdict = str(memory.get("last_verdict") or "").upper().strip()
        memory_count = max(0, int(memory.get("same_class_review_count") or 0))
        daily_budget = max(1, int(internal.get("trend_setup_review_daily_budget_per_class", 3) or 3))
        if memory_count >= daily_budget:
            return {
                "changed": False,
                "reason": f"setup_class_daily_budget_reached:{current_setup_class}:{memory_count}/{daily_budget}",
                "signature": current,
                "fingerprint": current_fingerprint,
                "setup_class": current_setup_class,
            }
        if memory_last_reviewed_at is not None and memory_verdict == "REVIEW":
            if memory_count <= 1:
                class_cooldown_minutes = review_cooldown_minutes
            elif memory_count == 2:
                class_cooldown_minutes = max(review_cooldown_minutes, int(internal.get("trend_setup_review_same_class_second_cooldown_minutes", 120) or 120))
            else:
                class_cooldown_minutes = max(review_cooldown_minutes, int(internal.get("trend_setup_review_same_class_third_cooldown_minutes", 240) or 240))
            if (datetime.now(timezone.utc) - memory_last_reviewed_at) < timedelta(minutes=class_cooldown_minutes):
                remaining_minutes = int((timedelta(minutes=class_cooldown_minutes) - (datetime.now(timezone.utc) - memory_last_reviewed_at)).total_seconds() // 60)
                return {
                    "changed": False,
                    "reason": f"setup_class_cooldown_active:{current_setup_class}:{remaining_minutes}m",
                    "signature": current,
                    "fingerprint": current_fingerprint,
                    "setup_class": current_setup_class,
                }
        return {
            "changed": True,
            "reason": "first_ai_review_for_watch_item",
            "signature": current,
            "fingerprint": current_fingerprint,
            "setup_class": current_setup_class,
        }
    if same_fingerprint_block_enabled and previous_fingerprint and current_fingerprint == previous_fingerprint:
        return {
            "changed": False,
            "reason": "setup_fingerprint_unchanged",
            "signature": current,
            "fingerprint": current_fingerprint,
            "setup_class": current_setup_class,
        }
    reasons: list[str] = []
    price_reasons: list[str] = []
    for key in ("entry_action", "setup_state", "entry_type"):
        if str(current.get(key)) != str(previous.get(key)):
            reasons.append(f"{key}_changed")
    if _pct_delta(current.get("entry_price"), previous.get("entry_price")) >= price_delta_threshold:
        price_reasons.append("entry_price_changed")
    if _pct_delta(current.get("stop_loss"), previous.get("stop_loss")) >= price_delta_threshold:
        price_reasons.append("stop_loss_changed")
    if _pct_delta(current.get("take_profit"), previous.get("take_profit")) >= price_delta_threshold:
        price_reasons.append("take_profit_changed")
    trend_score_delta = _float(current.get("trend_score")) - _float(previous.get("trend_score"))
    entry_readiness_delta = _float(current.get("entry_readiness_score")) - _float(previous.get("entry_readiness_score"))
    if trend_score_delta >= trend_score_delta_threshold:
        reasons.append("trend_score_improved")
    if entry_readiness_delta >= score_delta_threshold:
        reasons.append("entry_readiness_improved")
    previous_warnings = set(previous.get("warnings") or [])
    current_warnings = set(current.get("warnings") or [])
    if "no_chase_entry" in previous_warnings and "no_chase_entry" not in current_warnings:
        reasons.append("no_chase_cleared")
    if previous_setup_class and current_setup_class != previous_setup_class:
        if _trend_setup_quality_rank(current_setup_class) > _trend_setup_quality_rank(previous_setup_class):
            reasons.append(f"setup_class_improved:{previous_setup_class}->{current_setup_class}")
        else:
            price_reasons.append(f"setup_class_changed:{previous_setup_class}->{current_setup_class}")
    if price_reasons and not reasons:
        reasons.append("price_changed_only")
        reasons.extend(price_reasons)
    if reasons and reasons[0] == "price_changed_only":
        reasons = []
    if not reasons and last_reviewed_at is not None and last_verdict:
        now = datetime.now(timezone.utc)
        verdict_key = last_verdict
        if last_verdict == "REJECT":
            verdict_key = f"{last_verdict}:{last_reject_scope}:{last_reject_reason_type}"
        cooldown_minutes = review_cooldown_minutes
        if last_verdict == "APPROVE":
            cooldown_minutes = approve_cooldown_minutes
        elif last_verdict == "REVIEW":
            cooldown_minutes = review_cooldown_minutes
        elif last_verdict == "REJECT":
            if last_reject_scope == "WATCHLIST_REMOVE" or last_reject_reason_type in {"TREND_INVALID", "LIQUIDITY_RISK", "MARKET_CONFLICT", "SYSTEM_RISK"}:
                cooldown_minutes = reject_remove_cooldown_minutes
            else:
                cooldown_minutes = reject_setup_cooldown_minutes
        if same_verdict_count >= 2:
            cooldown_minutes = max(cooldown_minutes, reject_remove_cooldown_minutes if last_verdict == "REJECT" else review_cooldown_minutes * 2)
        if (now - last_reviewed_at) < timedelta(minutes=cooldown_minutes):
            remaining_minutes = int((timedelta(minutes=cooldown_minutes) - (now - last_reviewed_at)).total_seconds() // 60)
            return {
                "changed": False,
                "reason": f"verdict_cooldown_active:{verdict_key}:{remaining_minutes}m",
                "signature": current,
                "fingerprint": current_fingerprint,
                "setup_class": current_setup_class,
            }
    if not reasons:
        symbol = str(setup.get("symbol") or item.get("symbol") or "")
        side = str(setup.get("side") or item.get("side") or "").lower()
        memory = _load_trend_setup_class_memory(config, symbol, side, current_setup_class) if symbol and side else {}
        memory_last_reviewed_at = _parse_time(memory.get("last_review_at"))
        memory_verdict = str(memory.get("last_verdict") or "").upper().strip()
        memory_count = max(0, int(memory.get("same_class_review_count") or 0))
        daily_budget = max(1, int(internal.get("trend_setup_review_daily_budget_per_class", 3) or 3))
        if memory_count >= daily_budget:
            return {
                "changed": False,
                "reason": f"setup_class_daily_budget_reached:{current_setup_class}:{memory_count}/{daily_budget}",
                "signature": current,
                "fingerprint": current_fingerprint,
                "setup_class": current_setup_class,
            }
        if memory_last_reviewed_at is not None and memory_verdict == "REVIEW":
            if memory_count <= 1:
                class_cooldown_minutes = review_cooldown_minutes
            elif memory_count == 2:
                class_cooldown_minutes = max(review_cooldown_minutes, int(internal.get("trend_setup_review_same_class_second_cooldown_minutes", 120) or 120))
            else:
                class_cooldown_minutes = max(review_cooldown_minutes, int(internal.get("trend_setup_review_same_class_third_cooldown_minutes", 240) or 240))
            if (datetime.now(timezone.utc) - memory_last_reviewed_at) < timedelta(minutes=class_cooldown_minutes):
                remaining_minutes = int((timedelta(minutes=class_cooldown_minutes) - (datetime.now(timezone.utc) - memory_last_reviewed_at)).total_seconds() // 60)
                return {
                    "changed": False,
                    "reason": f"setup_class_cooldown_active:{current_setup_class}:{remaining_minutes}m",
                    "signature": current,
                    "fingerprint": current_fingerprint,
                    "setup_class": current_setup_class,
                }
    return {
        "changed": bool(reasons),
        "reason": ",".join(reasons) if reasons else "setup_unchanged",
        "signature": current,
        "fingerprint": current_fingerprint,
        "setup_class": current_setup_class,
    }

def _watchlist_review_notification_context(config: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    expires_at = _parse_time(item.get("expires_at"))
    remaining_minutes = 0
    if expires_at is not None:
        remaining_minutes = max(0, int((expires_at - now).total_seconds() // 60))
    internal = config.get("ai", {}).get("internal", {}) if isinstance(config.get("ai"), dict) else {}
    extension_minutes = max(0, int(internal.get("trend_watchlist_ai_review_extend_minutes", 30) or 0))
    will_extend = extension_minutes > 0 and (expires_at is None or expires_at < now + timedelta(minutes=extension_minutes))
    return {
        "watchlist_remaining_minutes": remaining_minutes,
        "watchlist_ai_review_extend_minutes": extension_minutes if will_extend else 0,
    }

def _ai_review_should_extend_watchlist(ai_review: dict[str, Any]) -> bool:
    decision = str(ai_review.get("decision") or "").upper()
    if decision == "APPROVE":
        return True
    if decision != "REVIEW":
        return False
    grade = str(ai_review.get("setup_grade") or "").upper()
    if grade in {"S", "A", "B"}:
        return True
    if bool(ai_review.get("allow_recheck_if_setup_changes")):
        return True
    return _float(ai_review.get("entry_quality")) >= 70.0 or _float(ai_review.get("continuation_score")) >= 65.0

def _ai_review_removes_watchlist_pair(ai_review: dict[str, Any]) -> bool:
    decision = str(ai_review.get("decision") or "").upper()
    if decision != "REJECT":
        return False
    reject_scope = str(ai_review.get("reject_scope") or "").upper()
    reject_reason_type = str(ai_review.get("reject_reason_type") or "").upper()
    remove_reasons = {"TREND_INVALID", "RR_BAD", "LIQUIDITY_RISK", "MARKET_CONFLICT", "SYSTEM_RISK"}
    return reject_scope == "WATCHLIST_REMOVE" or reject_reason_type in remove_reasons

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
    previous_verdict = str(item.get("last_ai_verdict") or "").upper().strip()
    previous_verdict_count = int(item.get("last_ai_same_verdict_count") or 0)
    decision = str(ai_review.get("decision") or "").upper()
    reject_scope = str(ai_review.get("reject_scope") or "").upper()
    reject_reason_type = str(ai_review.get("reject_reason_type") or "").upper()
    setup_class = _trend_setup_class(setup, item)
    should_remove = _ai_review_removes_watchlist_pair(ai_review) or (decision == "REJECT" and previous_reject_count >= 1)
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
        _save_trend_setup_class_memory(
            config,
            symbol,
            side,
            setup_class,
            {
                "symbol": symbol,
                "side": side,
                "setup_class": setup_class,
                "last_review_at": datetime.now(timezone.utc).isoformat(),
                "last_verdict": decision,
                "same_class_review_count": 0,
                "last_setup_signature": signature,
                "last_ai_reason": ai_review.get("reason"),
                "last_ai_grade": ai_review.get("setup_grade"),
                "last_ai_gpt_confidence": ai_review.get("gpt_confidence"),
                "reject_scope": reject_scope,
                "reject_reason_type": reject_reason_type,
            },
        )
        return
    item["last_ai_review_at"] = datetime.now(timezone.utc).isoformat()
    item["last_ai_decision"] = ai_review.get("decision")
    item["last_ai_verdict"] = str(ai_review.get("decision") or "").upper().strip()
    item["last_ai_reject_scope"] = str(ai_review.get("reject_scope") or "").upper().strip()
    item["last_ai_reject_reason_type"] = str(ai_review.get("reject_reason_type") or "").upper().strip()
    item["last_ai_setup_signature"] = signature
    item["last_ai_setup_fingerprint"] = _trend_setup_fingerprint(setup, item)
    item["last_ai_setup_class"] = setup_class
    item["last_ai_entry_action"] = setup.get("entry_action")
    item["last_ai_setup_state"] = setup.get("setup_state")
    now = datetime.now(timezone.utc)
    extension_minutes = max(
        0,
        int(
            (
                config.get("ai", {}).get("internal", {})
                if isinstance(config.get("ai"), dict)
                else {}
            ).get("trend_watchlist_ai_review_extend_minutes", 30)
            or 0
        ),
    )
    if extension_minutes > 0 and _ai_review_should_extend_watchlist(ai_review):
        current_expires_at = _parse_time(item.get("expires_at"))
        extended_expires_at = now + timedelta(minutes=extension_minutes)
        if current_expires_at is None or current_expires_at < extended_expires_at:
            item["expires_at"] = extended_expires_at.isoformat()
            item["ai_review_extended_at"] = now.isoformat()
            item["ai_review_extend_minutes"] = extension_minutes
    item["last_reject_scope"] = reject_scope
    item["last_reject_reason_type"] = reject_reason_type
    item["allow_recheck_if_setup_changes"] = bool(ai_review.get("allow_recheck_if_setup_changes"))
    item["last_ai_same_verdict_count"] = previous_verdict_count + 1 if previous_verdict == decision else 1
    if decision == "REJECT":
        item["status"] = "rejected_wait_new_setup"
        item["reject_count"] = previous_reject_count + 1
    elif decision in {"APPROVE", "REVIEW"}:
        item["reject_count"] = 0
    items[key] = item
    state["items"] = items
    set_journal_state(config, TREND_WATCHLIST_STATE_KEY, json.dumps(to_jsonable(state), ensure_ascii=False))
    memory = _load_trend_setup_class_memory(config, symbol, side, setup_class)
    previous_memory_verdict = str(memory.get("last_verdict") or "").upper().strip()
    previous_memory_count = int(memory.get("same_class_review_count") or 0)
    if decision == "REVIEW" and previous_memory_verdict == "REVIEW":
        same_class_review_count = previous_memory_count + 1
    elif decision == "REVIEW":
        same_class_review_count = 1
    else:
        same_class_review_count = 0
    _save_trend_setup_class_memory(
        config,
        symbol,
        side,
        setup_class,
        {
            "symbol": symbol,
            "side": side,
            "setup_class": setup_class,
            "last_review_at": item["last_ai_review_at"],
            "last_verdict": decision,
            "same_class_review_count": same_class_review_count,
            "last_setup_signature": signature,
            "last_ai_reason": ai_review.get("reason"),
            "last_ai_grade": ai_review.get("setup_grade"),
            "last_ai_gpt_confidence": ai_review.get("gpt_confidence"),
        },
    )

def run_trend_auto_shadow_reviews(config: dict[str, Any], watchlist: dict[str, Any]) -> dict[str, Any]:
    internal = config.get("ai", {}).get("internal", {}) if isinstance(config.get("ai"), dict) else {}
    if not bool(internal.get("trend_auto_shadow_review_enabled", True)):
        return {"enabled": False, "created": 0, "items": []}
    limit = max(1, int(internal.get("trend_auto_shadow_review_limit", 5) or 5))
    call_ai = bool(internal.get("trend_setup_review_ai_enabled", False))
    items = watchlist.get("items") if isinstance(watchlist.get("items"), dict) else {}
    reviewed: list[dict[str, Any]] = []
    def _review_priority(row: dict[str, Any]) -> tuple[float, float]:
        if str(row.get("status") or "") == "priority_rewatch" or str(row.get("source") or "") == "approved_hold_returned":
            return (3.0, _float(row.get("trend_score")))
        if str(row.get("watch_type") or "") == "post_move":
            return (1.0, _float(row.get("trend_score")))
        return (2.0, _float(row.get("trend_score")))

    for item in sorted(items.values(), key=_review_priority, reverse=True):
        if len(reviewed) >= limit:
            break
        if not isinstance(item, dict):
            continue
        if str(item.get("watch_type") or "") == "post_move" and not bool(item.get("ai_ready")):
            reviewed.append({
                "symbol": item.get("symbol"),
                "side": item.get("side"),
                "status": "skipped",
                "reason": "post_move_waiting_retest_or_reversal",
                "entry_action": item.get("entry_action"),
            })
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
            result = build_trend_setup_review_flow(
                config,
                row,
                call_ai=call_ai,
                notify_telegram=call_ai,
                notification_context=_watchlist_review_notification_context(config, item),
            )
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
    notification_context: dict[str, Any] | None = None,
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
        ai_review = review_setup_with_mini(
            config,
            setup,
            payload,
            notify_telegram=notify_telegram,
            notification_context=notification_context,
        )
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
    capital_plan = risk_capital.get("capital") if isinstance(risk_capital.get("capital"), dict) else {}
    intent = activation.get("trade_intent") if isinstance(activation.get("trade_intent"), dict) else {}
    if intent and capital_plan:
        intent.update(
            {
                "margin_usdt": capital_plan.get("margin_usdt"),
                "leverage": capital_plan.get("leverage"),
                "notional_usdt": capital_plan.get("notional_usdt"),
                "quantity_estimate": capital_plan.get("quantity_estimate"),
                "risk_usdt": capital_plan.get("risk_usdt"),
                "expected_profit_usdt": capital_plan.get("expected_profit_usdt"),
                "capital_allowed": capital_plan.get("allowed"),
                "capital_reason": capital_plan.get("reason"),
            }
        )
        activation["trade_intent"] = intent
        activation["capital_plan"] = capital_plan
    position_review = build_position_review_shadow(setup, ai_review, activation)
    trade_memory_plan = build_trade_memory_plan_shadow(setup, activation)
    pool_reduction_plan = build_pool_reduction_plan_shadow()
    pending_state = upsert_trend_pending_plan(config, activation)
    approved_hold_queue = None
    if str(ai_review.get("decision") or "").upper() == "APPROVE" and not bool((risk_capital.get("capital") or {}).get("approved")):
        try:
            approved_hold_queue = upsert_trend_approved_hold_queue(
                config,
                setup=setup,
                ai_review=ai_review,
                activation=activation,
                risk_capital=risk_capital,
            )
        except Exception as exc:
            approved_hold_queue = {"error": str(exc)}
            LOGGER.warning("Skipping approved hold queue upsert after error: %s", exc)
    result = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "phase_completion": {
            "phase_1_analysis_result_evidence": 100,
            "phase_2_trade_intent_shadow": 100,
            "phase_3_strategy_selector_shadow": 100,
            "phase_4_lifecycle_reads_strategy": 100,
            "phase_5_trend_scan_watchlist": 100,
            "phase_5_risk_capital_reads_intent": 100,
            "phase_6_code_based_entry_builder": 100,
            "phase_6_position_review_good_exit": 100,
            "phase_7_ai_setup_review": 100,
            "phase_7_pool_reduction_plan": 100,
            "phase_8_pending_order_trade_intent_activation": 100,
            "phase_8_trade_memory_plan": 100,
        },
        "execution_guard": {
            "enabled_for_execution": False,
            "reason": "Architecture is complete; real order execution remains behind explicit execution flags.",
        },
        "setup_proposal": setup,
        "ai_review": ai_review,
        "activation": activation,
        "risk_capital_shadow": risk_capital,
        "position_review_shadow": position_review,
        "pool_reduction_plan_shadow": pool_reduction_plan,
        "trade_memory_plan_shadow": trade_memory_plan,
        "pending_state": pending_state,
        "approved_hold_queue": approved_hold_queue,
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
        snapshot["approved_hold_recheck"] = recheck_trend_approved_hold_queue(config, now=now)
        snapshot["approved_hold_queue"] = process_trend_approved_hold_queue(config, now=now)
        if (snapshot["approved_hold_queue"].get("processed") or []):
            raw_watchlist = get_journal_state(config, TREND_WATCHLIST_STATE_KEY)
            watchlist = json.loads(raw_watchlist or "{}")
    except Exception as exc:
        snapshot["approved_hold_queue"] = {"error": str(exc)}
        LOGGER.warning("Skipping approved hold queue processing after error: %s", exc)
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
