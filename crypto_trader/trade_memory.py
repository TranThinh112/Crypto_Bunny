from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .models import to_jsonable
from .storage import save_trade_memory


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_payload(row: dict[str, Any], key: str) -> dict[str, Any]:
    raw = row.get(key)
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _result_r(row: dict[str, Any]) -> float | None:
    pnl = _float(row.get("pnl"))
    entry = _float(row.get("initial_entry_price") or row.get("entry_price"))
    stop = _float(row.get("initial_stop_loss") or row.get("stop_loss"))
    quantity = _float(row.get("initial_quantity") or row.get("quantity") or row.get("contracts"))
    contract_size = _float(row.get("contract_size") or row.get("contractSize"), 1.0)
    risk_per_unit = abs(entry - stop)
    initial_risk = risk_per_unit * quantity * contract_size
    if initial_risk <= 0:
        return None
    return round(pnl / initial_risk, 6)


def _source_from_row(row: dict[str, Any], payload: dict[str, Any]) -> str:
    source = str(
        row.get("source")
        or payload.get("source")
        or payload.get("scan_source")
        or payload.get("journal_type")
        or ""
    ).strip()
    prompt_version = str(row.get("prompt_version") or payload.get("prompt_version") or "")
    if "trend" in source.lower() or prompt_version == "trend-setup-review-v1":
        return "trend_scan"
    if source:
        return source
    return "pool_or_manual"


def _strategy_from_row(row: dict[str, Any], payload: dict[str, Any]) -> str:
    intent = payload.get("trade_intent") if isinstance(payload.get("trade_intent"), dict) else {}
    return str(
        row.get("strategy")
        or payload.get("strategy")
        or intent.get("strategy")
    ) or "unknown"


def _mistake_tags(row: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    pnl = _float(row.get("pnl"))
    close_reason = str(row.get("close_reason") or "")
    if pnl < 0:
        tags.append("loss")
    if pnl > 0:
        tags.append("win")
    if close_reason:
        tags.append(f"exit_{close_reason}")
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    for warning in warnings:
        text = str(warning)
        if text:
            tags.append(text)
    if _float(row.get("risk_reward")) < 1.5:
        tags.append("low_rr")
    return sorted(set(tags))


def build_trade_memory_record(row: dict[str, Any], *, history: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = _parse_payload(row, "payload_json")
    snapshot = _parse_payload(row, "snapshot_json")
    closed_at = str(row.get("closed_at") or row.get("updated_at") or datetime.now(timezone.utc).isoformat())
    execution_id = row.get("id") or row.get("trade_execution_id") or "-"
    pnl = _float(row.get("pnl"))
    record = {
        "key": f"trade_execution:{execution_id}:{closed_at}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "trade_execution_id": execution_id,
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "strategy": _strategy_from_row(row, payload),
        "entry_type": payload.get("entry_type") or payload.get("setup_type") or payload.get("entryAction"),
        "source": _source_from_row(row, payload),
        "opened_at": row.get("created_at"),
        "closed_at": closed_at,
        "holding_minutes": None,
        "entry_price": row.get("entry_price"),
        "initial_entry_price": row.get("initial_entry_price") or row.get("entry_price"),
        "stop_loss": row.get("stop_loss"),
        "initial_stop_loss": row.get("initial_stop_loss") or row.get("stop_loss"),
        "take_profit": row.get("take_profit"),
        "initial_take_profit": row.get("initial_take_profit") or row.get("take_profit"),
        "risk_reward": row.get("risk_reward"),
        "pnl_usdt": pnl,
        "pnl_pct": row.get("pnl_pct"),
        "result_r": _result_r(row),
        "exit_reason": row.get("close_reason"),
        "status": row.get("status"),
        "setup_grade": payload.get("setup_grade") or payload.get("grade"),
        "entry_quality": payload.get("entry_quality"),
        "continuation_score": payload.get("continuation_score"),
        "ai_decision": payload.get("decision") or payload.get("ai_decision"),
        "prompt_version": row.get("prompt_version") or payload.get("prompt_version"),
        "model_name": row.get("model_name") or row.get("model_version"),
        "market_context_at_entry": snapshot or payload.get("market_context") or {},
        "market_context_at_exit": {
            "exchange_close_source": row.get("exchange_close_source"),
            "history": to_jsonable(history) if history else _parse_payload(row, "exchange_close_history_json"),
        },
        "why_win_or_loss": "win" if pnl > 0 else "loss" if pnl < 0 else "flat",
        "mistake_tags": _mistake_tags(row, payload),
        "payload": {
            "trade_execution": to_jsonable(row),
            "payload": payload,
            "snapshot": snapshot,
            "history": to_jsonable(history) if history else None,
        },
    }
    opened = row.get("created_at")
    try:
        if opened:
            opened_at = datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
            closed_dt = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
            record["holding_minutes"] = round((closed_dt - opened_at).total_seconds() / 60.0, 2)
    except ValueError:
        pass
    return record


def record_trade_memory_from_execution(config: dict[str, Any], row: dict[str, Any], *, history: dict[str, Any] | None = None) -> bool:
    if not row or not row.get("symbol"):
        return False
    if not row.get("closed_at") and str(row.get("status") or "").upper() not in {"WIN", "LOSS", "BREAKEVEN", "CLOSED", "RECONCILED"}:
        return False
    return save_trade_memory(config, build_trade_memory_record(row, history=history), limit=int(config.get("trade_memory", {}).get("limit", 500) or 500))
