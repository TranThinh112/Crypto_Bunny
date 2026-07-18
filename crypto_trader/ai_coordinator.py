from __future__ import annotations

import json
import re
import unicodedata
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
    reject_lc_pipeline_setup,
    save_lc_pipeline_mini_scan,
)
from .market import apply_news_scores_to_snapshots, fetch_market_snapshots, fetch_top_volume_symbols, prefetch_market_data
from .market_guard import market_guard_symbol_layers
from .market_pattern import analyze_market_pattern_snapshots, attach_market_pattern_features_to_candidates
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
OKX_REJECTION_HARD_DELETE = "hard_delete"
OKX_REJECTION_KEEP_MONITOR = "keep_monitor"
LEGACY_OKX_REJECTION_WATCHLIST = "watchlist"
OKX_REVIEW_ACTION_KEEP_SETUP = "keep_setup"
OKX_REVIEW_ACTION_DELETE_SETUP = "delete_setup"
OKX_REVIEW_ACTION_ENTER_MARKET = "enter_market"
OKX_INITIAL_REVIEW_SOURCE = "mini_lc_okx"
OKX_INITIAL_REVIEW_FROM_STATUS = "MINI_APPROVED"
DEFAULT_OKX_SOFT_RECHECK_MINUTES = 30


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


def _ascii_fold(value: Any) -> str:
    text = str(value or "").lower()
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _okx_soft_recheck_minutes(config: dict[str, Any]) -> int:
    okx_config = ai_config(config).get("okx", {})
    raw = okx_config.get("soft_recheck_minutes", DEFAULT_OKX_SOFT_RECHECK_MINUTES)
    try:
        return max(1, int(raw or DEFAULT_OKX_SOFT_RECHECK_MINUTES))
    except (TypeError, ValueError):
        return DEFAULT_OKX_SOFT_RECHECK_MINUTES


def _okx_setup_rejection_policy(decision: dict[str, Any]) -> dict[str, Any]:
    if bool(decision.get("approved")):
        return {"policy": "approved", "cache_mode": "none"}
    raw = decision.get("raw") if isinstance(decision.get("raw"), dict) else {}
    text = _ascii_fold(
        " ".join(
            str(value or "")
            for value in (
                decision.get("reason"),
                decision.get("decision"),
                raw.get("reason"),
                raw.get("decision"),
            )
        )
    )
    hard_patterns = (
        r"\bsai huong\b",
        r"\bnguoc huong\b",
        r"\bopposite\b",
        r"\bwrong direction\b",
        r"\bxung dot\b",
        r"\bconflict\b",
        r"\bentry nguy hiem\b",
        r"\bdangerous entry\b",
        r"\bvolume\s*(=|:)?\s*0\b",
        r"\bvolume bang 0\b",
        r"\bkhoi luong bang 0\b",
        r"\bvolume mat du lieu\b",
        r"\bkhoi luong mat du lieu\b",
        r"\bmissing volume data\b",
        r"\bzero volume\b",
        r"\bvolume qua yeu\b",
    )
    if any(re.search(pattern, text) for pattern in hard_patterns):
        return {
            "policy": OKX_REJECTION_HARD_DELETE,
            "cache_mode": "permanent",
            "reason": "hard rejection: direction/conflict/unsafe entry or unusable volume",
        }
    keep_monitor_patterns = (
        r"\bgiu theo doi\b",
        r"\btheo doi\b",
        r"\bgiu setup\b",
        r"\bgiu lai setup\b",
        r"\bkeep watching\b",
        r"\bkeep monitoring\b",
        r"\bmonitor\b",
        r"\bwatch\b",
        r"\bwait confirmation\b",
        r"\bcho them xac nhan\b",
        r"\bcan cho them\b",
    )
    if any(re.search(pattern, text) for pattern in keep_monitor_patterns):
        return {
            "policy": OKX_REJECTION_KEEP_MONITOR,
            "cache_mode": "keep_monitor",
            "reason": "soft 5.5 decision: keep the setup monitored while it is submitted to OKX",
        }
    return {
        "policy": OKX_REJECTION_HARD_DELETE,
        "cache_mode": "permanent",
        "reason": "5.5 rejected the setup without a keep-monitor instruction",
    }


def _okx_review_text(decision: dict[str, Any]) -> str:
    raw = decision.get("raw") if isinstance(decision.get("raw"), dict) else {}
    values = (
        decision.get("setup_action"),
        decision.get("action"),
        decision.get("decision"),
        decision.get("reason"),
        raw.get("setup_action"),
        raw.get("action"),
        raw.get("decision"),
        raw.get("reason"),
    )
    return _ascii_fold(" ".join(str(value or "") for value in values))


