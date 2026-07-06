from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timedelta, timezone
from typing import Any

from .codex_features import (
    build_market_prompt_dto,
    build_prompt,
    call_openai_json,
    get_bunny_health_state,
    get_trading_system_state,
)
from .lc_pipeline import (
    lc_pipeline_internal_symbols,
    lc_pipeline_mini_pool,
    lc_pipeline_pool_rows,
    latest_lc_pipeline_mini_scan,
    notify_mini_pool_summary,
    save_lc_pipeline_mini_scan,
)
from .market import fetch_market_snapshots, fetch_top_volume_symbols
from .market_guard import market_guard_symbol_layers
from .models import RiskCheck, TradeCandidate
from .news import collect_news
from .sizing import apply_position_sizing
from .storage import list_pending_orders, recent_market_scan_memory
from .strategy import build_candidates, enrich_quantities


_PENDING_STATUS_PRIORITY = {
    "LC_OKX": 0,
    "OPEN": 1,
}
def ai_config(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("ai", {})


def ai_enabled(config: dict[str, Any]) -> bool:
    return bool(ai_config(config).get("enabled", True))


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
    return {
        "pending_total": memory.get("pending_total"),
        "lc_okx_count": memory.get("lc_okx_count"),
        "local_lc_count": memory.get("local_lc_count"),
        "preferred": memory.get("preferred"),
        "orders": (memory.get("orders") or [])[:3],
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
    local = [record for record in records if str(record.get("status") or "") == "OPEN"]
    preferred = records[0] if records else None
    memory = {
        "enabled": ai_enabled(config),
        "agent": "internal",
        "model": internal_config.get("model", "gpt-mini"),
        "provider": internal_config.get("provider", "local_policy"),
        "pending_total": len(records),
        "lc_okx_count": len(lc_okx),
        "local_lc_count": len(local),
        "priority": ["LC_OKX", "OPEN"],
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


def internal_market_scan_due(config: dict[str, Any], now: datetime | None = None) -> bool:
    if not ai_enabled(config):
        return False
    internal_config = ai_config(config).get("internal", {})
    if not bool(internal_config.get("market_scan_enabled", True)):
        return False
    now = now or datetime.now(timezone.utc)
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
            config.get("strategy", {}).get("min_win_probability_pct", 80),
        )
        or 80
    )
    max_symbols = max(1, min(3, int(internal_config.get("market_scan_max_symbols", 3) or 3)))
    ranked = sorted(
        candidates,
        key=lambda item: (float(item.win_probability_pct or 0), float(item.confidence or 0)),
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
        "qualified_symbols": [candidate.symbol for candidate in qualified],
        "approved_symbols": [candidate.symbol for candidate in eligible],
        "approved_count": len(eligible),
        "candidate_count": len(candidates),
        "warnings": warnings,
    }


def _validated_ai_symbols(review: dict[str, Any], allowed_symbols: set[str], fallback_symbols: list[str], max_symbols: int) -> list[str]:
    raw = review.get("approved_symbols")
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
    return symbols[:max_symbols] or fallback_symbols[:max(minimum_symbols, 1)]


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
    compact_payload = bool(internal_config.get("compact_ai_payload", True))
    source_symbols, source_warnings = fetch_top_volume_symbols(config)
    if source_symbols:
        source_symbols = source_symbols[:max_source_symbols]
        source = "okx_top_volume_24h"
    else:
        source_symbols = [str(symbol) for symbol in config.get("strategy", {}).get("symbols", []) if str(symbol)]
        source = "configured_fallback"

    warnings: list[str] = list(source_warnings)
    digest = collect_news(config)
    snapshots, market_warnings = fetch_market_snapshots(config, source_symbols)
    warnings.extend(market_warnings)
    market_layers: dict[str, dict[str, Any]] = {}
    if config.get("market_guard", {}).get("use_memory_in_strategy", True):
        try:
            market_layers = market_guard_symbol_layers(config, source_symbols)
        except Exception as exc:
            warnings.append(f"Market guard memory unavailable: {exc}")
    candidates = build_candidates(config, snapshots, digest, limit=None, market_layers=market_layers)
    apply_position_sizing(config, candidates)
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
    notify_mini_pool_summary(
        config,
        lc_pipeline_pool_rows(config, mini_candidate_symbols),
        slot_id=slot_id,
    )
    local_result = _local_market_scan_result(config, candidates, warnings)
    has_full_mini_pool = len(mini_candidate_symbols) >= max_symbols
    if mini_candidate_symbols:
        local_result = {
            **local_result,
            "approved_symbols": mini_candidate_symbols,
            "approved_count": len(mini_candidate_symbols),
            "selection_source": "lc_internal_pipeline",
        }
    if not has_full_mini_pool:
        local_result = {
            **local_result,
            "approved_symbols": [],
            "approved_count": 0,
            "selection_source": "lc_internal_pipeline_waiting",
            "skip_reason": f"waiting for {max_symbols} internal LC candidates, current={len(mini_candidate_symbols)}",
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
        "candidate_count": len(candidates),
        "local_policy": local_result,
        "pool_symbols": list(mini_candidate_symbols or []),
        "selected_symbols": list(local_result.get("approved_symbols") or []),
        "approved_symbols": list(local_result.get("approved_symbols") or []),
        "candidates": candidate_summaries[:max_symbols],
        "scan_memory": scan_memory if not compact_payload else {},
        "compact_ai_payload": compact_payload,
        "warnings": warnings[:20],
    }
    save_lc_pipeline_mini_scan(config, result)

    if has_full_mini_pool and result["provider"] == "openai" and bool(internal_config.get("market_scan_use_ai", True)):
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
                max(1, min(max_symbols, int(internal_config.get("market_scan_pending_limit", 1) or 1))),
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
    result["status"] = "done" if has_full_mini_pool else "waiting_lc"
    return save_lc_pipeline_mini_scan(config, result)


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


def _candidate_summary(candidate: TradeCandidate) -> dict[str, Any]:
    return {
        "symbol": candidate.symbol,
        "side": candidate.side,
        "confidence": candidate.confidence,
        "win_probability_pct": candidate.win_probability_pct,
        "risk_reward": candidate.risk_reward,
        "entry": candidate.entry,
        "stop_loss": candidate.stop_loss,
        "take_profit": candidate.take_profit,
        "quantity": candidate.quantity,
        "order_usdt": candidate.order_usdt,
        "planned_risk_usdt": round(candidate.planned_risk_usdt, 4),
        "indicator_summary": _compact_indicator_summary(candidate.indicator_summary),
        "reasons": candidate.reasons[:3],
        "warnings": candidate.warnings[:2],
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
) -> dict[str, Any]:
    okx_config = ai_config(config).get("okx", {})
    system_state = get_trading_system_state(config)
    health_state = get_bunny_health_state(config)
    runtime_state = _compact_runtime_state(system_state, health_state)
    prompt_package = build_prompt(
        config,
        build_market_prompt_dto(
            candidates=[_candidate_summary(candidate)],
            market_snapshot={
                "riskCheck": _compact_risk_check(risk_check),
                "route": _compact_dict(context, ["route", "lc_id", "from_status", "source"]),
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
        route=str(context.get("route") or ""),
    )
    decision = dict(response["parsed"])
    return {
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
    if not bool(okx_config.get("approval_enabled", True)):
        return {
            **local_decision,
            "approved": risk_check.passed,
            "decision": "approval_disabled",
            "reason": "OKX AI approval is disabled; using risk gate only",
        }

    provider = str(okx_config.get("provider", "local_policy") or "local_policy")
    if provider != "openai":
        return local_decision

    try:
        return _openai_json_decision(config, candidate, risk_check, context, pending_memory)
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
