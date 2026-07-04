from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

from .codex_features import (
    build_market_prompt_dto,
    build_prompt,
    call_openai_json,
    get_bunny_health_state,
    get_trading_system_state,
)
from .market import fetch_market_snapshots, fetch_top_volume_symbols
from .market_guard import market_guard_symbol_layers
from .models import RiskCheck, TradeCandidate, to_jsonable
from .news import collect_news
from .sizing import apply_position_sizing
from .storage import get_journal_state, list_pending_orders, recent_market_scan_memory, set_journal_state
from .strategy import build_candidates, enrich_quantities


_PENDING_STATUS_PRIORITY = {
    "LC_OKX": 0,
    "OPEN": 1,
}
INTERNAL_MARKET_SCAN_STATE_KEY = "ai_internal_market_scan_latest"


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


def _role_api_key(config: dict[str, Any], role_config: dict[str, Any]) -> tuple[str, str]:
    load_dotenv()
    key_env = str(role_config.get("api_key_env", ai_config(config).get("api_key_env", "OPENAI_API_KEY")))
    return key_env, os.getenv(key_env, "").strip()


def _openai_chat_json(role_config: dict[str, Any], api_key: str, messages: list[dict[str, str]]) -> dict[str, Any]:
    payload = {
        "model": str(role_config.get("model", "gpt-5.5")),
        "response_format": {"type": "json_object"},
        "messages": messages,
    }
    if "temperature" in role_config:
        payload["temperature"] = role_config.get("temperature")
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=float(role_config.get("timeout_seconds", 20) or 20)) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        detail = f"OpenAI HTTP {exc.code}"
        if body:
            detail = f"{detail}: {body[:500]}"
        raise RuntimeError(detail) from exc
    content = raw["choices"][0]["message"]["content"]
    return json.loads(content)


