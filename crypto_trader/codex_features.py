from __future__ import annotations

import hashlib
import json
import random
import re
import time
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
from .storage import connect_state_db, get_journal_state, set_journal_state


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
    with connect_state_db(config) as connection:
        row = connection.execute(
            "SELECT * FROM prompt_versions WHERE version = ?",
            (version,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO prompt_versions (version, hash, description, created_at, is_active, files_json, prompt_hash)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (version, prompt_hash, description, _iso_now(), files_json, prompt_hash),
            )
        else:
            connection.execute(
                """
                UPDATE prompt_versions
                SET hash = ?, description = ?, is_active = 1, files_json = ?, prompt_hash = ?
                WHERE version = ?
                """,
                (prompt_hash, description, files_json, prompt_hash, version),
            )
        connection.commit()
        stored = connection.execute(
            "SELECT * FROM prompt_versions WHERE version = ?",
            (version,),
        ).fetchone()
    return dict(stored) if stored else {
        "version": version,
        "hash": prompt_hash,
        "prompt_hash": prompt_hash,
    }


def _select_prompt_experiment(config: dict[str, Any]) -> dict[str, Any] | None:
    with connect_state_db(config) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM ai_experiments
            WHERE enabled = 1
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()
    if not rows:
        return None
    experiments = [dict(row) for row in rows]
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


def _record_ai_call_history(config: dict[str, Any], item: dict[str, Any]) -> None:
    try:
        history = recent_ai_call_history(config, limit=50)
        history.append(item)
        set_journal_state(config, AI_CALL_HISTORY_STATE_KEY, json.dumps(history[-50:], ensure_ascii=False))
    except Exception:
        return


def _ai_call_message(item: dict[str, Any]) -> str:
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

def call_openai_json(
    config: dict[str, Any],
    role_config: dict[str, Any],
    prompt_package: dict[str, Any],
    *,
    model_name: str,
    purpose: str | None = None,
    route: str | None = None,
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
        allowed_routes = {"new_vt", "local_lc_release", "lc_okx_release"}
        if route not in allowed_routes:
            raise RuntimeError(f"OpenAI OKX approval blocked by policy: route={route or '-'}")
        okx_config = ai_settings.get("okx", {})
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
        _notify_openai_api_call(
            config,
            model_name=model_name,
            prompt_package=prompt_package,
            success=False,
            error=detail,
        )
        raise RuntimeError(detail) from exc
    except Exception as exc:
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
    with connect_state_db(config) as connection:
        existing = connection.execute(
            """
            SELECT id
            FROM ai_model_versions
            WHERE model_name = ? AND model_version = ? AND prompt_version = ? AND prompt_hash = ?
            """,
            (model_name, model_version, prompt_version, prompt_hash),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO ai_model_versions (model_name, model_version, prompt_version, prompt_hash, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (model_name, model_version, prompt_version, prompt_hash, _iso_now()),
            )
            connection.commit()


def register_prompt_metric(config: dict[str, Any], metric: dict[str, Any]) -> None:
    key = str(metric.get("prompt_version") or "")
    if not key:
        return
    prompt_tokens = _safe_float(metric.get("prompt_tokens"))
    completion_tokens = _safe_float(metric.get("completion_tokens"))
    latency_ms = _safe_float(metric.get("latency_ms"))
    cached_tokens = _safe_float(metric.get("estimated_cached_tokens"))
    dynamic_tokens = _safe_float(metric.get("estimated_dynamic_tokens"))
    cache_hit_percent = _safe_float(metric.get("cache_hit_percent"))
    with connect_state_db(config) as connection:
        row = connection.execute(
            "SELECT * FROM prompt_metrics WHERE prompt_version = ?",
            (key,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO prompt_metrics (
                    prompt_version, prompt_hash, total_requests, average_prompt_tokens,
                    average_completion_tokens, average_latency, estimated_cached_tokens,
                    estimated_dynamic_tokens, cache_hit_percent, updated_at
                )
                VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    str(metric.get("prompt_hash") or ""),
                    prompt_tokens,
                    completion_tokens,
                    latency_ms,
                    cached_tokens,
                    dynamic_tokens,
                    cache_hit_percent,
                    _iso_now(),
                ),
            )
        else:
            total = _safe_int(row["total_requests"], 0)
            new_total = total + 1
            connection.execute(
                """
                UPDATE prompt_metrics
                SET prompt_hash = ?,
                    total_requests = ?,
                    average_prompt_tokens = ?,
                    average_completion_tokens = ?,
                    average_latency = ?,
                    estimated_cached_tokens = ?,
                    estimated_dynamic_tokens = ?,
                    cache_hit_percent = ?,
                    updated_at = ?
                WHERE prompt_version = ?
                """,
                (
                    str(metric.get("prompt_hash") or row["prompt_hash"]),
                    new_total,
                    ((float(row["average_prompt_tokens"]) * total) + prompt_tokens) / new_total,
                    ((float(row["average_completion_tokens"]) * total) + completion_tokens) / new_total,
                    ((float(row["average_latency"]) * total) + latency_ms) / new_total,
                    ((float(row["estimated_cached_tokens"]) * total) + cached_tokens) / new_total,
                    ((float(row["estimated_dynamic_tokens"]) * total) + dynamic_tokens) / new_total,
                    ((float(row["cache_hit_percent"]) * total) + cache_hit_percent) / new_total,
                    _iso_now(),
                    key,
                ),
            )
        connection.commit()


def prompt_status(config: dict[str, Any], dynamic_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    version_row = ensure_prompt_version(config)
    templates = load_prompt_templates(config)
    market_json = json.dumps(dynamic_payload or {"scanTime": _iso_now()}, ensure_ascii=False, separators=(",", ":"))
    estimator = estimate_prompt_cache(templates, market_json)
    with connect_state_db(config) as connection:
        metrics = connection.execute(
            "SELECT * FROM prompt_metrics WHERE prompt_version = ?",
            (str(version_row["version"]),),
        ).fetchone()
    payload = {
        "promptVersion": version_row["version"],
        "promptHash": version_row["prompt_hash"],
        "estimatedStaticTokens": estimator["estimated_static_tokens"],
        "estimatedDynamicTokens": estimator["estimated_dynamic_tokens"],
        "estimatedCacheHit": estimator["estimated_cache_hit"],
    }
    if metrics:
        payload["metrics"] = dict(metrics)
    return payload


def prompt_history(config: dict[str, Any]) -> list[dict[str, Any]]:
    ensure_prompt_version(config)
    with connect_state_db(config) as connection:
        rows = connection.execute(
            """
            SELECT version, created_at, description, prompt_hash AS hash, is_active
            FROM prompt_versions
            ORDER BY created_at DESC, id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


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
    with connect_state_db(config) as connection:
        connection.execute(
            """
            INSERT INTO ai_experiments (name, description, prompt_version, traffic_percent, enabled, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                description = excluded.description,
                prompt_version = excluded.prompt_version,
                traffic_percent = excluded.traffic_percent,
                enabled = excluded.enabled
            """,
            (
                record["name"],
                record["description"],
                record["prompt_version"],
                record["traffic_percent"],
                _bool_int(record["enabled"]),
                record["created_at"],
            ),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM ai_experiments WHERE name = ?", (record["name"],)).fetchone()
    return dict(row) if row else record


def list_ai_experiments(config: dict[str, Any]) -> list[dict[str, Any]]:
    with connect_state_db(config) as connection:
        rows = connection.execute(
            "SELECT * FROM ai_experiments ORDER BY created_at DESC, id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


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
    with connect_state_db(config) as connection:
        row = connection.execute(
            "SELECT id FROM strategy_versions WHERE version = ?",
            (version,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO strategy_versions (
                    version, name, description, created_at, is_active, traffic_percent,
                    indicators_json, rules_json, risk_config_json, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["version"],
                    record["name"],
                    record["description"],
                    record["created_at"],
                    record["is_active"],
                    record["traffic_percent"],
                    record["indicators_json"],
                    record["rules_json"],
                    record["risk_config_json"],
                    record["payload_json"],
                ),
            )
            connection.commit()


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
    with connect_state_db(config) as connection:
        if record["is_active"]:
            connection.execute("UPDATE strategy_versions SET is_active = 0")
        connection.execute(
            """
            INSERT INTO strategy_versions (
                version, name, description, created_at, is_active, traffic_percent,
                indicators_json, rules_json, risk_config_json, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(version) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                is_active = excluded.is_active,
                traffic_percent = excluded.traffic_percent,
                indicators_json = excluded.indicators_json,
                rules_json = excluded.rules_json,
                risk_config_json = excluded.risk_config_json,
                payload_json = excluded.payload_json
            """,
            (
                record["version"],
                record["name"],
                record["description"],
                record["created_at"],
                record["is_active"],
                record["traffic_percent"],
                record["indicators_json"],
                record["rules_json"],
                record["risk_config_json"],
                record["payload_json"],
            ),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM strategy_versions WHERE version = ?", (version,)).fetchone()
    result = dict(row) if row else record
    result["performance"] = _strategy_performance_stats(config, version)
    return result


def activate_strategy_version(config: dict[str, Any], version: str) -> dict[str, Any]:
    ensure_strategy_versions(config)
    with connect_state_db(config) as connection:
        connection.execute("UPDATE strategy_versions SET is_active = 0")
        connection.execute("UPDATE strategy_versions SET is_active = 1 WHERE version = ?", (version,))
        connection.commit()
        row = connection.execute("SELECT * FROM strategy_versions WHERE version = ?", (version,)).fetchone()
    if row is None:
        raise ValueError(f"Strategy version not found: {version}")
    result = dict(row)
    result["performance"] = _strategy_performance_stats(config, version)
    return result


def strategy_history(config: dict[str, Any]) -> list[dict[str, Any]]:
    ensure_strategy_versions(config)
    with connect_state_db(config) as connection:
        rows = connection.execute(
            "SELECT * FROM strategy_versions ORDER BY created_at DESC, id DESC"
        ).fetchall()
    items = [dict(row) for row in rows]
    for item in items:
        item["performance"] = _strategy_performance_stats(config, str(item.get("version") or ""))
    return items


def current_strategy_state(config: dict[str, Any]) -> dict[str, Any]:
    ensure_strategy_versions(config)
    with connect_state_db(config) as connection:
        rows = connection.execute(
            "SELECT * FROM strategy_versions WHERE is_active = 1 ORDER BY id ASC"
        ).fetchall()
    active = [dict(row) for row in rows]
    for item in active:
        item["performance"] = _strategy_performance_stats(config, str(item.get("version") or ""))
    return {
        "active": active,
        "count": len(active),
    }


def select_runtime_config(config: dict[str, Any]) -> dict[str, Any]:
    ensure_strategy_versions(config)
    runtime = deepcopy(config)
    with connect_state_db(config) as connection:
        rows = connection.execute(
            "SELECT * FROM strategy_versions WHERE is_active = 1 ORDER BY created_at ASC, id ASC"
        ).fetchall()
    active = [dict(row) for row in rows]
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
    overrides = _json_loads(selected.get("payload_json"), {})
    if isinstance(overrides, dict) and overrides:
        runtime = deep_merge(runtime, overrides)
    runtime["selected_strategy_version"] = selected.get("version")
    return runtime


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
        indicator = to_jsonable(getattr(snapshots[0], "indicator_summary", None) or snapshots[0].__dict__)
        regime, confidence, reason = _market_regime_from_indicators(config, indicator)
        result = {
            "created_at": _iso_now(),
            "regime": regime,
            "confidence": round(confidence, 2),
            "indicators": indicator,
            "reason": reason,
        }
    with connect_state_db(config) as connection:
        connection.execute(
            """
            INSERT INTO market_regime_history (created_at, regime, confidence, indicators_json, reason)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                result["created_at"],
                result["regime"],
                result["confidence"],
                json.dumps(result["indicators"], ensure_ascii=False),
                result["reason"],
            ),
        )
        connection.commit()
    return result


def current_market_regime(config: dict[str, Any]) -> dict[str, Any]:
    with connect_state_db(config) as connection:
        row = connection.execute(
            "SELECT * FROM market_regime_history ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
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
    with connect_state_db(config) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM market_regime_history
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
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
    rows: list[tuple[Any, ...]] = []
    for candidate in candidates:
        payload = _candidate_payload(candidate)
        rows.append(
            (
                now,
                candidate.symbol,
                candidate.side.upper(),
                _candidate_rule_score(candidate),
                float(candidate.confidence or 0),
                float(candidate.risk_reward or 0),
                candidate.entry,
                candidate.stop_loss,
                candidate.take_profit,
                json.dumps(payload, ensure_ascii=False),
            )
        )
    with connect_state_db(config) as connection:
        connection.executemany(
            """
            INSERT INTO trade_candidates (
                created_at, symbol, side, rule_score, gpt_confidence, risk_reward,
                entry_price, stop_loss, take_profit, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        connection.commit()
    return len(rows)


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
    candidate_payload = _candidate_payload(selected) if selected else {}
    indicator = candidate_payload.get("indicator_summary") if isinstance(candidate_payload, dict) else {}
    entry = selected.entry if selected else None
    scan = decision.scan_comparison or {}
    execution = decision.execution or None
    market_regime = ((scan.get("market_regime") or {}) if isinstance(scan.get("market_regime"), dict) else {}) or {}
    payload_json = json.dumps(to_jsonable(decision), ensure_ascii=False)
    with connect_state_db(config) as connection:
        cursor = connection.execute(
            """
            INSERT INTO ai_trade_decisions (
                created_at, symbol, timeframe, decision, confidence, rule_score, side,
                entry_price, stop_loss, take_profit1, take_profit2, risk_reward, funding_rate,
                open_interest_change, rsi, macd_signal, trend, volume_change, news_score,
                reason_json, raw_prompt, raw_response, order_id, trade_status, pnl, closed_at,
                prompt_version, prompt_hash, model_name, model_version, strategy_version,
                validator_version, recovery_version, health_version, experiment_name,
                market_regime, regime_confidence, snapshot_json, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                to_jsonable(decision.created_at),
                selected.symbol if selected else None,
                str(config.get("strategy", {}).get("timeframe", "")),
                _decision_label(decision),
                float(selected.confidence) if selected else None,
                _candidate_rule_score(selected),
                _side_label(selected),
                entry,
                selected.stop_loss if selected else None,
                selected.take_profit if selected else None,
                None,
                selected.risk_reward if selected else None,
                _safe_float(indicator.get("funding_rate")) if isinstance(indicator, dict) else None,
                _safe_float(indicator.get("open_interest_change")) if isinstance(indicator, dict) else None,
                _safe_float(indicator.get("rsi")) if isinstance(indicator, dict) else None,
                _safe_float(indicator.get("macd_signal")) if isinstance(indicator, dict) else None,
                indicator.get("trend") if isinstance(indicator, dict) else None,
                _safe_float(indicator.get("volume_ratio")) if isinstance(indicator, dict) else None,
                float(selected.news_score) if selected else None,
                json.dumps(
                    {
                        "risk_reasons": decision.risk_check.reasons,
                        "risk_warnings": decision.risk_check.warnings,
                        "candidate_reasons": selected.reasons if selected else [],
                        "candidate_warnings": selected.warnings if selected else [],
                    },
                    ensure_ascii=False,
                ),
                metadata.get("raw_prompt"),
                metadata.get("raw_response"),
                execution.order_id if execution else None,
                None,
                None,
                None,
                str(metadata.get("prompt_version") or config.get("prompt_engine", {}).get("default_prompt_version", "prompt-v1")),
                str(metadata.get("prompt_hash") or ensure_prompt_version(config)["prompt_hash"]),
                str(metadata.get("model") or config.get("ai", {}).get("okx", {}).get("model", "gpt-5.5")),
                str(metadata.get("model_version") or metadata.get("model") or config.get("ai", {}).get("okx", {}).get("model", "gpt-5.5")),
                str(config.get("selected_strategy_version") or config.get("strategy_versioning", {}).get("default_version", "strategy-v1")),
                str(config.get("strategy_versioning", {}).get("validator_version", "validator-v1")),
                str(config.get("strategy_versioning", {}).get("recovery_version", "recovery-v1")),
                str(config.get("strategy_versioning", {}).get("health_version", "health-v1")),
                metadata.get("experiment_name"),
                market_regime.get("regime") or selected.market_regime if selected else None,
                market_regime.get("confidence") or (selected.regime_confidence if selected else None),
                json.dumps(candidate_payload, ensure_ascii=False),
                payload_json,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def create_ai_trade_decision(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
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
        "raw_prompt": payload.get("raw_prompt"),
        "raw_response": payload.get("raw_response"),
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
        "snapshot_json": json.dumps(payload.get("snapshot_json") or {}, ensure_ascii=False),
        "payload_json": json.dumps(payload, ensure_ascii=False),
    }
    with connect_state_db(config) as connection:
        cursor = connection.execute(
            """
            INSERT INTO ai_trade_decisions (
                created_at, symbol, timeframe, decision, confidence, rule_score, side,
                entry_price, stop_loss, take_profit1, take_profit2, risk_reward, funding_rate,
                open_interest_change, rsi, macd_signal, trend, volume_change, news_score,
                reason_json, raw_prompt, raw_response, order_id, trade_status, pnl, closed_at,
                prompt_version, prompt_hash, model_name, model_version, strategy_version,
                validator_version, recovery_version, health_version, experiment_name,
                market_regime, regime_confidence, snapshot_json, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(row.values()),
        )
        connection.commit()
        row["id"] = int(cursor.lastrowid)
    return row


def recent_ai_trade_decisions(config: dict[str, Any], limit: int = 50) -> list[dict[str, Any]]:
    with connect_state_db(config) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM ai_trade_decisions
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        payload["reason"] = _json_loads(payload.get("reason_json"), {})
        payload["snapshot"] = _json_loads(payload.get("snapshot_json"), {})
        result.append(payload)
    return result


def ai_trade_decision_stats(config: dict[str, Any]) -> dict[str, Any]:
    rows = recent_ai_trade_decisions(config, limit=5000)
    total = len(rows)
    long_rows = [row for row in rows if row.get("decision") == "ENTER_LONG"]
    short_rows = [row for row in rows if row.get("decision") == "ENTER_SHORT"]
    no_trade_rows = [row for row in rows if row.get("decision") == "NO_TRADE"]

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
        "longCount": len(long_rows),
        "shortCount": len(short_rows),
        "noTradeCount": len(no_trade_rows),
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
    with connect_state_db(config) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM trade_executions
            WHERE status IN ('OPEN')
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _closed_trade_executions(config: dict[str, Any], *, limit: int = 5000) -> list[dict[str, Any]]:
    with connect_state_db(config) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM trade_executions
            WHERE status IN ('WIN', 'LOSS', 'BREAKEVEN', 'CLOSED')
            ORDER BY COALESCE(closed_at, updated_at, created_at) DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


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
    with connect_state_db(config) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM trade_executions
            WHERE strategy_version = ? AND status IN ('WIN', 'LOSS', 'BREAKEVEN', 'CLOSED')
            ORDER BY COALESCE(closed_at, updated_at, created_at) DESC, id DESC
            """,
            (version,),
        ).fetchall()
    return _trade_performance_stats([dict(row) for row in rows])


def _trading_risk_settings(config: dict[str, Any]) -> dict[str, Any]:
    settings = deepcopy(config.get("trading_risk", {}))
    settings["max_concurrent_positions"] = max(
        1,
        _safe_int(settings.get("max_concurrent_positions"), _safe_int(config.get("risk", {}).get("max_active_trades"), 1)),
    )
    return settings


def _slot_state(open_rows: list[dict[str, Any]], max_slots: int) -> tuple[int, list[int]]:
    used = sorted(
        {
            _safe_int(row.get("position_slot"))
            for row in open_rows
            if row.get("position_slot") is not None and _safe_int(row.get("position_slot")) > 0
        }
    )
    free = [slot for slot in range(1, max_slots + 1) if slot not in used]
    return len(open_rows), free


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
    with connect_state_db(config) as connection:
        existing = connection.execute("SELECT * FROM trading_system_state WHERE id = 1").fetchone()
        if existing:
            paused_until = _parse_time(existing["paused_until"])
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
    with connect_state_db(config) as connection:
        connection.execute(
            """
            INSERT INTO trading_system_state (
                id, mechanism_name, is_recovery_mode, global_loss_streak, is_paused,
                paused_until, current_normal_min_rule_score, current_normal_min_gpt_confidence,
                updated_at, payload_json
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                mechanism_name = excluded.mechanism_name,
                is_recovery_mode = excluded.is_recovery_mode,
                global_loss_streak = excluded.global_loss_streak,
                is_paused = excluded.is_paused,
                paused_until = excluded.paused_until,
                current_normal_min_rule_score = excluded.current_normal_min_rule_score,
                current_normal_min_gpt_confidence = excluded.current_normal_min_gpt_confidence,
                updated_at = excluded.updated_at,
                payload_json = excluded.payload_json
            """,
            (
                payload["mechanismName"],
                _bool_int(payload["isRecoveryMode"]),
                payload["globalLossStreak"],
                _bool_int(payload["isPaused"]),
                payload["pausedUntil"],
                payload["currentNormalMinRuleScore"],
                payload["currentNormalMinGptConfidence"],
                payload["updatedAt"],
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        connection.commit()
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
    with connect_state_db(config) as connection:
        connection.execute(
            """
            INSERT INTO trading_health_state (
                id, mechanism_name, is_healthy, is_warning, is_critical, total_trades,
                win_count, loss_count, breakeven_count, win_rate, gross_profit, gross_loss,
                profit_factor, total_pnl, max_drawdown_percent, risk_multiplier,
                score_adjustment, confidence_adjustment, is_paused, paused_until, reason,
                updated_at, payload_json
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                mechanism_name = excluded.mechanism_name,
                is_healthy = excluded.is_healthy,
                is_warning = excluded.is_warning,
                is_critical = excluded.is_critical,
                total_trades = excluded.total_trades,
                win_count = excluded.win_count,
                loss_count = excluded.loss_count,
                breakeven_count = excluded.breakeven_count,
                win_rate = excluded.win_rate,
                gross_profit = excluded.gross_profit,
                gross_loss = excluded.gross_loss,
                profit_factor = excluded.profit_factor,
                total_pnl = excluded.total_pnl,
                max_drawdown_percent = excluded.max_drawdown_percent,
                risk_multiplier = excluded.risk_multiplier,
                score_adjustment = excluded.score_adjustment,
                confidence_adjustment = excluded.confidence_adjustment,
                is_paused = excluded.is_paused,
                paused_until = excluded.paused_until,
                reason = excluded.reason,
                updated_at = excluded.updated_at,
                payload_json = excluded.payload_json
            """,
            (
                "Bunny Health Monitor",
                _bool_int(payload["isHealthy"]),
                _bool_int(payload["isWarning"]),
                _bool_int(payload["isCritical"]),
                payload["totalTrades"],
                payload["winCount"],
                payload["lossCount"],
                payload["breakevenCount"],
                payload["winRate"],
                payload["grossProfit"],
                payload["grossLoss"],
                payload["profitFactor"],
                payload["totalPnl"],
                payload["maxDrawdownPercent"],
                payload["riskMultiplier"],
                payload["scoreAdjustment"],
                payload["confidenceAdjustment"],
                _bool_int(payload["isPaused"]),
                payload["pausedUntil"],
                payload["reason"],
                payload["updatedAt"],
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        connection.commit()
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
    row = {
        "created_at": created_at,
        "updated_at": created_at,
        "symbol": candidate.symbol,
        "position_slot": validation.get("assignedPositionSlot") if allowed else None,
        "parent_position_id": None,
        "side": candidate.side.upper(),
        "entry_price": candidate.entry,
        "stop_loss": candidate.stop_loss,
        "take_profit": candidate.take_profit,
        "risk_reward": candidate.risk_reward,
        "risk_percent": validation.get("riskPercent") or candidate.risk_percent or 0,
        "rule_score": _candidate_rule_score(candidate),
        "gpt_confidence": candidate.confidence,
        "status": "OPEN" if allowed else "REJECTED",
        "pnl": None,
        "reject_reason": None if allowed else validation.get("reason"),
        "closed_at": None if allowed else created_at,
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
    with connect_state_db(config) as connection:
        cursor = connection.execute(
            """
            INSERT INTO trade_executions (
                created_at, updated_at, symbol, position_slot, parent_position_id, side, entry_price,
                stop_loss, take_profit, risk_reward, risk_percent, rule_score, gpt_confidence, status, pnl,
                reject_reason, closed_at, payload_json, market_regime, regime_confidence, strategy_version,
                rule_engine_version, validator_version, recovery_version, health_version, prompt_version,
                prompt_hash, model_name, model_version, system_version, decision_engine_version, bunny_version,
                health_monitor_version, slot_refill_version, experiment_name, prompt_tokens, completion_tokens,
                latency_ms, snapshot_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(row.values()),
        )
        connection.commit()
        row["id"] = int(cursor.lastrowid)
    row["validation"] = validation
    refresh_trading_system_state(config)
    refresh_bunny_health_state(config)
    return row


def _mark_recent_ai_decisions_closed(config: dict[str, Any], execution_row: dict[str, Any]) -> None:
    with connect_state_db(config) as connection:
        connection.execute(
            """
            UPDATE ai_trade_decisions
            SET trade_status = ?, pnl = ?, closed_at = ?
            WHERE symbol = ? AND side = ? AND trade_status IS NULL
            """,
            (
                execution_row["status"],
                execution_row["pnl"],
                execution_row["closed_at"],
                execution_row["symbol"],
                execution_row["side"],
            ),
        )
        connection.commit()


def _mark_trade_candidate_used(config: dict[str, Any], candidate_id: int) -> None:
    with connect_state_db(config) as connection:
        connection.execute(
            "UPDATE trade_candidates SET is_used = 1, used_at = ? WHERE id = ?",
            (_iso_now(), candidate_id),
        )
        connection.commit()


def _claim_trade_candidate(config: dict[str, Any], candidate_id: int) -> bool:
    with connect_state_db(config) as connection:
        cursor = connection.execute(
            "UPDATE trade_candidates SET is_used = 1, used_at = ? WHERE id = ? AND is_used = 0",
            (_iso_now(), candidate_id),
        )
        connection.commit()
    return cursor.rowcount == 1


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
    with connect_state_db(config) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM trade_candidates
            WHERE created_at >= ? AND is_used = 0 AND COALESCE(rule_score, 0) >= ?
            ORDER BY rule_score DESC, gpt_confidence DESC, risk_reward DESC, id ASC
            LIMIT ?
            """,
            (
                cutoff,
                _safe_float(settings.get("min_candidate_rule_score"), 78),
                max(1, _safe_int(settings.get("max_refill_attempts_per_slot"), 3)),
            ),
        ).fetchall()
    for row in rows:
        payload = _json_loads(row["payload_json"], {})
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


def close_trade_execution(config: dict[str, Any], trade_execution_id: int, status: str, pnl: float) -> dict[str, Any]:
    closed_at = _iso_now()
    normalized_status = str(status or "CLOSED").upper()
    with connect_state_db(config) as connection:
        row = connection.execute(
            "SELECT * FROM trade_executions WHERE id = ?",
            (trade_execution_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Trade execution not found: {trade_execution_id}")
        connection.execute(
            """
            UPDATE trade_executions
            SET status = ?, pnl = ?, closed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (normalized_status, pnl, closed_at, closed_at, trade_execution_id),
        )
        connection.commit()
        updated = connection.execute(
            "SELECT * FROM trade_executions WHERE id = ?",
            (trade_execution_id,),
        ).fetchone()
    payload = dict(updated) if updated else {}
    _mark_recent_ai_decisions_closed(config, payload)
    refresh_trading_system_state(config)
    refresh_bunny_health_state(config)
    refill = try_slot_refill(config, _safe_int(payload.get("position_slot"), 0)) if payload.get("position_slot") else {"refilled": False, "reason": "No slot"}
    payload["slotRefill"] = refill
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
    with connect_state_db(config) as connection:
        row = connection.execute(
            "SELECT * FROM trade_executions WHERE id = ?",
            (trade_execution_id,),
        ).fetchone()
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
    with connect_state_db(config) as connection:
        connection.execute(
            """
            INSERT INTO replay_history (
                trade_execution_id, prompt_version, strategy_version, model_version, old_decision,
                new_decision, old_confidence, new_confidence, latency, replay_at, decision_changed,
                confidence_changed, reason_changed, old_reason_json, new_reason_json, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result["tradeExecutionId"],
                result["promptVersion"],
                result["strategyVersion"],
                result["modelVersion"],
                result["oldDecision"],
                result["newDecision"],
                result["oldConfidence"],
                result["newConfidence"],
                result["latency"],
                result["replayAt"],
                _bool_int(result["decisionChanged"]),
                _bool_int(result["confidenceChanged"]),
                _bool_int(result["reasonChanged"]),
                json.dumps(result["oldReason"], ensure_ascii=False),
                json.dumps(result["newReason"], ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
            ),
        )
        connection.commit()
    return result


def replay_batch(config: dict[str, Any], limit: int) -> dict[str, Any]:
    with connect_state_db(config) as connection:
        rows = connection.execute(
            """
            SELECT id
            FROM trade_executions
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    results = [replay_trade_execution(config, _safe_int(row["id"])) for row in rows]
    stats = replay_stats(config)
    return {
        "count": len(results),
        "results": results,
        "stats": stats,
    }


def replay_stats(config: dict[str, Any]) -> dict[str, Any]:
    with connect_state_db(config) as connection:
        rows = connection.execute(
            """
            SELECT r.*, e.status AS trade_status, e.pnl AS trade_pnl, e.risk_reward, e.gpt_confidence,
                   e.created_at AS trade_created_at, e.closed_at AS trade_closed_at
            FROM replay_history r
            LEFT JOIN trade_executions e ON e.id = r.trade_execution_id
            ORDER BY r.replay_at DESC, r.id DESC
            """
        ).fetchall()
    payloads = [dict(row) for row in rows]
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

