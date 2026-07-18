from __future__ import annotations

import hashlib
import json
import random
import re
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv

from .config import deep_merge, project_path
from .models import Decision, RiskCheck, TradeCandidate, to_jsonable
from .storage import (
    activate_strategy_version_record,
    claim_trade_candidate,
    ensure_ai_model_version,
    ensure_strategy_version,
    get_journal_state,
    get_prompt_metric,
    get_prompt_version,
    get_strategy_version,
    get_trade_execution,
    get_trading_system_state_row,
    insert_ai_trade_decision_row,
    insert_market_regime_history,
    insert_replay_history_row,
    insert_trade_candidate_rows,
    insert_trade_execution_row,
    latest_market_regime_history,
    list_ai_experiment_rows,
    list_ai_trade_decision_rows,
    list_ai_trade_decision_stat_rows,
    list_ai_trade_decision_stat_rows_for_period,
    list_market_regime_rows,
    list_prompt_versions,
    list_replay_history_rows,
    list_strategy_versions,
    list_trade_candidate_rows,
    list_trade_execution_ids,
    list_trade_execution_rows,
    mark_ai_trade_decisions_closed,
    mark_trade_candidate_used,
    merge_prompt_metric,
    save_ai_experiment,
    save_prompt_version,
    save_strategy_version,
    set_journal_state,
    update_trade_execution,
    upsert_trading_health_state_row,
    upsert_trading_system_state_row,
)


PROMPT_FILE_ORDER = (
    "system.txt",
    "mini-analysis.txt",
    "final-decision.txt",
    "recovery-mode.txt",
    "health-warning.txt",
    "output-format.txt",
)
PROMPT_INSTRUCTION_MAP = {
    "mini-analysis": "mini-analysis.txt",
    "final-decision": "final-decision.txt",
}
OPEN_EXECUTION_STATUSES = {"OPEN"}
CLOSED_EXECUTION_STATUSES = {"WIN", "LOSS", "BREAKEVEN", "CLOSED"}
STATE_VERSION = "python-codex-v1"
AI_CALL_HISTORY_STATE_KEY = "ai_call_history"
AI_CALL_STATUS_STATS_STATE_KEY = "ai_call_status_stats"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utcnow().isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _json_loads(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return default


def _bool_int(value: bool) -> int:
    return 1 if value else 0


def _avg(values: Iterable[float]) -> float:
    items = [float(item) for item in values]
    return sum(items) / len(items) if items else 0.0


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _candidate_rule_score(candidate: TradeCandidate | dict[str, Any] | None) -> float:
    if candidate is None:
        return 0.0
    if isinstance(candidate, TradeCandidate):
        if candidate.rule_score is not None:
            return float(candidate.rule_score)
        return float(candidate.confidence or 0)
    rule_score = candidate.get("rule_score")
    if rule_score is not None:
        return _safe_float(rule_score)
    return _safe_float(candidate.get("confidence"))


def _candidate_payload(candidate: TradeCandidate) -> dict[str, Any]:
    payload = to_jsonable(candidate)
    indicator = payload.setdefault("indicator_summary", {})
    if "rule_score" not in payload or payload["rule_score"] is None:
        payload["rule_score"] = _candidate_rule_score(candidate)
    if isinstance(indicator, dict) and "rule_score" not in indicator:
        indicator["rule_score"] = payload["rule_score"]
    return payload


def _trim_text(value: Any, limit: int) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 12)].rstrip()}...[trimmed]"