def _openai_internal_market_scan(
    config: dict[str, Any],
    candidates: list[dict[str, Any]],
    local_result: dict[str, Any],
) -> dict[str, Any]:
    internal_config = ai_config(config).get("internal", {})
    system_state = get_trading_system_state(config)
    health_state = get_bunny_health_state(config)
    prompt_package = build_prompt(
        config,
        build_market_prompt_dto(
            candidates=candidates,
            market_snapshot=local_result,
            trading_system_state=system_state,
            trading_health_state=health_state,
            extra={"policyPrefilter": local_result},
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


def latest_internal_market_scan(config: dict[str, Any]) -> dict[str, Any] | None:
    raw = get_journal_state(config, INTERNAL_MARKET_SCAN_STATE_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


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
    return (now - created_at).total_seconds() >= internal_market_scan_interval(config)


def _candidate_market_summary(
    candidate: TradeCandidate,
    scan_memory_by_symbol: dict[str, dict[str, list[dict[str, Any]]]] | None = None,
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
    rolling_scan_memory = {
        timeframe: entries
        for timeframe, entries in symbol_memory.items()
        if timeframe.lower() in {"1m", "5m", "15m", "1h", "4h"}
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
        "indicator_summary": candidate.indicator_summary,
        "candlestick_patterns": candidate.candlestick_patterns,
        "code_timeframe_analysis": code_timeframe_analysis,
        "mini_context_4h": mini_context_4h,
        "rolling_scan_memory": rolling_scan_memory,
        "reasons": candidate.reasons[:6],
        "warnings": candidate.warnings[:4],
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
    eligible = [
        candidate
        for candidate in ranked
        if candidate.win_probability_pct is not None and float(candidate.win_probability_pct) >= threshold
    ][:max_symbols]
    return {
        "provider": "local_policy",
        "decision": "prefilter",
        "threshold_win_probability_pct": threshold,
        "approved_symbols": [candidate.symbol for candidate in eligible],
        "approved_count": len(eligible),
        "candidate_count": len(candidates),
        "warnings": warnings,
    }


def _validated_ai_symbols(review: dict[str, Any], allowed_symbols: set[str], fallback_symbols: list[str], max_symbols: int) -> list[str]:
    raw = review.get("approved_symbols")
    if not isinstance(raw, list):
        return fallback_symbols
    symbols: list[str] = []
    for item in raw:
        symbol = str(item or "")
        if symbol in allowed_symbols and symbol not in symbols:
            symbols.append(symbol)
    return symbols[:max_symbols] or fallback_symbols


def run_internal_market_scan(config: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    internal_config = ai_config(config).get("internal", {})
    max_source_symbols = max(1, min(50, int(internal_config.get("market_scan_source_symbols", 50) or 50)))
    max_symbols = max(1, int(internal_config.get("market_scan_max_symbols", 12) or 12))
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
        timeframes=["1m", "5m", "15m", "1h", "4h"],
        lookback_hours=12,
        per_symbol_timeframe_limit=3,
    )
    candidate_summaries = [
        _candidate_market_summary(candidate, scan_memory_by_symbol=scan_memory)
        for candidate in candidates[:max_source_symbols]
    ]
    local_result = _local_market_scan_result(config, candidates, warnings)

    result = {
        "enabled": True,
        "agent": "internal_market_scan",
        "created_at": now.isoformat(),
        "interval_seconds": internal_market_scan_interval(config),
        "provider": str(internal_config.get("provider", "local_policy") or "local_policy"),
        "model": str(internal_config.get("model", "gpt-5.4-mini")),
        "source": source,
        "source_symbols": source_symbols,
        "candidate_count": len(candidates),
        "local_policy": local_result,
        "approved_symbols": list(local_result.get("approved_symbols") or []),
        "candidates": candidate_summaries[:max_symbols],
        "scan_memory": scan_memory,
        "warnings": warnings[:20],
    }

    if result["provider"] == "openai" and bool(internal_config.get("market_scan_use_ai", True)):
        try:
            allowed = set(local_result.get("approved_symbols") or [])
            ai_review = _openai_internal_market_scan(
                config,
                candidate_summaries[:max_symbols],
                local_result,
            )
            result["ai_review"] = ai_review
            result["approved_symbols"] = _validated_ai_symbols(
                ai_review,
                allowed,
                list(local_result.get("approved_symbols") or []),
                max_symbols,
            )
            scores = ai_review.get("setup_scores")
            if isinstance(scores, dict):
                result["setup_scores"] = {
                    str(symbol): scores.get(symbol)
                    for symbol in result["approved_symbols"]
                    if symbol in scores
                }
        except (RuntimeError, urllib.error.URLError, urllib.error.HTTPError, KeyError, json.JSONDecodeError) as exc:
            result["ai_review_error"] = str(exc)
            result["fallback"] = "local_policy"

    set_journal_state(config, INTERNAL_MARKET_SCAN_STATE_KEY, json.dumps(result, ensure_ascii=False))
    return result


def run_internal_market_scan_if_due(config: dict[str, Any]) -> dict[str, Any] | None:
    if internal_market_scan_due(config):
        return run_internal_market_scan(config)
    return latest_internal_market_scan(config)


def internal_market_shortlist(config: dict[str, Any]) -> tuple[list[str], dict[str, Any] | None]:
    internal_config = ai_config(config).get("internal", {})
    if not ai_enabled(config) or not bool(internal_config.get("market_scan_use_shortlist", True)):
        return [], None
    latest = latest_internal_market_scan(config)
    if not latest:
        return [], None
    created_at = _parse_time(latest.get("created_at"))
    if not created_at:
        return [], None
    max_age = internal_market_scan_interval(config) * 1.5
    if (datetime.now(timezone.utc) - created_at).total_seconds() > max_age:
        return [], {**latest, "stale": True}
    symbols = [str(symbol) for symbol in latest.get("approved_symbols") or [] if str(symbol)]
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
        "reasons": candidate.reasons,
        "warnings": candidate.warnings,
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
    prompt_package = build_prompt(
        config,
        build_market_prompt_dto(
            candidates=[_candidate_summary(candidate)],
            market_snapshot={"riskCheck": to_jsonable(risk_check), "context": context},
            trading_system_state=system_state,
            trading_health_state=health_state,
            open_positions=pending_memory.get("orders") or [],
            recent_trades=[],
            extra={"internalLcMemory": pending_memory},
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
