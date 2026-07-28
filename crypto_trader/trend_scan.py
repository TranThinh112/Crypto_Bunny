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
        symbols.append(
            {
                "symbol": symbol,
                "trend_side": trend_side,
                "trend_score": trend_score,
                "long_score": long_score,
                "short_score": short_score,
                "frame_count": len(frames),
                "latest_at": latest_at or None,
                "frames": frames,
            }
        )
    symbols.sort(key=lambda item: (_float(item.get("trend_score")), str(item.get("latest_at") or "")), reverse=True)
    strong_threshold = _float(internal.get("trend_scan_strong_threshold"), 65.0)
    strong = [item for item in symbols if _float(item.get("trend_score")) >= strong_threshold and item.get("trend_side") in {"long", "short"}]
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
    items = state.get("items") if isinstance(state.get("items"), dict) else {}
    next_items: dict[str, dict[str, Any]] = {}
    expired: list[dict[str, Any]] = []
    for key, item in items.items():
        if not isinstance(item, dict):
            continue
        expires_at = _parse_time(item.get("expires_at"))
        if expires_at and expires_at > now and _float(item.get("trend_score")) >= 60:
            next_items[str(key)] = item
        else:
            expired.append(
                {
                    "key": key,
                    "symbol": item.get("symbol"),
                    "side": item.get("side"),
                    "expired_at": now.isoformat(),
                    "previous_status": item.get("status"),
                    "reason": "ttl_expired_or_trend_score_below_60",
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
        existing = next_items.get(key, {})
        ttl = _watch_ttl_minutes(config, trend)
        next_items[key] = {
            **existing,
            "symbol": symbol,
            "side": side,
            "status": "watching",
            "source": "trend_scan",
            "first_seen_at": existing.get("first_seen_at") or now.isoformat(),
            "updated_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=ttl)).isoformat(),
            "trend_score": trend.get("trend_score"),
            "long_score": trend.get("long_score"),
            "short_score": trend.get("short_score"),
            "frame_count": trend.get("frame_count"),
            "frames": trend.get("frames"),
            "last_reason": "trend_score_above_threshold",
        }
    payload = {
        "updated_at": now.isoformat(),
        "count": len(next_items),
        "items": next_items,
        "expired": expired[-50:],
    }
    set_journal_state(config, TREND_WATCHLIST_STATE_KEY, json.dumps(to_jsonable(payload), ensure_ascii=False))
    return payload


def _candidate_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
    return payload if isinstance(payload, dict) else {}


def _candidate_price(payload: dict[str, Any]) -> float:
    return _float(payload.get("entry") or payload.get("price") or payload.get("last"))


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

def _support_resistance_context(payload: dict[str, Any], frame: dict[str, Any]) -> dict[str, Any]:
    support_distance = _float(frame.get("support_distance_pct"), _float(payload.get("support_distance_pct"), 0.0))
    resistance_distance = _float(frame.get("resistance_distance_pct"), _float(payload.get("resistance_distance_pct"), 0.0))
    range_position = _float(frame.get("range_position"), _float(payload.get("range_position"), 0.5))
    return {
        "support_distance_pct": round(support_distance, 4),
        "resistance_distance_pct": round(resistance_distance, 4),
        "range_position": round(range_position, 4),
        "near_support": 0 <= support_distance <= 1.5,
        "near_resistance": 0 <= resistance_distance <= 1.5,
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
    sr_context = _support_resistance_context(payload, frame)
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
    risk_pct = max(0.6, min(3.5, atr_pct * (1.1 if entry_type == "breakout" else 0.9)))
    rr = 1.75 if entry_type in {"pullback", "continuation"} else 1.5
    if side == "long":
        stop_loss = entry * (1.0 - risk_pct / 100.0)
        take_profit = entry + (entry - stop_loss) * rr
        invalid_price = stop_loss
    elif side == "short":
        stop_loss = entry * (1.0 + risk_pct / 100.0)
        take_profit = entry - (stop_loss - entry) * rr
        invalid_price = stop_loss
    else:
        stop_loss = 0.0
        take_profit = 0.0
        invalid_price = 0.0
    warnings: list[str] = []
    if overextended:
        warnings.append("overextended_risk")
    if volume_ratio < 0.7:
        warnings.append("weak_volume")
    if alignment["opposite_count"] >= 2:
        warnings.append("lower_timeframe_misalignment")
    if _float(payload.get("risk_reward"), rr) < 1.3:
        warnings.append("risk_reward_too_low")
    if entry <= 0 or side not in {"long", "short"}:
        warnings.append("missing_entry_or_side")
    hard_warnings = {"missing_entry_or_side", "risk_reward_too_low"}
    setup_state = "blocked" if any(item in hard_warnings for item in warnings) else "ready_for_ai_review" if not overextended else "review_only"
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
        "invalid_price": round(invalid_price, 8),
        "overextended": overextended,
        "overextended_score": round(overextended_score, 2),
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
            "Do not change entry_price, stop_loss, take_profit, or risk_reward.",
        ],
        "expected_json": {
            "decision": "APPROVE|REJECT|REVIEW",
            "setup_grade": "S|A|B|C|D",
            "entry_quality": "number 0-100",
            "continuation_score": "number 0-100",
            "pending_order_allowed": "boolean",
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
        notify_telegram=notify_telegram,
    )
    parsed = dict(response.get("parsed") or {})
    parsed.update(
        {
            "model_version": internal_config.get("model", "gpt-5.4-mini"),
            "prompt_version": "trend-setup-review-v1",
            "latency_ms": response.get("latency_ms"),
        }
    )
    return normalize_ai_setup_review(parsed, setup)

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
    pending_allowed = bool(review.get("pending_order_allowed"))
    if decision == "APPROVE":
        pending_allowed = False
    if decision == "REJECT":
        pending_allowed = False
    return {
        **review,
        "decision": decision,
        "setup_grade": grade,
        "entry_quality": round(_clamp(_float(review.get("entry_quality"), _float(setup.get("pullback_quality"), 0.0))), 2),
        "continuation_score": round(_clamp(_float(review.get("continuation_score"), _float(setup.get("breakout_quality"), 0.0))), 2),
        "pending_order_allowed": pending_allowed,
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
    pending_state = upsert_trend_pending_plan(config, activation)
    result = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "phase_completion": {
            "phase_5_trend_watchlist": 100,
            "phase_6_entry_builder": 100,
            "phase_7_ai_setup_review": 100 if call_ai else 50,
            "phase_8_intent_activation": 100,
        },
        "setup_proposal": setup,
        "ai_review": ai_review,
        "activation": activation,
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
    try:
        watchlist = update_trend_watchlist(config, snapshot, now=now)
    except Exception as exc:
        LOGGER.warning("Skipping trend watchlist update after storage error: %s", exc)
        watchlist = {"error": str(exc)}
    snapshot["watchlist"] = {
        "count": watchlist.get("count"),
        "updated_at": watchlist.get("updated_at"),
        "expired_count": len(watchlist.get("expired") or []) if isinstance(watchlist.get("expired"), list) else None,
    }
    slot_id = str(snapshot.get("slot_id") or "")
    if not force:
        try:
            if get_journal_state(config, TREND_SCAN_LAST_SLOT_KEY) == slot_id:
                return {"ok": True, "skipped": True, "reason": "slot_already_logged", "slot_id": slot_id}
        except Exception as exc:
            LOGGER.warning("Skipping trend scan last-slot read after storage error: %s", exc)
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
