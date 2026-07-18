from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .codex_features import (
    candidate_from_payload,
    ensure_prompt_version,
    prompt_status,
    refresh_bunny_health_state,
    refresh_trading_system_state,
)
from .market import create_exchange
from .models import TradeCandidate, to_jsonable
from .storage import (
    ensure_ai_model_version,
    get_prompt_metric,
    insert_trade_execution_row,
    list_pending_orders,
    list_trade_execution_rows,
    refresh_pending_order,
    reclassify_unknown_trade_closures,
    save_pending_order,
    save_prompt_metric_snapshot,
    set_pending_order_exchange_order,
    update_trade_execution,
)


def _float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    number = _float(value)
    return default if number is None else number


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _execution_key(row: dict[str, Any]) -> tuple[str, str]:
    return (
        str(row.get("symbol") or "").strip(),
        str(row.get("side") or "").strip().upper(),
    )


def _close_stale_open_execution(
    config: dict[str, Any],
    row: dict[str, Any],
    *,
    closed_at: str,
    reason: str,
) -> None:
    update_trade_execution(
        config,
        int(row["id"]),
        {
            "status": "RECONCILED",
            "close_reason": reason,
            "closed_at": closed_at,
            "updated_at": closed_at,
            "position_slot": None,
        },
    )


def _position_side(position: dict[str, Any]) -> str:
    info = position.get("info", {}) if isinstance(position.get("info"), dict) else {}
    side = position.get("side") or info.get("posSide")
    if side and side != "net":
        return str(side).lower()
    contracts = _float(position.get("contracts") or info.get("pos")) or 0
    if contracts > 0:
        return "long"
    if contracts < 0:
        return "short"
    return "long"


def _order_side(order: dict[str, Any]) -> str:
    raw_side = str(order.get("side") or "").lower()
    return "long" if raw_side == "buy" else "short" if raw_side == "sell" else raw_side


def _base_symbol(symbol: str) -> str:
    return str(symbol or "").split("/")[0].split("-")[0].upper()


def _tp_sl_from_order(order: dict[str, Any]) -> tuple[float | None, float | None]:
    raw = order.get("raw") if isinstance(order.get("raw"), dict) else {}
    stop_loss = None
    take_profit = None
    attach_orders = raw.get("attachAlgoOrds")
    if isinstance(attach_orders, str):
        try:
            attach_orders = json.loads(attach_orders)
        except json.JSONDecodeError:
            attach_orders = []
    if isinstance(attach_orders, list):
        for item in attach_orders:
            if not isinstance(item, dict):
                continue
            if stop_loss is None:
                stop_loss = _float(item.get("slTriggerPx") or item.get("slOrdPx"))
            if take_profit is None:
                take_profit = _float(item.get("tpTriggerPx") or item.get("tpOrdPx"))
    return stop_loss, take_profit


def _fallback_take_profit(entry: float, side: str, pct: float) -> float:
    move = entry * max(0.0, pct) / 100.0
    return entry + move if side == "long" else entry - move


def _fallback_stop_loss(entry: float, side: str, pct: float) -> float:
    move = entry * max(0.0, pct) / 100.0
    return entry - move if side == "long" else entry + move


def _candidate_from_open_order(config: dict[str, Any], order: dict[str, Any]) -> TradeCandidate:
    symbol = str(order.get("symbol") or "")
    side = _order_side(order) or "long"
    price = _safe_float(order.get("price") or order.get("triggerPrice") or order.get("last"), 0.0)
    amount = _safe_float(order.get("remaining") or order.get("amount"), 0.0)
    stop_loss, take_profit = _tp_sl_from_order(order)
    stop_loss = stop_loss if stop_loss is not None else _fallback_stop_loss(price, side, 2.0)
    take_profit = take_profit if take_profit is not None else _fallback_take_profit(price, side, 3.0)
    leverage = max(1.0, _safe_float(config.get("exchange", {}).get("leverage"), 1.0))
    notional = price * amount
    order_usdt = notional / leverage if leverage else notional
    return TradeCandidate(
        symbol=symbol,
        base=_base_symbol(symbol),
        side=side,  # type: ignore[arg-type]
        confidence=0.0,
        entry=price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        risk_reward=0.0,
        order_usdt=order_usdt,
        quantity=amount,
        spread_pct=None,
        news_score=0.0,
        news_count=0,
        scan_source="okx_runtime_sync",
        decision_metadata={"source": "okx_open_order_sync"},
    )