def _normalize_okx_review_action(value: Any) -> str | None:
    clean = re.sub(r"[^a-z0-9]+", "_", _ascii_fold(value)).strip("_")
    if not clean:
        return None
    if clean in {
        "enter_market",
        "market_entry",
        "vao_market",
        "vao_lenh_market",
        "mo_market",
        "duyet_mo_market",
        "open_market",
        "market_order",
    }:
        return OKX_REVIEW_ACTION_ENTER_MARKET
    if clean in {
        "keep_setup",
        "giu_setup",
        "giu_lai_setup",
        "giu_theo_doi",
        "keep_monitor",
        "keep_monitoring",
        "keep_watching",
        "approve",
        "approved",
        "giu",
    }:
        return OKX_REVIEW_ACTION_KEEP_SETUP
    if clean in {
        "delete_setup",
        "xoa_setup",
        "remove_setup",
        "cancel_setup",
    }:
        return OKX_REVIEW_ACTION_DELETE_SETUP
    return None


def _okx_initial_mini_review_context(context: dict[str, Any]) -> bool:
    return (
        str(context.get("route") or "") == "lc_okx_setup_review"
        and str(context.get("source") or "") == OKX_INITIAL_REVIEW_SOURCE
        and str(context.get("from_status") or "") == OKX_INITIAL_REVIEW_FROM_STATUS
    )


def _normalize_okx_rejection_policy(value: Any) -> str:
    policy = str(value or "").strip().lower()
    if policy == LEGACY_OKX_REJECTION_WATCHLIST:
        return OKX_REJECTION_KEEP_MONITOR
    return policy


def okx_review_rejection_policy(decision: dict[str, Any]) -> str:
    policy = _normalize_okx_rejection_policy(decision.get("rejection_policy"))
    if policy:
        return policy
    return _okx_setup_rejection_policy(decision)["policy"]


def okx_review_is_keep_monitor(decision: dict[str, Any]) -> bool:
    return okx_review_rejection_policy(decision) == OKX_REJECTION_KEEP_MONITOR


def okx_review_action(decision: dict[str, Any]) -> str:
    raw = decision.get("raw") if isinstance(decision.get("raw"), dict) else {}
    for value in (
        decision.get("setup_action"),
        decision.get("action"),
        decision.get("decision"),
        raw.get("setup_action"),
        raw.get("action"),
        raw.get("decision"),
    ):
        action = _normalize_okx_review_action(value)
        if action is not None:
            return action
    text = _okx_review_text(decision)
    market_patterns = (
        r"\benter market\b",
        r"\benter_market\b",
        r"\bmarket entry\b",
        r"\bmarket_order\b",
        r"\bmarket order\b",
        r"\bvao lenh market\b",
        r"\bmo lenh market\b",
        r"\bduyet mo market\b",
    )
    if any(re.search(pattern, text) for pattern in market_patterns):
        return OKX_REVIEW_ACTION_ENTER_MARKET
    if bool(decision.get("approved")) or okx_review_is_keep_monitor(decision):
        return OKX_REVIEW_ACTION_KEEP_SETUP
    return OKX_REVIEW_ACTION_DELETE_SETUP


def okx_review_requests_market_entry(decision: dict[str, Any]) -> bool:
    return okx_review_action(decision) == OKX_REVIEW_ACTION_ENTER_MARKET


def okx_review_allows_okx_submission(decision: dict[str, Any]) -> bool:
    return okx_review_action(decision) == OKX_REVIEW_ACTION_KEEP_SETUP


def okx_review_state(decision: dict[str, Any]) -> str:
    action = okx_review_action(decision)
    if action == OKX_REVIEW_ACTION_ENTER_MARKET:
        return "GPT55_ENTER_MARKET"
    if action == OKX_REVIEW_ACTION_KEEP_SETUP:
        return "GPT55_KEEP_SETUP"
    policy = okx_review_rejection_policy(decision)
    if policy == OKX_REJECTION_HARD_DELETE:
        return "GPT55_DELETE_SETUP"
    if policy == OKX_REJECTION_KEEP_MONITOR:
        return "GPT55_KEEP_SETUP"
    return "GPT55_DELETE_SETUP"


def _okx_review_cache_key(candidate: TradeCandidate, route: str) -> str:
    return "|".join(
        [
            str(route or "").strip().lower(),
            str(candidate.symbol or "").strip().upper(),
            str(candidate.side or "").strip().upper(),
        ]
    )


