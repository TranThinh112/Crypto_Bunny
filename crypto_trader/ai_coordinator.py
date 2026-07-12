from __future__ import annotations

import json
import re
import urllib.error
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from .codex_features import (
    build_market_prompt_dto,
    build_prompt,
    call_openai_json,
    get_bunny_health_state,
    record_ai_call_event,
    get_trading_system_state,
)
from .lc_pipeline import (
    lc_pipeline_four_hour_symbols,
    lc_pipeline_internal_symbols,
    lc_pipeline_mini_pool,
    lc_pipeline_pool_rows,
    latest_lc_pipeline_four_hour_event,
    latest_lc_pipeline_mini_scan,
    notify_mini_pool_summary,
    save_lc_pipeline_mini_scan,
)
from .market import fetch_market_snapshots, fetch_top_volume_symbols, prefetch_market_data
from .market_guard import market_guard_symbol_layers
from .models import RiskCheck, TradeCandidate
from .news import collect_news
from .sizing import apply_position_sizing
from .storage import is_retryable_storage_error, list_pending_orders, recent_market_scan_memory
from .strategy import build_candidates, enrich_quantities


_PENDING_STATUS_PRIORITY = {
    "LC_OKX": 0,
    "WAIT_SLOT": 1,
    "OPEN": 2,
}
OKX_REVIEW_CACHE_STATE_KEY = "okx_review_cache"