def _compact_candidate_indicator(indicator: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(indicator, dict):
        return {}
    fields = (
        "timeframe",
        "last",
        "trend",
        "rsi",
        "macd_signal",
        "volume_ratio",
        "spread_pct",
        "funding_rate",
        "open_interest_change",
        "signal_summary",
        "direction",
        "rule_score",
    )
    return {
        key: indicator.get(key)
        for key in fields
        if indicator.get(key) not in (None, "", [], {})
    }


def _compact_candidate_storage_payload(candidate: TradeCandidate) -> dict[str, Any]:
    payload = _candidate_payload(candidate)
    return {
        "symbol": candidate.symbol,
        "base": candidate.base,
        "side": candidate.side,
        "confidence": round(float(candidate.confidence or 0), 3),
        "rule_score": round(float(_candidate_rule_score(candidate) or 0), 3),
        "entry": round(float(candidate.entry or 0), 8),
        "stop_loss": round(float(candidate.stop_loss or 0), 8),
        "take_profit": round(float(candidate.take_profit or 0), 8),
        "risk_reward": round(float(candidate.risk_reward or 0), 4),
        "order_usdt": round(float(candidate.order_usdt or 0), 4),
        "quantity": None if candidate.quantity is None else round(float(candidate.quantity), 8),
        "spread_pct": None if candidate.spread_pct is None else round(float(candidate.spread_pct), 5),
        "news_score": round(float(candidate.news_score or 0), 4),
        "news_count": int(candidate.news_count or 0),
        "win_probability_pct": None if candidate.win_probability_pct is None else round(float(candidate.win_probability_pct), 3),
        "target_mode": candidate.target_mode,
        "take_profit_pct": None if candidate.take_profit_pct is None else round(float(candidate.take_profit_pct), 3),
        "stop_loss_pct": None if candidate.stop_loss_pct is None else round(float(candidate.stop_loss_pct), 3),
        "price_take_profit_pct": None if candidate.price_take_profit_pct is None else round(float(candidate.price_take_profit_pct), 4),
        "price_stop_loss_pct": None if candidate.price_stop_loss_pct is None else round(float(candidate.price_stop_loss_pct), 4),
        "scan_source": candidate.scan_source,
        "setup_quality": candidate.setup_quality,
        "market_regime": candidate.market_regime,
        "regime_confidence": None if candidate.regime_confidence is None else round(float(candidate.regime_confidence), 3),
        "indicator_summary": _compact_candidate_indicator(payload.get("indicator_summary") or candidate.indicator_summary),
        "reasons": [str(item) for item in (payload.get("reasons") or [])[:3]],
        "warnings": [str(item) for item in (payload.get("warnings") or [])[:2]],
    }


def _compact_decision_reason_payload(decision: Decision, selected: TradeCandidate | None) -> dict[str, Any]:
    return {
        "risk_reasons": [str(item) for item in (decision.risk_check.reasons or [])[:3]],
        "risk_warnings": [str(item) for item in (decision.risk_check.warnings or [])[:2]],
        "candidate_reasons": [str(item) for item in ((selected.reasons if selected else []) or [])[:4]],
        "candidate_warnings": [str(item) for item in ((selected.warnings if selected else []) or [])[:3]],
    }


def _compact_decision_payload(decision: Decision) -> dict[str, Any]:
    selected = decision.selected
    candidates = decision.candidates or []
    return {
        "created_at": to_jsonable(decision.created_at),
        "mode": decision.mode,
        "action": decision.action,
        "selected_symbol": selected.symbol if selected else None,
        "selected_side": selected.side if selected else None,
        "selected_confidence": float(selected.confidence) if selected else None,
        "candidate_count": len(candidates),
        "top_symbols": [str(item.symbol) for item in candidates[:3]],
        "risk_passed": bool(decision.risk_check.passed),
        "execution_submitted": bool(decision.execution.submitted) if decision.execution else False,
        "execution_order_id": decision.execution.order_id if decision.execution else None,
    }


def _candidate_from_payload(payload: dict[str, Any]) -> TradeCandidate:
    return TradeCandidate(
        symbol=str(payload.get("symbol") or ""),
        base=str(payload.get("base") or str(payload.get("symbol") or "").split("/")[0]),
        side=str(payload.get("side") or "long"),  # type: ignore[arg-type]
        confidence=_safe_float(payload.get("confidence")),
        entry=_safe_float(payload.get("entry") or payload.get("entry_price")),
        stop_loss=_safe_float(payload.get("stop_loss")),
        take_profit=_safe_float(payload.get("take_profit") or payload.get("take_profit1")),
        risk_reward=_safe_float(payload.get("risk_reward"), 1.0),
        order_usdt=_safe_float(payload.get("order_usdt") or payload.get("notional_usdt")),
        quantity=None if payload.get("quantity") is None else _safe_float(payload.get("quantity")),
        spread_pct=None if payload.get("spread_pct") is None else _safe_float(payload.get("spread_pct")),
        news_score=_safe_float(payload.get("news_score")),
        news_count=_safe_int(payload.get("news_count")),
        higher_timeframes=payload.get("higher_timeframes") or {},
        indicator_summary=payload.get("indicator_summary") or {},
        candlestick_patterns=payload.get("candlestick_patterns") or {},
        rule_score=None if payload.get("rule_score") is None else _safe_float(payload.get("rule_score")),
        margin_usdt=None if payload.get("margin_usdt") is None else _safe_float(payload.get("margin_usdt")),
        recovery_margin_usdt=None
        if payload.get("recovery_margin_usdt") is None
        else _safe_float(payload.get("recovery_margin_usdt")),
        recovery_source_key=payload.get("recovery_source_key"),
        sizing_notes=[str(item) for item in payload.get("sizing_notes") or []],
        win_probability_pct=None
        if payload.get("win_probability_pct") is None
        else _safe_float(payload.get("win_probability_pct")),
        target_mode=str(payload.get("target_mode") or "atr_rr"),
        take_profit_pct=None
        if payload.get("take_profit_pct") is None
        else _safe_float(payload.get("take_profit_pct")),
        stop_loss_pct=None
        if payload.get("stop_loss_pct") is None
        else _safe_float(payload.get("stop_loss_pct")),
        price_take_profit_pct=None
        if payload.get("price_take_profit_pct") is None
        else _safe_float(payload.get("price_take_profit_pct")),
        price_stop_loss_pct=None
        if payload.get("price_stop_loss_pct") is None
        else _safe_float(payload.get("price_stop_loss_pct")),
        reasons=[str(item) for item in payload.get("reasons") or []],
        warnings=[str(item) for item in payload.get("warnings") or []],
        previous_win_probability_pct=None
        if payload.get("previous_win_probability_pct") is None
        else _safe_float(payload.get("previous_win_probability_pct")),
        win_delta_pct=None if payload.get("win_delta_pct") is None else _safe_float(payload.get("win_delta_pct")),
        scan_source=str(payload.get("scan_source") or "new_scan"),
        setup_quality=payload.get("setup_quality"),
        position_slot=None if payload.get("position_slot") is None else _safe_int(payload.get("position_slot")),
        risk_percent=None if payload.get("risk_percent") is None else _safe_float(payload.get("risk_percent")),
        market_regime=payload.get("market_regime"),
        regime_confidence=None
        if payload.get("regime_confidence") is None
        else _safe_float(payload.get("regime_confidence")),
        decision_metadata=payload.get("decision_metadata") or {},
    )


def candidate_to_payload(candidate: TradeCandidate) -> dict[str, Any]:
    return _candidate_payload(candidate)


def candidate_from_payload(payload: dict[str, Any]) -> TradeCandidate:
    return _candidate_from_payload(payload)


def _prompt_dir(config: dict[str, Any]) -> Path:
    prompt_config = config.get("prompt_engine", {})
    return project_path(config, str(prompt_config.get("directory", "Prompts") or "Prompts"))


def load_prompt_templates(config: dict[str, Any], *, force_reload: bool = False) -> dict[str, str]:
    path = _prompt_dir(config)
    cache = config.setdefault("_prompt_template_cache", {})
    if cache and not force_reload:
        return dict(cache)
    templates: dict[str, str] = {}
    path.mkdir(parents=True, exist_ok=True)
    for name in PROMPT_FILE_ORDER:
        file_path = path / name
        templates[name] = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
    config["_prompt_template_cache"] = dict(templates)
    return templates


def _prompt_hash(templates: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name in PROMPT_FILE_ORDER:
        if name == "output-format.txt" or name in templates:
            digest.update(name.encode("utf-8"))
            digest.update(b"\n")
            digest.update(str(templates.get(name, "")).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def ensure_prompt_version(config: dict[str, Any]) -> dict[str, Any]:
    templates = load_prompt_templates(config)
    prompt_hash = _prompt_hash(templates)
    prompt_config = config.get("prompt_engine", {})
    version = str(prompt_config.get("default_prompt_version", "prompt-v1") or "prompt-v1")
    description = "Dong bo tu file prompt trong workspace"
    files_json = json.dumps(templates, ensure_ascii=False)
    stored = save_prompt_version(
        config,
        {
            "version": version,
            "hash": prompt_hash,
            "description": description,
            "created_at": _iso_now(),
            "is_active": 1,
            "files_json": files_json,
            "prompt_hash": prompt_hash,
        },
    )
    return stored if stored else {
        "version": version,
        "hash": prompt_hash,
        "prompt_hash": prompt_hash,
    }


def _select_prompt_experiment(config: dict[str, Any]) -> dict[str, Any] | None:
    rows = list_ai_experiment_rows(config, enabled_only=True)
    if not rows:
        return None
    experiments = sorted(rows, key=lambda item: (str(item.get("created_at") or ""), _safe_int(item.get("id"))))
    total = sum(max(0.0, _safe_float(item.get("traffic_percent"), 0.0)) for item in experiments)
    if total <= 0:
        return experiments[0]
    ticket = random.uniform(0.0, total)
    cumulative = 0.0
    for experiment in experiments:
        cumulative += max(0.0, _safe_float(experiment.get("traffic_percent"), 0.0))
        if ticket <= cumulative:
            return experiment
    return experiments[-1]


def _resolve_prompt_version(config: dict[str, Any]) -> tuple[str, str, str | None]:
    version_row = ensure_prompt_version(config)
    experiment = _select_prompt_experiment(config)
    if experiment:
        return str(experiment.get("prompt_version") or version_row["version"]), str(version_row["prompt_hash"]), str(
            experiment.get("name")
        )
    return str(version_row["version"]), str(version_row["prompt_hash"]), None


def estimate_prompt_cache(templates: dict[str, str], market_json: str) -> dict[str, float]:
    static_text = "\n".join(templates.get(name, "") for name in PROMPT_FILE_ORDER if name != "mini-analysis.txt")
    static_tokens = max(1.0, round(len(static_text) / 4.0, 2))
    dynamic_tokens = max(1.0, round(len(market_json) / 4.0, 2))
    total = static_tokens + dynamic_tokens
    cache_hit_percent = round(static_tokens / total * 100, 2) if total else 0.0
    return {
        "estimated_static_tokens": static_tokens,
        "estimated_dynamic_tokens": dynamic_tokens,
        "estimated_cache_hit": cache_hit_percent,
    }


def build_prompt(
    config: dict[str, Any],
    market_dto: dict[str, Any],
    *,
    instruction_key: str,
    recovery_mode: bool = False,
    health_warning: bool = False,
) -> dict[str, Any]:
    templates = load_prompt_templates(config)
    prompt_version, prompt_hash, experiment_name = _resolve_prompt_version(config)
    market_json = json.dumps(market_dto, ensure_ascii=False, separators=(",", ":"))
    sections = [
        templates.get("system.txt", "").strip(),
        templates.get(PROMPT_INSTRUCTION_MAP.get(instruction_key, "final-decision.txt"), "").strip(),
    ]
    if recovery_mode:
        sections.append(templates.get("recovery-mode.txt", "").strip())
    if health_warning:
        sections.append(templates.get("health-warning.txt", "").strip())
    sections.append(templates.get("output-format.txt", "").strip())
    user_parts = [part for part in sections[1:] if part]
    user_parts.append(market_json)
    estimator = estimate_prompt_cache(templates, market_json)
    return {
        "messages": [
            {"role": "system", "content": sections[0] or "Return JSON only."},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ],
        "prompt_version": prompt_version,
        "prompt_hash": prompt_hash,
        "experiment_name": experiment_name,
        "instruction_key": instruction_key,
        "market_json": market_json,
        "sections": {
            "system": sections[0],
            "instruction": sections[1] if len(sections) > 1 else "",
            "recovery": templates.get("recovery-mode.txt", "").strip() if recovery_mode else "",
            "health": templates.get("health-warning.txt", "").strip() if health_warning else "",
            "output": templates.get("output-format.txt", "").strip(),
        },
        **estimator,
    }


def _role_api_key(config: dict[str, Any], role_config: dict[str, Any]) -> tuple[str, str]:
    load_dotenv()
    key_env = str(role_config.get("api_key_env", config.get("ai", {}).get("api_key_env", "OPENAI_API_KEY")))
    import os

    return key_env, os.getenv(key_env, "").strip()


def _telegram_notify_ai_api_calls(config: dict[str, Any]) -> bool:
    telegram_config = config.get("notifications", {}).get("telegram", {})
    return bool(telegram_config.get("notify_ai_api_calls", True))


def _local_time_label(iso_value: str) -> str:
    try:
        dt = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
    except ValueError:
        dt = _utcnow()
    return dt.astimezone(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S")


def _ai_call_role(model_name: str, prompt_package: dict[str, Any]) -> str:
    instruction_key = str(prompt_package.get("instruction_key") or "")
    lowered = model_name.lower()
    if instruction_key == "mini-analysis" or "mini" in lowered or "5.4" in lowered:
        return "mini"
    if instruction_key == "final-decision" or "5.5" in lowered:
        return "okx"
    return "ai"


def _okx_reason_vi(reason: Any) -> str:
    text = re.sub(r"\s+", " ", str(reason or "").strip())
    if not text or text == "-":
        return "-"
    replacements = [
        (r"^Missing required 4h bias and 15m confirmation$", "Thiếu bias 4h và xác nhận 15m bắt buộc"),
        (r"^Missing 4h bias and 15m confirmation$", "Thiếu bias 4h và xác nhận 15m"),
        (r"^Missing 4h/15m confirmation$", "Thiếu xác nhận 4h/15m"),
        (
            r"^Cannot verify required 4h bias or 15m confirmation$",
            "Chưa xác minh được bias 4h hoặc xác nhận 15m bắt buộc",
        ),
        (
            r"^volume ratio ([0-9.]+) is not strong enough for final approval$",
            r"volume ratio \1 chưa đủ mạnh để duyệt cuối",
        ),
        (
            r"^volume ratio ([0-9.]+) is weak despite 1h/5m support$",
            r"volume ratio \1 còn yếu dù 1h/5m đang ủng hộ",
        ),
        (
            r"^volume ratio ([0-9.]+) is weak$",
            r"volume ratio \1 còn yếu",
        ),
        (
            r"^volume ratio ([0-9.]+) and RR ([0-9.]+) are only borderline despite clean risk/slot$",
            r"volume ratio \1 và R:R \2 chỉ ở mức sát ngưỡng dù risk/slot đang sạch",
        ),
        (
            r"^RR ([0-9.]+) is only borderline despite clean risk/slot$",
            r"R:R \1 chỉ ở mức sát ngưỡng dù risk/slot đang sạch",
        ),
        (r"^approval criteria not fully met$", "chưa đạt đủ toàn bộ tiêu chí duyệt"),
        (r"^clean risk/slot$", "risk/slot đang sạch"),
    ]
    parts = [part.strip(" .;") for part in re.split(r"\s*;\s*", text) if part.strip(" .;")]
    translated_parts: list[str] = []
    for part in parts or [text]:
        translated = part
        for pattern, replacement in replacements:
            if re.search(pattern, translated, flags=re.IGNORECASE):
                translated = re.sub(pattern, replacement, translated, flags=re.IGNORECASE)
                break
        translated = translated.replace("RR ", "R:R ").replace(" 4H ", " 4h ").replace(" 15M ", " 15m ")
        translated_parts.append(translated[:220].rstrip(" ."))
    return "; ".join(translated_parts) or "-"


def okx_review_explanation_vi(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "").upper()
    market_reason = _okx_reason_vi(item.get("market_reason"))
    keep_reason = _okx_reason_vi(item.get("keep_reason"))
    delete_reason = _okx_reason_vi(item.get("delete_reason"))
    if "XÓA SETUP" in status:
        return f"5.5 từ chối vì {delete_reason if delete_reason != '-' else 'setup chưa đạt duyệt cuối'}."
    if "MỞ MARKET" in status:
        return f"5.5 đồng ý mở Market vì {market_reason if market_reason != '-' else 'setup đã đủ điều kiện vào lệnh'}."
    return f"5.5 chưa mở Market và giữ setup vì {keep_reason if keep_reason != '-' else 'setup cần chờ xác nhận thêm'}."


def _extract_prompt_symbols(prompt_package: dict[str, Any], parsed: dict[str, Any] | None = None) -> list[str]:
    symbols: list[str] = []
    parsed = parsed or {}
    for value in parsed.get("approved_symbols") or []:
        symbol = str(value)
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    content = str(prompt_package.get("market_json") or "")
    if not content:
        messages = prompt_package.get("messages") or []
        content = "\n".join(str(message.get("content") or "") for message in messages if isinstance(message, dict))
    for match in re.finditer(r'"symbol"\s*:\s*"([^"]+)"', content):
        symbol = match.group(1)
        if symbol and symbol not in symbols:
            symbols.append(symbol)
        if len(symbols) >= 5:
            break
    return symbols


def _extract_prompt_candidates(prompt_package: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    content = str(prompt_package.get("market_json") or "")
    if not content:
        return candidates
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return candidates
    raw_candidates = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(raw_candidates, list):
        return candidates
    for item in raw_candidates[:3]:
        if not isinstance(item, dict):
            continue
        candidates.append(
            {
                "symbol": item.get("symbol"),
                "side": item.get("side"),
                "confidence": item.get("confidence"),
                "win_probability_pct": item.get("win_probability_pct"),
                "risk_reward": item.get("risk_reward"),
                "reasons": item.get("reasons") if isinstance(item.get("reasons"), list) else [],
            }
        )
    return candidates


def recent_ai_call_history(config: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
    raw = get_journal_state(config, AI_CALL_HISTORY_STATE_KEY)
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)][-limit:]


def _ascii_fold(value: Any) -> str:
    text = str(value or "").lower()
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _is_gpt55_review_call(item: dict[str, Any]) -> bool:
    model = _ascii_fold(item.get("model"))
    role = _ascii_fold(item.get("role"))
    review_kind = _ascii_fold(item.get("review_kind"))
    route = _ascii_fold(item.get("route"))
    return (
        "5.5" in model
        or role == "okx"
        or review_kind == "lc_okx_review"
        or "lc_okx" in route
    )


def _is_gpt55_no_trade_rejection(item: dict[str, Any]) -> bool:
    if not _is_gpt55_review_call(item):
        return False
    status = _ascii_fold(item.get("status") or item.get("decision") or item.get("result"))
    if not status:
        return False
    if any(pattern in status for pattern in ("giu setup", "giu theo doi", "giu lai setup", "keep", "watchlist")):
        return False
    return any(
        pattern in status
        for pattern in (
            "xoa setup",
            "khong vao lenh",
            "no_trade",
            "no trade",
            "tu choi",
            "reject",
            "rejected",
            "delete",
            "cancel",
            "canceled",
            "cancelled",
        )
    )


def _ai_call_status_stats_from_history(items: list[dict[str, Any]]) -> dict[str, Any]:
    no_trade_count = sum(1 for item in items if _is_gpt55_no_trade_rejection(item))
    return {
        "version": 1,
        "no_trade_count": no_trade_count,
    }


def _load_ai_call_status_stats(config: dict[str, Any], *, seed_from_history: bool = True) -> dict[str, Any]:
    raw = get_journal_state(config, AI_CALL_STATUS_STATS_STATE_KEY)
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            return {
                "version": int(payload.get("version") or 1),
                "no_trade_count": max(0, int(payload.get("no_trade_count") or 0)),
                "updated_at": payload.get("updated_at"),
            }
    if not seed_from_history:
        return {"version": 1, "no_trade_count": 0}
    stats = _ai_call_status_stats_from_history(recent_ai_call_history(config, limit=50))
    stats["updated_at"] = _iso_now()
    set_journal_state(config, AI_CALL_STATUS_STATS_STATE_KEY, json.dumps(stats, ensure_ascii=False))
    return stats


def _record_ai_call_status_stats(config: dict[str, Any], item: dict[str, Any]) -> None:
    try:
        stats = _load_ai_call_status_stats(config, seed_from_history=True)
        if _is_gpt55_no_trade_rejection(item):
            stats["no_trade_count"] = max(0, int(stats.get("no_trade_count") or 0)) + 1
        stats["updated_at"] = _iso_now()
        set_journal_state(config, AI_CALL_STATUS_STATS_STATE_KEY, json.dumps(stats, ensure_ascii=False))
    except Exception:
        return


def _record_ai_call_history(config: dict[str, Any], item: dict[str, Any]) -> None:
    try:
        history = recent_ai_call_history(config, limit=50)
        history.append(item)
        set_journal_state(config, AI_CALL_HISTORY_STATE_KEY, json.dumps(history[-50:], ensure_ascii=False))
    except Exception:
        return


def _lc_okx_review_call_message(item: dict[str, Any]) -> str:
    created_at = str(item.get("created_at") or _iso_now())
    lc_id = item.get("lc_okx_id")
    symbol = str(item.get("symbol") or ((item.get("symbols") or ["-"])[0]))
    side = str(item.get("side") or "-").upper()
    status = str(item.get("status") or "GIỮ SETUP")
    lines = [
        f"🤖 5.5 DUYỆT LC_OKX #{lc_id if lc_id not in (None, '') else '-'}",
        f"Cặp: {symbol} | {side}",
        f"Duyệt lúc: {_local_time_label(created_at).split()[-1]}",
        f"Kết quả: {status}",
        f"Giải thích: {okx_review_explanation_vi(item)[:700]}",
    ]
    return "\n".join(lines)


def _ai_call_message(item: dict[str, Any]) -> str:
    if str(item.get("review_kind") or "") == "lc_okx_review":
        return _lc_okx_review_call_message(item)
    role = str(item.get("role") or "ai")
    status = str(item.get("status") or "-")
    symbols = ", ".join(str(symbol) for symbol in item.get("symbols") or []) or "-"
    lines = [
        "🤖 AI được gọi" if role != "okx" else "🤖 AI OKX 5.5 được gọi",
        f"Model: {item.get('model', '-')}",
        f"Cặp giao dịch: {symbols}",
        f"Trạng thái: {status}",
    ]
    if role == "mini":
        lines.append(f"Thời gian mini đề xuất LC: {_local_time_label(str(item.get('created_at') or _iso_now()))}")
    elif role == "okx":
        if item.get("approved"):
            lines.append(f"Thời gian vào lệnh: {_local_time_label(str(item.get('created_at') or _iso_now()))}")
            lines.append(f"Lý do vào lệnh: {str(item.get('reason') or '-')[:700]}")
        else:
            lines.append(f"Thời gian check: {_local_time_label(str(item.get('created_at') or _iso_now()))}")
            lines.append(f"Lý do không vào lệnh: {str(item.get('reason') or '-')[:700]}")
    else:
        lines.append(f"Thời gian gọi: {_local_time_label(str(item.get('created_at') or _iso_now()))}")
    if item.get("latency_ms") is not None:
        lines.append(f"Độ trễ: {item.get('latency_ms')} ms")
    return "\n".join(lines)


def _notify_openai_api_call(
    config: dict[str, Any],
    *,
    model_name: str,
    prompt_package: dict[str, Any],
    success: bool,
    latency_ms: float | None = None,
    usage: dict[str, Any] | None = None,
    error: str | None = None,
    parsed: dict[str, Any] | None = None,
) -> None:
    created_at = _iso_now()
    role = _ai_call_role(model_name, prompt_package)
    parsed = parsed or {}
    approved = bool(parsed.get("approved")) if success else False
    status = "ERROR"
    if success:
        if role == "okx":
            status = "VÀO LỆNH" if approved else "KHÔNG VÀO LỆNH"
        elif role == "mini":
            approved_symbols = parsed.get("approved_symbols") or []
            status = "MINI ĐỀ XUẤT LC" if approved_symbols or approved else "NO_TRADE"
        else:
            status = "OK"
    item = {
        "created_at": created_at,
        "role": role,
        "model": model_name,
        "symbols": _extract_prompt_symbols(prompt_package, parsed),
        "candidate_details": _extract_prompt_candidates(prompt_package),
        "approved_symbols": parsed.get("approved_symbols") if isinstance(parsed.get("approved_symbols"), list) else [],
        "setup_scores": parsed.get("setup_scores") if isinstance(parsed.get("setup_scores"), dict) else {},
        "status": status,
        "approved": approved,
        "decision": parsed.get("decision"),
        "reason": str(parsed.get("reason") or error or ""),
        "prompt_version": prompt_package.get("prompt_version"),
        "prompt_hash": prompt_package.get("prompt_hash"),
        "latency_ms": latency_ms,
        "prompt_tokens": _safe_int((usage or {}).get("prompt_tokens")),
        "completion_tokens": _safe_int((usage or {}).get("completion_tokens")),
    }
    _record_ai_call_status_stats(config, item)
    _record_ai_call_history(config, item)
    if not _telegram_notify_ai_api_calls(config):
        return
    try:
        from .notifier import send_telegram_message

        if success:
            text = _ai_call_message(item)
        else:
            text = "\n".join(
                [
                    "🚨 GPT API lỗi",
                    f"Model: {model_name}",
                    f"Cặp giao dịch: {', '.join(item['symbols']) if item['symbols'] else '-'}",
                    f"Trạng thái: {status}",
                    f"Lỗi: {(error or '-')[:450]}",
                ]
            )
        send_telegram_message(config, text, with_buttons=False, replace_previous=False)
    except Exception:
        return


def record_ai_call_event(
    config: dict[str, Any],
    item: dict[str, Any],
    *,
    notify_telegram: bool = True,
) -> None:
    payload = dict(item)
    payload.setdefault("created_at", _iso_now())
    _record_ai_call_status_stats(config, payload)
    _record_ai_call_history(config, payload)
    if not notify_telegram or not _telegram_notify_ai_api_calls(config):
        return
    try:
        from .notifier import send_telegram_message

        send_telegram_message(config, _ai_call_message(payload), with_buttons=False, replace_previous=False)
    except Exception:
        return

def call_openai_json(
    config: dict[str, Any],
    role_config: dict[str, Any],
    prompt_package: dict[str, Any],
    *,
    model_name: str,
    purpose: str | None = None,
    route: str | None = None,
    manual_trigger: bool = False,
    lc_okx_review_once: bool = False,
    record_history: bool = True,
    notify_telegram: bool = True,
) -> dict[str, Any]:
    ai_settings = config.get("ai", {})
    purpose = str(purpose or "").strip()
    route = str(route or "").strip()
    if not bool(ai_settings.get("enabled", True)):
        raise RuntimeError("OpenAI API calls are disabled by config: ai.enabled=false")
    if not bool(ai_settings.get("allow_api_calls", False)):
        raise RuntimeError("OpenAI API calls are disabled by config: ai.allow_api_calls=false")
    if not purpose:
        raise RuntimeError("OpenAI API call blocked: missing ai call purpose")
    allowed_purposes = {
        "mini_market_scan",
        "okx_final_approval",
    }
    if bool(ai_settings.get("replay", {}).get("allow_api_calls", False)):
        allowed_purposes.add("replay")
    if bool(ai_settings.get("debug", {}).get("allow_api_calls", False)):
        allowed_purposes.add("debug_fake_flow")
    if purpose not in allowed_purposes:
        raise RuntimeError(f"OpenAI API call blocked by policy: purpose={purpose}")
    if purpose == "mini_market_scan":
        internal_config = ai_settings.get("internal", {})
        if not bool(internal_config.get("market_scan_enabled", True)):
            raise RuntimeError("OpenAI mini scan blocked: ai.internal.market_scan_enabled=false")
        if not bool(internal_config.get("market_scan_use_ai", True)):
            raise RuntimeError("OpenAI mini scan blocked: ai.internal.market_scan_use_ai=false")
    if purpose == "okx_final_approval":
        allowed_routes = {"lc_okx_setup_review"}
        if route not in allowed_routes:
            raise RuntimeError(f"OpenAI OKX approval blocked by policy: route={route or '-'}")
        okx_config = ai_settings.get("okx", {})
        if manual_trigger:
            raise RuntimeError("OpenAI OKX approval blocked: manual 5.5 calls are disabled")
        elif lc_okx_review_once:
            if route != "lc_okx_setup_review":
                raise RuntimeError(f"OpenAI OKX LC_OKX one-shot blocked by policy: route={route or '-'}")
            if not bool(okx_config.get("auto_lc_okx_review_once_enabled", False)):
                raise RuntimeError("OpenAI OKX approval blocked: ai.okx.auto_lc_okx_review_once_enabled=false")
        elif not bool(okx_config.get("auto_openai_enabled", False)):
            raise RuntimeError("OpenAI OKX approval blocked: ai.okx.auto_openai_enabled=false")
        if not bool(okx_config.get("approval_enabled", True)):
            raise RuntimeError("OpenAI OKX approval blocked: ai.okx.approval_enabled=false")
    key_env, api_key = _role_api_key(config, role_config)
    if not api_key:
        raise RuntimeError(f"missing {key_env}")
    payload = {
        "model": model_name,
        "response_format": {"type": "json_object"},
        "messages": prompt_package["messages"],
    }
    start = time.perf_counter()
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
        if record_history or notify_telegram:
            _notify_openai_api_call(
                config,
                model_name=model_name,
                prompt_package=prompt_package,
                success=False,
                error=detail,
            )
        raise RuntimeError(detail) from exc
    except Exception as exc:
        if record_history or notify_telegram:
            _notify_openai_api_call(
                config,
                model_name=model_name,
                prompt_package=prompt_package,
                success=False,
                error=str(exc),
            )
        raise
    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    content = raw["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    usage = raw.get("usage") or {}
    register_prompt_metric(
        config,
        {
            "prompt_version": prompt_package["prompt_version"],
            "prompt_hash": prompt_package["prompt_hash"],
            "prompt_tokens": _safe_float(usage.get("prompt_tokens"), prompt_package["estimated_static_tokens"]),
            "completion_tokens": _safe_float(usage.get("completion_tokens"), 0),
            "latency_ms": latency_ms,
            "estimated_cached_tokens": prompt_package["estimated_static_tokens"],
            "estimated_dynamic_tokens": prompt_package["estimated_dynamic_tokens"],
            "cache_hit_percent": prompt_package["estimated_cache_hit"],
        },
    )
    if record_history or notify_telegram:
        _notify_openai_api_call(
            config,
            model_name=model_name,
            prompt_package=prompt_package,
            success=True,
            latency_ms=latency_ms,
            usage=usage,
            parsed=parsed,
        )
    register_model_version(
        config,
        model_name=model_name,
        model_version=model_name,
        prompt_version=prompt_package["prompt_version"],
        prompt_hash=prompt_package["prompt_hash"],
    )
    return {
        "parsed": parsed,
        "raw_response": content,
        "raw_payload": raw,
        "latency_ms": latency_ms,
        "prompt_tokens": _safe_int(usage.get("prompt_tokens"), _safe_int(prompt_package["estimated_static_tokens"])),
        "completion_tokens": _safe_int(usage.get("completion_tokens")),
    }


def register_model_version(
    config: dict[str, Any],
    *,
    model_name: str,
    model_version: str,
    prompt_version: str,
    prompt_hash: str,
) -> None:
    ensure_ai_model_version(
        config,
        model_name=model_name,
        model_version=model_version,
        prompt_version=prompt_version,
        prompt_hash=prompt_hash,
        created_at=_iso_now(),
    )


def register_prompt_metric(config: dict[str, Any], metric: dict[str, Any]) -> None:
    key = str(metric.get("prompt_version") or "")
    if not key:
        return
    merge_prompt_metric(config, metric)


def prompt_status(config: dict[str, Any], dynamic_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    version_row = ensure_prompt_version(config)
    templates = load_prompt_templates(config)
    market_json = json.dumps(dynamic_payload or {"scanTime": _iso_now()}, ensure_ascii=False, separators=(",", ":"))
    estimator = estimate_prompt_cache(templates, market_json)
    metrics = get_prompt_metric(config, str(version_row["version"]))
    payload = {
        "promptVersion": version_row["version"],
        "promptHash": version_row["prompt_hash"],
        "estimatedStaticTokens": estimator["estimated_static_tokens"],
        "estimatedDynamicTokens": estimator["estimated_dynamic_tokens"],
        "estimatedCacheHit": estimator["estimated_cache_hit"],
    }
    if metrics:
        payload["metrics"] = metrics
    return payload


def prompt_history(config: dict[str, Any]) -> list[dict[str, Any]]:
    ensure_prompt_version(config)
    rows = list_prompt_versions(config)
    return [
        {
            "version": row.get("version"),
            "created_at": row.get("created_at"),
            "description": row.get("description"),
            "hash": row.get("prompt_hash"),
            "is_active": row.get("is_active"),
        }
        for row in rows
    ]


def create_ai_experiment(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    prompt_version = str(payload.get("prompt_version") or ensure_prompt_version(config)["version"])
    record = {
        "name": str(payload.get("name") or f"experiment-{_safe_int(time.time())}"),
        "description": str(payload.get("description") or ""),
        "prompt_version": prompt_version,
        "traffic_percent": max(0.0, _safe_float(payload.get("traffic_percent"), 50.0)),
        "enabled": bool(payload.get("enabled", True)),
        "created_at": _iso_now(),
    }
    return save_ai_experiment(
        config,
        {
            **record,
            "enabled": _bool_int(record["enabled"]),
        },
    )


def list_ai_experiments(config: dict[str, Any]) -> list[dict[str, Any]]:
    return list_ai_experiment_rows(config)


def ensure_strategy_versions(config: dict[str, Any]) -> None:
    settings = config.get("strategy_versioning", {})
    version = str(settings.get("default_version", "strategy-v1") or "strategy-v1")
    payload = {
        "strategy": deepcopy(config.get("strategy", {})),
        "risk": deepcopy(config.get("risk", {})),
        "pending_orders": deepcopy(config.get("pending_orders", {})),
        "position_sizing": deepcopy(config.get("position_sizing", {})),
        "trading_risk": deepcopy(config.get("trading_risk", {})),
    }
    record = {
        "version": version,
        "name": version.upper(),
        "description": "Cau hinh chuan dang chay trong repo Python",
        "created_at": _iso_now(),
        "is_active": 1,
        "traffic_percent": 100.0,
        "indicators_json": json.dumps(config.get("strategy", {}).get("confirmation_timeframes", {}), ensure_ascii=False),
        "rules_json": json.dumps(config.get("strategy", {}), ensure_ascii=False),
        "risk_config_json": json.dumps(config.get("risk", {}), ensure_ascii=False),
        "payload_json": json.dumps(payload, ensure_ascii=False),
    }
    existing = get_strategy_version(config, version)
    if existing is None:
        ensure_strategy_version(config, record)
        return
    if str(existing.get("payload_json") or "{}") == str(record["payload_json"]):
        return
    record["created_at"] = existing.get("created_at") or record["created_at"]
    record["is_active"] = int(existing.get("is_active", record["is_active"]) or 0)
    record["traffic_percent"] = float(existing.get("traffic_percent", record["traffic_percent"]) or 0)
    save_strategy_version(config, record, deactivate_others=False)


def create_strategy_version(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    ensure_strategy_versions(config)
    version = str(payload.get("version") or f"strategy-{int(time.time())}")
    record = {
        "version": version,
        "name": str(payload.get("name") or version.upper()),
        "description": str(payload.get("description") or ""),
        "created_at": _iso_now(),
        "is_active": _bool_int(bool(payload.get("is_active", False))),
        "traffic_percent": max(0.0, _safe_float(payload.get("traffic_percent"), 100.0)),
        "indicators_json": json.dumps(payload.get("indicators") or {}, ensure_ascii=False),
        "rules_json": json.dumps(payload.get("rules") or payload.get("strategy") or {}, ensure_ascii=False),
        "risk_config_json": json.dumps(payload.get("risk_config") or payload.get("risk") or {}, ensure_ascii=False),
        "payload_json": json.dumps(payload.get("payload") or payload.get("overrides") or {}, ensure_ascii=False),
    }
    result = save_strategy_version(config, record, deactivate_others=bool(record["is_active"]))
    result["performance"] = _strategy_performance_stats(config, version)
    return result


def activate_strategy_version(config: dict[str, Any], version: str) -> dict[str, Any]:
    ensure_strategy_versions(config)
    row = activate_strategy_version_record(config, version)
    if row is None:
        raise ValueError(f"Strategy version not found: {version}")
    result = dict(row)
    result["performance"] = _strategy_performance_stats(config, version)
    return result


def strategy_history(config: dict[str, Any]) -> list[dict[str, Any]]:
    ensure_strategy_versions(config)
    items = list_strategy_versions(config)
    for item in items:
        item["performance"] = _strategy_performance_stats(config, str(item.get("version") or ""))
    return items


def current_strategy_state(config: dict[str, Any]) -> dict[str, Any]:
    ensure_strategy_versions(config)
    active = list_strategy_versions(config, active_only=True, order="id_asc")
    for item in active:
        item["performance"] = _strategy_performance_stats(config, str(item.get("version") or ""))
    return {
        "active": active,
        "count": len(active),
    }


def select_runtime_config(config: dict[str, Any]) -> dict[str, Any]:
    ensure_strategy_versions(config)
    runtime = deepcopy(config)
    active = list_strategy_versions(config, active_only=True, order="created_asc")
    if not active:
        runtime["selected_strategy_version"] = config.get("strategy_versioning", {}).get("default_version", "strategy-v1")
        return runtime
    if len(active) == 1:
        selected = active[0]
    else:
        total = sum(max(0.0, _safe_float(item.get("traffic_percent"), 0.0)) for item in active)
        ticket = random.uniform(0.0, total if total > 0 else float(len(active)))
        cumulative = 0.0
        selected = active[-1]
        for item in active:
            cumulative += max(0.0, _safe_float(item.get("traffic_percent"), 0.0)) or 1.0
            if ticket <= cumulative:
                selected = item
                break
    default_version = str(config.get("strategy_versioning", {}).get("default_version", "strategy-v1"))
    overrides = _json_loads(selected.get("payload_json"), {})
    # The default Mongo row mirrors the deployed repository config. It is not
    # an override source; otherwise an old cooldown/slot limit survives every
    # deployment and also masks newer runtime UI settings.
    if str(selected.get("version") or "") != default_version and isinstance(overrides, dict) and overrides:
        runtime = deep_merge(runtime, overrides)
    runtime["selected_strategy_version"] = selected.get("version")
    return runtime


MARKET_REGIME_DEFAULT_TOP_SYMBOLS = ("BTC/USDT:USDT", "SOL/USDT:USDT", "ETH/USDT:USDT")
MARKET_REGIME_DEFAULT_AGGREGATE_LIMIT = 40


def _market_regime_top_symbols(config: dict[str, Any]) -> list[str]:
    raw = config.get("market_regime", {}).get("top_symbols")
    symbols = raw if isinstance(raw, list) else list(MARKET_REGIME_DEFAULT_TOP_SYMBOLS)
    result: list[str] = []
    for symbol in symbols:
        clean = str(symbol or "").strip()
        if clean and clean not in result:
            result.append(clean)
    return result or list(MARKET_REGIME_DEFAULT_TOP_SYMBOLS)


def _market_regime_aggregate_limit(config: dict[str, Any]) -> int:
    settings = config.get("market_regime", {})
    universe = config.get("strategy", {}).get("universe", {})
    raw = settings.get("aggregate_limit", universe.get("max_symbols", MARKET_REGIME_DEFAULT_AGGREGATE_LIMIT))
    try:
        limit = int(raw or MARKET_REGIME_DEFAULT_AGGREGATE_LIMIT)
    except (TypeError, ValueError):
        limit = MARKET_REGIME_DEFAULT_AGGREGATE_LIMIT
    return max(1, min(40, limit))


def _market_regime_indicator_from_snapshot(snapshot: Any) -> dict[str, Any]:
    indicator = to_jsonable(getattr(snapshot, "indicator_summary", None) or getattr(snapshot, "__dict__", {}) or {})
    if not isinstance(indicator, dict):
        indicator = {}
    symbol = str(indicator.get("symbol") or getattr(snapshot, "symbol", "") or "").strip()
    if symbol:
        indicator["symbol"] = symbol
    return indicator


def _finite_market_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def _market_numbers(indicators: list[dict[str, Any]], key: str) -> list[float]:
    numbers: list[float] = []
    for indicator in indicators:
        value = _finite_market_number(indicator.get(key))
        if value is not None:
            numbers.append(value)
    return numbers


def _market_average(values: list[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _market_median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _market_percent_true(indicators: list[dict[str, Any]], left_key: str, right_key: str) -> float | None:
    valid = 0
    passed = 0
    for indicator in indicators:
        left = _finite_market_number(indicator.get(left_key))
        right = _finite_market_number(indicator.get(right_key))
        if left is None or right is None:
            continue
        valid += 1
        if left >= right:
            passed += 1
    if not valid:
        return None
    return round((passed / valid) * 100.0, 2)


def _market_symbol_trend_score(indicator: dict[str, Any]) -> float | None:
    checks: list[bool] = []
    last = _finite_market_number(indicator.get("last"))
    ema_fast = _finite_market_number(indicator.get("ema_fast"))
    ema_slow = _finite_market_number(indicator.get("ema_slow"))
    ema_long = _finite_market_number(indicator.get("ema200"))
    current_vwap = _finite_market_number(indicator.get("vwap"))
    if last is not None and ema_fast is not None:
        checks.append(last >= ema_fast)
    if ema_fast is not None and ema_slow is not None:
        checks.append(ema_fast >= ema_slow)
    if last is not None and ema_long is not None:
        checks.append(last >= ema_long)
    if last is not None and current_vwap is not None:
        checks.append(last >= current_vwap)
    if not checks:
        return None
    return (sum(1 for item in checks if item) / len(checks)) * 100.0


def _aggregate_market_regime_indicators(
    config: dict[str, Any],
    indicators: list[dict[str, Any]],
    *,
    target_symbols: list[str],
    target_count: int | None = None,
    detail_symbols: list[str] | None = None,
    created_at: str,
) -> tuple[dict[str, Any], str, float, str]:
    covered_symbols = [str(item.get("symbol") or "").strip() for item in indicators if str(item.get("symbol") or "").strip()]
    missing_symbols = [symbol for symbol in target_symbols if symbol not in covered_symbols]
    expected_count = max(len(target_symbols), int(target_count or 0))
    coverage_pct = round((len(covered_symbols) / expected_count) * 100.0, 2) if expected_count else 0.0
    trend_scores = [
        score
        for score in (_market_symbol_trend_score(indicator) for indicator in indicators)
        if score is not None
    ]
    trend_score = _market_average(trend_scores)
    rsi_values = _market_numbers(indicators, "rsi")
    atr_pct_values = _market_numbers(indicators, "atr_pct")
    volume_ratio_values = _market_numbers(indicators, "volume_ratio")
    adx_values = _market_numbers(indicators, "adx")
    fear_greed_values = _market_numbers(indicators, "fear_greed")
    news_score_values = _market_numbers(indicators, "news_score")
    aggregate = {
        "scope": "aggregate",
        "symbol": "MARKET",
        "market_scope": "top_volume",
        "created_at": created_at,
        "aggregate_limit": expected_count,
        "market_symbols": target_symbols,
        "detail_symbols": detail_symbols or _market_regime_top_symbols(config),
        "top_symbols": target_symbols,
        "covered_symbols": covered_symbols,
        "missing_symbols": missing_symbols,
        "coverage_count": len(covered_symbols),
        "target_count": expected_count,
        "coverage_pct": coverage_pct,
        "trend_score": None if trend_score is None else round(trend_score, 2),
        "price_above_ema20_pct": _market_percent_true(indicators, "last", "ema_fast"),
        "ema20_above_ema50_pct": _market_percent_true(indicators, "ema_fast", "ema_slow"),
        "price_above_ema200_pct": _market_percent_true(indicators, "last", "ema200"),
        "price_above_vwap_pct": _market_percent_true(indicators, "last", "vwap"),
        "rsi": None if not rsi_values else round(_market_median(rsi_values) or 0.0, 2),
        "median_rsi": None if not rsi_values else round(_market_median(rsi_values) or 0.0, 2),
        "average_rsi": None if not rsi_values else round(_market_average(rsi_values) or 0.0, 2),
        "adx": None if not adx_values else round(_market_median(adx_values) or 0.0, 2),
        "fear_greed": None if not fear_greed_values else round(_market_average(fear_greed_values) or 0.0, 2),
        "news_score": None if not news_score_values else round(_market_average(news_score_values) or 0.0, 2),
        "median_atr_pct": None if not atr_pct_values else round(_market_median(atr_pct_values) or 0.0, 4),
        "median_volume_ratio": None if not volume_ratio_values else round(_market_median(volume_ratio_values) or 0.0, 4),
    }
    settings = config.get("market_regime", {})
    atr_pct = _finite_market_number(aggregate.get("median_atr_pct")) or 0.0
    median_rsi = _finite_market_number(aggregate.get("median_rsi"))
    score = _finite_market_number(aggregate.get("trend_score"))
    if not indicators:
        regime = "UNKNOWN"
        confidence = 0.0
        reason = f"Khong co snapshot top {expected_count} de danh gia toan thi truong"
    elif atr_pct >= _safe_float(settings.get("high_volatility_atr_pct"), 4.0):
        regime = "HIGH_VOLATILITY"
        confidence = min(99.0, 60.0 + atr_pct * 6.0)
        reason = f"ATR trung vi top {expected_count} {atr_pct:.2f}% cao; do phu {len(covered_symbols)}/{expected_count}"
    elif atr_pct and atr_pct <= _safe_float(settings.get("low_volatility_atr_pct"), 1.2):
        regime = "LOW_VOLATILITY"
        confidence = min(95.0, 58.0 + max(0.0, 1.2 - atr_pct) * 12.0)
        reason = f"ATR trung vi top {expected_count} {atr_pct:.2f}% thap; do phu {len(covered_symbols)}/{expected_count}"
    elif score is not None and score >= 66.0 and (median_rsi is None or median_rsi >= 50.0):
        regime = "BULL"
        confidence = min(96.0, 52.0 + score * 0.35 + coverage_pct * 0.12)
        reason = f"Trend score top {expected_count} {score:.2f}/100 nghieng tang; do phu {len(covered_symbols)}/{expected_count}"
    elif score is not None and score <= 34.0 and (median_rsi is None or median_rsi <= 50.0):
        regime = "BEAR"
        confidence = min(96.0, 52.0 + (100.0 - score) * 0.35 + coverage_pct * 0.12)
        reason = f"Trend score top {expected_count} {score:.2f}/100 nghieng giam; do phu {len(covered_symbols)}/{expected_count}"
    elif score is not None:
        regime = "SIDEWAY"
        confidence = min(88.0, 50.0 + (100.0 - abs(score - 50.0)) * 0.22 + coverage_pct * 0.08)
        reason = f"Trend score top {expected_count} {score:.2f}/100 chua lech manh; do phu {len(covered_symbols)}/{expected_count}"
    else:
        regime = "UNKNOWN"
        confidence = 40.0 + coverage_pct * 0.2
        reason = f"Chua du chi bao xu huong top {expected_count}; do phu {len(covered_symbols)}/{expected_count}"
    if expected_count and len(covered_symbols) < expected_count:
        confidence *= max(0.45, len(covered_symbols) / expected_count)
    return aggregate, regime, round(confidence, 2), reason


def _market_regime_from_indicators(config: dict[str, Any], indicator: dict[str, Any]) -> tuple[str, float, str]:
    settings = config.get("market_regime", {})
    ema_fast = _safe_float(indicator.get("ema_fast"))
    ema_slow = _safe_float(indicator.get("ema_slow"))
    last = _safe_float(indicator.get("last"))
    atr_pct = _safe_float(indicator.get("atr_pct"))
    volume_ratio = _safe_float(indicator.get("volume_ratio"), 1.0)
    rsi = _safe_float(indicator.get("rsi"), 50.0)
    adx = _safe_float(indicator.get("adx"), 0.0)
    if atr_pct >= _safe_float(settings.get("high_volatility_atr_pct"), 4.0):
        return "HIGH_VOLATILITY", min(99.0, 70.0 + atr_pct), f"ATR {atr_pct:.2f}% rat cao"
    if atr_pct <= _safe_float(settings.get("low_volatility_atr_pct"), 1.2):
        return "LOW_VOLATILITY", min(95.0, 65.0 + (1.2 - atr_pct) * 10), f"ATR {atr_pct:.2f}% thap"
    if ema_fast > ema_slow and last >= ema_fast and adx >= _safe_float(settings.get("trend_adx_min"), 25):
        confidence = min(97.0, 60.0 + max(0.0, (rsi - 50.0)) + max(0.0, (volume_ratio - 1.0) * 10.0))
        return "BULL", confidence, "EMA20 > EMA50, gia nam tren EMA va ADX xac nhan"
    if ema_fast < ema_slow and last <= ema_fast and adx >= _safe_float(settings.get("trend_adx_min"), 25):
        confidence = min(97.0, 60.0 + max(0.0, (50.0 - rsi)) + max(0.0, (volume_ratio - 1.0) * 10.0))
        return "BEAR", confidence, "EMA20 < EMA50, gia nam duoi EMA va ADX xac nhan"
    if adx <= _safe_float(settings.get("sideway_adx_max"), 20):
        return "SIDEWAY", min(90.0, 55.0 + max(0.0, 20.0 - adx)), "ADX thap, thi truong di ngang"
    return "UNKNOWN", 50.0, "Chua du tin hieu de xac dinh regime"


def detect_market_regime(config: dict[str, Any], snapshots: list[Any]) -> dict[str, Any]:
    if not snapshots:
        result = {
            "created_at": _iso_now(),
            "regime": "UNKNOWN",
            "confidence": 0.0,
            "indicators": {},
            "reason": "Khong co snapshot de danh gia",
        }
    else:
        indicator = _market_regime_indicator_from_snapshot(snapshots[0])
        regime, confidence, reason = _market_regime_from_indicators(config, indicator)
        result = {
            "created_at": _iso_now(),
            "regime": regime,
            "confidence": round(confidence, 2),
            "indicators": indicator,
            "reason": reason,
        }
    created_at = str(result["created_at"])
    detail_symbols = _market_regime_top_symbols(config)
    aggregate_limit = _market_regime_aggregate_limit(config)
    indicators_by_symbol: dict[str, dict[str, Any]] = {}
    aggregate_symbols: list[str] = []
    for snapshot in snapshots:
        indicator = _market_regime_indicator_from_snapshot(snapshot)
        symbol = str(indicator.get("symbol") or "").strip()
        if symbol and symbol not in indicators_by_symbol:
            indicators_by_symbol[symbol] = indicator
        if symbol and symbol not in aggregate_symbols and len(aggregate_symbols) < aggregate_limit:
            aggregate_symbols.append(symbol)
    target_indicators: list[dict[str, Any]] = []
    for symbol in detail_symbols:
        indicator = indicators_by_symbol.get(symbol)
        if not indicator:
            continue
        scoped_indicator = {**indicator, "scope": "symbol", "created_at": created_at}
        target_indicators.append(scoped_indicator)
        regime, confidence, reason = _market_regime_from_indicators(config, scoped_indicator)
        insert_market_regime_history(
            config,
            {
                "created_at": created_at,
                "regime": regime,
                "confidence": round(confidence, 2),
                "indicators_json": json.dumps(scoped_indicator, ensure_ascii=False),
                "reason": reason,
            },
        )
    aggregate_indicators = [
        {**indicators_by_symbol[symbol], "scope": "market_member", "created_at": created_at}
        for symbol in aggregate_symbols
        if symbol in indicators_by_symbol
    ]
    if aggregate_indicators:
        aggregate_indicator, aggregate_regime, aggregate_confidence, aggregate_reason = _aggregate_market_regime_indicators(
            config,
            aggregate_indicators,
            target_symbols=aggregate_symbols,
            target_count=aggregate_limit,
            detail_symbols=detail_symbols,
            created_at=created_at,
        )
        result = {
            "created_at": created_at,
            "regime": aggregate_regime,
            "confidence": aggregate_confidence,
            "indicators": aggregate_indicator,
            "reason": aggregate_reason,
        }
        insert_market_regime_history(
            config,
            {
                "created_at": created_at,
                "regime": aggregate_regime,
                "confidence": aggregate_confidence,
                "indicators_json": json.dumps(aggregate_indicator, ensure_ascii=False),
                "reason": aggregate_reason,
            },
        )
    elif not snapshots:
        insert_market_regime_history(
            config,
            {
                "created_at": created_at,
                "regime": result["regime"],
                "confidence": result["confidence"],
                "indicators_json": json.dumps(result["indicators"], ensure_ascii=False),
                "reason": result["reason"],
            },
        )
    return result


def current_market_regime(config: dict[str, Any]) -> dict[str, Any]:
    row = latest_market_regime_history(config)
    if row is None:
        return {
            "created_at": None,
            "regime": "UNKNOWN",
            "confidence": 0.0,
            "indicators": {},
            "reason": "Chua co lich su regime",
        }
    payload = dict(row)
    payload["indicators"] = _json_loads(payload.get("indicators_json"), {})
    return payload


def market_regime_history(config: dict[str, Any], limit: int = 100) -> list[dict[str, Any]]:
    rows = list_market_regime_rows(config, limit=limit)
    result: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        payload["indicators"] = _json_loads(payload.get("indicators_json"), {})
        result.append(payload)
    return result


def record_trade_candidates(config: dict[str, Any], candidates: list[TradeCandidate]) -> int:
    if not candidates:
        return 0
    now = _iso_now()
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        payload = _compact_candidate_storage_payload(candidate)
        rows.append(
            {
                "created_at": now,
                "symbol": candidate.symbol,
                "side": candidate.side.upper(),
                "rule_score": _candidate_rule_score(candidate),
                "gpt_confidence": float(candidate.confidence or 0),
                "risk_reward": float(candidate.risk_reward or 0),
                "entry_price": candidate.entry,
                "stop_loss": candidate.stop_loss,
                "take_profit": candidate.take_profit,
                "is_used": 0,
                "used_at": None,
                "payload_json": json.dumps(payload, ensure_ascii=False),
            }
        )
    return insert_trade_candidate_rows(config, rows)


def _side_label(candidate: TradeCandidate | None) -> str:
    if candidate is None:
        return "NONE"
    return candidate.side.upper()


def _decision_label(decision: Decision) -> str:
    if decision.selected is None or not decision.risk_check.passed:
        return "NO_TRADE"
    return "ENTER_LONG" if decision.selected.side == "long" else "ENTER_SHORT"


def _decision_ai_metadata(decision: Decision) -> dict[str, Any]:
    scan = decision.scan_comparison or {}
    ai_okx = scan.get("ai_okx_approval") or {}
    if isinstance(ai_okx, dict) and ai_okx:
        return ai_okx
    internal = scan.get("ai_internal_market_scan") or scan.get("internal_market_scan") or {}
    return internal if isinstance(internal, dict) else {}


def record_ai_trade_decision(config: dict[str, Any], decision: Decision) -> int:
    selected = decision.selected
    metadata = _decision_ai_metadata(decision)
    candidate_payload = _compact_candidate_storage_payload(selected) if selected else {}
    indicator = candidate_payload.get("indicator_summary") if isinstance(candidate_payload, dict) else {}
    entry = selected.entry if selected else None
    scan = decision.scan_comparison or {}
    execution = decision.execution or None
    market_regime = ((scan.get("market_regime") or {}) if isinstance(scan.get("market_regime"), dict) else {}) or {}
    payload_json = json.dumps(_compact_decision_payload(decision), ensure_ascii=False)
    return insert_ai_trade_decision_row(
        config,
        {
            "created_at": to_jsonable(decision.created_at),
            "symbol": selected.symbol if selected else None,
            "timeframe": str(config.get("strategy", {}).get("timeframe", "")),
            "decision": _decision_label(decision),
            "confidence": float(selected.confidence) if selected else None,
            "rule_score": _candidate_rule_score(selected),
            "side": _side_label(selected),
            "entry_price": entry,
            "stop_loss": selected.stop_loss if selected else None,
            "take_profit1": selected.take_profit if selected else None,
            "take_profit2": None,
            "risk_reward": selected.risk_reward if selected else None,
            "funding_rate": _safe_float(indicator.get("funding_rate")) if isinstance(indicator, dict) else None,
            "open_interest_change": _safe_float(indicator.get("open_interest_change")) if isinstance(indicator, dict) else None,
            "rsi": _safe_float(indicator.get("rsi")) if isinstance(indicator, dict) else None,
            "macd_signal": _safe_float(indicator.get("macd_signal")) if isinstance(indicator, dict) else None,
            "trend": indicator.get("trend") if isinstance(indicator, dict) else None,
            "volume_change": _safe_float(indicator.get("volume_ratio")) if isinstance(indicator, dict) else None,
            "news_score": float(selected.news_score) if selected else None,
            "reason_json": json.dumps(_compact_decision_reason_payload(decision, selected), ensure_ascii=False),
            "raw_prompt": _trim_text(metadata.get("raw_prompt"), 800),
            "raw_response": _trim_text(metadata.get("raw_response"), 1200),
            "order_id": execution.order_id if execution else None,
            "trade_status": None,
            "pnl": None,
            "closed_at": None,
            "prompt_version": str(metadata.get("prompt_version") or config.get("prompt_engine", {}).get("default_prompt_version", "prompt-v1")),
            "prompt_hash": str(metadata.get("prompt_hash") or ensure_prompt_version(config)["prompt_hash"]),
            "model_name": str(metadata.get("model") or config.get("ai", {}).get("okx", {}).get("model", "gpt-5.5")),
            "model_version": str(metadata.get("model_version") or metadata.get("model") or config.get("ai", {}).get("okx", {}).get("model", "gpt-5.5")),
            "strategy_version": str(config.get("selected_strategy_version") or config.get("strategy_versioning", {}).get("default_version", "strategy-v1")),
            "validator_version": str(config.get("strategy_versioning", {}).get("validator_version", "validator-v1")),
            "recovery_version": str(config.get("strategy_versioning", {}).get("recovery_version", "recovery-v1")),
            "health_version": str(config.get("strategy_versioning", {}).get("health_version", "health-v1")),
            "experiment_name": metadata.get("experiment_name"),
            "market_regime": market_regime.get("regime") or selected.market_regime if selected else None,
            "regime_confidence": market_regime.get("confidence") or (selected.regime_confidence if selected else None),
            "snapshot_json": json.dumps(candidate_payload, ensure_ascii=False),
            "payload_json": payload_json,
        },
    )


def create_ai_trade_decision(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    snapshot_payload = payload.get("snapshot_json") if isinstance(payload.get("snapshot_json"), dict) else {}
    if not snapshot_payload and isinstance(payload.get("snapshot"), dict):
        snapshot_payload = dict(payload.get("snapshot") or {})
    compact_snapshot = {}
    if isinstance(snapshot_payload, dict) and snapshot_payload:
        try:
            compact_snapshot = _compact_candidate_storage_payload(candidate_from_payload(snapshot_payload))
        except Exception:
            compact_snapshot = snapshot_payload
    compact_payload = payload.get("payload_json")
    if not isinstance(compact_payload, dict):
        compact_payload = {
            "created_at": payload.get("created_at"),
            "decision": payload.get("decision"),
            "symbol": payload.get("symbol"),
            "side": payload.get("side"),
            "confidence": payload.get("confidence"),
            "trade_status": payload.get("trade_status"),
        }
    row = {
        "created_at": str(payload.get("created_at") or _iso_now()),
        "symbol": payload.get("symbol"),
        "timeframe": payload.get("timeframe"),
        "decision": str(payload.get("decision") or "NO_TRADE"),
        "confidence": payload.get("confidence"),
        "rule_score": payload.get("rule_score"),
        "side": str(payload.get("side") or "NONE"),
        "entry_price": payload.get("entry_price"),
        "stop_loss": payload.get("stop_loss"),
        "take_profit1": payload.get("take_profit1"),
        "take_profit2": payload.get("take_profit2"),
        "risk_reward": payload.get("risk_reward"),
        "funding_rate": payload.get("funding_rate"),
        "open_interest_change": payload.get("open_interest_change"),
        "rsi": payload.get("rsi"),
        "macd_signal": payload.get("macd_signal"),
        "trend": payload.get("trend"),
        "volume_change": payload.get("volume_change"),
        "news_score": payload.get("news_score"),
        "reason_json": json.dumps(payload.get("reason_json") or {}, ensure_ascii=False),
        "raw_prompt": _trim_text(payload.get("raw_prompt"), 800),
        "raw_response": _trim_text(payload.get("raw_response"), 1200),
        "order_id": payload.get("order_id"),
        "trade_status": payload.get("trade_status"),
        "pnl": payload.get("pnl"),
        "closed_at": payload.get("closed_at"),
        "prompt_version": payload.get("prompt_version") or ensure_prompt_version(config)["version"],
        "prompt_hash": payload.get("prompt_hash") or ensure_prompt_version(config)["prompt_hash"],
        "model_name": payload.get("model_name") or config.get("ai", {}).get("okx", {}).get("model", "gpt-5.5"),
        "model_version": payload.get("model_version") or payload.get("model_name") or config.get("ai", {}).get("okx", {}).get("model", "gpt-5.5"),
        "strategy_version": payload.get("strategy_version") or config.get("strategy_versioning", {}).get("default_version", "strategy-v1"),
        "validator_version": payload.get("validator_version") or config.get("strategy_versioning", {}).get("validator_version", "validator-v1"),
        "recovery_version": payload.get("recovery_version") or config.get("strategy_versioning", {}).get("recovery_version", "recovery-v1"),
        "health_version": payload.get("health_version") or config.get("strategy_versioning", {}).get("health_version", "health-v1"),
        "experiment_name": payload.get("experiment_name"),
        "market_regime": payload.get("market_regime"),
        "regime_confidence": payload.get("regime_confidence"),
        "snapshot_json": json.dumps(compact_snapshot, ensure_ascii=False),
        "payload_json": json.dumps(compact_payload, ensure_ascii=False),
    }
    row["id"] = insert_ai_trade_decision_row(config, row)
    return row


def recent_ai_trade_decisions(
    config: dict[str, Any],
    limit: int = 50,
    *,
    include_details: bool = True,
) -> list[dict[str, Any]]:
    rows = list_ai_trade_decision_rows(config, limit=limit, include_details=include_details)
    result: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        payload["reason"] = _json_loads(payload.get("reason_json"), {}) if include_details else {}
        payload["snapshot"] = _json_loads(payload.get("snapshot_json"), {}) if include_details else {}
        result.append(payload)
    return result


def _ai_call_history_in_period(
    config: dict[str, Any],
    *,
    created_from: str,
    created_to: str,
) -> list[dict[str, Any]]:
    start = _parse_time(created_from)
    end = _parse_time(created_to)
    if start is None or end is None:
        return []
    items = recent_ai_call_history(config, limit=50)
    output: list[dict[str, Any]] = []
    for item in items:
        created_at = _parse_time(item.get("created_at"))
        if created_at is not None and start <= created_at < end:
            output.append(item)
    return output


def _is_mini_review_call(item: dict[str, Any]) -> bool:
    model = _ascii_fold(item.get("model"))
    role = _ascii_fold(item.get("role"))
    return role == "mini" or "5.4-mini" in model


def _mini_selected_side(item: dict[str, Any]) -> str | None:
    approved = [str(symbol) for symbol in item.get("approved_symbols") or [] if str(symbol)]
    if not approved:
        return None
    selected = approved[0]
    for detail in item.get("candidate_details") or []:
        if not isinstance(detail, dict):
            continue
        if str(detail.get("symbol") or "") != selected:
            continue
        side = str(detail.get("side") or "").strip().lower()
        if side in {"long", "short"}:
            return side
    return None


def _mini_call_confidence(item: dict[str, Any]) -> float | None:
    selected = [str(symbol) for symbol in item.get("approved_symbols") or [] if str(symbol)]
    if not selected:
        return None
    symbol = selected[0]
    for detail in item.get("candidate_details") or []:
        if not isinstance(detail, dict) or str(detail.get("symbol") or "") != symbol:
            continue
        try:
            return float(detail.get("confidence"))
        except (TypeError, ValueError):
            return None
    return None


def ai_call_decision_stats(
    config: dict[str, Any],
    *,
    created_from: str | None = None,
    created_to: str | None = None,
) -> dict[str, Any]:
    items = (
        _ai_call_history_in_period(config, created_from=created_from, created_to=created_to)
        if created_from and created_to
        else recent_ai_call_history(config, limit=5000)
    )
    mini_calls = [item for item in items if _is_mini_review_call(item)]
    okx_calls = [item for item in items if _is_gpt55_review_call(item)]
    selected_sides = [_mini_selected_side(item) for item in mini_calls]
    long_count = sum(1 for side in selected_sides if side == "long")
    short_count = sum(1 for side in selected_sides if side == "short")
    mini_no_trade_count = sum(1 for item in mini_calls if not _mini_selected_side(item))
    no_trade_count = sum(1 for item in okx_calls if _is_gpt55_no_trade_rejection(item))
    total_calls = len(mini_calls) + len(okx_calls)

    def avg_confidence(side_name: str) -> float:
        values = [
            confidence
            for item in mini_calls
            if _mini_selected_side(item) == side_name
            for confidence in [_mini_call_confidence(item)]
            if confidence is not None
        ]
        return round(_avg(values), 2) if values else 0.0

    return {
        "totalDecisions": total_calls,
        "totalRecords": len(items),
        "miniCallCount": len(mini_calls),
        "okxCallCount": len(okx_calls),
        "miniNoTradeCount": mini_no_trade_count,
        "longCount": long_count,
        "shortCount": short_count,
        "noTradeCount": no_trade_count,
        "longPercent": round(long_count / total_calls * 100, 2) if total_calls else 0.0,
        "shortPercent": round(short_count / total_calls * 100, 2) if total_calls else 0.0,
        "avgConfidenceLong": avg_confidence("long"),
        "avgConfidenceShort": avg_confidence("short"),
        "biasWarning": None,
    }


def ai_trade_decision_stats(
    config: dict[str, Any],
    *,
    created_from: str | None = None,
    created_to: str | None = None,
) -> dict[str, Any]:
    if created_from and created_to:
        rows = list_ai_trade_decision_stat_rows_for_period(
            config,
            created_from=created_from,
            created_to=created_to,
            limit=5000,
        )
    else:
        rows = list_ai_trade_decision_stat_rows(config, limit=5000)
    raw_total = len(rows)
    long_rows = [row for row in rows if row.get("decision") == "ENTER_LONG"]
    short_rows = [row for row in rows if row.get("decision") == "ENTER_SHORT"]
    if created_from and created_to:
        ai_call_stats = _ai_call_status_stats_from_history(
            _ai_call_history_in_period(config, created_from=created_from, created_to=created_to)
        )
    else:
        ai_call_stats = _load_ai_call_status_stats(config)
    no_trade_count = max(0, int(ai_call_stats.get("no_trade_count") or 0))
    total = len(long_rows) + len(short_rows) + no_trade_count

    def winrate(items: list[dict[str, Any]]) -> float:
        closed = [row for row in items if row.get("trade_status") in {"WIN", "LOSS", "BREAKEVEN"}]
        wins = [row for row in closed if row.get("trade_status") == "WIN"]
        return round(len(wins) / len(closed) * 100, 2) if closed else 0.0

    def avg_confidence(items: list[dict[str, Any]]) -> float:
        values = [float(row["confidence"]) for row in items if row.get("confidence") is not None]
        return round(_avg(values), 2) if values else 0.0

    def profit_factor(items: list[dict[str, Any]]) -> float:
        profits = sum(float(row.get("pnl") or 0) for row in items if float(row.get("pnl") or 0) > 0)
        losses = abs(sum(float(row.get("pnl") or 0) for row in items if float(row.get("pnl") or 0) < 0))
        if losses == 0:
            return 999.0 if profits > 0 else 0.0
        return round(profits / losses, 4)

    long_ratio = round(len(long_rows) / total * 100, 2) if total else 0.0
    short_ratio = round(len(short_rows) / total * 100, 2) if total else 0.0
    warning = None
    if long_ratio > 70:
        warning = f"Bias LONG dang chiem {long_ratio:.2f}%"
    elif short_ratio > 70:
        warning = f"Bias SHORT dang chiem {short_ratio:.2f}%"
    return {
        "totalDecisions": total,
        "totalRecords": raw_total,
        "longCount": len(long_rows),
        "shortCount": len(short_rows),
        "noTradeCount": no_trade_count,
        "longPercent": long_ratio,
        "shortPercent": short_ratio,
        "winrateLong": winrate(long_rows),
        "winrateShort": winrate(short_rows),
        "avgConfidenceLong": avg_confidence(long_rows),
        "avgConfidenceShort": avg_confidence(short_rows),
        "profitFactorLong": profit_factor(long_rows),
        "profitFactorShort": profit_factor(short_rows),
        "biasWarning": warning,
    }


def _open_trade_executions(config: dict[str, Any]) -> list[dict[str, Any]]:
    return list_trade_execution_rows(config, statuses=["OPEN"], order="created_asc")


def _closed_trade_executions(config: dict[str, Any], *, limit: int = 5000) -> list[dict[str, Any]]:
    return list_trade_execution_rows(
        config,
        statuses=["WIN", "LOSS", "BREAKEVEN", "CLOSED"],
        limit=limit,
        order="closed_desc",
    )


def _trade_performance_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [row for row in rows if str(row.get("status") or "") in {"WIN", "LOSS", "BREAKEVEN", "CLOSED"}]
    win_count = sum(1 for row in closed if str(row.get("status") or "") == "WIN")
    loss_count = sum(1 for row in closed if str(row.get("status") or "") == "LOSS")
    breakeven_count = sum(1 for row in closed if str(row.get("status") or "") == "BREAKEVEN")
    pnl_values = [_safe_float(row.get("pnl")) for row in reversed(closed)]
    gross_profit = sum(pnl for pnl in pnl_values if pnl > 0)
    gross_loss = abs(sum(pnl for pnl in pnl_values if pnl < 0))
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnl_values:
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)
    hold_times: list[float] = []
    for row in closed:
        opened_at = _parse_time(row.get("created_at"))
        closed_at = _parse_time(row.get("closed_at"))
        if opened_at and closed_at:
            hold_times.append(max(0.0, (closed_at - opened_at).total_seconds() / 60.0))
    settled = win_count + loss_count
    return {
        "totalTrades": len(closed),
        "winCount": win_count,
        "lossCount": loss_count,
        "breakevenCount": breakeven_count,
        "winRate": round(win_count / settled * 100, 2) if settled else 0.0,
        "profitFactor": 999.0 if gross_loss == 0 and gross_profit > 0 else round(gross_profit / gross_loss, 4) if gross_loss else 0.0,
        "drawdown": round(max_drawdown, 4),
        "totalPnl": round(sum(pnl_values), 6),
        "averageRiskReward": round(_avg(_safe_float(row.get("risk_reward")) for row in closed), 4) if closed else 0.0,
        "averageHoldMinutes": round(_avg(hold_times), 2) if hold_times else 0.0,
        "averageConfidence": round(_avg(_safe_float(row.get("gpt_confidence")) for row in closed), 2) if closed else 0.0,
    }


def _strategy_performance_stats(config: dict[str, Any], version: str) -> dict[str, Any]:
    rows = list_trade_execution_rows(
        config,
        statuses=["WIN", "LOSS", "BREAKEVEN", "CLOSED"],
        strategy_version=version,
        order="closed_desc",
    )
    return _trade_performance_stats(rows)


def _trading_risk_settings(config: dict[str, Any]) -> dict[str, Any]:
    settings = deepcopy(config.get("trading_risk", {}))
    risk_max_active = max(1, _safe_int(config.get("risk", {}).get("max_active_trades"), 1))
    configured_max_positions = _safe_int(settings.get("max_concurrent_positions"), 0)
    if configured_max_positions > 1:
        settings["max_concurrent_positions"] = configured_max_positions
    else:
        settings["max_concurrent_positions"] = risk_max_active
    return settings


def _slot_state(open_rows: list[dict[str, Any]], max_slots: int) -> tuple[int, list[int]]:
    unique_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for row in open_rows:
        symbol = str(row.get("symbol") or "").strip()
        side = str(row.get("side") or "").strip().upper()
        key = (symbol, side) if symbol and side else (f"__row_{row.get('id')}", "")
        unique_rows.setdefault(key, row)
    used = sorted(
        {
            _safe_int(row.get("position_slot"))
            for row in unique_rows.values()
            if row.get("position_slot") is not None and _safe_int(row.get("position_slot")) > 0
        }
    )
    free = [slot for slot in range(1, max_slots + 1) if slot not in used]
    return len(unique_rows), free


def get_global_loss_streak(config: dict[str, Any]) -> int:
    streak = 0
    for row in _closed_trade_executions(config):
        status = str(row.get("status") or "")
        if status == "LOSS":
            streak += 1
            continue
        if status in {"WIN", "BREAKEVEN"}:
            break
    return streak


def get_symbol_loss_streak(config: dict[str, Any], symbol: str) -> int:
    streak = 0
    for row in _closed_trade_executions(config):
        if str(row.get("symbol") or "") != symbol:
            continue
        status = str(row.get("status") or "")
        if status == "LOSS":
            streak += 1
            continue
        if status in {"WIN", "BREAKEVEN"}:
            break
    return streak


def _adaptive_thresholds(config: dict[str, Any]) -> tuple[float, float]:
    settings = _trading_risk_settings(config)
    score = float(settings.get("normal_min_rule_score", 78))
    confidence = float(settings.get("normal_min_gpt_confidence", 82))
    if not bool(settings.get("enable_adaptive_threshold", True)):
        return score, confidence
    cutoff = _utcnow() - timedelta(days=7)
    recent = [row for row in _closed_trade_executions(config) if (_parse_time(row.get("closed_at")) or _utcnow()) >= cutoff]
    trades = len(recent)
    if trades < _safe_int(settings.get("weekly_target_min_trades"), 3):
        score -= _safe_float(settings.get("adaptive_score_step"), 3)
        confidence -= _safe_float(settings.get("adaptive_confidence_step"), 3)
    elif trades > _safe_int(settings.get("weekly_target_max_trades"), 7):
        score += _safe_float(settings.get("adaptive_score_step"), 3)
        confidence += _safe_float(settings.get("adaptive_confidence_step"), 3)
    score = max(score, _safe_float(settings.get("absolute_min_rule_score"), 75))
    confidence = max(confidence, _safe_float(settings.get("absolute_min_gpt_confidence"), 80))
    return round(score, 2), round(confidence, 2)


def _paused_until_from_row(row: dict[str, Any] | None) -> datetime | None:
    if not row:
        return None
    return _parse_time(row.get("paused_until"))


def refresh_trading_system_state(config: dict[str, Any]) -> dict[str, Any]:
    settings = _trading_risk_settings(config)
    global_loss_streak = get_global_loss_streak(config)
    current_rule_score, current_confidence = _adaptive_thresholds(config)
    paused_until: datetime | None = None
    existing = get_trading_system_state_row(config)
    if existing:
        paused_until = _parse_time(existing.get("paused_until"))
    if paused_until and paused_until <= _utcnow():
        paused_until = None
    if global_loss_streak >= _safe_int(settings.get("pause_trading_loss_streak"), 4):
        paused_until = _utcnow() + timedelta(hours=_safe_int(settings.get("pause_trading_hours"), 24))
    is_recovery_mode = global_loss_streak >= _safe_int(settings.get("global_loss_streak_threshold"), 2)
    payload = {
        "id": 1,
        "mechanismName": str(settings.get("mechanism_name", "Bunny minimize losses")),
        "isRecoveryMode": is_recovery_mode,
        "globalLossStreak": global_loss_streak,
        "isPaused": bool(paused_until and paused_until > _utcnow()),
        "pausedUntil": paused_until.isoformat() if paused_until else None,
        "currentNormalMinRuleScore": current_rule_score,
        "currentNormalMinGptConfidence": current_confidence,
        "updatedAt": _iso_now(),
    }
    upsert_trading_system_state_row(
        config,
        {
            "mechanism_name": payload["mechanismName"],
            "is_recovery_mode": _bool_int(payload["isRecoveryMode"]),
            "global_loss_streak": payload["globalLossStreak"],
            "is_paused": _bool_int(payload["isPaused"]),
            "paused_until": payload["pausedUntil"],
            "current_normal_min_rule_score": payload["currentNormalMinRuleScore"],
            "current_normal_min_gpt_confidence": payload["currentNormalMinGptConfidence"],
            "updated_at": payload["updatedAt"],
            "payload_json": json.dumps(payload, ensure_ascii=False),
        },
    )
    return payload


def get_trading_system_state(config: dict[str, Any]) -> dict[str, Any]:
    state = refresh_trading_system_state(config)
    settings = _trading_risk_settings(config)
    open_rows = _open_trade_executions(config)
    open_count, _free = _slot_state(open_rows, _safe_int(settings.get("max_concurrent_positions"), 3))
    state.update(
        {
            "maxConcurrentPositions": _safe_int(settings.get("max_concurrent_positions"), 3),
            "openPositionsCount": open_count,
            "normalMinRiskReward": _safe_float(settings.get("normal_min_risk_reward"), 1.8),
            "recoveryMinRuleScore": _safe_float(settings.get("recovery_min_rule_score"), 90),
            "recoveryMinGptConfidence": _safe_float(settings.get("recovery_min_gpt_confidence"), 92),
            "recoveryMinRiskReward": _safe_float(settings.get("recovery_min_risk_reward"), 2.5),
        }
    )
    return state


def refresh_bunny_health_state(config: dict[str, Any]) -> dict[str, Any]:
    settings = deepcopy(config.get("bunny_health_monitor", {}))
    lookback = max(1, _safe_int(settings.get("lookback_trades"), 20))
    rows = _closed_trade_executions(config, limit=lookback)
    if not rows:
        payload = {
            "isHealthy": True,
            "isWarning": False,
            "isCritical": False,
            "totalTrades": 0,
            "winCount": 0,
            "lossCount": 0,
            "breakevenCount": 0,
            "winRate": 0.0,
            "grossProfit": 0.0,
            "grossLoss": 0.0,
            "profitFactor": 999.0,
            "totalPnl": 0.0,
            "maxDrawdownPercent": 0.0,
            "riskMultiplier": 1.0,
            "scoreAdjustment": 0.0,
            "confidenceAdjustment": 0.0,
            "isPaused": False,
            "pausedUntil": None,
            "reason": "Not enough trades",
            "updatedAt": _iso_now(),
        }
    else:
        ordered = list(reversed(rows))
        pnl_values = [float(row.get("pnl") or 0) for row in ordered]
        win_count = sum(1 for row in rows if str(row.get("status") or "") == "WIN")
        loss_count = sum(1 for row in rows if str(row.get("status") or "") == "LOSS")
        breakeven_count = sum(1 for row in rows if str(row.get("status") or "") == "BREAKEVEN")
        closed_total = max(1, win_count + loss_count)
        win_rate = round(win_count / closed_total * 100, 2)
        gross_profit = round(sum(pnl for pnl in pnl_values if pnl > 0), 6)
        gross_loss = round(abs(sum(pnl for pnl in pnl_values if pnl < 0)), 6)
        profit_factor = 999.0 if gross_loss == 0 else round(gross_profit / gross_loss, 6)
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for pnl in pnl_values:
            equity += pnl
            peak = max(peak, equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)
        reason = "Healthy"
        risk_multiplier = 1.0
        score_adjustment = 0.0
        confidence_adjustment = 0.0
        is_warning = False
        is_critical = False
        paused_until = None
        if (
            win_rate < _safe_float(settings.get("critical_win_rate"), 35)
            or profit_factor < _safe_float(settings.get("critical_profit_factor"), 0.8)
            or max_drawdown > _safe_float(settings.get("critical_drawdown_percent"), 15)
        ):
            is_critical = True
            reason = "Critical health threshold breached"
            paused_until = _utcnow() + timedelta(hours=_safe_int(settings.get("critical_pause_hours"), 12))
            risk_multiplier = 0.0
        elif (
            win_rate < _safe_float(settings.get("min_win_rate"), 50)
            or profit_factor < _safe_float(settings.get("min_profit_factor"), 1.2)
            or max_drawdown > _safe_float(settings.get("max_drawdown_percent"), 10)
        ):
            is_warning = True
            reason = "Warning health threshold breached"
            risk_multiplier = max(0.0, 1.0 - (_safe_float(settings.get("risk_reduction_percent"), 40) / 100.0))
            score_adjustment = _safe_float(settings.get("score_increase_step"), 4)
            confidence_adjustment = _safe_float(settings.get("confidence_increase_step"), 4)
        payload = {
            "isHealthy": not is_warning and not is_critical,
            "isWarning": is_warning,
            "isCritical": is_critical,
            "totalTrades": len(rows),
            "winCount": win_count,
            "lossCount": loss_count,
            "breakevenCount": breakeven_count,
            "winRate": win_rate,
            "grossProfit": gross_profit,
            "grossLoss": gross_loss,
            "profitFactor": profit_factor,
            "totalPnl": round(sum(pnl_values), 6),
            "maxDrawdownPercent": round(max_drawdown, 4),
            "riskMultiplier": round(risk_multiplier, 4),
            "scoreAdjustment": round(score_adjustment, 2),
            "confidenceAdjustment": round(confidence_adjustment, 2),
            "isPaused": bool(paused_until),
            "pausedUntil": paused_until.isoformat() if paused_until else None,
            "reason": reason,
            "updatedAt": _iso_now(),
        }
    upsert_trading_health_state_row(
        config,
        {
            "mechanism_name": "Bunny Health Monitor",
            "is_healthy": _bool_int(payload["isHealthy"]),
            "is_warning": _bool_int(payload["isWarning"]),
            "is_critical": _bool_int(payload["isCritical"]),
            "total_trades": payload["totalTrades"],
            "win_count": payload["winCount"],
            "loss_count": payload["lossCount"],
            "breakeven_count": payload["breakevenCount"],
            "win_rate": payload["winRate"],
            "gross_profit": payload["grossProfit"],
            "gross_loss": payload["grossLoss"],
            "profit_factor": payload["profitFactor"],
            "total_pnl": payload["totalPnl"],
            "max_drawdown_percent": payload["maxDrawdownPercent"],
            "risk_multiplier": payload["riskMultiplier"],
            "score_adjustment": payload["scoreAdjustment"],
            "confidence_adjustment": payload["confidenceAdjustment"],
            "is_paused": _bool_int(payload["isPaused"]),
            "paused_until": payload["pausedUntil"],
            "reason": payload["reason"],
            "updated_at": payload["updatedAt"],
            "payload_json": json.dumps(payload, ensure_ascii=False),
        },
    )
    return payload


def get_bunny_health_state(config: dict[str, Any]) -> dict[str, Any]:
    return refresh_bunny_health_state(config)


def _entry_distance_pct(entry_price: float, current_price: float) -> float:
    if entry_price <= 0 or current_price <= 0:
        return 0.0
    return abs(entry_price - current_price) / current_price * 100.0


def validate_entry(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    state = get_trading_system_state(config)
    health = get_bunny_health_state(config)
    settings = _trading_risk_settings(config)
    open_rows = _open_trade_executions(config)
    open_count, free_slots = _slot_state(open_rows, _safe_int(settings.get("max_concurrent_positions"), 3))
    reasons: list[str] = []
    rule_score = _safe_float(payload.get("ruleScore", payload.get("rule_score", payload.get("confidence"))))
    gpt_confidence = _safe_float(payload.get("gptConfidence", payload.get("gpt_confidence", payload.get("confidence"))))
    risk_reward = _safe_float(payload.get("riskReward", payload.get("risk_reward")), 0.0)
    spread = _safe_float(payload.get("spread", payload.get("spread_pct")), 0.0)
    funding_rate = _safe_float(payload.get("fundingRate", payload.get("funding_rate")), 0.0)
    volume_confirmed = bool(payload.get("volumeConfirmed", payload.get("volume_confirmed", True)))
    no_high_impact_news = bool(
        payload.get("noHighImpactNewsWithin60m", payload.get("no_high_impact_news_within_60m", True))
    )
    entry_price = _safe_float(payload.get("entryPrice", payload.get("entry_price")), 0.0)
    current_price = _safe_float(payload.get("currentPrice", payload.get("current_price", entry_price)), entry_price)
    distance_pct = _entry_distance_pct(entry_price, current_price)
    if state["isPaused"]:
        reasons.append(f"He thong dang pause den {state.get('pausedUntil')}")
    if health["isPaused"]:
        reasons.append(f"Bunny Health Monitor dang pause den {health.get('pausedUntil')}")
    preferred_slot = _safe_int(payload.get("preferredPositionSlot", payload.get("preferred_position_slot")), 0)
    if preferred_slot and preferred_slot not in free_slots:
        reasons.append(f"Slot {preferred_slot} khong con trong")
    if open_count >= _safe_int(settings.get("max_concurrent_positions"), 3):
        reasons.append(f"Da het slot: {open_count}/{settings.get('max_concurrent_positions')}")
    current_rule_threshold = float(state["currentNormalMinRuleScore"])
    current_conf_threshold = float(state["currentNormalMinGptConfidence"])
    normal_rr = _safe_float(settings.get("normal_min_risk_reward"), 1.8)
    if health["isWarning"]:
        current_rule_threshold += _safe_float(health["scoreAdjustment"], 0.0)
        current_conf_threshold += _safe_float(health["confidenceAdjustment"], 0.0)
    is_recovery = bool(state["isRecoveryMode"])
    if is_recovery:
        current_rule_threshold = max(current_rule_threshold, _safe_float(settings.get("recovery_min_rule_score"), 90))
        current_conf_threshold = max(
            current_conf_threshold, _safe_float(settings.get("recovery_min_gpt_confidence"), 92)
        )
        normal_rr = max(normal_rr, _safe_float(settings.get("recovery_min_risk_reward"), 2.5))
        if abs(funding_rate) > _safe_float(settings.get("max_safe_funding_rate_abs"), 0.03):
            reasons.append(f"Funding rate {funding_rate:.4f} vuot nguong an toan")
        if spread > _safe_float(config.get("risk", {}).get("max_spread_pct"), 0.15):
            reasons.append(f"Spread {spread:.4f}% vuot nguong toi da")
        if not volume_confirmed:
            reasons.append("Volume chua xac nhan")
        if not no_high_impact_news:
            reasons.append("Co tin anh huong cao trong 60 phut")
        if distance_pct > _safe_float(settings.get("max_entry_distance_pct"), 0.6):
            reasons.append(f"Entry cach gia hien tai {distance_pct:.4f}% vuot nguong")
    if rule_score < current_rule_threshold:
        reasons.append(f"Rule score {rule_score:.2f} < {current_rule_threshold:.2f}")
    if gpt_confidence < current_conf_threshold:
        reasons.append(f"GPT confidence {gpt_confidence:.2f} < {current_conf_threshold:.2f}")
    if risk_reward < normal_rr:
        reasons.append(f"Risk reward {risk_reward:.2f} < {normal_rr:.2f}")
    setup_quality = "REJECTED"
    if not reasons:
        if (
            rule_score >= _safe_float(settings.get("strong_setup_rule_score"), 85)
            and gpt_confidence >= _safe_float(settings.get("strong_setup_gpt_confidence"), 88)
            and risk_reward >= _safe_float(settings.get("strong_setup_min_risk_reward"), 2.0)
        ):
            setup_quality = "STRONG"
        elif is_recovery:
            setup_quality = "RECOVERY"
        else:
            setup_quality = "NORMAL"
    risk_percent = (
        _safe_float(settings.get("recovery_mode_risk_percent"), 0.5)
        if is_recovery
        else _safe_float(settings.get("normal_risk_percent"), 1.0)
    )
    if health["isWarning"] or health["isCritical"]:
        risk_percent *= _safe_float(health["riskMultiplier"], 1.0)
    assigned_slot = preferred_slot if preferred_slot in free_slots else free_slots[0] if free_slots else None
    return {
        "allowed": not reasons,
        "reason": "; ".join(reasons) if reasons else "PASS",
        "assignedPositionSlot": assigned_slot,
        "riskPercent": round(risk_percent, 4),
        "isRecoveryMode": is_recovery,
        "setupQuality": setup_quality,
        "currentRuleThreshold": round(current_rule_threshold, 2),
        "currentConfidenceThreshold": round(current_conf_threshold, 2),
        "currentRiskRewardThreshold": round(normal_rr, 2),
        "healthState": health,
    }


def apply_system_validation_to_candidate(config: dict[str, Any], candidate: TradeCandidate) -> tuple[list[str], list[str]]:
    response = validate_entry(
        config,
        {
            "symbol": candidate.symbol,
            "side": candidate.side,
            "ruleScore": _candidate_rule_score(candidate),
            "gptConfidence": candidate.confidence,
            "riskReward": candidate.risk_reward,
            "spread_pct": candidate.spread_pct,
            "entryPrice": candidate.entry,
            "currentPrice": candidate.entry,
            "volumeConfirmed": _safe_float(candidate.indicator_summary.get("volume_ratio"), 1.0) >= 1.0,
            "noHighImpactNewsWithin60m": abs(candidate.news_score) < 4.0,
            "fundingRate": candidate.indicator_summary.get("funding_rate"),
        },
    )
    candidate.rule_score = _candidate_rule_score(candidate)
    candidate.position_slot = response.get("assignedPositionSlot")
    candidate.risk_percent = response.get("riskPercent")
    candidate.setup_quality = response.get("setupQuality")
    health = response.get("healthState") or {}
    warnings: list[str] = []
    if health.get("isWarning"):
        warnings.append("Bunny Health Monitor dang o trang thai warning")
    if response["allowed"]:
        return [], warnings
    return [str(response.get("reason") or "System validation rejected entry")], warnings


def record_trade_execution(
    config: dict[str, Any],
    candidate: TradeCandidate,
    execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    execution_meta = execution if isinstance(execution, dict) else {}
    journal_type = str(execution_meta.get("journal_type") or "").upper()
    order_type = str(
        execution_meta.get("order_type")
        or ((execution_meta.get("raw") or {}).get("type") if isinstance(execution_meta.get("raw"), dict) else "")
        or ""
    ).lower()
    validation = validate_entry(
        config,
        {
            "symbol": candidate.symbol,
            "side": candidate.side,
            "ruleScore": _candidate_rule_score(candidate),
            "gptConfidence": candidate.confidence,
            "riskReward": candidate.risk_reward,
            "entryPrice": candidate.entry,
            "currentPrice": candidate.entry,
            "preferredPositionSlot": candidate.position_slot,
            "spread_pct": candidate.spread_pct,
            "volumeConfirmed": _safe_float(candidate.indicator_summary.get("volume_ratio"), 1.0) >= 1.0,
            "noHighImpactNewsWithin60m": abs(candidate.news_score) < 4.0,
            "fundingRate": candidate.indicator_summary.get("funding_rate"),
        },
    )
    metadata = candidate.decision_metadata or {}
    created_at = _iso_now()
    payload = _candidate_payload(candidate)
    allowed = bool(validation.get("allowed"))
    execution_status = "OPEN" if allowed else "REJECTED"
    if allowed and journal_type == "LC" and order_type == "limit":
        execution_status = "LC_PENDING"
    row = {
        "created_at": created_at,
        "updated_at": created_at,
        "symbol": candidate.symbol,
        "position_slot": validation.get("assignedPositionSlot") if execution_status == "OPEN" else None,
        "parent_position_id": None,
        "side": candidate.side.upper(),
        "entry_price": candidate.entry,
        "stop_loss": candidate.stop_loss,
        "take_profit": candidate.take_profit,
        "risk_reward": candidate.risk_reward,
        "risk_percent": validation.get("riskPercent") or candidate.risk_percent or 0,
        "rule_score": _candidate_rule_score(candidate),
        "gpt_confidence": candidate.confidence,
        "status": execution_status,
        "pnl": None,
        "close_reason": None,
        "reject_reason": None if execution_status in {"OPEN", "LC_PENDING"} else validation.get("reason"),
        "closed_at": None if execution_status in {"OPEN", "LC_PENDING"} else created_at,
        "payload_json": json.dumps(payload, ensure_ascii=False),
        "market_regime": candidate.market_regime,
        "regime_confidence": candidate.regime_confidence,
        "strategy_version": str(config.get("selected_strategy_version") or config.get("strategy_versioning", {}).get("default_version", "strategy-v1")),
        "rule_engine_version": str(config.get("strategy_versioning", {}).get("rule_engine_version", "rule-engine-v1")),
        "validator_version": str(config.get("strategy_versioning", {}).get("validator_version", "validator-v1")),
        "recovery_version": str(config.get("strategy_versioning", {}).get("recovery_version", "recovery-v1")),
        "health_version": str(config.get("strategy_versioning", {}).get("health_version", "health-v1")),
        "prompt_version": metadata.get("prompt_version") or config.get("prompt_engine", {}).get("default_prompt_version", "prompt-v1"),
        "prompt_hash": metadata.get("prompt_hash") or ensure_prompt_version(config)["prompt_hash"],
        "model_name": metadata.get("model") or config.get("ai", {}).get("okx", {}).get("model", "gpt-5.5"),
        "model_version": metadata.get("model_version") or metadata.get("model") or config.get("ai", {}).get("okx", {}).get("model", "gpt-5.5"),
        "system_version": str(config.get("prompt_engine", {}).get("system_version", "system-v1")),
        "decision_engine_version": str(config.get("prompt_engine", {}).get("decision_engine_version", "decision-engine-v1")),
        "bunny_version": str(config.get("prompt_engine", {}).get("bunny_version", "bunny-v1")),
        "health_monitor_version": str(config.get("prompt_engine", {}).get("health_version", "health-v1")),
        "slot_refill_version": str(config.get("prompt_engine", {}).get("slot_refill_version", "slot-refill-v1")),
        "experiment_name": metadata.get("experiment_name"),
        "prompt_tokens": metadata.get("prompt_tokens"),
        "completion_tokens": metadata.get("completion_tokens"),
        "latency_ms": metadata.get("latency_ms"),
        "snapshot_json": json.dumps(payload, ensure_ascii=False),
    }
    row = insert_trade_execution_row(config, row)
    row["validation"] = validation
    refresh_trading_system_state(config)
    refresh_bunny_health_state(config)
    return row


def _mark_recent_ai_decisions_closed(config: dict[str, Any], execution_row: dict[str, Any]) -> None:
    mark_ai_trade_decisions_closed(
        config,
        symbol=str(execution_row.get("symbol") or ""),
        side=str(execution_row.get("side") or ""),
        trade_status=str(execution_row.get("status") or ""),
        pnl=execution_row.get("pnl"),
        closed_at=execution_row.get("closed_at"),
    )


def _mark_trade_candidate_used(config: dict[str, Any], candidate_id: int) -> None:
    mark_trade_candidate_used(config, candidate_id, used_at=_iso_now())


def _claim_trade_candidate(config: dict[str, Any], candidate_id: int) -> bool:
    return claim_trade_candidate(config, candidate_id, used_at=_iso_now())


def _slot_refill_settings(config: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(config.get("slot_refill", {}))


def try_slot_refill(config: dict[str, Any], position_slot: int) -> dict[str, Any]:
    settings = _slot_refill_settings(config)
    if not bool(settings.get("enable_auto_refill", True)):
        return {"refilled": False, "reason": "Auto refill disabled"}
    state = get_trading_system_state(config)
    if state["isPaused"]:
        return {"refilled": False, "reason": "Trading is paused"}
    if state["isRecoveryMode"] and not bool(settings.get("allow_refill_in_recovery_mode", True)):
        return {"refilled": False, "reason": "Refill disabled in Recovery Mode"}
    open_rows = _open_trade_executions(config)
    open_count, free_slots = _slot_state(open_rows, _safe_int(_trading_risk_settings(config).get("max_concurrent_positions"), 3))
    if position_slot not in free_slots:
        return {"refilled": False, "reason": "Slot is no longer free"}
    if open_count >= _safe_int(_trading_risk_settings(config).get("max_concurrent_positions"), 3):
        return {"refilled": False, "reason": "Max concurrent positions reached"}
    cutoff = (_utcnow() - timedelta(minutes=_safe_int(settings.get("candidate_lookback_minutes"), 240))).isoformat()
    rows = list_trade_candidate_rows(
        config,
        min_created_at=cutoff,
        unused_only=True,
        min_rule_score=_safe_float(settings.get("min_candidate_rule_score"), 78),
        limit=max(1, _safe_int(settings.get("max_refill_attempts_per_slot"), 3)),
        order="refill",
    )
    for row in rows:
        payload = _json_loads(row.get("payload_json"), {})
        candidate = _candidate_from_payload(payload)
        validation = validate_entry(
            config,
            {
                "symbol": candidate.symbol,
                "side": candidate.side,
                "ruleScore": _candidate_rule_score(candidate),
                "gptConfidence": candidate.confidence,
                "riskReward": candidate.risk_reward,
                "entryPrice": candidate.entry,
                "currentPrice": candidate.entry,
                "preferredPositionSlot": position_slot,
                "spread_pct": candidate.spread_pct,
                "volumeConfirmed": _safe_float(candidate.indicator_summary.get("volume_ratio"), 1.0) >= 1.0,
                "noHighImpactNewsWithin60m": abs(candidate.news_score) < 4.0,
                "fundingRate": candidate.indicator_summary.get("funding_rate"),
            },
        )
        if not validation["allowed"]:
            continue
        if validation.get("assignedPositionSlot") != position_slot:
            continue
        if not _claim_trade_candidate(config, _safe_int(row["id"])):
            continue
        candidate.position_slot = position_slot
        candidate.risk_percent = validation.get("riskPercent")
        candidate.setup_quality = "RECOVERY" if validation.get("isRecoveryMode") else "NORMAL"
        row_payload = record_trade_execution(config, candidate, execution={"source": "slot_refill"})
        if str(row_payload.get("status") or "") == "REJECTED":
            return {"refilled": False, "reason": row_payload.get("reject_reason") or "Refill validation rejected"}
        return {"refilled": True, "reason": "Refill created trade execution", "tradeExecution": row_payload}
    return {"refilled": False, "reason": "No candidate passed refill validation"}


def close_trade_execution(
    config: dict[str, Any],
    trade_execution_id: int,
    status: str,
    pnl: float,
    close_reason: str | None = None,
) -> dict[str, Any]:
    closed_at = _iso_now()
    normalized_status = str(status or "CLOSED").upper()
    row = get_trade_execution(config, trade_execution_id)
    if row is None:
        raise ValueError(f"Trade execution not found: {trade_execution_id}")
    payload = update_trade_execution(
        config,
        trade_execution_id,
        {
            "status": normalized_status,
            "pnl": pnl,
            "close_reason": str(close_reason or "").strip() or None,
            "closed_at": closed_at,
            "updated_at": closed_at,
        },
    ) or {}
    _mark_recent_ai_decisions_closed(config, payload)
    refresh_trading_system_state(config)
    refresh_bunny_health_state(config)
    refill = try_slot_refill(config, _safe_int(payload.get("position_slot"), 0)) if payload.get("position_slot") else {"refilled": False, "reason": "No slot"}
    payload["slotRefill"] = refill
    try:
        from .notifier import send_telegram_message
        from .reporting import format_trade_execution_close_message

        send_telegram_message(
            config,
            format_trade_execution_close_message(config, payload),
            with_buttons=False,
            replace_previous=False,
        )
    except Exception:
        pass
    return payload


def build_market_prompt_dto(
    *,
    candidates: list[dict[str, Any]] | None = None,
    market_snapshot: dict[str, Any] | None = None,
    trading_system_state: dict[str, Any] | None = None,
    trading_health_state: dict[str, Any] | None = None,
    open_positions: list[dict[str, Any]] | None = None,
    recent_trades: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "scanTime": _iso_now(),
        "marketSnapshot": market_snapshot or {},
        "candidates": candidates or [],
        "tradingSystemState": trading_system_state or {},
        "tradingHealthState": trading_health_state or {},
        "openPositions": open_positions or [],
        "recentTrades": recent_trades or [],
    }
    if extra:
        payload.update(extra)
    return payload


def replay_trade_execution(config: dict[str, Any], trade_execution_id: int) -> dict[str, Any]:
    row = get_trade_execution(config, trade_execution_id)
    if row is None:
        raise ValueError(f"Trade execution not found: {trade_execution_id}")
    trade_row = dict(row)
    market_snapshot = _json_loads(trade_row.get("snapshot_json"), {})
    system_state = get_trading_system_state(config)
    health_state = get_bunny_health_state(config)
    market_dto = build_market_prompt_dto(
        market_snapshot=market_snapshot,
        candidates=[market_snapshot] if market_snapshot else [],
        trading_system_state=system_state,
        trading_health_state=health_state,
        open_positions=_open_trade_executions(config),
        recent_trades=_closed_trade_executions(config, limit=10),
    )
    prompt_package = build_prompt(
        config,
        market_dto,
        instruction_key="final-decision",
        recovery_mode=bool(system_state.get("isRecoveryMode")),
        health_warning=bool(health_state.get("isWarning") or health_state.get("isCritical")),
    )
    model_name = str(trade_row.get("model_name") or config.get("ai", {}).get("okx", {}).get("model", "gpt-5.5"))
    provider = str(config.get("ai", {}).get("okx", {}).get("provider", "openai"))
    old_decision = str(trade_row.get("side") or "NONE")
    old_confidence = _safe_float(trade_row.get("gpt_confidence") or trade_row.get("rule_score"))
    if provider == "openai":
        try:
            role_config = deepcopy(config.get("ai", {}).get("okx", {}))
            response = call_openai_json(
                config,
                role_config,
                prompt_package,
                model_name=model_name,
                purpose="replay",
            )
            parsed = response["parsed"]
            new_decision = str(parsed.get("decision") or ("LONG" if parsed.get("approved") else "NO_TRADE")).upper()
            new_confidence = _safe_float(parsed.get("confidence"), old_confidence)
            new_reason = parsed.get("reason") or ""
            raw_prompt = prompt_package["messages"][1]["content"]
        except Exception as exc:
            new_decision = "NO_TRADE"
            new_confidence = max(0.0, old_confidence - 10.0)
            new_reason = f"Replay fallback do AI loi: {exc}"
            response = {"latency_ms": 0.0}
            raw_prompt = prompt_package["messages"][1]["content"]
    else:
        score = _safe_float(trade_row.get("rule_score"), old_confidence)
        if score >= float(system_state["currentNormalMinRuleScore"]):
            new_decision = old_decision
            new_confidence = old_confidence
            new_reason = "Heuristic replay kept original direction"
        else:
            new_decision = "NO_TRADE"
            new_confidence = max(0.0, old_confidence - 12.0)
            new_reason = "Heuristic replay rejected setup"
        response = {"latency_ms": 0.0}
        raw_prompt = prompt_package["messages"][1]["content"]
    decision_changed = new_decision != old_decision
    confidence_changed = abs(new_confidence - old_confidence) >= 0.01
    reason_changed = True
    result = {
        "tradeExecutionId": trade_execution_id,
        "promptVersion": prompt_package["prompt_version"],
        "strategyVersion": trade_row.get("strategy_version"),
        "modelVersion": trade_row.get("model_version"),
        "oldDecision": old_decision,
        "newDecision": new_decision,
        "oldConfidence": old_confidence,
        "newConfidence": round(new_confidence, 2),
        "latency": _safe_float(response.get("latency_ms")),
        "replayAt": _iso_now(),
        "decisionChanged": decision_changed,
        "confidenceChanged": confidence_changed,
        "reasonChanged": reason_changed,
        "oldReason": _json_loads(trade_row.get("payload_json"), {}),
        "newReason": {"reason": new_reason, "rawPrompt": raw_prompt},
    }
    insert_replay_history_row(
        config,
        {
            "trade_execution_id": result["tradeExecutionId"],
            "prompt_version": result["promptVersion"],
            "strategy_version": result["strategyVersion"],
            "model_version": result["modelVersion"],
            "old_decision": result["oldDecision"],
            "new_decision": result["newDecision"],
            "old_confidence": result["oldConfidence"],
            "new_confidence": result["newConfidence"],
            "latency": result["latency"],
            "replay_at": result["replayAt"],
            "decision_changed": _bool_int(result["decisionChanged"]),
            "confidence_changed": _bool_int(result["confidenceChanged"]),
            "reason_changed": _bool_int(result["reasonChanged"]),
            "old_reason_json": json.dumps(result["oldReason"], ensure_ascii=False),
            "new_reason_json": json.dumps(result["newReason"], ensure_ascii=False),
            "payload_json": json.dumps(result, ensure_ascii=False),
        },
    )
    return result


def replay_batch(config: dict[str, Any], limit: int) -> dict[str, Any]:
    execution_ids = list_trade_execution_ids(config, limit=limit)
    results = [replay_trade_execution(config, trade_execution_id) for trade_execution_id in execution_ids]
    stats = replay_stats(config)
    return {
        "count": len(results),
        "results": results,
        "stats": stats,
    }


def replay_stats(config: dict[str, Any]) -> dict[str, Any]:
    payloads = list_replay_history_rows(config, include_trade_execution=True)
    total = len(payloads)
    changed = sum(1 for row in payloads if _safe_int(row.get("decision_changed")) == 1)
    confidence_changed = sum(1 for row in payloads if _safe_int(row.get("confidence_changed")) == 1)
    replayed_trades = [
        {
            "status": row.get("trade_status"),
            "pnl": row.get("trade_pnl"),
            "risk_reward": row.get("risk_reward"),
            "gpt_confidence": row.get("new_confidence"),
            "created_at": row.get("trade_created_at"),
            "closed_at": row.get("trade_closed_at"),
        }
        for row in payloads
        if str(row.get("new_decision") or "").upper() not in {"NO_TRADE", "NONE", "REJECTED"}
    ]
    performance = _trade_performance_stats(replayed_trades)
    return {
        "replayCount": total,
        "decisionChangedPercent": round(changed / total * 100, 2) if total else 0.0,
        "confidenceChangedPercent": round(confidence_changed / total * 100, 2) if total else 0.0,
        "averageLatency": round(_avg(_safe_float(row.get("latency")) for row in payloads), 2) if payloads else 0.0,
        "replayWinRate": performance["winRate"],
        "replayProfitFactor": performance["profitFactor"],
        "replayDrawdown": performance["drawdown"],
        "performance": performance,
        "recent": payloads[:20],
    }