def _candidate_mini_setup_id(candidate: TradeCandidate) -> str:
    metadata = candidate.decision_metadata if isinstance(candidate.decision_metadata, dict) else {}
    mini_setup = metadata.get("mini_setup")
    if not isinstance(mini_setup, dict):
        return ""
    return str(mini_setup.get("setup_id") or "").strip()


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
    if _candidate_mini_setup_id(candidate):
        return None
    cache = _load_okx_review_cache(config)
    cache_key = _okx_review_cache_key(candidate, route)
    entry = cache.get(cache_key)
    if not isinstance(entry, dict):
        return None
    if bool(entry.get("approved")):
        return None
    policy = _okx_setup_rejection_policy(entry)
    rejection_policy = _normalize_okx_rejection_policy(entry.get("rejection_policy") or policy["policy"])
    if rejection_policy not in {OKX_REJECTION_HARD_DELETE, OKX_REJECTION_KEEP_MONITOR}:
        recheck_minutes = _okx_soft_recheck_minutes(config)
        reviewed_at = _parse_time(entry.get("reviewed_at") or entry.get("created_at"))
        if reviewed_at is None or (datetime.now(timezone.utc) - reviewed_at) > timedelta(minutes=recheck_minutes):
            cache.pop(cache_key, None)
            _save_okx_review_cache(config, cache)
            return None
    return {
        **entry,
        "setup_action": okx_review_action({**entry, "rejection_policy": rejection_policy}),
        "rejection_policy": rejection_policy,
        "review_state": okx_review_state({**entry, "rejection_policy": rejection_policy}),
        "accepted_for_okx": okx_review_allows_okx_submission({**entry, "rejection_policy": rejection_policy}),
        "cached": True,
        "cache_reason": (
            "Reused permanent rejected 5.5 setup review"
            if rejection_policy == OKX_REJECTION_HARD_DELETE
            else f"Reused recent 5.5 keep-monitor review for {_okx_soft_recheck_minutes(config)} minute(s)"
        ),
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
    # Mini setup lineage is persisted by setup_id in journal_state. Keeping a
    # second symbol/side cache here could leak a decision into a later pool.
    if _candidate_mini_setup_id(candidate):
        return
    cache = _load_okx_review_cache(config)
    cache_key = _okx_review_cache_key(candidate, route)
    if bool(decision.get("approved")):
        if cache.pop(cache_key, None) is not None:
            _save_okx_review_cache(config, cache)
        return
    policy = _okx_setup_rejection_policy(decision)
    rejection_policy = _normalize_okx_rejection_policy(policy["policy"])
    cache[cache_key] = {
        "approved": bool(decision.get("approved")),
        "setup_action": okx_review_action(decision),
        "decision": str(decision.get("decision") or ("approve" if decision.get("approved") else "reject")),
        "reason": str(decision.get("reason") or ""),
        "rejection_policy": rejection_policy,
        "cache_mode": policy["cache_mode"],
        "policy_reason": policy["reason"],
        "review_state": okx_review_state({**decision, "rejection_policy": rejection_policy}),
        "accepted_for_okx": okx_review_allows_okx_submission({**decision, "rejection_policy": rejection_policy}),
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
    market_pattern = summary.get("market_pattern")
    if isinstance(market_pattern, dict):
        compact["market_pattern"] = {
            key: market_pattern.get(key)
            for key in (
                "snapshot_id",
                "timeframe",
                "trend_regime",
                "structure_state",
                "trend_strength",
                "bos_detected",
                "bos_direction",
                "choch_detected",
                "choch_direction",
                "confluence_bias",
                "confluence_score",
                "data_quality_score",
                "candlestick_count",
                "chart_pattern_count",
                "smart_money_count",
                "support_zone_count",
                "resistance_zone_count",
            )
            if market_pattern.get(key) not in (None, "", [], {})
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
            str(frame): _compact_dict(data, ["direction", "strongest_pattern", "patterns", "signal_summary"])
            for frame, data in candlesticks.items()
            if str(frame).lower() in {"5m", "15m", "1h", "4h"} and isinstance(data, dict)
        }
    market_pattern = summary.get("market_pattern")
    if isinstance(market_pattern, dict):
        compact["market_pattern"] = {
            key: market_pattern.get(key)
            for key in (
                "snapshot_id",
                "timeframe",
                "trend_regime",
                "structure_state",
                "bos_detected",
                "bos_direction",
                "choch_detected",
                "choch_direction",
                "confluence_bias",
                "confluence_score",
                "data_quality_score",
            )
            if market_pattern.get(key) not in (None, "", [], {})
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


def _current_four_hour_event_for_market_scan(
    config: dict[str, Any],
    now: datetime,
    *,
    require_approved: bool = True,
) -> dict[str, Any] | None:
    latest_four_hour = latest_lc_pipeline_four_hour_event(config)
    if not latest_four_hour:
        return None
    four_hour_slot = _parse_time(latest_four_hour.get("slot") or latest_four_hour.get("created_at"))
    current_slot = _internal_market_scan_slot_start(config, now)
    if four_hour_slot is None or four_hour_slot != current_slot:
        return None
    if require_approved and not list(latest_four_hour.get("approved") or []):
        return None
    return latest_four_hour


def _empty_current_four_hour_event_for_market_scan(config: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    current_event = _current_four_hour_event_for_market_scan(config, now, require_approved=False)
    if current_event is not None and not list(current_event.get("approved") or []):
        return current_event
    return None


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


def _mini_symbol_name(symbol: str) -> str:
    return str(symbol or "").split("/")[0] or str(symbol or "-")


def _mini_reason_signals(raw_reason: str) -> tuple[list[str], list[str]]:
    lowered = raw_reason.lower()
    keep_reasons: list[str] = []
    reject_reasons: list[str] = []
    if "4h/1h/5m" in lowered and ("align" in lowered or "aligned" in lowered):
        keep_reasons.append("4h, 1h và 5m cùng chiều với setup")
    elif "1h/5m" in lowered and ("align" in lowered or "aligned" in lowered):
        keep_reasons.append("1h và 5m cùng chiều với setup")
    elif "1h" in lowered and "5m" in lowered and ("confirm" in lowered or "confirmation" in lowered):
        keep_reasons.append("có xác nhận từ 1h và 5m")
    if "rr acceptable" in lowered or "rr đạt" in lowered or "risk reward" in lowered:
        keep_reasons.append("RR đạt yêu cầu")
    if "spread low" in lowered or "clean spread" in lowered or "spread thấp" in lowered:
        keep_reasons.append("spread thấp")
    if "strong volume" in lowered or "volume strong" in lowered or "volume hỗ trợ" in lowered:
        keep_reasons.append("volume hỗ trợ")
    if "no news risk" in lowered or "không có rủi ro tin" in lowered:
        keep_reasons.append("không có rủi ro tin tức lớn")
    if "best" in lowered or "better" in lowered:
        keep_reasons.append("điểm setup nổi bật hơn nhóm còn lại")

    if "critical health" in lowered:
        reject_reasons.append("sức khỏe hệ thống đang ở mức nghiêm trọng")
    if "4h bias missing" in lowered or "4h absent" in lowered or "missing 4h" in lowered or "4h not provided" in lowered:
        reject_reasons.append("thiếu dữ liệu hoặc xác nhận xu hướng 4h")
    if "missing 15m" in lowered or "15m confirmation" in lowered or "confirm 15m" in lowered:
        reject_reasons.append("xác nhận 15m chưa đủ")
    if "5m/1h conflict" in lowered or ("5m" in lowered and "1h" in lowered and "conflict" in lowered):
        reject_reasons.append("xu hướng 5m và 1h xung đột")
    elif "5m" in lowered and ("conflict" in lowered or "mixed" in lowered):
        reject_reasons.append("5m còn xung đột")
    if "1h" in lowered and "conflict" in lowered:
        reject_reasons.append("1h còn xung đột")
    if "volume is weak" in lowered or "weak volume" in lowered or "missing volume" in lowered or "volume yếu" in lowered:
        reject_reasons.append("volume yếu hoặc thiếu volume xác nhận")
    if "rr only" in lowered or "rr just" in lowered:
        reject_reasons.append("RR chỉ ở mức tối thiểu")
    if "confidence capped" in lowered or "modest confidence" in lowered or "confidence modest" in lowered:
        reject_reasons.append("độ tin cậy bị giới hạn")
    return keep_reasons, reject_reasons


def _mini_ai_reason_parts_vi(review: dict[str, Any], pool_symbols: list[str], selected_symbols: list[str]) -> dict[str, str]:
    rejected = [symbol for symbol in pool_symbols if symbol not in selected_symbols]
    scores = review.get("setup_scores") if isinstance(review.get("setup_scores"), dict) else {}
    score_labels = [
        f"{_mini_symbol_name(symbol)} {scores[symbol]}/100"
        for symbol in rejected
        if symbol in scores
    ]
    raw_reason = str(review.get("reason") or "").strip()
    keep_reasons, reject_reasons = _mini_reason_signals(raw_reason)
    if not selected_symbols:
        names = ", ".join(_mini_symbol_name(symbol) for symbol in rejected) or "toàn bộ setup"
        selection_reason = "Mini chưa chọn cặp nào vì chưa có setup đủ điều kiện đi tiếp."
        rejection_reason = f"Mini loại {names}"
        if score_labels:
            rejection_reason += f" (điểm setup: {', '.join(score_labels)})"
        rejection_reason += ". Lý do: " + "; ".join(reject_reasons or ["setup chưa đủ chất lượng để đi tiếp"]) + "."
    else:
        kept = ", ".join(_mini_symbol_name(symbol) for symbol in selected_symbols)
        selection_reason = f"Mini giữ {kept} vì " + "; ".join(
            keep_reasons or ["có điểm setup tốt nhất trong nhóm Mini"]
        ) + "."
        if rejected:
            names = ", ".join(_mini_symbol_name(symbol) for symbol in rejected)
            rejection_reason = f"Mini loại {names}"
            if score_labels:
                rejection_reason += f" (điểm setup: {', '.join(score_labels)})"
            rejection_reason += ". Lý do: " + "; ".join(reject_reasons or ["điểm setup yếu hơn cặp được chọn"]) + "."
        elif reject_reasons:
            rejection_reason = "Điểm cần theo dõi: " + "; ".join(reject_reasons) + "."
        else:
            rejection_reason = "Không có cặp khác bị loại trong nhóm Mini."
    return {
        "selection_reason_vi": selection_reason,
        "rejection_reason_vi": rejection_reason,
        "decision_reason_vi": f"{selection_reason} {rejection_reason}".strip(),
    }


def _mini_ai_reason_vi(review: dict[str, Any], pool_symbols: list[str], selected_symbols: list[str]) -> str:
    return _mini_ai_reason_parts_vi(review, pool_symbols, selected_symbols)["decision_reason_vi"]


def run_internal_market_scan(config: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    internal_config = ai_config(config).get("internal", {})
    fixed_schedule = bool(internal_config.get("market_scan_fixed_schedule", True))
    slot_id = _internal_market_scan_slot_id(config, now) if fixed_schedule else None
    slot_start = _internal_market_scan_slot_start(config, now) if fixed_schedule else None
    empty_four_hour = _empty_current_four_hour_event_for_market_scan(config, now)
    if empty_four_hour is not None:
        return {
            "enabled": True,
            "agent": "internal_market_scan",
            "created_at": now.isoformat(),
            "slot_id": slot_id,
            "slot_start": slot_start.isoformat() if slot_start else None,
            "status": "waiting_lc",
            "skipped": True,
            "skip_reason": "LC 4h pool has no approved symbols; Mini scan skipped",
            "source": "latest_lc_4h_empty",
            "source_symbols": [],
            "source_base_symbols": [],
            "source_four_hour_symbols": [],
            "candidate_count": 0,
            "pool_symbols": [],
            "selected_symbols": [],
            "approved_symbols": [],
            "approved_count": 0,
            "candidates": [],
            "suppress_pending_notification": True,
            "four_hour_slot": empty_four_hour.get("slot") or empty_four_hour.get("created_at"),
            "four_hour_index": empty_four_hour.get("index"),
        }
    latest = latest_internal_market_scan(config)
    latest_created_at = _parse_time((latest or {}).get("created_at"))
    if (
        fixed_schedule
        and latest
        and (latest.get("slot_id") == slot_id or (latest_created_at and slot_start and latest_created_at >= slot_start))
    ):
        return {
            **latest,
            "skipped": True,
            "skip_reason": f"mini scan already ran for slot {slot_id}",
            "duplicate_slot_guard": True,
        }
    max_source_symbols = max(1, min(30, int(internal_config.get("market_scan_source_symbols", 30) or 30)))
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
    apply_news_scores_to_snapshots(snapshots, digest)
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
    snapshots_by_symbol = {snapshot.symbol: snapshot for snapshot in snapshots}
    scan_memory = recent_market_scan_memory(
        config,
        symbols=source_symbols,
        timeframes=["5m", "1h", "4h"] if compact_payload else ["1m", "5m", "15m", "1h", "4h"],
        lookback_hours=12,
        per_symbol_timeframe_limit=1 if compact_payload else 3,
    )
    ranked_candidates = lc_pipeline_mini_pool(config, candidates, limit=max_symbols)
    market_pattern_result = analyze_market_pattern_snapshots(
        config,
        [snapshots_by_symbol[candidate.symbol] for candidate in ranked_candidates[:max_symbols] if candidate.symbol in snapshots_by_symbol],
        correlation_id=f"mini_scan:{slot_id}",
        source="internal_market_scan",
    )
    attach_market_pattern_features_to_candidates(ranked_candidates, market_pattern_result.get("by_symbol") or {})
    warnings.extend(str(item) for item in (market_pattern_result.get("warnings") or [])[:5])
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
        "market_pattern_engine": {
            "enabled": market_pattern_result.get("enabled"),
            "source": market_pattern_result.get("source"),
            "analyzed": market_pattern_result.get("analyzed"),
            "symbols": list((market_pattern_result.get("by_symbol") or {}).keys()),
            "warnings": (market_pattern_result.get("warnings") or [])[:5],
        },
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
    mini_reason_parts = (
        _mini_ai_reason_parts_vi(
            result["ai_review"],
            list(result.get("pool_symbols") or []),
            list(result.get("selected_symbols") or []),
        )
        if result.get("ai_review")
        else {}
    )
    if mini_reason_parts:
        result.update(mini_reason_parts)
    elif not result["approved_symbols"]:
        result["selection_reason_vi"] = "Mini chưa chọn cặp nào vì hiện chưa có LC 4h đủ điều kiện."
        result["rejection_reason_vi"] = str(
            (local_result or {}).get("skip_reason") or "Không có setup đủ điều kiện đi tiếp."
        )
        result["decision_reason_vi"] = f"{result['selection_reason_vi']} Lý do loại: {result['rejection_reason_vi']}"
    else:
        result["selection_reason_vi"] = (
            "Mini đã chọn từ nhóm LC 4h hiện tại dựa trên Win Rate, chất lượng setup, xu hướng và chỉ báo."
        )
        result["rejection_reason_vi"] = "Các cặp không được chọn có điểm tổng hợp yếu hơn cặp được giữ."
        result["decision_reason_vi"] = f"{result['selection_reason_vi']} {result['rejection_reason_vi']}"
    saved_result = save_lc_pipeline_mini_scan(config, result)
    notify_mini_pool_summary(
        config,
        lc_pipeline_pool_rows(config, list(saved_result.get("selected_symbols") or [])),
        scan=saved_result,
        slot_id=slot_id,
    )
    return saved_result


def run_internal_market_scan_if_due(config: dict[str, Any]) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    if _empty_current_four_hour_event_for_market_scan(config, now) is not None:
        return None
    if internal_market_scan_due(config, now=now):
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
        "gpt55_checked": True,
    }
    result["setup_action"] = okx_review_action(result)
    if route == "lc_okx_setup_review" and not result["approved"]:
        policy = _okx_setup_rejection_policy(result)
        result["rejection_policy"] = _normalize_okx_rejection_policy(policy["policy"])
        result["cache_mode"] = policy["cache_mode"]
        result["policy_reason"] = policy["reason"]
        result["setup_action"] = okx_review_action(result)
    result["review_state"] = okx_review_state(result)
    result["accepted_for_okx"] = okx_review_allows_okx_submission(result)
    if route in {"lc_okx_setup_review", "lc_okx_release"}:
        setup_action = okx_review_action(result)
        review_status = "GIU SETUP"
        market_reason = "-"
        keep_reason = "-"
        delete_reason = "-"
        if setup_action == OKX_REVIEW_ACTION_ENTER_MARKET:
            review_status = "VAO MARKET"
            market_reason = result["reason"] or "5.5 chon vao lenh Market ngay."
        elif setup_action == OKX_REVIEW_ACTION_KEEP_SETUP:
            review_status = "GIU SETUP"
            keep_reason = result["reason"] or "5.5 giu setup va cho phep luu lenh cho tren OKX."
        else:
            review_status = "XOA SETUP"
            delete_reason = result["reason"] or "5.5 loai setup vi chua du chat luong."
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
                "rejection_policy": result.get("rejection_policy"),
                "cache_mode": result.get("cache_mode"),
                "review_state": result.get("review_state"),
                "accepted_for_okx": result.get("accepted_for_okx"),
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
    context = context or {}
    route = str(context.get("route") or "")
    initial_mini_review = route == "lc_okx_setup_review" and _okx_initial_mini_review_context(context)
    if not ai_enabled(config):
        if initial_mini_review:
            return {
                "approved": False,
                "decision": "external_ai_disabled",
                "reason": "GPT-5.5 is disabled; the Mini setup cannot continue to OKX.",
                "provider": "disabled",
                "model": None,
                "gpt55_checked": False,
            }
        return {
            "approved": True,
            "decision": "ai_disabled",
            "reason": "AI coordinator is disabled",
            "provider": "disabled",
            "model": None,
        }

    pending_memory = pending_memory or internal_lc_memory(config)
    local_decision = _local_okx_policy(config, candidate, risk_check, context, pending_memory)
    okx_config = ai_config(config).get("okx", {})
    manual_openai_once = bool(context.get("manual_openai_once"))
    lc_okx_review_once = (
        initial_mini_review
        and not manual_openai_once
        and bool(okx_config.get("auto_lc_okx_review_once_enabled", False))
    )
    if manual_openai_once:
        return {
            **local_decision,
            "decision": "manual_openai_blocked",
            "reason": "5.5 is restricted to the initial Mini LC_OKX setup review and cannot be called manually.",
        }
    if initial_mini_review and not lc_okx_review_once:
        return {
            "approved": False,
            "decision": "initial_mini_review_required",
            "reason": "GPT-5.5 initial Mini review is not enabled; the setup cannot continue to OKX.",
            "provider": "blocked",
            "model": str(okx_config.get("model", "gpt-5.5")),
            "pending_memory": pending_memory,
            "gpt55_checked": False,
        }
    if manual_openai_once or lc_okx_review_once:
        if manual_openai_once and not bool(okx_config.get("manual_openai_enabled", False)):
            return {
                **local_decision,
                "decision": "manual_openai_disabled",
                "reason": "Manual OKX OpenAI approval is disabled; using local policy only",
            }
        if not bool(okx_config.get("approval_enabled", True)):
            if initial_mini_review:
                return {
                    "approved": False,
                    "decision": "approval_disabled",
                    "reason": "GPT-5.5 approval is disabled; the Mini setup cannot continue to OKX.",
                    "provider": "blocked",
                    "model": str(okx_config.get("model", "gpt-5.5")),
                    "pending_memory": pending_memory,
                    "gpt55_checked": False,
                }
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
        except Exception as exc:
            if initial_mini_review or bool(okx_config.get("require_external_approval", False)):
                return {
                    "approved": False,
                    "decision": "external_ai_unavailable",
                    "reason": f"External OKX AI approval unavailable: {exc}",
                    "provider": "openai",
                    "model": str(okx_config.get("model", "gpt-5.5")),
                    "pending_memory": pending_memory,
                    "gpt55_checked": False,
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


def okx_setup_review_recheck_state(
    config: dict[str, Any],
    candidate: TradeCandidate,
    *,
    route: str = "lc_okx_setup_review",
    now: datetime | None = None,
) -> dict[str, Any]:
    review = candidate_okx_review(candidate, route=route)
    if review is None:
        return {"has_review": False, "due": True, "review": None}
    stored_action = okx_review_action(review)
    if route != "lc_okx_setup_review" or stored_action in {
        OKX_REVIEW_ACTION_KEEP_SETUP,
        OKX_REVIEW_ACTION_ENTER_MARKET,
    }:
        return {
            "has_review": True,
            "due": False,
            "review": {
                **review,
                "setup_action": stored_action,
                "review_state": okx_review_state(review),
                "accepted_for_okx": okx_review_allows_okx_submission(review),
            },
            "rejection_policy": review.get("rejection_policy"),
        }

    policy = _okx_setup_rejection_policy(review)
    rejection_policy = _normalize_okx_rejection_policy(review.get("rejection_policy") or policy["policy"])
    normalized_review = {
        **review,
        "rejection_policy": rejection_policy,
        "cache_mode": review.get("cache_mode") or policy["cache_mode"],
        "policy_reason": review.get("policy_reason") or policy["reason"],
        "review_state": okx_review_state({**review, "rejection_policy": rejection_policy}),
        "accepted_for_okx": okx_review_allows_okx_submission({**review, "rejection_policy": rejection_policy}),
    }
    if rejection_policy == OKX_REJECTION_HARD_DELETE:
        return {
            "has_review": True,
            "due": False,
            "review": normalized_review,
            "rejection_policy": rejection_policy,
            "hard_delete": True,
        }
    if rejection_policy == OKX_REJECTION_KEEP_MONITOR:
        return {
            "has_review": True,
            "due": False,
            "review": normalized_review,
            "rejection_policy": rejection_policy,
            "keep_monitor": True,
        }

    recheck_minutes = _okx_soft_recheck_minutes(config)
    try:
        recheck_minutes = max(1, int(review.get("recheck_after_minutes") or recheck_minutes))
    except (TypeError, ValueError):
        recheck_minutes = _okx_soft_recheck_minutes(config)
    reviewed_at = _parse_time(review.get("reviewed_at") or review.get("created_at"))
    if reviewed_at is None:
        return {
            "has_review": True,
            "due": True,
            "review": {**normalized_review, "recheck_after_minutes": recheck_minutes},
            "rejection_policy": rejection_policy,
        }

    check_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    next_recheck_at = reviewed_at + timedelta(minutes=recheck_minutes)
    due = check_now >= next_recheck_at
    return {
        "has_review": True,
        "due": due,
        "review": {
            **normalized_review,
            "recheck_after_minutes": recheck_minutes,
            "next_recheck_at": next_recheck_at.isoformat(),
            **(
                {}
                if due
                else {
                    "cached": True,
                    "cache_reason": f"Waiting for fresh confirmation before retrying 5.5 until {next_recheck_at.isoformat()}",
                }
            ),
        },
        "rejection_policy": rejection_policy,
        "reviewed_at": reviewed_at.isoformat(),
        "next_recheck_at": next_recheck_at.isoformat(),
    }


def attach_okx_review_metadata(
    candidate: TradeCandidate,
    decision: dict[str, Any],
    *,
    route: str,
    context: dict[str, Any] | None = None,
) -> TradeCandidate:
    reviewed = deepcopy(candidate)
    metadata = reviewed.decision_metadata if isinstance(reviewed.decision_metadata, dict) else {}
    rejection_policy = okx_review_rejection_policy(decision)
    normalized_decision = {**decision, "rejection_policy": rejection_policy}
    setup_action = okx_review_action(normalized_decision)
    review_payload = {
        "route": route,
        "approved": bool(decision.get("approved")),
        "setup_action": setup_action,
        "decision": str(decision.get("decision") or ("approve" if decision.get("approved") else "reject")),
        "reason": str(decision.get("reason") or ""),
        "provider": decision.get("provider"),
        "model": decision.get("model"),
        "model_version": decision.get("model_version"),
        "prompt_version": decision.get("prompt_version"),
        "prompt_hash": decision.get("prompt_hash"),
        "experiment_name": decision.get("experiment_name"),
        "rejection_policy": rejection_policy,
        "cache_mode": decision.get("cache_mode"),
        "policy_reason": decision.get("policy_reason"),
        "review_state": okx_review_state(normalized_decision),
        "accepted_for_okx": okx_review_allows_okx_submission(normalized_decision),
        "gpt55_checked": bool(decision.get("gpt55_checked", True)),
        "reviewed_at": str(decision.get("reviewed_at") or datetime.now(timezone.utc).isoformat()),
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
    fresh_mini_setup = (
        route == "lc_okx_setup_review"
        and _okx_initial_mini_review_context(review_context)
        and bool(review_context.get("mini_setup_id") or _candidate_mini_setup_id(candidate))
    )
    existing_state = okx_setup_review_recheck_state(config, candidate, route=route)
    existing = existing_state.get("review")
    if existing is not None and not force and not fresh_mini_setup:
        if route != "lc_okx_setup_review" or bool(existing.get("approved")):
            return candidate, existing
        if (
            existing_state.get("rejection_policy") == OKX_REJECTION_HARD_DELETE
            or not bool(existing_state.get("due", True))
        ):
            if existing_state.get("rejection_policy") == OKX_REJECTION_HARD_DELETE:
                reject_lc_pipeline_setup(
                    config,
                    candidate.symbol,
                    side=candidate.side,
                    reason=str(existing.get("reason") or existing.get("decision") or "cached 5.5 rejection"),
                    lc_id=review_context.get("lc_id"),
                    route=route,
                )
            return attach_okx_review_metadata(candidate, existing, route=route, context=review_context), existing
    cached = None if fresh_mini_setup else _recent_rejected_okx_review(config, candidate, route=route)
    if cached is not None and not force:
        if cached.get("rejection_policy") == OKX_REJECTION_HARD_DELETE:
            reject_lc_pipeline_setup(
                config,
                candidate.symbol,
                side=candidate.side,
                reason=str(cached.get("reason") or cached.get("decision") or "cached 5.5 rejection"),
                lc_id=review_context.get("lc_id"),
                route=route,
            )
        return attach_okx_review_metadata(candidate, cached, route=route, context=review_context), cached
    if route == "lc_okx_setup_review" and not _okx_initial_mini_review_context(review_context):
        return candidate, {
            "approved": False,
            "decision": "initial_mini_review_required",
            "reason": "5.5 can only be called once when Mini first promotes this setup.",
            "provider": "blocked",
            "model": ai_config(config).get("okx", {}).get("model", "gpt-5.5"),
            "setup_action": OKX_REVIEW_ACTION_DELETE_SETUP,
            "review_state": "GPT55_NOT_REVIEWED",
            "accepted_for_okx": False,
            "gpt55_checked": False,
        }
    decision = okx_ai_approval(
        config,
        candidate,
        risk_check,
        context=review_context,
        pending_memory=pending_memory,
    )
    if route == "lc_okx_setup_review" and not bool(decision.get("approved")) and not decision.get("rejection_policy"):
        policy = _okx_setup_rejection_policy(decision)
        decision = {
            **decision,
            "rejection_policy": _normalize_okx_rejection_policy(policy["policy"]),
            "cache_mode": policy["cache_mode"],
            "policy_reason": policy["reason"],
        }
    if route == "lc_okx_setup_review":
        decision = {
            **decision,
            "setup_action": okx_review_action(decision),
            "rejection_policy": okx_review_rejection_policy(decision),
            "review_state": okx_review_state(decision),
            "accepted_for_okx": okx_review_allows_okx_submission(decision),
        }
    _remember_okx_review_cache(config, candidate, route=route, decision=decision)
    if (
        route == "lc_okx_setup_review"
        and not bool(decision.get("approved"))
        and okx_review_rejection_policy(decision) == OKX_REJECTION_HARD_DELETE
    ):
        reject_lc_pipeline_setup(
            config,
            candidate.symbol,
            side=candidate.side,
            reason=str(decision.get("reason") or decision.get("decision") or "5.5 rejected LC_OKX setup"),
            lc_id=review_context.get("lc_id"),
            route=route,
        )
    return attach_okx_review_metadata(candidate, decision, route=route, context=review_context), decision