def _ordered_unique_symbols(symbols: list[Any]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for symbol in symbols:
        clean = str(symbol or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        ordered.append(clean)
    return ordered


def _storage_warning(label: str, exc: Exception) -> str:
    return f"{label} unavailable: {exc}"


def _block_candidates_for_storage_hold(candidates: list[TradeCandidate], reason: str) -> None:
    for candidate in candidates:
        candidate.margin_usdt = 0.0
        candidate.order_usdt = 0.0
        candidate.recovery_margin_usdt = None
        candidate.warnings.append(reason)


def ai_config(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("ai", {})


def ai_enabled(config: dict[str, Any]) -> bool:
    return bool(ai_config(config).get("enabled", True))


def _okx_review_reject_reuse_minutes(config: dict[str, Any]) -> int:
    okx_config = ai_config(config).get("okx", {})
    return max(0, int(okx_config.get("reject_reuse_minutes", 15) or 15))


def _okx_review_cache_key(candidate: TradeCandidate, route: str) -> str:
    return "|".join(
        [
            str(route or "").strip().lower(),
            str(candidate.symbol or "").strip().upper(),
            str(candidate.side or "").strip().upper(),
        ]
    )


def _load_okx_review_cache(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    try:
        from .storage import get_journal_state

        raw = get_journal_state(config, OKX_REVIEW_CACHE_STATE_KEY)
    except Exception:
        return {}
    try:
        payload = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): value
        for key, value in payload.items()
        if isinstance(key, str) and isinstance(value, dict)
    }


def _save_okx_review_cache(config: dict[str, Any], cache: dict[str, dict[str, Any]]) -> None:
    try:
        from .storage import set_journal_state

        set_journal_state(config, OKX_REVIEW_CACHE_STATE_KEY, json.dumps(cache, ensure_ascii=False))
    except Exception:
        return


def _recent_rejected_okx_review(
    config: dict[str, Any],
    candidate: TradeCandidate,
    *,
    route: str,
) -> dict[str, Any] | None:
    if route != "lc_okx_setup_review":
        return None
    ttl_minutes = _okx_review_reject_reuse_minutes(config)
    if ttl_minutes <= 0:
        return None
    cache = _load_okx_review_cache(config)
    cache_key = _okx_review_cache_key(candidate, route)
    entry = cache.get(cache_key)
    if not isinstance(entry, dict):
        return None
    reviewed_at = _parse_time(entry.get("reviewed_at") or entry.get("created_at"))
    if reviewed_at is None or (datetime.now(timezone.utc) - reviewed_at) > timedelta(minutes=ttl_minutes):
        cache.pop(cache_key, None)
        _save_okx_review_cache(config, cache)
        return None
    if bool(entry.get("approved")):
        return None
    return {
        **entry,
        "cached": True,
        "cache_reason": f"Reused rejected 5.5 review for {ttl_minutes} minute(s)",
    }


def _remember_okx_review_cache(
    config: dict[str, Any],
    candidate: TradeCandidate,
    *,
    route: str,
    decision: dict[str, Any],
) -> None:
    if route != "lc_okx_setup_review":
        return
    cache = _load_okx_review_cache(config)
    cache_key = _okx_review_cache_key(candidate, route)
    if bool(decision.get("approved")):
        if cache.pop(cache_key, None) is not None:
            _save_okx_review_cache(config, cache)
        return
    cache[cache_key] = {
        "approved": bool(decision.get("approved")),
        "decision": str(decision.get("decision") or ("approve" if decision.get("approved") else "reject")),
        "reason": str(decision.get("reason") or ""),
        "provider": decision.get("provider"),
        "model": decision.get("model"),
        "model_version": decision.get("model_version"),
        "prompt_version": decision.get("prompt_version"),
        "prompt_hash": decision.get("prompt_hash"),
        "experiment_name": decision.get("experiment_name"),
        "pending_memory": decision.get("pending_memory"),
        "raw": decision.get("raw"),
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_okx_review_cache(config, cache)


def pending_priority_key(record: dict[str, Any]) -> tuple[int, float, int]:
    status = str(record.get("status") or "OPEN")
    try:
        win_probability = float(record.get("win_probability_pct") or 0)
    except (TypeError, ValueError):
        win_probability = 0.0
    try:
        row_id = int(record.get("id") or 0)
    except (TypeError, ValueError):
        row_id = 0
    return (_PENDING_STATUS_PRIORITY.get(status, 99), -win_probability, row_id)


def prioritize_pending_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(records, key=pending_priority_key)


def _pending_summary_row(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record.get("id"),
        "lc_id": record.get("journal_id") or record.get("id"),
        "status": record.get("status"),
        "symbol": record.get("symbol"),
        "side": record.get("side"),
        "entry": record.get("entry"),
        "stop_loss": record.get("stop_loss"),
        "take_profit": record.get("take_profit"),
        "quantity": record.get("quantity"),
        "order_usdt": record.get("order_usdt"),
        "confidence": record.get("confidence"),
        "win_probability_pct": record.get("win_probability_pct"),
        "exchange_order_id": record.get("exchange_order_id"),
        "created_at": record.get("created_at"),
        "expires_at": record.get("expires_at"),
    }


def _compact_dict(source: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {key: source.get(key) for key in keys if key in source and source.get(key) is not None}


def _compact_runtime_state(system_state: dict[str, Any], health_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "system": _compact_dict(
            system_state,
            [
                "isRecoveryMode",
                "currentNormalMinRuleScore",
                "currentRecoveryMinRuleScore",
                "maxActiveTrades",
                "activeTrades",
                "dailyOrderCount",
                "dailyPlannedRiskUsdt",
            ],
        ),
        "health": _compact_dict(
            health_state,
            [
                "isWarning",
                "isCritical",
                "status",
                "drawdownPercent",
                "consecutiveLosses",
                "blockedUntil",
            ],
        ),
    }


def _compact_risk_check(risk_check: RiskCheck) -> dict[str, Any]:
    return {
        "passed": risk_check.passed,
        "reasons": risk_check.reasons[:3],
        "warnings": risk_check.warnings[:2],
    }


def _compact_lc_memory(memory: dict[str, Any]) -> dict[str, Any]:
    preferred = memory.get("preferred") if isinstance(memory.get("preferred"), dict) else {}
    orders = memory.get("orders") if isinstance(memory.get("orders"), list) else []
    same_symbol_pending: dict[str, dict[str, Any]] = {}
    for record in orders:
        if not isinstance(record, dict):
            continue
        symbol = str(record.get("symbol") or "")
        if not symbol:
            continue
        bucket = same_symbol_pending.setdefault(symbol, {"count": 0, "sides": set(), "statuses": []})
        bucket["count"] += 1
        side = str(record.get("side") or "")
        status = str(record.get("status") or "")
        if side:
            bucket["sides"].add(side)
        if status and status not in bucket["statuses"]:
            bucket["statuses"].append(status)
    return {
        "pending_total": memory.get("pending_total"),
        "lc_okx_count": memory.get("lc_okx_count"),
        "wait_slot_count": memory.get("wait_slot_count"),
        "local_lc_count": memory.get("local_lc_count"),
        "highest_priority": _compact_dict(preferred, ["status", "lc_id", "symbol", "side", "win_probability_pct"]),
        "same_symbol_pending": {
            symbol: {
                "count": data["count"],
                "sides": sorted(data["sides"]),
                "statuses": data["statuses"][:2],
            }
            for symbol, data in same_symbol_pending.items()
        },
    }


def _round_optional(value: Any, digits: int = 4) -> float | str | None:
    if value is None or value == "":
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return str(value)


def _level_distance_pct(price: Any, level: Any) -> float | None:
    try:
        price_value = float(price)
        level_value = float(level)
    except (TypeError, ValueError):
        return None
    if price_value <= 0:
        return None
    return round((level_value - price_value) / price_value * 100, 3)


def _compact_pattern_summary(patterns: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(patterns, dict):
        return {}
    raw_patterns = patterns.get("patterns")
    return {
        "direction": patterns.get("direction"),
        "trend_context": patterns.get("trend_context"),
        "strongest_pattern": patterns.get("strongest_pattern"),
        "patterns": raw_patterns[:3] if isinstance(raw_patterns, list) else raw_patterns,
        "bullish_score": _round_optional(patterns.get("bullish_score"), 2),
        "bearish_score": _round_optional(patterns.get("bearish_score"), 2),
        "signal_summary": patterns.get("signal_summary"),
    }


def _compact_indicator_summary(summary: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(summary, dict):
        return {}
    last = summary.get("last")
    ema_fast = summary.get("ema_fast")
    ema_slow = summary.get("ema_slow")
    support = summary.get("support")
    resistance = summary.get("resistance")
    compact = {
        "last": _round_optional(last),
        "trend": summary.get("trend"),
        "rsi": _round_optional(summary.get("rsi"), 2),
        "atr_pct": _round_optional(summary.get("atr_pct"), 3),
        "volume_ratio": _round_optional(summary.get("volume_ratio"), 3),
        "spread_pct": _round_optional(summary.get("spread_pct"), 4),
        "ema_gap_pct": _level_distance_pct(ema_slow, ema_fast),
        "price_vs_ema_fast_pct": _level_distance_pct(ema_fast, last),
        "support_distance_pct": _level_distance_pct(last, support),
        "resistance_distance_pct": _level_distance_pct(last, resistance),
    }
    higher_timeframes = summary.get("higher_timeframes")
    if isinstance(higher_timeframes, dict):
        compact["higher_timeframes"] = {
            str(frame): {
                "trend": data.get("trend"),
                "rsi": _round_optional(data.get("rsi"), 2),
                "ema_gap_pct": _round_optional(data.get("ema_gap_pct"), 3),
                "price_vs_ema_slow_pct": _round_optional(data.get("price_vs_ema_slow_pct"), 3),
                "range_position": data.get("range_position"),
            }
            for frame, data in higher_timeframes.items()
            if str(frame).lower() in {"5m", "15m", "1h", "4h"} and isinstance(data, dict)
        }
    candlesticks = summary.get("candlestick_patterns")
    if isinstance(candlesticks, dict):
        compact["candlestick_patterns"] = {
            str(frame): _compact_pattern_summary(data)
            for frame, data in candlesticks.items()
            if str(frame).lower() in {"5m", "15m", "1h", "4h"} and isinstance(data, dict)
        }
    return {key: value for key, value in compact.items() if value not in (None, {}, [])}


def _compact_okx_indicator_summary(summary: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(summary, dict):
        return {}
    compact = {
        "trend": summary.get("trend"),
        "rsi": _round_optional(summary.get("rsi"), 2),
        "atr_pct": _round_optional(summary.get("atr_pct"), 3),
        "volume_ratio": _round_optional(summary.get("volume_ratio"), 3),
        "spread_pct": _round_optional(summary.get("spread_pct"), 4),
    }
    higher_timeframes = summary.get("higher_timeframes")
    if isinstance(higher_timeframes, dict):
        compact["higher_timeframes"] = {
            str(frame): _compact_dict(data, ["trend"])
            for frame, data in higher_timeframes.items()
            if str(frame).lower() in {"5m", "15m", "1h", "4h"} and isinstance(data, dict)
        }
    candlesticks = summary.get("candlestick_patterns")
    if isinstance(candlesticks, dict):
        compact["candlestick_patterns"] = {
            str(frame): _compact_dict(data, ["direction", "strongest_pattern"])
            for frame, data in candlesticks.items()
            if str(frame).lower() in {"5m", "15m", "1h", "4h"} and isinstance(data, dict)
        }
    return {key: value for key, value in compact.items() if value not in (None, {}, [])}


def _pattern_supports_side(side: str, direction: Any) -> bool:
    clean = str(direction or "").lower()
    return (side == "long" and clean == "bullish") or (side == "short" and clean == "bearish")


def _pattern_conflicts_side(side: str, direction: Any) -> bool:
    clean = str(direction or "").lower()
    return clean in {"bullish", "bearish"} and not _pattern_supports_side(side, clean)


def _frame_context(candidate: TradeCandidate, frame_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    higher = candidate.higher_timeframes.get(frame_name) if isinstance(candidate.higher_timeframes, dict) else None
    higher_payload = dict(higher) if isinstance(higher, dict) else {}
    indicator = candidate.indicator_summary if isinstance(candidate.indicator_summary, dict) else {}
    indicator_higher = indicator.get("higher_timeframes") if isinstance(indicator.get("higher_timeframes"), dict) else {}
    indicator_frame = indicator_higher.get(frame_name) if isinstance(indicator_higher.get(frame_name), dict) else {}
    merged_frame = {**indicator_frame, **higher_payload}

    pattern = merged_frame.get("candlestick_patterns") if isinstance(merged_frame.get("candlestick_patterns"), dict) else None
    indicator_patterns = indicator.get("candlestick_patterns") if isinstance(indicator.get("candlestick_patterns"), dict) else {}
    if not isinstance(pattern, dict):
        pattern = indicator_patterns.get(frame_name) if isinstance(indicator_patterns.get(frame_name), dict) else {}
    return merged_frame, dict(pattern or {})


def _frame_setup_check(
    candidate: TradeCandidate,
    frame_name: str,
    *,
    acceptable_statuses: set[str],
) -> dict[str, Any]:
    frame, pattern = _frame_context(candidate, frame_name)
    side = str(candidate.side or "").lower()
    trend = str(frame.get("trend") or "").lower()
    pattern_direction = str(pattern.get("direction") or "").lower()
    trend_supports = _side_matches_trend(side, trend)
    trend_conflicts = trend in {"up", "down"} and not trend_supports
    pattern_supports = _pattern_supports_side(side, pattern_direction)
    pattern_conflicts = _pattern_conflicts_side(side, pattern_direction)
    signal_summary = str(pattern.get("signal_summary") or "")
    strongest = pattern.get("strongest_pattern")
    patterns = pattern.get("patterns") if isinstance(pattern.get("patterns"), list) else []

    if trend_supports and pattern_supports:
        status = "confirmed"
    elif trend_conflicts or pattern_conflicts:
        status = "conflict"
    elif trend_supports or pattern_supports:
        status = "supportive"
    elif trend or pattern_direction or signal_summary or patterns:
        status = "neutral"
    else:
        status = "missing"
    return {
        "frame": frame_name,
        "status": status,
        "acceptable": status in acceptable_statuses,
        "trend": trend or None,
        "pattern_direction": pattern_direction or None,
        "strongest_pattern": strongest,
        "signal_summary": signal_summary or None,
        "patterns": patterns[:2],
    }


def _volume_setup_check(candidate: TradeCandidate) -> dict[str, Any]:
    indicator = candidate.indicator_summary if isinstance(candidate.indicator_summary, dict) else {}
    ratio = _round_optional(indicator.get("volume_ratio"), 3)
    try:
        volume_ratio = float(indicator.get("volume_ratio") or 0.0)
    except (TypeError, ValueError):
        volume_ratio = 0.0
    if volume_ratio >= 1.15:
        status = "strong"
        acceptable = True
    elif volume_ratio >= 1.0:
        status = "acceptable"
        acceptable = True
    elif volume_ratio > 0:
        status = "weak"
        acceptable = False
    else:
        status = "missing"
        acceptable = False
    return {
        "status": status,
        "acceptable": acceptable,
        "volume_ratio": ratio,
        "preferred_threshold": 1.15,
        "minimum_threshold": 1.0,
    }


def _risk_reward_setup_check(config: dict[str, Any], candidate: TradeCandidate) -> dict[str, Any]:
    strategy_threshold = float(config.get("strategy", {}).get("min_risk_reward", 1.5) or 1.5)
    review_threshold = float(config.get("pending_orders", {}).get("review", {}).get("min_risk_reward", 1.5) or 1.5)
    threshold = max(strategy_threshold, review_threshold)
    rr = _round_optional(candidate.risk_reward, 2)
    try:
        risk_reward = float(candidate.risk_reward or 0.0)
    except (TypeError, ValueError):
        risk_reward = 0.0
    if risk_reward >= threshold + 0.2:
        status = "strong"
        acceptable = True
    elif risk_reward >= threshold:
        status = "borderline"
        acceptable = True
    elif risk_reward > 0:
        status = "weak"
        acceptable = False
    else:
        status = "missing"
        acceptable = False
    return {
        "status": status,
        "acceptable": acceptable,
        "risk_reward": rr,
        "minimum_threshold": round(threshold, 2),
    }


def _spread_setup_check(config: dict[str, Any], candidate: TradeCandidate) -> dict[str, Any]:
    indicator = candidate.indicator_summary if isinstance(candidate.indicator_summary, dict) else {}
    raw_spread = candidate.spread_pct if candidate.spread_pct is not None else indicator.get("spread_pct")
    max_spread = float(config.get("risk", {}).get("max_spread_pct", 0.15) or 0.15)
    spread_pct = _round_optional(raw_spread, 4)
    try:
        spread_value = float(raw_spread)
    except (TypeError, ValueError):
        spread_value = -1.0
    if spread_value < 0:
        status = "missing"
        acceptable = False
    elif spread_value <= max_spread:
        status = "ok"
        acceptable = True
    else:
        status = "wide"
        acceptable = False
    return {
        "status": status,
        "acceptable": acceptable,
        "spread_pct": spread_pct,
        "max_spread_pct": round(max_spread, 4),
    }


def _news_setup_check(config: dict[str, Any], candidate: TradeCandidate) -> dict[str, Any]:
    threshold = float(config.get("risk", {}).get("news_conflict_threshold", 2.0) or 2.0)
    score = _round_optional(candidate.news_score, 2)
    severity = abs(float(candidate.news_score or 0.0))
    if severity >= threshold:
        status = "conflict"
        acceptable = False
    elif candidate.news_count > 0:
        status = "monitored"
        acceptable = True
    else:
        status = "clean"
        acceptable = True
    return {
        "status": status,
        "acceptable": acceptable,
        "news_score": score,
        "news_count": int(candidate.news_count or 0),
        "conflict_threshold": round(threshold, 2),
    }


def _setup_checks_summary(config: dict[str, Any], candidate: TradeCandidate) -> dict[str, Any]:
    bias_4h = _frame_setup_check(candidate, "4h", acceptable_statuses={"confirmed", "supportive", "neutral"})
    confirm_15m = _frame_setup_check(candidate, "15m", acceptable_statuses={"confirmed", "supportive"})
    confirm_1h = _frame_setup_check(candidate, "1h", acceptable_statuses={"confirmed", "supportive"})
    confirm_5m = _frame_setup_check(candidate, "5m", acceptable_statuses={"confirmed", "supportive"})
    supportive_frames = [
        check["frame"]
        for check in (confirm_15m, confirm_1h, confirm_5m)
        if check["status"] in {"confirmed", "supportive"}
    ]
    conflict_frames = [check["frame"] for check in (confirm_15m, confirm_1h, confirm_5m) if check["status"] == "conflict"]
    missing_frames = [check["frame"] for check in (confirm_15m, confirm_1h, confirm_5m) if check["status"] == "missing"]
    entry_ok = "15m" in supportive_frames and len(supportive_frames) >= 2 and not conflict_frames
    if conflict_frames:
        entry_status = "conflict"
    elif entry_ok:
        entry_status = "confirmed"
    elif supportive_frames:
        entry_status = "partial"
    elif missing_frames:
        entry_status = "missing"
    else:
        entry_status = "weak"
    return {
        "bias_4h": bias_4h,
        "confirm_15m": confirm_15m,
        "confirm_1h": confirm_1h,
        "confirm_5m": confirm_5m,
        "entry_confirmation": {
            "status": entry_status,
            "acceptable": entry_ok,
            "supportive_frames": supportive_frames,
            "conflict_frames": conflict_frames,
            "missing_frames": missing_frames,
        },
        "volume": _volume_setup_check(candidate),
        "risk_reward": _risk_reward_setup_check(config, candidate),
        "spread": _spread_setup_check(config, candidate),
        "news": _news_setup_check(config, candidate),
    }


def _side_matches_trend(side: str, trend: Any) -> bool:
    direction = str(trend or "").lower()
    return (side == "long" and direction == "up") or (side == "short" and direction == "down")


def _mini_pattern_alignment(candidate: TradeCandidate) -> int:
    aligned = 0
    side = str(candidate.side or "").lower()
    frames: list[dict[str, Any]] = []
    for pattern_data in (candidate.candlestick_patterns or {}).values():
        if isinstance(pattern_data, dict):
            frames.append(pattern_data)
    for frame_data in (candidate.higher_timeframes or {}).values():
        patterns = frame_data.get("candlestick_patterns") if isinstance(frame_data, dict) else None
        if isinstance(patterns, dict):
            frames.append(patterns)
    for frame in frames:
        direction = str(frame.get("direction") or "").lower()
        has_signal = bool(frame.get("strongest_pattern") or frame.get("patterns"))
        if has_signal and ((side == "long" and direction == "bullish") or (side == "short" and direction == "bearish")):
            aligned += 1
    return aligned


def _mini_trend_alignment(candidate: TradeCandidate) -> int:
    aligned = 0
    side = str(candidate.side or "").lower()
    if _side_matches_trend(side, (candidate.indicator_summary or {}).get("trend")):
        aligned += 1
    for frame_data in (candidate.higher_timeframes or {}).values():
        if isinstance(frame_data, dict) and _side_matches_trend(side, frame_data.get("trend")):
            aligned += 1
    return aligned


def _mini_indicator_score(candidate: TradeCandidate) -> float:
    indicator = candidate.indicator_summary or {}
    score = 0.0
    score += min(4.0, max(0.0, float(candidate.risk_reward or 0.0))) * 1.8
    score += min(4.0, max(0.0, float(indicator.get("volume_ratio") or 0.0))) * 1.4
    score += min(120.0, max(0.0, float(candidate.rule_score or candidate.confidence or 0.0))) * 0.08
    spread_pct = float(indicator.get("spread_pct") or candidate.spread_pct or 0.0)
    score -= max(0.0, spread_pct) * 25.0
    rsi = indicator.get("rsi")
    try:
        rsi_value = float(rsi)
    except (TypeError, ValueError):
        rsi_value = None
    if rsi_value is not None:
        if candidate.side == "long" and 45.0 <= rsi_value <= 72.0:
            score += 3.0
        elif candidate.side == "short" and 28.0 <= rsi_value <= 55.0:
            score += 3.0
    return score


def _mini_candidate_priority(candidate: TradeCandidate) -> tuple[float, float, float, float, float]:
    trend_alignment = float(_mini_trend_alignment(candidate))
    pattern_alignment = float(_mini_pattern_alignment(candidate))
    win_probability = float(candidate.win_probability_pct or 0.0)
    confidence = float(candidate.confidence or 0.0)
    composite = (
        win_probability * 0.45
        + confidence * 0.12
        + _mini_indicator_score(candidate)
        + trend_alignment * 6.0
        + pattern_alignment * 7.0
    )
    return (
        composite,
        win_probability,
        float(candidate.rule_score or 0.0),
        trend_alignment,
        pattern_alignment,
    )


def _openai_internal_market_scan(
    config: dict[str, Any],
    candidates: list[dict[str, Any]],
    local_result: dict[str, Any],
) -> dict[str, Any]:
    internal_config = ai_config(config).get("internal", {})
    system_state = get_trading_system_state(config)
    health_state = get_bunny_health_state(config)
    runtime_state = _compact_runtime_state(system_state, health_state)
    prompt_package = build_prompt(
        config,
        build_market_prompt_dto(
            candidates=candidates,
            market_snapshot={"localPolicy": local_result},
            trading_system_state=runtime_state["system"],
            trading_health_state=runtime_state["health"],
        ),
        instruction_key="mini-analysis",
        recovery_mode=bool(system_state.get("isRecoveryMode")),
        health_warning=bool(health_state.get("isWarning") or health_state.get("isCritical")),
    )
    response = call_openai_json(
        config,
        internal_config,
        prompt_package,
        model_name=str(internal_config.get("model", "gpt-5.4-mini")),
        purpose="mini_market_scan",
    )
    parsed = dict(response["parsed"])
    parsed.update(
        {
            "prompt_version": prompt_package["prompt_version"],
            "prompt_hash": prompt_package["prompt_hash"],
            "experiment_name": prompt_package["experiment_name"],
            "raw_prompt": prompt_package["messages"][1]["content"],
            "raw_response": response["raw_response"],
            "model_version": str(internal_config.get("model", "gpt-5.4-mini")),
            "prompt_tokens": response.get("prompt_tokens"),
            "completion_tokens": response.get("completion_tokens"),
            "latency_ms": response.get("latency_ms"),
        }
    )
    return parsed


def internal_lc_memory(config: dict[str, Any], *, limit: int = 50) -> dict[str, Any]:
    internal_config = ai_config(config).get("internal", {})
    records = prioritize_pending_records(list_pending_orders(config, status="ACTIVE", limit=limit))
    lc_okx = [record for record in records if str(record.get("status") or "") == "LC_OKX"]
    wait_slot = [record for record in records if str(record.get("status") or "") == "WAIT_SLOT"]
    local = [record for record in records if str(record.get("status") or "") == "OPEN"]
    preferred = records[0] if records else None
    memory = {
        "enabled": ai_enabled(config),
        "agent": "internal",
        "model": internal_config.get("model", "gpt-mini"),
        "provider": internal_config.get("provider", "local_policy"),
        "pending_total": len(records),
        "lc_okx_count": len(lc_okx),
        "wait_slot_count": len(wait_slot),
        "local_lc_count": len(local),
        "priority": ["LC_OKX", "WAIT_SLOT", "OPEN"],
        "preferred": _pending_summary_row(preferred) if preferred else None,
        "orders": [_pending_summary_row(record) for record in records],
    }
    return memory


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def internal_market_scan_interval(config: dict[str, Any]) -> int:
    internal_config = ai_config(config).get("internal", {})
    return max(3600, int(internal_config.get("market_scan_interval_seconds", 14400) or 14400))


def _internal_market_scan_timezone(config: dict[str, Any]) -> timezone:
    internal_config = ai_config(config).get("internal", {})
    name = str(internal_config.get("market_scan_timezone") or config.get("timezone") or "Asia/Ho_Chi_Minh")
    if name in {"Asia/Ho_Chi_Minh", "Asia/Saigon", "UTC+7", "+07:00"}:
        return timezone(timedelta(hours=7))
    return timezone.utc


def _internal_market_scan_slot_start(config: dict[str, Any], now: datetime) -> datetime:
    interval_hours = max(1, int(internal_market_scan_interval(config) // 3600))
    local_now = now.astimezone(_internal_market_scan_timezone(config))
    slot_hour = (local_now.hour // interval_hours) * interval_hours
    local_slot = local_now.replace(hour=slot_hour, minute=0, second=0, microsecond=0)
    return local_slot.astimezone(timezone.utc)


def _internal_market_scan_slot_tolerance_minutes(config: dict[str, Any]) -> int:
    internal_config = ai_config(config).get("internal", {})
    return max(1, min(10, int(internal_config.get("market_scan_slot_tolerance_minutes", 3) or 3)))


def _internal_market_scan_slot_open(config: dict[str, Any], now: datetime) -> bool:
    slot_start = _internal_market_scan_slot_start(config, now)
    elapsed = (now - slot_start).total_seconds()
    return 0 <= elapsed <= _internal_market_scan_slot_tolerance_minutes(config) * 60


def _internal_market_scan_slot_id(config: dict[str, Any], now: datetime) -> str:
    return _internal_market_scan_slot_start(config, now).isoformat()


def next_internal_market_scan_at(config: dict[str, Any], now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if bool(ai_config(config).get("internal", {}).get("market_scan_fixed_schedule", True)):
        interval_hours = max(1, int(internal_market_scan_interval(config) // 3600))
        slot_start = _internal_market_scan_slot_start(config, now)
        return slot_start + timedelta(hours=interval_hours)
    latest = latest_internal_market_scan(config)
    created_at = _parse_time((latest or {}).get("created_at"))
    if not created_at:
        return now
    return created_at + timedelta(seconds=internal_market_scan_interval(config))


def latest_internal_market_scan(config: dict[str, Any]) -> dict[str, Any] | None:
    return latest_lc_pipeline_mini_scan(config)


def _current_four_hour_event_for_market_scan(config: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    latest_four_hour = latest_lc_pipeline_four_hour_event(config)
    if not latest_four_hour:
        return None
    four_hour_slot = _parse_time(latest_four_hour.get("slot") or latest_four_hour.get("created_at"))
    current_slot = _internal_market_scan_slot_start(config, now)
    if four_hour_slot is None or four_hour_slot != current_slot:
        return None
    return latest_four_hour


def internal_market_scan_due(config: dict[str, Any], now: datetime | None = None) -> bool:
    if not ai_enabled(config):
        return False
    internal_config = ai_config(config).get("internal", {})
    if not bool(internal_config.get("market_scan_enabled", True)):
        return False
    now = now or datetime.now(timezone.utc)
    if not _internal_market_scan_slot_open(config, now):
        return False
    if _current_four_hour_event_for_market_scan(config, now) is None:
        return False
    latest = latest_internal_market_scan(config)
    created_at = _parse_time((latest or {}).get("created_at"))
    if not created_at:
        return True
    if bool(internal_config.get("market_scan_fixed_schedule", True)):
        slot_start = _internal_market_scan_slot_start(config, now)
        slot_id = _internal_market_scan_slot_id(config, now)
        if latest.get("slot_id") == slot_id:
            return False
        return created_at < slot_start <= now
    return (now - created_at).total_seconds() >= internal_market_scan_interval(config)


def _candidate_market_summary(
    candidate: TradeCandidate,
    scan_memory_by_symbol: dict[str, dict[str, list[dict[str, Any]]]] | None = None,
    *,
    compact: bool = True,
) -> dict[str, Any]:
    def _frame_payload(frame_name: str, pattern_data: dict[str, Any], frame_data: dict[str, Any] | None = None) -> dict[str, Any]:
        frame_data = frame_data or {}
        return {
            "timeframe": str(frame_name),
            "direction": pattern_data.get("direction"),
            "trend_context": pattern_data.get("trend_context"),
            "strongest_pattern": pattern_data.get("strongest_pattern"),
            "signal_summary": pattern_data.get("signal_summary"),
            "reversal_patterns": pattern_data.get("reversal_patterns"),
            "pattern_details": pattern_data.get("pattern_details", [])[:3],
            "trend": frame_data.get("trend"),
            "rsi": frame_data.get("rsi"),
            "ema_gap_pct": frame_data.get("ema_gap_pct"),
            "price_vs_ema_slow_pct": frame_data.get("price_vs_ema_slow_pct"),
            "range_position": frame_data.get("range_position"),
        }

    code_timeframe_analysis: list[dict[str, Any]] = []
    mini_context_4h: dict[str, Any] | None = None
    for frame_name, pattern_data in (candidate.candlestick_patterns or {}).items():
        if not isinstance(pattern_data, dict):
            continue
        payload = _frame_payload(str(frame_name), pattern_data)
        if str(frame_name).lower() == "4h":
            mini_context_4h = payload
        elif str(frame_name).lower() in {"5m", "15m", "1h"}:
            code_timeframe_analysis.append(payload)
    for frame_name, frame_data in (candidate.higher_timeframes or {}).items():
        if not isinstance(frame_data, dict):
            continue
        pattern_data = frame_data.get("candlestick_patterns")
        if not isinstance(pattern_data, dict):
            continue
        payload = _frame_payload(str(frame_name), pattern_data, frame_data)
        if str(frame_name).lower() == "4h":
            mini_context_4h = payload
        elif str(frame_name).lower() in {"5m", "15m", "1h"}:
            code_timeframe_analysis.append(payload)
    symbol_memory = (scan_memory_by_symbol or {}).get(candidate.symbol, {})
    allowed_memory_timeframes = {"5m", "1h", "4h"} if compact else {"1m", "5m", "15m", "1h", "4h"}
    memory_limit = 1 if compact else 3
    rolling_scan_memory = {
        timeframe: entries[:memory_limit]
        for timeframe, entries in symbol_memory.items()
        if timeframe.lower() in allowed_memory_timeframes
    }
    return {
        "symbol": candidate.symbol,
        "side": candidate.side,
        "confidence": candidate.confidence,
        "win_probability_pct": candidate.win_probability_pct,
        "risk_reward": candidate.risk_reward,
        "entry": candidate.entry,
        "stop_loss": candidate.stop_loss,
        "take_profit": candidate.take_profit,
        "spread_pct": candidate.spread_pct,
        "news_score": candidate.news_score,
        "news_count": candidate.news_count,
        "indicator_summary": _compact_indicator_summary(candidate.indicator_summary),
        "code_timeframe_analysis": code_timeframe_analysis,
        "mini_context_4h": mini_context_4h,
        "rolling_scan_memory": rolling_scan_memory,
        "reasons": candidate.reasons[:3] if compact else candidate.reasons[:6],
        "warnings": candidate.warnings[:2] if compact else candidate.warnings[:4],
    }


def _local_market_scan_result(config: dict[str, Any], candidates: list[TradeCandidate], warnings: list[str]) -> dict[str, Any]:
    internal_config = ai_config(config).get("internal", {})
    threshold = float(
        internal_config.get(
            "market_scan_min_win_probability_pct",
            config.get("strategy", {}).get("min_win_probability_pct", 62),
        )
        or 62
    )
    max_symbols = max(1, min(3, int(internal_config.get("market_scan_max_symbols", 3) or 3)))
    ranked = sorted(
        candidates,
        key=_mini_candidate_priority,
        reverse=True,
    )
    qualified = [
        candidate
        for candidate in ranked
        if candidate.win_probability_pct is not None and float(candidate.win_probability_pct) >= threshold
    ][:max_symbols]
    minimum_approved = max(1, min(max_symbols, int(internal_config.get("market_scan_min_approved_symbols", 1) or 1)))
    fallback = ranked[:max_symbols]
    eligible = qualified or fallback[:minimum_approved]
    return {
        "provider": "local_policy",
        "decision": "prefilter",
        "threshold_win_probability_pct": threshold,
        "selection_checks": ["win_rate", "setup_quality", "trend_alignment", "indicator_strength"],
        "qualified_symbols": [candidate.symbol for candidate in qualified],
        "approved_symbols": [candidate.symbol for candidate in eligible],
        "approved_count": len(eligible),
        "candidate_count": len(candidates),
        "warnings": warnings,
    }


def _validated_ai_symbols(review: dict[str, Any], allowed_symbols: set[str], fallback_symbols: list[str], max_symbols: int) -> list[str]:
    raw = review.get("approved_symbols")
    decision = str(review.get("decision") or "").strip().upper()
    if decision in {"NO_TRADE", "NONE", "REJECTED", "REJECT"}:
        return []
    minimum_symbols = 1
    if isinstance(review.get("minimum_approved_symbols"), int):
        minimum_symbols = max(1, int(review.get("minimum_approved_symbols") or 1))
    if not isinstance(raw, list):
        return fallback_symbols[:max(minimum_symbols, 1)]
    symbols: list[str] = []
    for item in raw:
        symbol = str(item or "")
        if symbol in allowed_symbols and symbol not in symbols:
            symbols.append(symbol)
    # An explicit empty list means the AI rejected every setup. Fallback is
    # reserved for malformed responses where approved_symbols is missing.
    return symbols[:max_symbols]


def _mini_ai_reason_vi(review: dict[str, Any], pool_symbols: list[str], selected_symbols: list[str]) -> str:
    rejected = [symbol for symbol in pool_symbols if symbol not in selected_symbols]
    scores = review.get("setup_scores") if isinstance(review.get("setup_scores"), dict) else {}
    score_labels = [
        f"{symbol.split('/')[0]} {scores[symbol]}/100"
        for symbol in rejected
        if symbol in scores
    ]
    if not selected_symbols:
        names = ", ".join(symbol.split("/")[0] for symbol in rejected) or "toàn bộ setup"
        result = f"Mini loại {names}"
        if score_labels:
            result += f" (điểm setup: {', '.join(score_labels)})"
    else:
        kept = ", ".join(symbol.split("/")[0] for symbol in selected_symbols)
        result = f"Mini giữ {kept}"
        if rejected:
            result += "; loại " + ", ".join(symbol.split("/")[0] for symbol in rejected)

    raw_reason = str(review.get("reason") or "").strip()
    lowered = raw_reason.lower()
    reasons: list[str] = []
    if "critical health" in lowered:
        reasons.append("sức khỏe hệ thống đang ở mức nghiêm trọng")
    if "4h absent" in lowered or "missing 4h" in lowered:
        reasons.append("thiếu dữ liệu hoặc xác nhận xu hướng 4h")
    if "5m/1h conflict" in lowered or ("5m" in lowered and "1h" in lowered and "conflict" in lowered):
        reasons.append("xu hướng 5m và 1h xung đột")
    rr_match = re.search(r"\brr\s*([0-9.]+)", raw_reason, flags=re.IGNORECASE)
    if rr_match:
        reasons.append(f"RR {rr_match.group(1)} chưa đủ tốt")
    if "missing volume" in lowered or "volume support" in lowered:
        reasons.append("thiếu volume xác nhận")
    if not reasons:
        reasons.append("AI nội bộ đánh giá setup chưa đủ chất lượng để đi tiếp")
    return result + ". Lý do: " + "; ".join(reasons) + "."


def run_internal_market_scan(config: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    internal_config = ai_config(config).get("internal", {})
    fixed_schedule = bool(internal_config.get("market_scan_fixed_schedule", True))
    slot_id = _internal_market_scan_slot_id(config, now) if fixed_schedule else None
    slot_start = _internal_market_scan_slot_start(config, now) if fixed_schedule else None
    latest = latest_internal_market_scan(config)
    latest_created_at = _parse_time((latest or {}).get("created_at"))
    if (
        fixed_schedule
        and not force
        and latest
        and (latest.get("slot_id") == slot_id or (latest_created_at and slot_start and latest_created_at >= slot_start))
    ):
        return {
            **latest,
            "skipped": True,
            "skip_reason": f"mini scan already ran for slot {slot_id}",
        }
    max_source_symbols = max(1, min(40, int(internal_config.get("market_scan_source_symbols", 40) or 40)))
    max_symbols = max(1, min(3, int(internal_config.get("market_scan_max_symbols", 3) or 3)))
    pending_limit = max(1, min(max_symbols, int(internal_config.get("market_scan_pending_limit", 1) or 1)))
    compact_payload = bool(internal_config.get("compact_ai_payload", True))
    prefetched_market_data = prefetch_market_data(config, require_all_tickers=True)
    base_source_symbols, source_warnings = fetch_top_volume_symbols(config, market_data=prefetched_market_data)
    if base_source_symbols:
        base_source_symbols = base_source_symbols[:max_source_symbols]
        source = "okx_top_volume_24h"
    else:
        base_source_symbols = [str(symbol) for symbol in config.get("strategy", {}).get("symbols", []) if str(symbol)]
        source = "configured_fallback"
    latest_four_hour_symbols = lc_pipeline_four_hour_symbols(config)
    source_symbols = _ordered_unique_symbols(base_source_symbols + latest_four_hour_symbols)
    if latest_four_hour_symbols:
        source = f"{source}+latest_lc_4h"

    warnings: list[str] = list(source_warnings)
    digest = collect_news(config)
    snapshots, market_warnings = fetch_market_snapshots(config, source_symbols, market_data=prefetched_market_data)
    warnings.extend(market_warnings)
    market_layers: dict[str, dict[str, Any]] = {}
    if config.get("market_guard", {}).get("use_memory_in_strategy", True):
        try:
            market_layers = market_guard_symbol_layers(config, source_symbols)
        except Exception as exc:
            warnings.append(f"Market guard memory unavailable: {exc}")
    candidates = build_candidates(config, snapshots, digest, limit=None, market_layers=market_layers)
    try:
        apply_position_sizing(config, candidates)
    except Exception as exc:
        if not is_retryable_storage_error(exc):
            raise
        warnings.append(_storage_warning("Position sizing state", exc))
        _block_candidates_for_storage_hold(
            candidates,
            "Position sizing state unavailable; mini scan is holding new entries until storage recovers",
        )
    warnings.extend(enrich_quantities(config, candidates))
    scan_memory = recent_market_scan_memory(
        config,
        symbols=source_symbols,
        timeframes=["5m", "1h", "4h"] if compact_payload else ["1m", "5m", "15m", "1h", "4h"],
        lookback_hours=12,
        per_symbol_timeframe_limit=1 if compact_payload else 3,
    )
    ranked_candidates = lc_pipeline_mini_pool(config, candidates, limit=max_symbols)
    candidate_summaries = [
        _candidate_market_summary(candidate, scan_memory_by_symbol=scan_memory, compact=compact_payload)
        for candidate in ranked_candidates[:max_symbols]
    ]
    mini_candidate_symbols = [str(item.get("symbol")) for item in candidate_summaries if item.get("symbol")]
    local_result = _local_market_scan_result(config, candidates, warnings)
    has_mini_pool = len(mini_candidate_symbols) > 0
    if mini_candidate_symbols:
        local_result = {
            **local_result,
            "approved_symbols": mini_candidate_symbols,
            "approved_count": len(mini_candidate_symbols),
            "selection_source": "lc_internal_pipeline",
        }
    if not has_mini_pool:
        local_result = {
            **local_result,
            "approved_symbols": [],
            "approved_count": 0,
            "selection_source": "lc_internal_pipeline_waiting",
            "skip_reason": "waiting for at least 1 internal LC candidate, current=0",
        }

    result = {
        "enabled": True,
        "agent": "internal_market_scan",
        "created_at": now.isoformat(),
        "slot_id": slot_id,
        "slot_start": slot_start.isoformat() if slot_start else None,
        "status": "scanning",
        "interval_seconds": internal_market_scan_interval(config),
        "provider": str(internal_config.get("provider", "local_policy") or "local_policy"),
        "model": str(internal_config.get("model", "gpt-5.4-mini")),
        "source": source,
        "source_symbols": source_symbols,
        "source_base_symbols": list(base_source_symbols),
        "source_four_hour_symbols": list(latest_four_hour_symbols),
        "candidate_count": len(candidates),
        "local_policy": local_result,
        "pool_symbols": list(mini_candidate_symbols or []),
        "selected_symbols": list((local_result.get("approved_symbols") or [])[:pending_limit]),
        "approved_symbols": list((local_result.get("approved_symbols") or [])[:pending_limit]),
        "candidates": candidate_summaries[:max_symbols],
        "scan_memory": scan_memory if not compact_payload else {},
        "compact_ai_payload": compact_payload,
        "warnings": warnings[:20],
    }
    result["decision_reason_vi"] = "Mini đang chờ hoàn tất bước chọn cuối cùng."
    saved_result = save_lc_pipeline_mini_scan(config, result)
    result["mini_index"] = saved_result.get("mini_index")

    if has_mini_pool and result["provider"] == "openai" and bool(internal_config.get("market_scan_use_ai", True)):
        try:
            allowed = set(mini_candidate_symbols or local_result.get("approved_symbols") or [])
            ai_review = _openai_internal_market_scan(
                config,
                candidate_summaries[:max_symbols],
                local_result,
            )
            result["ai_review"] = ai_review
            result["selected_symbols"] = _validated_ai_symbols(
                ai_review,
                allowed,
                list(mini_candidate_symbols or local_result.get("approved_symbols") or []),
                pending_limit,
            )
            scores = ai_review.get("setup_scores")
            if isinstance(scores, dict):
                result["setup_scores"] = {
                    str(symbol): scores.get(symbol)
                    for symbol in result["selected_symbols"]
                    if symbol in scores
                }
        except (RuntimeError, urllib.error.URLError, urllib.error.HTTPError, KeyError, json.JSONDecodeError) as exc:
            result["ai_review_error"] = str(exc)
            result["fallback"] = "local_policy"

    result["approved_symbols"] = list(result.get("selected_symbols") or [])
    result["status"] = "done" if has_mini_pool else "waiting_lc"
    result["decision_reason_vi"] = (
        _mini_ai_reason_vi(
            result["ai_review"],
            list(result.get("pool_symbols") or []),
            list(result.get("selected_symbols") or []),
        )
        if result.get("ai_review")
        else (
            "Mini chưa chọn cặp nào vì hiện chưa có LC 4h đủ điều kiện."
            if not result["approved_symbols"]
            else "Mini đã chọn từ nhóm LC 4h hiện tại dựa trên Win Rate, chất lượng setup, xu hướng và chỉ báo."
        )
    )
    saved_result = save_lc_pipeline_mini_scan(config, result)
    notify_mini_pool_summary(
        config,
        lc_pipeline_pool_rows(config, list(saved_result.get("selected_symbols") or [])),
        scan=saved_result,
        slot_id=slot_id,
    )
    return saved_result


def run_internal_market_scan_if_due(config: dict[str, Any]) -> dict[str, Any] | None:
    if internal_market_scan_due(config):
        return run_internal_market_scan(config)
    return latest_internal_market_scan(config)


def internal_market_shortlist(config: dict[str, Any]) -> tuple[list[str], dict[str, Any] | None]:
    internal_config = ai_config(config).get("internal", {})
    if not ai_enabled(config) or not bool(internal_config.get("market_scan_use_shortlist", True)):
        return [], None
    latest = latest_internal_market_scan(config)
    symbols = lc_pipeline_internal_symbols(
        config,
        limit=max(1, min(3, int(internal_config.get("market_scan_max_symbols", 3) or 3))),
    )
    if not latest:
        return symbols, None
    created_at = _parse_time(latest.get("created_at"))
    if not created_at:
        return symbols, None
    max_age = internal_market_scan_interval(config) * 1.5
    if (datetime.now(timezone.utc) - created_at).total_seconds() > max_age:
        return symbols, {**latest, "stale": True}
    return symbols, {**latest, "stale": False}


def should_defer_new_vt_to_internal_lc(config: dict[str, Any], memory: dict[str, Any]) -> bool:
    if not ai_enabled(config):
        return False
    okx_config = ai_config(config).get("okx", {})
    if not bool(okx_config.get("ask_internal_before_entry", True)):
        return False
    return int(memory.get("pending_total") or 0) > 0


def _candidate_summary(candidate: TradeCandidate, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    effective_config = config or {}
    return {
        "symbol": candidate.symbol,
        "side": candidate.side,
        "confidence": candidate.confidence,
        "win_probability_pct": candidate.win_probability_pct,
        "risk_reward": candidate.risk_reward,
        "indicator_summary": _compact_okx_indicator_summary(candidate.indicator_summary),
        "setup_checks": _setup_checks_summary(effective_config, candidate),
    }


def _local_okx_policy(
    config: dict[str, Any],
    candidate: TradeCandidate,
    risk_check: RiskCheck,
    context: dict[str, Any],
    pending_memory: dict[str, Any],
) -> dict[str, Any]:
    route = str(context.get("route") or "new_vt")
    if route == "new_vt" and should_defer_new_vt_to_internal_lc(config, pending_memory):
        preferred = pending_memory.get("preferred") or {}
        return {
            "approved": False,
            "decision": "defer_to_internal_lc",
            "reason": (
                "Internal LC memory has priority before a new VT "
                f"({preferred.get('status') or 'LC'} #{preferred.get('lc_id') or '-'})"
            ),
            "provider": "local_policy",
            "model": ai_config(config).get("okx", {}).get("model", "gpt-5.5"),
            "pending_memory": pending_memory,
        }
    if not risk_check.passed:
        return {
            "approved": False,
            "decision": "risk_blocked",
            "reason": "; ".join(risk_check.reasons[:3]) or "Risk check failed",
            "provider": "local_policy",
            "model": ai_config(config).get("okx", {}).get("model", "gpt-5.5"),
            "pending_memory": pending_memory,
        }
    return {
        "approved": True,
        "decision": "approve",
        "reason": "Risk gate passed and no higher-priority LC blocks this route",
        "provider": "local_policy",
        "model": ai_config(config).get("okx", {}).get("model", "gpt-5.5"),
        "pending_memory": pending_memory,
    }


def _openai_json_decision(
    config: dict[str, Any],
    candidate: TradeCandidate,
    risk_check: RiskCheck,
    context: dict[str, Any],
    pending_memory: dict[str, Any],
    *,
    manual_trigger: bool = False,
    lc_okx_review_once: bool = False,
) -> dict[str, Any]:
    okx_config = ai_config(config).get("okx", {})
    route = str((context or {}).get("route") or "")
    system_state = get_trading_system_state(config)
    health_state = get_bunny_health_state(config)
    runtime_state = _compact_runtime_state(system_state, health_state)
    prompt_package = build_prompt(
        config,
        build_market_prompt_dto(
            candidates=[_candidate_summary(candidate, config=config)],
            market_snapshot={
                "riskCheck": _compact_risk_check(risk_check),
                "route": _compact_dict(context, ["route", "from_status"]),
            },
            trading_system_state=runtime_state["system"],
            trading_health_state=runtime_state["health"],
            open_positions=[],
            recent_trades=[],
            extra={"lcMemory": _compact_lc_memory(pending_memory)},
        ),
        instruction_key="final-decision",
        recovery_mode=bool(system_state.get("isRecoveryMode")),
        health_warning=bool(health_state.get("isWarning") or health_state.get("isCritical")),
    )
    response = call_openai_json(
        config,
        okx_config,
        prompt_package,
        model_name=str(okx_config.get("model", "gpt-5.5")),
        purpose="okx_final_approval",
        route=route,
        manual_trigger=manual_trigger,
        lc_okx_review_once=lc_okx_review_once,
        record_history=route not in {"lc_okx_setup_review", "lc_okx_release"},
        notify_telegram=route not in {"lc_okx_setup_review", "lc_okx_release"},
    )
    decision = dict(response["parsed"])
    result = {
        "approved": bool(decision.get("approved")),
        "decision": str(decision.get("decision") or ("approve" if decision.get("approved") else "reject")),
        "reason": str(decision.get("reason") or ""),
        "provider": "openai",
        "model": str(okx_config.get("model", "gpt-5.5")),
        "model_version": str(okx_config.get("model", "gpt-5.5")),
        "prompt_version": prompt_package["prompt_version"],
        "prompt_hash": prompt_package["prompt_hash"],
        "experiment_name": prompt_package["experiment_name"],
        "raw_prompt": prompt_package["messages"][1]["content"],
        "raw_response": response["raw_response"],
        "prompt_tokens": response.get("prompt_tokens"),
        "completion_tokens": response.get("completion_tokens"),
        "latency_ms": response.get("latency_ms"),
        "pending_memory": pending_memory,
        "raw": decision,
    }
    if route in {"lc_okx_setup_review", "lc_okx_release"}:
        review_status = "GIỮ SETUP"
        market_reason = "-"
        keep_reason = "-"
        delete_reason = "-"
        if route == "lc_okx_release" and result["approved"]:
            review_status = "DUYỆT MỞ MARKET"
            market_reason = result["reason"] or "5.5 xác nhận setup đủ điều kiện để đi tiếp vào Market."
        elif result["approved"]:
            review_status = "GIỮ SETUP"
            keep_reason = result["reason"] or "5.5 giữ lại setup vì cấu trúc lệnh vẫn hợp lệ."
        elif route == "lc_okx_setup_review":
            review_status = "XÓA SETUP"
            delete_reason = result["reason"] or "5.5 loại setup vì chưa đủ chất lượng."
        else:
            review_status = "GIỮ SETUP"
            keep_reason = result["reason"] or "5.5 chưa cho phép mở Market, setup tiếp tục được giữ lại."
        record_ai_call_event(
            config,
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "role": "okx",
                "review_kind": "lc_okx_review",
                "model": str(okx_config.get("model", "gpt-5.5")),
                "status": review_status,
                "approved": bool(result["approved"]),
                "decision": result["decision"],
                "reason": result["reason"],
                "symbol": candidate.symbol,
                "symbols": [candidate.symbol],
                "side": candidate.side,
                "lc_okx_id": context.get("lc_id"),
                "market_reason": market_reason,
                "keep_reason": keep_reason,
                "delete_reason": delete_reason,
            },
        )
    return result


def okx_ai_approval(
    config: dict[str, Any],
    candidate: TradeCandidate,
    risk_check: RiskCheck,
    *,
    context: dict[str, Any] | None = None,
    pending_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not ai_enabled(config):
        return {
            "approved": True,
            "decision": "ai_disabled",
            "reason": "AI coordinator is disabled",
            "provider": "disabled",
            "model": None,
        }

    context = context or {}
    pending_memory = pending_memory or internal_lc_memory(config)
    local_decision = _local_okx_policy(config, candidate, risk_check, context, pending_memory)
    okx_config = ai_config(config).get("okx", {})
    manual_openai_once = bool(context.get("manual_openai_once"))
    route = str(context.get("route") or "")
    lc_okx_review_once = (
        route == "lc_okx_setup_review"
        and not manual_openai_once
        and bool(okx_config.get("auto_lc_okx_review_once_enabled", False))
    )
    if manual_openai_once or lc_okx_review_once:
        if manual_openai_once and not bool(okx_config.get("manual_openai_enabled", False)):
            return {
                **local_decision,
                "decision": "manual_openai_disabled",
                "reason": "Manual OKX OpenAI approval is disabled; using local policy only",
            }
        if not bool(okx_config.get("approval_enabled", True)):
            return {
                **local_decision,
                "decision": "approval_disabled",
                "reason": "OKX AI approval is disabled; using local policy only",
            }
        try:
            return _openai_json_decision(
                config,
                candidate,
                risk_check,
                context,
                pending_memory,
                manual_trigger=manual_openai_once,
                lc_okx_review_once=lc_okx_review_once,
            )
        except (RuntimeError, urllib.error.URLError, urllib.error.HTTPError, KeyError, json.JSONDecodeError) as exc:
            if bool(okx_config.get("require_external_approval", False)):
                return {
                    "approved": False,
                    "decision": "external_ai_unavailable",
                    "reason": f"External OKX AI approval unavailable: {exc}",
                    "provider": "openai",
                    "model": str(okx_config.get("model", "gpt-5.5")),
                    "pending_memory": pending_memory,
                }
            return {
                **local_decision,
                "fallback": "local_policy",
                "external_error": str(exc),
            }
    return local_decision


def candidate_okx_review(candidate: TradeCandidate, *, route: str | None = None) -> dict[str, Any] | None:
    metadata = candidate.decision_metadata if isinstance(candidate.decision_metadata, dict) else {}
    review = metadata.get("okx_review")
    if not isinstance(review, dict):
        return None
    if route and str(review.get("route") or "") != str(route):
        return None
    return review


def attach_okx_review_metadata(
    candidate: TradeCandidate,
    decision: dict[str, Any],
    *,
    route: str,
    context: dict[str, Any] | None = None,
) -> TradeCandidate:
    reviewed = deepcopy(candidate)
    metadata = reviewed.decision_metadata if isinstance(reviewed.decision_metadata, dict) else {}
    review_payload = {
        "route": route,
        "approved": bool(decision.get("approved")),
        "decision": str(decision.get("decision") or ("approve" if decision.get("approved") else "reject")),
        "reason": str(decision.get("reason") or ""),
        "provider": decision.get("provider"),
        "model": decision.get("model"),
        "model_version": decision.get("model_version"),
        "prompt_version": decision.get("prompt_version"),
        "prompt_hash": decision.get("prompt_hash"),
        "experiment_name": decision.get("experiment_name"),
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "context": dict(context or {}),
    }
    reviewed.decision_metadata = {
        **metadata,
        "okx_review": review_payload,
    }
    return reviewed


def review_candidate_for_lc_okx(
    config: dict[str, Any],
    candidate: TradeCandidate,
    risk_check: RiskCheck,
    *,
    context: dict[str, Any] | None = None,
    pending_memory: dict[str, Any] | None = None,
    force: bool = False,
) -> tuple[TradeCandidate, dict[str, Any]]:
    review_context = dict(context or {})
    route = str(review_context.get("route") or "lc_okx_setup_review")
    review_context["route"] = route
    existing = candidate_okx_review(candidate, route=route)
    if existing is not None and not force:
        return candidate, existing
    cached = _recent_rejected_okx_review(config, candidate, route=route)
    if cached is not None and not force:
        return attach_okx_review_metadata(candidate, cached, route=route, context=review_context), cached
    decision = okx_ai_approval(
        config,
        candidate,
        risk_check,
        context=review_context,
        pending_memory=pending_memory,
    )
    _remember_okx_review_cache(config, candidate, route=route, decision=decision)
    return attach_okx_review_metadata(candidate, decision, route=route, context=review_context), decision