def _candidate_from_pending_row(row: dict[str, Any]) -> TradeCandidate | None:
    try:
        payload = json.loads(str(row.get("payload_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    candidate = candidate_from_payload(payload)
    if not candidate.symbol or candidate.side not in {"long", "short"}:
        return None
    return candidate


def _mini_pending_placeholder_candidate(row: dict[str, Any]) -> TradeCandidate | None:
    if str(row.get("exchange_order_id") or ""):
        return None
    candidate = _candidate_from_pending_row(row)
    if candidate is None:
        return None
    metadata = candidate.decision_metadata
    if not isinstance(metadata, dict) or not isinstance(metadata.get("mini_setup"), dict):
        return None
    review = metadata.get("okx_review")
    if not isinstance(review, dict) or not bool(review.get("accepted_for_okx")):
        return None
    return candidate


def _fetch_account_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    exchange = create_exchange(config, authenticated=True)
    exchange.load_markets()
    return {
        "enabled": True,
        "mode": config.get("mode"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "positions": list(exchange.fetch_positions() or []),
        "open_orders": list(exchange.fetch_open_orders() or []),
    }


def sync_ai_runtime_metadata(config: dict[str, Any]) -> dict[str, Any]:
    prompt_row = ensure_prompt_version(config)
    prompt_version = str(prompt_row.get("version") or "prompt-v1")
    prompt_hash = str(prompt_row.get("prompt_hash") or "")
    seeded_models: list[str] = []
    internal_model = str(config.get("ai", {}).get("internal", {}).get("model") or "").strip()
    okx_model = str(config.get("ai", {}).get("okx", {}).get("model") or "").strip()
    now = datetime.now(timezone.utc).isoformat()
    for model_name in [internal_model, okx_model]:
        if not model_name:
            continue
        ensure_ai_model_version(
            config,
            model_name=model_name,
            model_version=model_name,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            created_at=now,
        )
        seeded_models.append(model_name)
    prompt_metric = get_prompt_metric(config, prompt_version)
    seeded_prompt_metric = False
    if prompt_metric is None:
        status = prompt_status(config)
        save_prompt_metric_snapshot(
            config,
            {
                "prompt_version": prompt_version,
                "prompt_hash": prompt_hash,
                "total_requests": 0,
                "average_prompt_tokens": 0,
                "average_completion_tokens": 0,
                "average_latency": 0,
                "estimated_cached_tokens": status.get("estimatedStaticTokens") or 0,
                "estimated_dynamic_tokens": status.get("estimatedDynamicTokens") or 0,
                "cache_hit_percent": status.get("estimatedCacheHit") or 0,
                "updated_at": now,
                "source": "config_seed",
                "notes": "No successful OpenAI usage recorded yet; seeded from current prompt configuration.",
            },
        )
        seeded_prompt_metric = True
    return {
        "prompt_version": prompt_version,
        "models_seeded": seeded_models,
        "seeded_prompt_metric": seeded_prompt_metric,
    }


def sync_exchange_runtime_state(
    config: dict[str, Any],
    *,
    account_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if config.get("mode") == "dry_run":
        return {"enabled": False, "reason": "dry_run", "positions_synced": 0, "orders_synced": 0}
    snapshot = account_snapshot or _fetch_account_snapshot(config)
    reclassified_executions = reclassify_unknown_trade_closures(config)
    created_at = str(snapshot.get("created_at") or datetime.now(timezone.utc).isoformat())
    position_rows = [item for item in (snapshot.get("positions") or []) if isinstance(item, dict)]
    open_orders = [item for item in (snapshot.get("open_orders") or []) if isinstance(item, dict)]
    active_pending = list_pending_orders(config, status="ACTIVE", limit=1000)
    pending_by_exchange_id = {
        str(row.get("exchange_order_id") or ""): row
        for row in active_pending
        if str(row.get("exchange_order_id") or "")
    }
    mini_placeholders_by_key: dict[tuple[str, str], list[tuple[dict[str, Any], TradeCandidate]]] = {}
    for row in active_pending:
        placeholder_candidate = _mini_pending_placeholder_candidate(row)
        if placeholder_candidate is None:
            continue
        key = (placeholder_candidate.symbol, placeholder_candidate.side)
        mini_placeholders_by_key.setdefault(key, []).append((row, placeholder_candidate))
    orders_synced = 0
    for order in open_orders:
        exchange_order_id = str(order.get("id") or order.get("clientOrderId") or "")
        if not exchange_order_id:
            continue
        candidate = _candidate_from_open_order(config, order)
        existing = pending_by_exchange_id.get(exchange_order_id)
        if existing:
            stored_candidate = _candidate_from_pending_row(existing)
            refresh_candidate = stored_candidate or candidate
            if candidate.quantity and candidate.quantity > 0:
                refresh_candidate.quantity = candidate.quantity
            refresh_pending_order(
                config,
                int(existing["id"]),
                refresh_candidate,
                status="LC_OKX",
                max_age_days=float(config.get("pending_orders", {}).get("exchange_max_age_days", 1.5) or 1.5),
            )
        else:
            placeholder_rows = mini_placeholders_by_key.get((candidate.symbol, candidate.side)) or []
            placeholder = placeholder_rows.pop(0) if placeholder_rows else None
            if placeholder:
                placeholder_row, placeholder_candidate = placeholder
                set_pending_order_exchange_order(
                    config,
                    int(placeholder_row["id"]),
                    placeholder_candidate,
                    exchange_order_id,
                    max_age_days=float(
                        config.get("pending_orders", {}).get("exchange_max_age_days", 1.5) or 1.5
                    ),
                )
            else:
                save_pending_order(
                    config,
                    candidate,
                    exchange_order_id,
                    status="LC_OKX",
                    max_age_days=float(
                        config.get("pending_orders", {}).get("exchange_max_age_days", 1.5) or 1.5
                    ),
                )
        orders_synced += 1

    open_execution_rows = list_trade_execution_rows(config, statuses=["OPEN"], limit=1000)
    executions_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in open_execution_rows:
        key = _execution_key(row)
        if all(key):
            executions_by_key.setdefault(key, []).append(row)

    active_position_keys: set[tuple[str, str]] = set()
    matched_execution_ids: set[int] = set()
    positions_synced = 0
    next_slot = 1
    for position in position_rows:
        info = position.get("info", {}) if isinstance(position.get("info"), dict) else {}
        contracts = abs(_safe_float(position.get("contracts") or info.get("pos"), 0.0))
        if contracts <= 0:
            continue
        symbol = str(position.get("symbol") or info.get("instId") or "")
        side = _position_side(position)
        if not symbol or side not in {"long", "short"}:
            continue
        position_key = (symbol, side.upper())
        active_position_keys.add(position_key)
        entry_price = _float(position.get("entry_price") or position.get("entryPrice") or info.get("avgPx"))
        mark_price = _float(position.get("mark_price") or position.get("markPrice") or info.get("markPx"))
        leverage = _safe_float(position.get("leverage") or info.get("lever"), _safe_float(config.get("exchange", {}).get("leverage"), 1.0))
        stop_loss = _float(position.get("stop_loss"))
        take_profit = _float(position.get("take_profit"))
        payload = {
            "source": "okx_position_sync",
            "position": to_jsonable(position),
            "snapshot_created_at": created_at,
        }
        matching_rows = executions_by_key.get(position_key, [])
        matched = matching_rows[0] if matching_rows else None
        updates = {
            "updated_at": created_at,
            "status": "OPEN",
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "pnl": _float(position.get("unrealized_pnl") or position.get("unrealizedPnl") or info.get("upl")),
            "snapshot_json": json.dumps(payload, ensure_ascii=False),
        }
        if matched:
            update_trade_execution(config, int(matched["id"]), updates)
            matched_execution_ids.add(int(matched["id"]))
        else:
            prompt_row = ensure_prompt_version(config)
            insert_trade_execution_row(
                config,
                {
                    "created_at": created_at,
                    "updated_at": created_at,
                    "symbol": symbol,
                    "position_slot": next_slot,
                    "parent_position_id": None,
                    "side": side.upper(),
                    "entry_price": entry_price,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "risk_reward": None,
                    "risk_percent": 0,
                    "rule_score": None,
                    "gpt_confidence": None,
                    "status": "OPEN",
                    "pnl": _float(position.get("unrealized_pnl") or position.get("unrealizedPnl") or info.get("upl")),
                    "reject_reason": None,
                    "closed_at": None,
                    "payload_json": json.dumps(payload, ensure_ascii=False),
                    "market_regime": None,
                    "regime_confidence": None,
                    "strategy_version": str(config.get("selected_strategy_version") or config.get("strategy_versioning", {}).get("default_version", "strategy-v1")),
                    "rule_engine_version": str(config.get("strategy_versioning", {}).get("rule_engine_version", "rule-engine-v1")),
                    "validator_version": str(config.get("strategy_versioning", {}).get("validator_version", "validator-v1")),
                    "recovery_version": str(config.get("strategy_versioning", {}).get("recovery_version", "recovery-v1")),
                    "health_version": str(config.get("strategy_versioning", {}).get("health_version", "health-v1")),
                    "prompt_version": str(prompt_row.get("version") or "prompt-v1"),
                    "prompt_hash": str(prompt_row.get("prompt_hash") or ""),
                    "model_name": str(config.get("ai", {}).get("okx", {}).get("model", "gpt-5.5")),
                    "model_version": str(config.get("ai", {}).get("okx", {}).get("model", "gpt-5.5")),
                    "system_version": str(config.get("prompt_engine", {}).get("system_version", "system-v1")),
                    "decision_engine_version": str(config.get("prompt_engine", {}).get("decision_engine_version", "decision-engine-v1")),
                    "bunny_version": str(config.get("prompt_engine", {}).get("bunny_version", "bunny-v1")),
                    "health_monitor_version": str(config.get("prompt_engine", {}).get("health_version", "health-v1")),
                    "slot_refill_version": str(config.get("prompt_engine", {}).get("slot_refill_version", "slot-refill-v1")),
                    "experiment_name": None,
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "latency_ms": None,
                    "snapshot_json": json.dumps(payload, ensure_ascii=False),
                    "entry_mode": config.get("mode"),
                    "exchange_leverage": leverage,
                },
            )
            next_slot += 1
        positions_synced += 1

    # A successful OKX snapshot is authoritative. Close internal OPEN rows that
    # no longer have a matching exchange position, and collapse duplicate rows.
    snapshot_authoritative = snapshot.get("enabled", True) is not False
    grace_seconds = max(0, int(config.get("runtime_sync", {}).get("position_close_grace_seconds", 120) or 0))
    snapshot_time = _parse_time(created_at) or datetime.now(timezone.utc)
    executions_closed = 0
    duplicate_executions_closed = 0
    for row in open_execution_rows:
        row_id = int(row["id"])
        key = _execution_key(row)
        if row_id in matched_execution_ids:
            continue
        if not snapshot_authoritative:
            continue
        created_time = _parse_time(row.get("created_at"))
        age_seconds = (snapshot_time - created_time).total_seconds() if created_time else grace_seconds + 1
        if key not in active_position_keys and age_seconds < grace_seconds:
            continue
        if key in active_position_keys:
            reason = "duplicate_open_execution_reconciled"
            duplicate_executions_closed += 1
        else:
            reason = "exchange_position_no_longer_open"
        _close_stale_open_execution(config, row, closed_at=created_at, reason=reason)
        executions_closed += 1

    refresh_trading_system_state(config)
    refresh_bunny_health_state(config)
    return {
        "enabled": True,
        "created_at": created_at,
        "positions_seen": len(position_rows),
        "open_orders_seen": len(open_orders),
        "positions_synced": positions_synced,
        "orders_synced": orders_synced,
        "executions_closed": executions_closed,
        "duplicate_executions_closed": duplicate_executions_closed,
        "reclassified_executions": reclassified_executions,
    }


def sync_runtime_state(
    config: dict[str, Any],
    *,
    account_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ai": sync_ai_runtime_metadata(config),
        "exchange": sync_exchange_runtime_state(config, account_snapshot=account_snapshot),
    }
