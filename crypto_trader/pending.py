from __future__ import annotations
from copy import deepcopy
import json
from datetime import datetime, timezone
from typing import Any

from .ai_coordinator import (
    candidate_okx_review,
    okx_review_allows_okx_submission,
    okx_review_is_keep_monitor,
    okx_review_rejection_policy,
    prioritize_pending_records,
    okx_review_requests_market_entry,
)
from .codex_features import record_trade_execution
from .executor import candidate_client_order_id, execute_candidate
from .ledger import append_event
from .lc_pipeline import latest_lc_pipeline_mini_scan
from .market import create_exchange
from .models import RiskCheck, TradeCandidate
from .risk import evaluate_candidate, mini_pending_risk_config
from .storage import (
    close_latest_trade_execution_by_status,
    close_pending_order,
    list_pending_orders,
    next_global_counter,
    prune_pending_orders,
    refresh_pending_order,
    set_pending_order_exchange_order,
)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _order_id(order: dict[str, Any]) -> str:
    info = order.get("info") if isinstance(order.get("info"), dict) else {}
    return str(order.get("id") or info.get("ordId") or order.get("clientOrderId") or "")


def _order_client_id(order: dict[str, Any]) -> str:
    info = order.get("info") if isinstance(order.get("info"), dict) else {}
    return str(order.get("clientOrderId") or info.get("clOrdId") or "").strip()


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _position_symbol(position: dict[str, Any]) -> str:
    return str(position.get("symbol") or position.get("info", {}).get("instId") or "")


def _open_position_symbols(positions: list[dict[str, Any]]) -> set[str]:
    symbols: set[str] = set()
    for position in positions:
        size = position.get("contracts")
        if size is None:
            size = position.get("info", {}).get("pos")
        if abs(_float(size)) > 0:
            symbol = _position_symbol(position)
            if symbol:
                symbols.add(symbol)
    return symbols


def _open_position_count(positions: list[dict[str, Any]]) -> int:
    count = 0
    for position in positions:
        size = position.get("contracts")
        if size is None:
            size = position.get("info", {}).get("pos")
        if abs(_float(size)) > 0:
            count += 1
    return count


def _active_order_symbols(orders: list[dict[str, Any]]) -> set[str]:
    symbols: set[str] = set()
    for order in orders:
        symbol = str(order.get("symbol") or order.get("info", {}).get("instId") or "")
        if symbol:
            symbols.add(symbol)
    return symbols


def _candidate_key(candidate: TradeCandidate) -> tuple[str, str]:
    return candidate.symbol, candidate.side


def _record_key(record: dict[str, Any]) -> tuple[str, str]:
    return str(record.get("symbol") or ""), str(record.get("side") or "")


def _fill_missing_quantity(candidate: TradeCandidate, record: dict[str, Any]) -> None:
    if candidate.quantity and candidate.quantity > 0:
        return
    quantity = _float(record.get("quantity"))
    if quantity > 0:
        candidate.quantity = quantity


def _candidate_from_record(record: dict[str, Any]) -> TradeCandidate | None:
    try:
        payload = json.loads(str(record.get("payload_json") or "{}"))
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    symbol = str(payload.get("symbol") or record.get("symbol") or "")
    side = str(payload.get("side") or record.get("side") or "").lower()
    if not symbol or side not in {"long", "short"}:
        return None
    candidate_fields = set(TradeCandidate.__dataclass_fields__.keys())
    clean_payload = {key: payload.get(key) for key in candidate_fields if key in payload}
    clean_payload.setdefault("symbol", symbol)
    clean_payload.setdefault("base", str(payload.get("base") or record.get("base") or symbol.split("/")[0]))
    clean_payload.setdefault("side", side)
    clean_payload.setdefault("confidence", _float(payload.get("confidence") or record.get("confidence")))
    clean_payload.setdefault("entry", _float(payload.get("entry") or record.get("entry")))
    clean_payload.setdefault("stop_loss", _float(payload.get("stop_loss") or record.get("stop_loss")))
    clean_payload.setdefault("take_profit", _float(payload.get("take_profit") or record.get("take_profit")))
    clean_payload.setdefault("risk_reward", _float(payload.get("risk_reward") or record.get("risk_reward")))
    clean_payload.setdefault("order_usdt", _float(payload.get("order_usdt") or record.get("order_usdt")))
    clean_payload.setdefault("quantity", _optional_float(payload.get("quantity") or record.get("quantity")))
    clean_payload.setdefault("spread_pct", _optional_float(payload.get("spread_pct")))
    clean_payload.setdefault("news_score", _float(payload.get("news_score")))
    clean_payload.setdefault("news_count", int(payload.get("news_count") or 0))
    clean_payload.setdefault("higher_timeframes", payload.get("higher_timeframes") or {})
    clean_payload.setdefault("indicator_summary", payload.get("indicator_summary") or {})
    clean_payload.setdefault("candlestick_patterns", payload.get("candlestick_patterns") or {})
    clean_payload.setdefault("reasons", payload.get("reasons") or [])
    clean_payload.setdefault("warnings", payload.get("warnings") or [])
    return TradeCandidate(**clean_payload)


def _candidate_with_record_metadata(candidate: TradeCandidate, record: dict[str, Any]) -> TradeCandidate:
    record_candidate = _candidate_from_record(record)
    if record_candidate is None or not isinstance(record_candidate.decision_metadata, dict):
        return candidate
    record_metadata = record_candidate.decision_metadata
    if not record_metadata:
        return candidate
    merged = deepcopy(candidate)
    current_metadata = merged.decision_metadata if isinstance(merged.decision_metadata, dict) else {}
    merged.decision_metadata = {
        **record_metadata,
        **current_metadata,
    }
    return merged


def _candidate_for_local_pending_recheck(
    record: dict[str, Any],
    latest_candidate: TradeCandidate,
) -> TradeCandidate | None:
    """Keep the old order geometry while refreshing its current signal context."""
    old_candidate = _candidate_from_record(record)
    if old_candidate is None:
        return None
    refreshed = deepcopy(old_candidate)
    for field in (
        "confidence",
        "spread_pct",
        "news_score",
        "news_count",
        "higher_timeframes",
        "indicator_summary",
        "candlestick_patterns",
        "rule_score",
        "win_probability_pct",
        "setup_quality",
        "market_regime",
        "regime_confidence",
        "reasons",
        "warnings",
    ):
        setattr(refreshed, field, deepcopy(getattr(latest_candidate, field)))
    refreshed.scan_source = "old_pending_local_recheck"
    return refreshed


def _candidate_current_market_price(candidate: TradeCandidate) -> float | None:
    indicator = candidate.indicator_summary if isinstance(candidate.indicator_summary, dict) else {}
    last = _optional_float(indicator.get("last"))
    if last is not None and last > 0:
        return last
    entry = _optional_float(candidate.entry)
    return entry if entry is not None and entry > 0 else None


def _fresh_market_guard_layers_for_recheck(
    config: dict[str, Any],
    symbol: str,
    market_layers: dict[str, dict[str, Any]] | None,
) -> tuple[dict[str, dict[str, Any]], bool, list[str]]:
    symbol_layers = (market_layers or {}).get(symbol)
    if not isinstance(symbol_layers, dict):
        return {}, False, []
    guard_interval = max(30, int(config.get("market_guard", {}).get("interval_seconds", 60) or 60))
    review_config = config.get("pending_orders", {}).get("review", {})
    max_age_seconds = max(
        guard_interval * 3,
        int(review_config.get("market_guard_max_age_seconds", 300) or 300),
    )
    now = datetime.now(timezone.utc)
    fresh: dict[str, Any] = {"symbol": symbol}
    stale_labels: list[str] = []
    for key, label in (("layer_5m", "5m"), ("layer_20m", "20m")):
        layer = symbol_layers.get(key)
        if not isinstance(layer, dict):
            continue
        observed_at = _parse_time(str(layer.get("latest_observed_at") or ""))
        if observed_at is None or (now - observed_at).total_seconds() > max_age_seconds:
            stale_labels.append(label)
            continue
        fresh[key] = layer
    warnings = []
    if stale_labels:
        warnings.append(
            f"{symbol}: ignored stale Market Guard layer(s) during old setup recheck: {', '.join(stale_labels)}"
        )
    checked = any(key in fresh for key in ("layer_5m", "layer_20m"))
    return ({symbol: fresh} if checked else {}), checked, warnings


def _close_latest_lc_trade_execution(
    config: dict[str, Any],
    *,
    symbol: str,
    side: str,
    status: str,
    reason: str | None = None,
) -> None:
    normalized_side = str(side or "").upper()
    if not symbol or not normalized_side:
        return
    close_latest_trade_execution_by_status(
        config,
        symbol=symbol,
        side=normalized_side,
        source_status="LC_PENDING",
        target_status=str(status or "CANCELED").upper(),
        reason=reason,
        closed_at=datetime.now(timezone.utc).isoformat(),
    )


def _record_mini_setup_id(record: dict[str, Any]) -> str:
    candidate = _candidate_from_record(record)
    metadata = candidate.decision_metadata if candidate and isinstance(candidate.decision_metadata, dict) else {}
    mini_setup = metadata.get("mini_setup")
    if not isinstance(mini_setup, dict):
        return ""
    return str(mini_setup.get("setup_id") or "").strip()


def _record_is_mini_keep_setup(record: dict[str, Any]) -> bool:
    candidate = _candidate_from_record(record)
    if candidate is None or not _record_mini_setup_id(record):
        return False
    review = candidate_okx_review(candidate, route="lc_okx_setup_review")
    return bool(review and okx_review_allows_okx_submission(review))


def _cancel_pending_records(
    config: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    reason: str,
) -> dict[str, Any]:
    canceled: list[dict[str, Any]] = []
    warnings: list[str] = []
    exchange = None
    needs_exchange = config.get("mode") != "dry_run" and any(
        str(record.get("exchange_order_id") or "") for record in records
    )
    if needs_exchange:
        try:
            exchange = create_exchange(config, authenticated=True)
            exchange.load_markets()
        except Exception as exc:
            return {
                "ok": False,
                "canceled": [],
                "warnings": [f"Could not connect to OKX to cancel the previous setup: {exc}"],
            }

    for record in records:
        local_id = int(record["id"])
        symbol = str(record.get("symbol") or "")
        exchange_order_id = str(record.get("exchange_order_id") or "")
        if exchange_order_id and exchange is not None:
            try:
                exchange.cancel_order(exchange_order_id, symbol)
            except Exception as exc:
                warnings.append(f"{symbol} order {exchange_order_id} could not be canceled: {exc}")
                continue
        close_pending_order(config, local_id, "CANCELED", reason)
        _close_latest_lc_trade_execution(
            config,
            symbol=symbol,
            side=str(record.get("side") or ""),
            status="CANCELED",
            reason=reason,
        )
        canceled.append(
            {
                "id": local_id,
                "lc_id": record.get("journal_id") or local_id,
                "symbol": symbol,
                "side": str(record.get("side") or ""),
                "exchange_order_id": exchange_order_id or None,
                "setup_id": _record_mini_setup_id(record) or None,
            }
        )
    return {
        "ok": len(canceled) == len(records),
        "canceled": canceled,
        "warnings": warnings,
    }


def cancel_pending_orders_for_symbol(
    config: dict[str, Any],
    symbol: str,
    *,
    reason: str,
) -> dict[str, Any]:
    records = [
        record
        for record in list_pending_orders(config, status="OPEN", limit=200)
        if str(record.get("symbol") or "") == str(symbol or "")
    ]
    if not records:
        return {"ok": True, "canceled": [], "warnings": []}
    return _cancel_pending_records(config, records, reason=reason)


def _record_vt_from_pending(config: dict[str, Any], record: dict[str, Any], *, vt_id: int, lc_id: int) -> None:
    candidate = _candidate_from_record(record)
    if candidate is None:
        return
    record_trade_execution(
        config,
        candidate,
        execution={
            "journal_type": "VT",
            "journal_id": vt_id,
            "linked_journal_id": lc_id,
            "entry_type": "pending_filled",
            "order_type": "limit",
        },
    )


def _record_age_hours(record: dict[str, Any], now: datetime) -> float:
    created_at = _parse_time(str(record.get("created_at") or ""))
    if not created_at:
        return 0.0
    return max(0.0, (now - created_at).total_seconds() / 3600)


def _lifecycle_config(config: dict[str, Any]) -> dict[str, Any]:
    pending_config = config.get("pending_orders", {})
    return {
        "local_max_age_hours": float(pending_config.get("local_max_age_hours", 6) or 6),
        "exchange_max_age_days": float(pending_config.get("exchange_max_age_days", 1.5) or 1.5),
        "order_type": str(pending_config.get("order_type", "limit") or "limit"),
    }


def _review_config(config: dict[str, Any]) -> dict[str, Any]:
    pending_config = config.get("pending_orders", {})
    review = pending_config.get("review", {})
    strategy_config = config.get("strategy", {})
    return {
        "enabled": bool(review.get("enabled", True)),
        "min_confidence": float(
            review.get("min_confidence", max(0.0, float(strategy_config.get("min_confidence", 75)) - 5.0))
        ),
        "min_win_probability_pct": float(review.get("min_win_probability_pct", 0.0)),
        "max_confidence_drop": float(review.get("max_confidence_drop", 12.0)),
        "max_win_probability_drop_pct": float(review.get("max_win_probability_drop_pct", 8.0)),
        "min_risk_reward": float(review.get("min_risk_reward", strategy_config.get("min_risk_reward", 1.5))),
        "max_entry_drift_pct": float(review.get("max_entry_drift_pct", 1.2)),
        "use_market_guard_memory": bool(review.get("use_market_guard_memory", True)),
        "cancel_on_guard_avoid": bool(review.get("cancel_on_guard_avoid", True)),
        "cancel_on_opposite_guard_direction": bool(review.get("cancel_on_opposite_guard_direction", True)),
        "opposite_guard_min_risk_score": float(review.get("opposite_guard_min_risk_score", 4.0)),
    }


def _direction_opposes_side(direction: str, side: str) -> bool:
    clean_direction = direction.lower()
    clean_side = side.lower()
    return (clean_side == "long" and clean_direction == "down") or (
        clean_side == "short" and clean_direction == "up"
    )


def _guard_review_reasons(
    layers: dict[str, Any],
    side: str,
    review: dict[str, Any],
) -> list[str]:
    if not review["use_market_guard_memory"] or not layers:
        return []
    reasons: list[str] = []
    for label, key in (("5p", "layer_5m"), ("20p", "layer_20m")):
        layer = layers.get(key) or {}
        if not isinstance(layer, dict) or int(layer.get("sample_count") or 0) <= 0:
            continue
        action = str(layer.get("action") or "normal")
        risk_score = float(layer.get("risk_score") or 0)
        direction = str(layer.get("direction") or "")
        if review["cancel_on_guard_avoid"] and action == "avoid_new_entry":
            reasons.append(f"Market Guard {label} báo tránh vào lệnh mới (risk {risk_score:.2f})")
            continue
        if (
            review["cancel_on_opposite_guard_direction"]
            and action == "wait_confirmation"
            and risk_score >= review["opposite_guard_min_risk_score"]
            and _direction_opposes_side(direction, side)
        ):
            reasons.append(
                f"Market Guard {label} đang ngược hướng {side.upper()} "
                f"(hướng {direction}, risk {risk_score:.2f})"
            )
    return reasons


def _pending_review_reasons(
    config: dict[str, Any],
    record: dict[str, Any],
    candidate: TradeCandidate | None,
    check: RiskCheck | None,
    market_layers: dict[str, dict[str, Any]] | None,
    *,
    current_market_price: float | None = None,
) -> list[str]:
    review = _review_config(config)
    if not review["enabled"]:
        return list(check.reasons) if check and not check.passed else []

    if candidate is None:
        return ["LC không còn được xác nhận bởi lần scan mới"]

    reasons: list[str] = []
    if check and not check.passed:
        reasons.extend(check.reasons)

    confidence = float(candidate.confidence or 0)
    if confidence < review["min_confidence"]:
        reasons.append(f"Confidence {confidence:.2f} thấp hơn ngưỡng giữ LC {review['min_confidence']:.2f}")
    original_confidence = _optional_float(record.get("confidence"))
    if original_confidence is not None and original_confidence - confidence > review["max_confidence_drop"]:
        reasons.append(
            f"Confidence giảm {original_confidence - confidence:.2f} điểm so với lúc tạo LC"
        )

    win_probability = _optional_float(candidate.win_probability_pct)
    if win_probability is not None and win_probability < review["min_win_probability_pct"]:
        reasons.append(
            f"Tỉ lệ thắng {win_probability:.2f}% thấp hơn ngưỡng giữ LC "
            f"{review['min_win_probability_pct']:.2f}%"
        )
    original_win = _optional_float(record.get("win_probability_pct"))
    if (
        original_win is not None
        and win_probability is not None
        and original_win - win_probability > review["max_win_probability_drop_pct"]
    ):
        reasons.append(
            f"Tỉ lệ thắng giảm {original_win - win_probability:.2f}% so với lúc tạo LC"
        )

    risk_reward = float(candidate.risk_reward or 0)
    if risk_reward < review["min_risk_reward"]:
        reasons.append(f"RR {risk_reward:.2f} thấp hơn ngưỡng giữ LC {review['min_risk_reward']:.2f}")

    old_entry = _optional_float(record.get("entry"))
    if old_entry and old_entry > 0:
        comparison_price = _optional_float(current_market_price)
        if comparison_price is None:
            comparison_price = float(candidate.entry or 0)
        entry_drift_pct = abs(comparison_price - old_entry) / old_entry * 100
        if entry_drift_pct > review["max_entry_drift_pct"]:
            reasons.append(
                f"Giá thị trường hiện tại lệch {entry_drift_pct:.2f}% so với LC cũ "
                f"(ngưỡng {review['max_entry_drift_pct']:.2f}%)"
            )

    symbol_layers = (market_layers or {}).get(candidate.symbol) or {}
    reasons.extend(_guard_review_reasons(symbol_layers, candidate.side, review))
    return reasons


def revalidate_pending_orders_for_symbol(
    config: dict[str, Any],
    latest_candidate: TradeCandidate,
    *,
    reason_prefix: str = "New Mini setup was rejected by 5.5",
    market_layers: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Recheck old pending snapshots with fresh local signals, without recalling 5.5."""
    records = [
        record
        for record in list_pending_orders(config, status="OPEN", limit=200)
        if str(record.get("symbol") or "") == latest_candidate.symbol
    ]
    records.sort(
        key=lambda record: (
            int(bool(_record_mini_setup_id(record))),
            int(_record_is_mini_keep_setup(record)),
            str(record.get("updated_at") or ""),
            str(record.get("created_at") or ""),
            int(record.get("id") or 0),
        ),
        reverse=True,
    )
    invalid: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    warnings: list[str] = []
    cancellation_warnings: list[str] = []
    validation_config = mini_pending_risk_config(config)
    current_market_price = _candidate_current_market_price(latest_candidate)
    effective_market_layers, market_guard_checked, market_guard_warnings = _fresh_market_guard_layers_for_recheck(
        config,
        latest_candidate.symbol,
        market_layers,
    )
    warnings.extend(market_guard_warnings)
    keeper_selected = False
    for record in records:
        record_side = str(record.get("side") or "").lower()
        recheck_candidate = _candidate_for_local_pending_recheck(record, latest_candidate)
        if record_side != str(latest_candidate.side or "").lower():
            reasons = [
                f"Latest Mini signal is {str(latest_candidate.side or '').upper()}, opposite to the old setup"
            ]
        elif recheck_candidate is None:
            reasons = ["Old pending setup snapshot is missing or invalid"]
        else:
            check = evaluate_candidate(
                validation_config,
                recheck_candidate,
                check_active_trades=False,
                check_order_limits=False,
            )
            reasons = _pending_review_reasons(
                config,
                record,
                recheck_candidate,
                check,
                effective_market_layers,
                current_market_price=current_market_price,
            )
        expires_at = _parse_time(str(record.get("expires_at") or ""))
        if expires_at is not None and expires_at <= datetime.now(timezone.utc):
            reasons.append("Old pending setup has expired")
        if reasons:
            invalid.append({"record": record, "reasons": reasons})
        elif keeper_selected:
            invalid.append(
                {
                    "record": record,
                    "reasons": ["A newer valid pending setup for this symbol is already being kept"],
                }
            )
        else:
            keeper_selected = True
            kept.append(
                {
                    "id": record.get("id"),
                    "lc_id": record.get("journal_id") or record.get("id"),
                    "symbol": record.get("symbol"),
                    "side": record.get("side"),
                    "exchange_order_id": record.get("exchange_order_id"),
                    "setup_id": _record_mini_setup_id(record) or None,
                    "validation_source": "old_pending_snapshot_with_latest_market_context",
                    "current_market_price": current_market_price,
                    "market_guard_checked": market_guard_checked,
                }
            )

    canceled: list[dict[str, Any]] = []
    for item in invalid:
        reasons = item["reasons"]
        cancel_reason = f"{reason_prefix}; old setup is no longer valid: " + "; ".join(reasons[:3])
        cancel_result = _cancel_pending_records(config, [item["record"]], reason=cancel_reason)
        canceled.extend(cancel_result["canceled"])
        warnings.extend(cancel_result["warnings"])
        cancellation_warnings.extend(cancel_result["warnings"])
        if not cancel_result["ok"]:
            kept.append(
                {
                    "id": item["record"].get("id"),
                    "symbol": item["record"].get("symbol"),
                    "side": item["record"].get("side"),
                    "reason": "Cancellation failed; kept to avoid losing OKX/local synchronization",
                }
            )
    return {
        "method": "local_old_setup_snapshot_with_latest_market_context",
        "gpt55_called": False,
        "checked": len(records),
        "kept": kept,
        "canceled": canceled,
        "warnings": warnings,
        "cancellation_warnings": cancellation_warnings,
        "current_market_price": current_market_price,
        "market_guard_checked": market_guard_checked,
    }


def _missing_candidate_reason(record: dict[str, Any], candidates_by_symbol: dict[str, TradeCandidate]) -> str:
    symbol = str(record.get("symbol") or "")
    side = str(record.get("side") or "").upper()
    replacement = candidates_by_symbol.get(symbol)
    if replacement:
        return (
            f"Scan mới không còn ủng hộ LC {side}; "
            f"tín hiệu hiện tại nghiêng về {replacement.side.upper()}"
        )
    return "LC không còn nằm trong danh sách setup tốt của lần scan mới"


def _candidate_priority_key(candidate: TradeCandidate) -> tuple[float, float, float]:
    try:
        win_probability = float(candidate.win_probability_pct or 0)
    except (TypeError, ValueError):
        win_probability = 0.0
    try:
        confidence = float(candidate.confidence or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    try:
        volume_ratio = float((candidate.indicator_summary or {}).get("volume_ratio") or 0)
    except (TypeError, ValueError):
        volume_ratio = 0.0
    return (win_probability, confidence, volume_ratio)


def _rank_unique_candidates(candidates: list[TradeCandidate]) -> list[TradeCandidate]:
    ranked = sorted(candidates, key=_candidate_priority_key, reverse=True)
    output: list[TradeCandidate] = []
    seen: set[str] = set()
    for candidate in ranked:
        if candidate.symbol in seen:
            continue
        output.append(candidate)
        seen.add(candidate.symbol)
    return output


def _wait_slot_queue_meta(record: dict[str, Any]) -> dict[str, Any]:
    candidate = _candidate_from_record(record)
    if candidate is None:
        return {}
    meta = candidate.decision_metadata.get("wait_slot_queue") if isinstance(candidate.decision_metadata, dict) else {}
    return meta if isinstance(meta, dict) else {}


def _legacy_keep_monitor_meta(record: dict[str, Any]) -> dict[str, Any]:
    candidate = _candidate_from_record(record)
    if candidate is None:
        return {}
    meta = candidate.decision_metadata.get("setup_watchlist") if isinstance(candidate.decision_metadata, dict) else {}
    return meta if isinstance(meta, dict) else {}


def _record_keep_monitor_review(record: dict[str, Any]) -> dict[str, Any] | None:
    candidate = _candidate_from_record(record)
    if candidate is None:
        return None
    return candidate_okx_review(candidate, route="lc_okx_setup_review")


def _stored_review_blocks_okx_submission(candidate: TradeCandidate) -> dict[str, Any] | None:
    review = candidate_okx_review(candidate, route="lc_okx_setup_review")
    if review is None or okx_review_allows_okx_submission(review):
        return None
    return review


def _review_block_reason(review: dict[str, Any]) -> str:
    return str(review.get("reason") or review.get("decision") or "Stored 5.5 setup review does not allow OKX submission")


def _is_keep_monitor_review(record: dict[str, Any]) -> bool:
    review = _record_keep_monitor_review(record)
    return bool(review and not review.get("approved") and okx_review_is_keep_monitor(review))


def _is_wait_slot_reason(reason: str) -> bool:
    clean = str(reason or "").strip()
    if not clean:
        return False
    return clean.startswith(("Da het slot:", "Slot ", "Active trade limit reached:"))


def _is_real_wait_slot_record(record: dict[str, Any]) -> bool:
    meta = _wait_slot_queue_meta(record)
    reason = str(meta.get("reason") or "")
    return _is_wait_slot_reason(reason)


def _recheck_wait_slot_candidate(
    config: dict[str, Any],
    record: dict[str, Any],
    candidates_by_key: dict[tuple[str, str], TradeCandidate],
    candidates_by_symbol: dict[str, TradeCandidate],
) -> dict[str, Any]:
    symbol = str(record.get("symbol") or "")
    side = str(record.get("side") or "").lower()
    current_candidate = candidates_by_key.get((symbol, side))
    if current_candidate is None:
        replacement = candidates_by_symbol.get(symbol)
        if replacement is not None:
            return {
                "action": "cancel",
                "reason": (
                    f"{symbol}: setup hien tai da doi sang {replacement.side.upper()}, "
                    f"khong con phu hop voi LC mini {side.upper()}"
                ),
                "candidate": None,
                "comparison": {},
            }
        return {
            "action": "cancel",
            "reason": f"{symbol}: khong con setup hien tai de cap nhat khi tai kiem",
            "candidate": None,
            "comparison": {},
        }

    refreshed = current_candidate
    latest_scan = latest_lc_pipeline_mini_scan(config) or {}
    pool_symbols: list[str] = []
    for raw_symbol in list(latest_scan.get("pool_symbols") or latest_scan.get("approved_symbols") or []):
        clean = str(raw_symbol or "")
        if clean and clean not in pool_symbols:
            pool_symbols.append(clean)
    selected_symbols = [str(item) for item in list(latest_scan.get("selected_symbols") or []) if str(item)]
    queue_meta = _wait_slot_queue_meta(record) or _legacy_keep_monitor_meta(record)
    queued_slot_id = str(queue_meta.get("scan_slot_id") or "")
    latest_slot_id = str(latest_scan.get("slot_id") or "")

    comparison_candidates: list[TradeCandidate] = []
    for pool_symbol in pool_symbols:
        candidate = candidates_by_symbol.get(pool_symbol)
        if candidate is not None:
            comparison_candidates.append(candidate)
    if refreshed.symbol not in {candidate.symbol for candidate in comparison_candidates}:
        comparison_candidates.append(refreshed)
    ranked_pool = _rank_unique_candidates(comparison_candidates)
    current_rank = next(
        (index for index, candidate in enumerate(ranked_pool, 1) if candidate.symbol == refreshed.symbol),
        None,
    )
    top_candidate = ranked_pool[0] if ranked_pool else None
    comparison = {
        "latest_scan_slot_id": latest_slot_id or None,
        "queued_scan_slot_id": queued_slot_id or None,
        "pool_symbols": pool_symbols,
        "selected_symbols": selected_symbols,
        "top_symbol": top_candidate.symbol if top_candidate else None,
        "current_rank": current_rank,
        "ranked_symbols": [candidate.symbol for candidate in ranked_pool],
    }

    if pool_symbols and refreshed.symbol not in pool_symbols:
        return {
            "action": "cancel",
            "reason": f"{symbol}: khong con nam trong pool 4h moi nhat",
            "candidate": refreshed,
            "comparison": comparison,
        }

    if latest_slot_id and queued_slot_id and latest_slot_id != queued_slot_id and selected_symbols and refreshed.symbol not in selected_symbols:
        return {
            "action": "cancel",
            "reason": f"{symbol}: mini 4h moi nhat khong con chon cap nay",
            "candidate": refreshed,
            "comparison": comparison,
        }

    if top_candidate is not None and top_candidate.symbol != refreshed.symbol:
        return {
            "action": "keep",
            "reason": (
                f"{symbol}: du lieu moi cho thay {top_candidate.symbol} dang duoc uu tien hon "
                "trong pool 4h hien tai, tiep tuc cho tai kiem tiep theo"
            ),
            "candidate": refreshed,
            "comparison": comparison,
        }

    return {
        "action": "release",
        "reason": f"{symbol}: setup van phu hop sau tai kiem va dung dau pool 4h hien tai",
        "candidate": refreshed,
        "comparison": comparison,
    }


def maintain_pending_orders(
    config: dict[str, Any],
    candidates: list[TradeCandidate],
    *,
    allow_release: bool = True,
    market_layers: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    pending_config = config.get("pending_orders", {})
    if not pending_config.get("enabled", True):
        return {
            "enabled": False,
            "reviewed": 0,
            "kept": 0,
            "canceled": 0,
            "closed": 0,
            "converted": 0,
            "submitted": 0,
            "events": [],
            "warnings": [],
        }

    prune_pending_orders(config)
    open_records = prioritize_pending_records(list_pending_orders(config, status="OPEN", limit=200))
    if not open_records:
        return {
            "enabled": True,
            "reviewed": 0,
            "kept": 0,
            "canceled": 0,
            "closed": 0,
            "converted": 0,
            "submitted": 0,
            "events": [],
            "warnings": [],
        }

    warnings: list[str] = []
    events: list[dict[str, Any]] = []
    open_exchange_order_ids: set[str] | None = None
    open_position_symbols: set[str] = set()
    active_symbols: set[str] = set()
    active_count: int | None = None
    position_count: int | None = None
    exchange = None
    open_orders: list[dict[str, Any]] = []
    if config.get("mode") != "dry_run":
        try:
            exchange = create_exchange(config, authenticated=True)
            exchange.load_markets()
            open_orders = exchange.fetch_open_orders()
            open_exchange_order_ids = {order_id for order_id in (_order_id(order) for order in open_orders) if order_id}
            positions = exchange.fetch_positions()
            open_position_symbols = _open_position_symbols(positions)
            active_symbols = set(open_position_symbols) | _active_order_symbols(open_orders)
            position_count = _open_position_count(positions)
            active_count = position_count + len(open_orders)
        except Exception as exc:
            warnings.append(f"Pending order sync skipped: {exc}")

    now = datetime.now(timezone.utc)
    lifecycle = _lifecycle_config(config)
    max_active = int(config.get("risk", {}).get("max_active_trades", 1))
    candidates_by_key = {_candidate_key(candidate): candidate for candidate in candidates}
    candidates_by_symbol = {candidate.symbol: candidate for candidate in candidates}
    reviewed = 0
    kept = 0
    canceled = 0
    closed = 0
    converted = 0
    submitted = 0
    open_orders_by_client_id = {
        _order_client_id(order): order
        for order in open_orders
        if isinstance(order, dict) and _order_client_id(order)
    }

    for record in open_records:
        reviewed += 1
        local_id = int(record["id"])
        lc_id = int(record.get("journal_id") or local_id)
        exchange_order_id = str(record.get("exchange_order_id") or "")
        symbol = str(record.get("symbol") or "")
        if not exchange_order_id and exchange is not None:
            stored_candidate = _candidate_from_record(record)
            expected_client_id = (
                candidate_client_order_id(stored_candidate, entry_type="mini_lc_okx")
                if stored_candidate is not None
                else None
            )
            matched_order = open_orders_by_client_id.get(str(expected_client_id or ""))
            matched_order_id = _order_id(matched_order) if matched_order else ""
            if stored_candidate is not None and matched_order_id:
                set_pending_order_exchange_order(
                    config,
                    local_id,
                    stored_candidate,
                    matched_order_id,
                    max_age_days=float(lifecycle["exchange_max_age_days"]),
                )
                exchange_order_id = matched_order_id
                record = {
                    **record,
                    "status": "LC_OKX",
                    "exchange_order_id": matched_order_id,
                }
        if open_exchange_order_ids is not None and exchange_order_id and exchange_order_id not in open_exchange_order_ids:
            side = str(record.get("side") or "")
            if symbol in open_position_symbols:
                vt_id = next_global_counter(config, "VT")
                reason = f"LC #{lc_id} filled and converted to VT #{vt_id}"
                close_pending_order(config, local_id, "FILLED", reason)
                _close_latest_lc_trade_execution(
                    config,
                    symbol=symbol,
                    side=side,
                    status="FILLED",
                    reason=reason,
                )
                _record_vt_from_pending(config, record, vt_id=vt_id, lc_id=lc_id)
                append_event(
                    config,
                    {
                        "mode": config.get("mode", "dry_run"),
                        "submitted": True,
                        "order_id": exchange_order_id,
                        "entry_type": "pending_filled",
                        "order_type": "limit",
                        "journal_type": "VT",
                        "journal_id": vt_id,
                        "linked_journal_id": lc_id,
                        "symbol": symbol,
                        "side": side,
                        "entry": record.get("entry"),
                        "stop_loss": record.get("stop_loss"),
                        "take_profit": record.get("take_profit"),
                        "quantity": record.get("quantity"),
                        "notional_usdt": record.get("order_usdt"),
                        "leverage": config.get("exchange", {}).get("leverage", 1),
                    },
                )
                events.append(
                    {
                        "type": "pending_converted",
                        "source": "lc_okx_filled",
                        "from_status": str(record.get("status") or "LC_OKX"),
                        "lc_id": lc_id,
                        "vt_id": vt_id,
                        "symbol": symbol,
                        "side": side,
                        "exchange_order_id": exchange_order_id,
                    }
                )
                converted += 1
            else:
                reason = "Exchange order is no longer open"
                close_pending_order(config, local_id, "CANCELED", reason)
                _close_latest_lc_trade_execution(
                    config,
                    symbol=symbol,
                    side=str(record.get("side") or ""),
                    status="CANCELED",
                    reason=reason,
                )
                events.append(
                    {
                        "type": "pending_canceled",
                        "lc_id": lc_id,
                        "symbol": symbol,
                        "side": str(record.get("side") or ""),
                        "reason": reason,
                    }
                )
                canceled += 1
            continue

        expires_at = _parse_time(str(record.get("expires_at") or ""))
        record_status = str(record.get("status") or "").upper()
        candidate = candidates_by_key.get(_record_key(record))

        if record_status == "WATCHLIST":
            reason = "Legacy WATCHLIST disabled; Mini setups must be reviewed by GPT-5.5 once and stored on OKX only"
            close_pending_order(config, local_id, "CANCELED", reason)
            events.append(
                {
                    "type": "pending_canceled",
                    "source": "legacy_watchlist_disabled",
                    "lc_id": lc_id,
                    "symbol": symbol,
                    "side": str(record.get("side") or ""),
                    "reason": reason,
                }
            )
            canceled += 1
            continue

            legacy_candidate = _candidate_from_record(record)
            if legacy_candidate is None:
                reason = "Legacy keep-monitor payload is invalid"
                close_pending_order(config, local_id, "CANCELED", reason)
                events.append(
                    {
                        "type": "pending_canceled",
                        "source": "legacy_keep_monitor",
                        "lc_id": lc_id,
                        "symbol": symbol,
                        "side": str(record.get("side") or ""),
                        "reason": reason,
                    }
                )
                canceled += 1
                continue

            if config.get("mode") != "dry_run" and (exchange is None or position_count is None):
                refresh_pending_order(
                    config,
                    local_id,
                    legacy_candidate,
                    status="OPEN",
                    max_age_hours=float(lifecycle["local_max_age_hours"]),
                )
                warnings.append(f"{symbol}: legacy keep-monitor moved to OPEN because OKX sync is unavailable")
                kept += 1
                continue

            if config.get("mode") != "dry_run" and position_count is not None and position_count >= max_active:
                refresh_pending_order(
                    config,
                    local_id,
                    legacy_candidate,
                    status="WAIT_SLOT",
                    max_age_hours=float(lifecycle["local_max_age_hours"]),
                )
                events.append(
                    {
                        "type": "pending_kept",
                        "source": "legacy_keep_monitor_slot_full",
                        "status": "WAIT_SLOT",
                        "lc_id": lc_id,
                        "symbol": symbol,
                        "side": str(record.get("side") or ""),
                        "reason": f"Slot dang day {position_count}/{max_active}; legacy keep-monitor moved to WAIT_SLOT",
                    }
                )
                kept += 1
                continue

            recheck = _recheck_wait_slot_candidate(config, record, candidates_by_key, candidates_by_symbol)
            refreshed_candidate = recheck.get("candidate")
            comparison = recheck.get("comparison") or {}
            if refreshed_candidate is None:
                reason = str(recheck.get("reason") or "Legacy keep-monitor setup is no longer valid")
                close_pending_order(config, local_id, "CANCELED", reason)
                events.append(
                    {
                        "type": "pending_canceled",
                        "source": "legacy_keep_monitor_recheck",
                        "lc_id": lc_id,
                        "symbol": symbol,
                        "side": str(record.get("side") or ""),
                        "reason": reason,
                        "comparison": comparison,
                    }
                )
                canceled += 1
                continue

            refreshed_candidate = _candidate_with_record_metadata(refreshed_candidate, record)
            refresh_check = evaluate_candidate(
                config,
                refreshed_candidate,
                check_active_trades=False,
                check_order_limits=False,
            )
            refresh_reasons = _pending_review_reasons(config, record, refreshed_candidate, refresh_check, market_layers)
            if refresh_reasons:
                reason = "; ".join(refresh_reasons[:3])
                close_pending_order(config, local_id, "CANCELED", reason)
                events.append(
                    {
                        "type": "pending_canceled",
                        "source": "legacy_keep_monitor_recheck",
                        "lc_id": lc_id,
                        "symbol": symbol,
                        "side": str(record.get("side") or ""),
                        "reason": reason,
                        "comparison": comparison,
                    }
                )
                canceled += 1
                continue

            if str(recheck.get("action") or "") == "keep":
                refresh_pending_order(
                    config,
                    local_id,
                    refreshed_candidate,
                    status="OPEN",
                    max_age_hours=float(lifecycle["local_max_age_hours"]),
                )
                events.append(
                    {
                        "type": "pending_kept",
                        "source": "legacy_keep_monitor_recheck",
                        "status": "OPEN",
                        "lc_id": lc_id,
                        "symbol": symbol,
                        "side": str(record.get("side") or ""),
                        "reason": str(recheck.get("reason") or "Legacy keep-monitor moved to OPEN"),
                        "comparison": comparison,
                    }
                )
                kept += 1
                continue

            if config.get("mode") == "dry_run":
                refresh_pending_order(
                    config,
                    local_id,
                    refreshed_candidate,
                    status="OPEN",
                    max_age_hours=float(lifecycle["local_max_age_hours"]),
                )
                kept += 1
                continue

            submit_check = evaluate_candidate(
                config,
                refreshed_candidate,
                check_active_trades=False,
                check_order_limits=True,
            )
            if not submit_check.passed:
                target_status = "WAIT_SLOT" if any(_is_wait_slot_reason(reason) for reason in submit_check.reasons) else "OPEN"
                refresh_pending_order(
                    config,
                    local_id,
                    refreshed_candidate,
                    status=target_status,
                    max_age_hours=float(lifecycle["local_max_age_hours"]),
                )
                warnings.append(
                    f"{symbol}: legacy keep-monitor kept as {target_status} because submit risk check failed: "
                    + "; ".join(submit_check.reasons[:3])
                )
                kept += 1
                continue

            reviewed_candidate = refreshed_candidate
            blocked_review = _stored_review_blocks_okx_submission(reviewed_candidate)
            if blocked_review is not None:
                reason = _review_block_reason(blocked_review)
                close_pending_order(config, local_id, "CANCELED", reason)
                events.append(
                    {
                        "type": "pending_canceled",
                        "source": "legacy_keep_monitor_stored_review",
                        "lc_id": lc_id,
                        "symbol": symbol,
                        "side": str(record.get("side") or ""),
                        "reason": reason,
                        "comparison": comparison,
                    }
                )
                canceled += 1
                continue

            execution = execute_candidate(
                config,
                reviewed_candidate,
                order_type_override=str(lifecycle["order_type"]),
                entry_type="legacy_keep_monitor_okx",
                journal_type="LC",
                journal_id=lc_id,
            )
            if not execution.submitted or not execution.order_id:
                refresh_pending_order(
                    config,
                    local_id,
                    reviewed_candidate,
                    status="OPEN",
                    max_age_hours=float(lifecycle["local_max_age_hours"]),
                )
                warnings.append(f"{symbol}: legacy keep-monitor submit to OKX failed: {execution.message}")
                kept += 1
                continue

            set_pending_order_exchange_order(
                config,
                local_id,
                reviewed_candidate,
                execution.order_id,
                max_age_days=float(lifecycle["exchange_max_age_days"]),
            )
            events.append(
                {
                    "type": "pending_submitted",
                    "source": "legacy_keep_monitor_release",
                    "status": "LC_OKX",
                    "lc_id": lc_id,
                    "symbol": symbol,
                    "side": str(record.get("side") or ""),
                    "exchange_order_id": execution.order_id,
                    "reason": str(recheck.get("reason") or "Legacy keep-monitor released to LC_OKX"),
                    "comparison": comparison,
                }
            )
            submitted += 1
            kept += 1
            active_symbols.add(symbol)
            continue

        if record_status == "WAIT_SLOT":
            reason = "Legacy WAIT_SLOT disabled; Mini setups must be reviewed by GPT-5.5 once and stored on OKX only"
            close_pending_order(config, local_id, "CANCELED", reason)
            events.append(
                {
                    "type": "pending_canceled",
                    "source": "legacy_wait_slot_disabled",
                    "lc_id": lc_id,
                    "symbol": symbol,
                    "side": str(record.get("side") or ""),
                    "reason": reason,
                }
            )
            canceled += 1
            continue

            wait_slot_candidate = _candidate_from_record(record)
            if wait_slot_candidate is None:
                reason = "WAIT_SLOT khong con payload hop le"
                close_pending_order(config, local_id, "CANCELED", reason)
                events.append(
                    {
                        "type": "pending_canceled",
                        "source": "mini_wait_slot",
                        "lc_id": lc_id,
                        "symbol": symbol,
                        "side": str(record.get("side") or ""),
                        "reason": reason,
                    }
                )
                canceled += 1
                continue

            if config.get("mode") != "dry_run" and (exchange is None or position_count is None):
                warnings.append(f"{symbol}: WAIT_SLOT kept because OKX sync is unavailable")
                refresh_pending_order(
                    config,
                    local_id,
                    wait_slot_candidate,
                    status="WAIT_SLOT",
                    max_age_hours=float(lifecycle["local_max_age_hours"]),
                )
                kept += 1
                continue

            if config.get("mode") != "dry_run" and position_count is not None and position_count >= max_active:
                refresh_pending_order(
                    config,
                    local_id,
                    wait_slot_candidate,
                    status="WAIT_SLOT",
                    max_age_hours=float(lifecycle["local_max_age_hours"]),
                )
                kept += 1
                continue

            recheck = _recheck_wait_slot_candidate(config, record, candidates_by_key, candidates_by_symbol)
            refreshed_candidate = recheck.get("candidate")
            comparison = recheck.get("comparison") or {}
            if refreshed_candidate is None:
                reason = str(recheck.get("reason") or "WAIT_SLOT khong con du dieu kien sau tai kiem")
                close_pending_order(config, local_id, "CANCELED", reason)
                events.append(
                    {
                        "type": "pending_canceled",
                        "source": "mini_wait_slot",
                        "lc_id": lc_id,
                        "symbol": symbol,
                        "side": str(record.get("side") or ""),
                        "reason": reason,
                        "comparison": comparison,
                    }
                )
                canceled += 1
                continue

            refreshed_candidate = _candidate_with_record_metadata(refreshed_candidate, record)
            refresh_check = evaluate_candidate(
                config,
                refreshed_candidate,
                check_active_trades=False,
                check_order_limits=False,
            )
            refresh_reasons = _pending_review_reasons(config, record, refreshed_candidate, refresh_check, market_layers)
            if refresh_reasons:
                reason = "; ".join(refresh_reasons[:3])
                close_pending_order(config, local_id, "CANCELED", reason)
                events.append(
                    {
                        "type": "pending_canceled",
                        "source": "mini_wait_slot_recheck",
                        "lc_id": lc_id,
                        "symbol": symbol,
                        "side": str(record.get("side") or ""),
                        "reason": reason,
                        "comparison": comparison,
                    }
                )
                canceled += 1
                continue

            if str(recheck.get("action") or "") == "keep":
                refresh_pending_order(
                    config,
                    local_id,
                    refreshed_candidate,
                    status="WAIT_SLOT",
                    max_age_hours=float(lifecycle["local_max_age_hours"]),
                )
                events.append(
                    {
                        "type": "pending_wait_slot_kept",
                        "source": "mini_wait_slot_recheck",
                        "lc_id": lc_id,
                        "symbol": symbol,
                        "side": str(record.get("side") or ""),
                        "reason": str(recheck.get("reason") or "WAIT_SLOT tiep tuc cho tai kiem"),
                        "comparison": comparison,
                    }
                )
                kept += 1
                continue

            if config.get("mode") == "dry_run":
                refresh_pending_order(
                    config,
                    local_id,
                    refreshed_candidate,
                    status="OPEN",
                    max_age_hours=float(lifecycle["local_max_age_hours"]),
                )
                events.append(
                    {
                        "type": "pending_wait_slot_promoted",
                        "source": "mini_wait_slot_recheck",
                        "status": "OPEN",
                        "lc_id": lc_id,
                        "symbol": symbol,
                        "side": str(record.get("side") or ""),
                        "reason": str(recheck.get("reason") or "WAIT_SLOT promoted to OPEN after recheck"),
                        "comparison": comparison,
                    }
                )
                kept += 1
                continue

            submit_check = evaluate_candidate(
                config,
                refreshed_candidate,
                check_active_trades=False,
                check_order_limits=True,
            )
            if not submit_check.passed:
                refresh_pending_order(
                    config,
                    local_id,
                    refreshed_candidate,
                    status="WAIT_SLOT",
                    max_age_hours=float(lifecycle["local_max_age_hours"]),
                )
                warnings.append(
                    f"{symbol}: WAIT_SLOT kept because recheck release gate failed: "
                    + "; ".join(submit_check.reasons[:3])
                )
                kept += 1
                continue

            reviewed_candidate = refreshed_candidate
            blocked_review = _stored_review_blocks_okx_submission(reviewed_candidate)
            if blocked_review is not None:
                reason = _review_block_reason(blocked_review)
                close_pending_order(config, local_id, "CANCELED", reason)
                events.append(
                    {
                        "type": "pending_canceled",
                        "source": "mini_wait_slot_stored_review",
                        "lc_id": lc_id,
                        "symbol": symbol,
                        "side": str(record.get("side") or ""),
                        "reason": reason,
                        "rejection_policy": okx_review_rejection_policy(blocked_review),
                        "comparison": comparison,
                    }
                )
                canceled += 1
                continue

            execution = execute_candidate(
                config,
                reviewed_candidate,
                order_type_override=str(lifecycle["order_type"]),
                entry_type="mini_wait_slot_okx",
                journal_type="LC",
                journal_id=lc_id,
            )
            if not execution.submitted or not execution.order_id:
                refresh_pending_order(
                    config,
                    local_id,
                    reviewed_candidate,
                    status="WAIT_SLOT",
                    max_age_hours=float(lifecycle["local_max_age_hours"]),
                )
                warnings.append(f"{symbol}: WAIT_SLOT submit to OKX failed: {execution.message}")
                kept += 1
                continue

            set_pending_order_exchange_order(
                config,
                local_id,
                reviewed_candidate,
                execution.order_id,
                max_age_days=float(lifecycle["exchange_max_age_days"]),
            )
            events.append(
                {
                    "type": "pending_submitted",
                    "source": "mini_wait_slot_release",
                    "status": "LC_OKX",
                    "lc_id": lc_id,
                    "symbol": symbol,
                    "side": str(record.get("side") or ""),
                    "exchange_order_id": execution.order_id,
                    "reason": str(recheck.get("reason") or "WAIT_SLOT released to LC_OKX"),
                    "comparison": comparison,
                }
            )
            submitted += 1
            kept += 1
            active_symbols.add(symbol)
            continue

        if exchange_order_id and _record_is_mini_keep_setup(record):
            cancel_reason = ""
            if expires_at and expires_at <= now:
                cancel_reason = f"LC OKX expired after {lifecycle['exchange_max_age_days']:.1f} day(s)"
            elif symbol in open_position_symbols:
                cancel_reason = f"Active OKX position already exists for {symbol}"
            if cancel_reason:
                if exchange is None and config.get("mode") != "dry_run":
                    warnings.append(f"{symbol}: 5.5-kept pending order could not be canceled because OKX sync is unavailable")
                    kept += 1
                    continue
                if exchange is not None:
                    try:
                        exchange.cancel_order(exchange_order_id, symbol)
                    except Exception as exc:
                        warnings.append(f"{symbol}: 5.5-kept pending cancel failed: {exc}")
                        kept += 1
                        continue
                close_pending_order(config, local_id, "CANCELED", cancel_reason)
                _close_latest_lc_trade_execution(
                    config,
                    symbol=symbol,
                    side=str(record.get("side") or ""),
                    status="CANCELED",
                    reason=cancel_reason,
                )
                events.append(
                    {
                        "type": "pending_canceled",
                        "source": "mini_5_5_keep_setup_lifecycle",
                        "lc_id": lc_id,
                        "symbol": symbol,
                        "side": str(record.get("side") or ""),
                        "reason": cancel_reason,
                    }
                )
                canceled += 1
                continue
            # KEEP_SETUP is an explicit OKX pending instruction. Routine scans
            # must not cancel or convert it; replacement/recheck is handled by
            # the next same-symbol Mini setup in engine.py.
            kept += 1
            continue

        if config.get("mode") != "dry_run" and record_status == "OPEN" and not exchange_order_id and not _record_mini_setup_id(record):
            reason = "Legacy local pending disabled; Mini setups must be reviewed by GPT-5.5 once and stored on OKX only"
            close_pending_order(config, local_id, "CANCELED", reason)
            events.append(
                {
                    "type": "pending_canceled",
                    "source": "legacy_local_pending_disabled",
                    "lc_id": lc_id,
                    "symbol": symbol,
                    "side": str(record.get("side") or ""),
                    "reason": reason,
                }
            )
            canceled += 1
            continue

        validation_config = mini_pending_risk_config(config) if _record_mini_setup_id(record) else config
        check = (
            evaluate_candidate(
                validation_config,
                candidate,
                check_active_trades=False,
                check_order_limits=False,
            )
            if candidate
            else None
        )
        review_reasons = _pending_review_reasons(config, record, candidate, check, market_layers)
        if candidate and not review_reasons:
            candidate = _candidate_with_record_metadata(candidate, record)
            _fill_missing_quantity(candidate, record)
            local_age_hours = _record_age_hours(record, now)

            if exchange_order_id:
                if expires_at and expires_at <= now:
                    if exchange is None:
                        warnings.append(f"{symbol}: expired OKX pending order was not canceled because OKX sync is unavailable")
                        kept += 1
                        continue
                    reason = f"LC OKX expired after {lifecycle['exchange_max_age_days']:.1f} day(s)"
                    try:
                        exchange.cancel_order(exchange_order_id, symbol)
                    except Exception as exc:
                        warnings.append(f"{symbol}: expired pending cancel failed: {exc}")
                        kept += 1
                        continue
                    close_pending_order(config, local_id, "CANCELED", reason)
                    _close_latest_lc_trade_execution(
                        config,
                        symbol=symbol,
                        side=str(record.get("side") or ""),
                        status="CANCELED",
                        reason=reason,
                    )
                    events.append(
                        {
                            "type": "pending_canceled",
                            "lc_id": lc_id,
                            "symbol": symbol,
                            "side": str(record.get("side") or ""),
                            "reason": reason,
                        }
                    )
                    canceled += 1
                    continue

                if symbol in open_position_symbols:
                    if exchange is None:
                        warnings.append(f"{symbol}: OKX pending order kept because OKX sync is unavailable")
                        kept += 1
                        continue
                    reason = f"Active OKX position already exists for {symbol}"
                    try:
                        exchange.cancel_order(exchange_order_id, symbol)
                    except Exception as exc:
                        warnings.append(f"{symbol}: pending cancel failed: {exc}")
                        kept += 1
                        continue
                    close_pending_order(config, local_id, "CANCELED", reason)
                    _close_latest_lc_trade_execution(
                        config,
                        symbol=symbol,
                        side=str(record.get("side") or ""),
                        status="CANCELED",
                        reason=reason,
                    )
                    events.append(
                        {
                            "type": "pending_canceled",
                            "lc_id": lc_id,
                            "symbol": symbol,
                            "side": str(record.get("side") or ""),
                            "reason": reason,
                        }
                    )
                    canceled += 1
                    continue

                if position_count is not None and position_count > max_active and exchange is not None:
                    reason = f"Open position limit exceeded: {position_count}/{max_active}"
                    try:
                        exchange.cancel_order(exchange_order_id, symbol)
                    except Exception as exc:
                        warnings.append(f"{symbol}: pending cancel failed: {exc}")
                        kept += 1
                        continue
                    close_pending_order(config, local_id, "CANCELED", reason)
                    _close_latest_lc_trade_execution(
                        config,
                        symbol=symbol,
                        side=str(record.get("side") or ""),
                        status="CANCELED",
                        reason=reason,
                    )
                    events.append(
                        {
                            "type": "pending_canceled",
                            "lc_id": lc_id,
                            "symbol": symbol,
                            "side": str(record.get("side") or ""),
                            "reason": reason,
                        }
                    )
                    canceled += 1
                    continue

                if (
                    allow_release
                    and config.get("mode") != "dry_run"
                    and exchange is not None
                    and position_count is not None
                    and position_count < max_active
                ):
                    release_check = evaluate_candidate(
                        config,
                        candidate,
                        check_active_trades=False,
                        check_order_limits=True,
                    )
                    if not release_check.passed:
                        warnings.append(
                            f"{symbol}: OKX pending order kept because direct release risk check failed: "
                            + "; ".join(release_check.reasons[:3])
                        )
                        kept += 1
                        continue
                    stored_review = candidate_okx_review(candidate, route="lc_okx_setup_review")
                    if stored_review is None:
                        record_candidate = _candidate_from_record(record)
                        if record_candidate is not None:
                            stored_review = candidate_okx_review(record_candidate, route="lc_okx_setup_review")
                    if stored_review is None:
                        warnings.append(f"{symbol}: LC_OKX kept because no initial 5.5 setup review is stored")
                        kept += 1
                        continue
                    if not okx_review_requests_market_entry(stored_review):
                        events.append(
                            {
                                "type": "pending_kept",
                                "source": "lc_okx_stored_setup",
                                "lc_id": lc_id,
                                "symbol": symbol,
                                "side": str(record.get("side") or ""),
                                "reason": stored_review.get("reason") or stored_review.get("decision"),
                            }
                        )
                        kept += 1
                        continue
                    final_check = evaluate_candidate(
                        config,
                        candidate,
                        check_active_trades=False,
                        check_order_limits=True,
                    )
                    if not final_check.passed:
                        warnings.append(
                            f"{symbol}: final validator kept LC_OKX after OKX AI approval: "
                            + "; ".join(final_check.reasons[:3])
                        )
                        kept += 1
                        continue
                    try:
                        exchange.cancel_order(exchange_order_id, symbol)
                    except Exception as exc:
                        warnings.append(f"{symbol}: OKX pending cancel before direct release failed: {exc}")
                        kept += 1
                        continue

                    vt_id = next_global_counter(config, "VT")
                    execution = execute_candidate(
                        config,
                        candidate,
                        order_type_override="market",
                        entry_type="pending_released",
                        journal_type="VT",
                        journal_id=vt_id,
                        linked_journal_id=lc_id,
                    )
                    if not execution.submitted:
                        reason = f"OKX LC was canceled for direct entry, but market entry failed: {execution.message}"
                        close_pending_order(config, local_id, "CANCELED", reason)
                        _close_latest_lc_trade_execution(
                            config,
                            symbol=symbol,
                            side=str(record.get("side") or ""),
                            status="CANCELED",
                            reason=reason,
                        )
                        events.append(
                            {
                                "type": "pending_canceled",
                                "lc_id": lc_id,
                                "symbol": symbol,
                                "side": str(record.get("side") or ""),
                                "reason": reason,
                            }
                        )
                        canceled += 1
                        continue

                    reason = f"LC #{lc_id} canceled on OKX and converted to VT #{vt_id}"
                    close_pending_order(config, local_id, "FILLED", reason)
                    _close_latest_lc_trade_execution(
                        config,
                        symbol=symbol,
                        side=str(record.get("side") or ""),
                        status="FILLED",
                        reason=reason,
                    )
                    events.append(
                        {
                            "type": "pending_converted",
                            "source": "lc_okx_released",
                            "from_status": str(record.get("status") or "LC_OKX"),
                            "lc_id": lc_id,
                            "vt_id": vt_id,
                            "symbol": symbol,
                            "side": str(record.get("side") or ""),
                            "exchange_order_id": execution.order_id,
                        }
                    )
                    converted += 1
                    if active_count is not None:
                        active_count += 1
                    if position_count is not None:
                        position_count += 1
                    active_symbols.add(symbol)
                    continue

                if not expires_at:
                    refresh_pending_order(
                        config,
                        local_id,
                        candidate,
                        max_age_days=float(lifecycle["exchange_max_age_days"]),
                    )
                kept += 1
                continue

            if not allow_release:
                kept += 1
                continue
            if config.get("mode") == "dry_run":
                kept += 1
                continue
            if exchange is None or position_count is None:
                warnings.append(f"{symbol}: local pending order kept because OKX sync is unavailable")
                kept += 1
                continue
            if symbol in active_symbols:
                reason = f"Active OKX position/order already exists for {symbol}"
                close_pending_order(config, local_id, "CANCELED", reason)
                _close_latest_lc_trade_execution(
                    config,
                    symbol=symbol,
                    side=str(record.get("side") or ""),
                    status="CANCELED",
                    reason=reason,
                )
                events.append(
                    {
                        "type": "pending_canceled",
                        "lc_id": lc_id,
                        "symbol": symbol,
                        "side": str(record.get("side") or ""),
                        "reason": reason,
                    }
                )
                canceled += 1
                continue

            if position_count >= max_active:
                if local_age_hours >= float(lifecycle["local_max_age_hours"]):
                    submit_check = evaluate_candidate(
                        config,
                        candidate,
                        check_active_trades=False,
                        check_order_limits=True,
                    )
                    if not submit_check.passed:
                        warnings.append(
                            f"{symbol}: local pending order kept because OKX submit risk check failed: "
                            + "; ".join(submit_check.reasons[:3])
                        )
                        kept += 1
                        continue
                    blocked_review = _stored_review_blocks_okx_submission(candidate)
                    if blocked_review is not None:
                        reason = _review_block_reason(blocked_review)
                        close_pending_order(config, local_id, "CANCELED", reason)
                        events.append(
                            {
                                "type": "pending_canceled",
                                "source": "local_pending_stored_review",
                                "lc_id": lc_id,
                                "symbol": symbol,
                                "side": str(record.get("side") or ""),
                                "reason": reason,
                                "rejection_policy": okx_review_rejection_policy(blocked_review),
                            }
                        )
                        canceled += 1
                        continue
                    stored_review = candidate_okx_review(candidate, route="lc_okx_setup_review")
                    if stored_review is None:
                        warnings.append(f"{symbol}: local pending kept because no initial 5.5 setup review is stored")
                        kept += 1
                        continue
                    reviewed_candidate = candidate
                    final_check = evaluate_candidate(
                        config,
                        reviewed_candidate,
                        check_active_trades=False,
                        check_order_limits=True,
                    )
                    if not final_check.passed:
                        warnings.append(
                            f"{symbol}: final validator kept local LC before OKX submit: "
                            + "; ".join(final_check.reasons[:3])
                        )
                        kept += 1
                        continue
                    execution = execute_candidate(
                        config,
                        reviewed_candidate,
                        order_type_override=str(lifecycle["order_type"]),
                        entry_type="pending_okx",
                        journal_type="LC",
                        journal_id=lc_id,
                    )
                    if not execution.submitted or not execution.order_id:
                        warnings.append(f"{symbol}: local pending submit to OKX failed: {execution.message}")
                        kept += 1
                        continue
                    set_pending_order_exchange_order(
                        config,
                        local_id,
                        reviewed_candidate,
                        execution.order_id,
                        max_age_days=float(lifecycle["exchange_max_age_days"]),
                    )
                    events.append(
                        {
                            "type": "pending_submitted",
                            "status": "LC_OKX",
                            "lc_id": lc_id,
                            "symbol": symbol,
                            "side": str(record.get("side") or ""),
                            "exchange_order_id": execution.order_id,
                            "local_age_hours": round(local_age_hours, 2),
                            "expires_in_days": float(lifecycle["exchange_max_age_days"]),
                        }
                    )
                    submitted += 1
                    kept += 1
                    continue
                if not expires_at or expires_at <= now:
                    refresh_pending_order(
                        config,
                        local_id,
                        candidate,
                        max_age_hours=float(lifecycle["local_max_age_hours"]),
                    )
                kept += 1
                continue

            if position_count < max_active:
                release_check = evaluate_candidate(
                    config,
                    candidate,
                    check_active_trades=False,
                    check_order_limits=True,
                )
                if not release_check.passed:
                    warnings.append(
                        f"{symbol}: local pending order kept because release risk check failed: "
                        + "; ".join(release_check.reasons[:3])
                    )
                    kept += 1
                    continue
                stored_review = candidate_okx_review(candidate, route="lc_okx_setup_review")
                if stored_review is None:
                    warnings.append(f"{symbol}: local pending kept because no initial 5.5 setup review is stored")
                    kept += 1
                    continue
                if okx_review_allows_okx_submission(stored_review):
                    final_check = evaluate_candidate(
                        config,
                        candidate,
                        check_active_trades=False,
                        check_order_limits=True,
                    )
                    if not final_check.passed:
                        warnings.append(
                            f"{symbol}: final validator kept local LC before OKX submit: "
                            + "; ".join(final_check.reasons[:3])
                        )
                        kept += 1
                        continue
                    execution = execute_candidate(
                        config,
                        candidate,
                        order_type_override=str(lifecycle["order_type"]),
                        entry_type="pending_okx",
                        journal_type="LC",
                        journal_id=lc_id,
                    )
                    if not execution.submitted or not execution.order_id:
                        warnings.append(f"{symbol}: local pending submit to OKX failed: {execution.message}")
                        kept += 1
                        continue
                    set_pending_order_exchange_order(
                        config,
                        local_id,
                        candidate,
                        execution.order_id,
                        max_age_days=float(lifecycle["exchange_max_age_days"]),
                    )
                    events.append(
                        {
                            "type": "pending_submitted",
                            "source": "local_pending_okx",
                            "status": "LC_OKX",
                            "lc_id": lc_id,
                            "symbol": symbol,
                            "side": str(record.get("side") or ""),
                            "exchange_order_id": execution.order_id,
                        }
                    )
                    submitted += 1
                    kept += 1
                    active_symbols.add(symbol)
                    continue
                if not okx_review_requests_market_entry(stored_review):
                    reason = _review_block_reason(stored_review)
                    close_pending_order(config, local_id, "CANCELED", reason)
                    events.append(
                        {
                            "type": "pending_canceled",
                            "source": "local_pending_stored_review",
                            "lc_id": lc_id,
                            "symbol": symbol,
                            "side": str(record.get("side") or ""),
                            "reason": reason,
                        }
                    )
                    canceled += 1
                    continue
                final_check = evaluate_candidate(
                    config,
                    candidate,
                    check_active_trades=False,
                    check_order_limits=True,
                )
                if not final_check.passed:
                    warnings.append(
                        f"{symbol}: final validator kept local LC before VT: "
                        + "; ".join(final_check.reasons[:3])
                    )
                    kept += 1
                    continue

                vt_id = next_global_counter(config, "VT")
                execution = execute_candidate(
                    config,
                    candidate,
                    order_type_override="market",
                    entry_type="pending_released",
                    journal_type="VT",
                    journal_id=vt_id,
                    linked_journal_id=lc_id,
                )
                if not execution.submitted:
                    warnings.append(f"{symbol}: local pending release failed: {execution.message}")
                    kept += 1
                    continue

                reason = f"LC #{lc_id} released and converted to VT #{vt_id}"
                close_pending_order(config, local_id, "FILLED", reason)
                events.append(
                    {
                        "type": "pending_converted",
                        "source": "local_released",
                        "from_status": str(record.get("status") or "OPEN"),
                        "lc_id": lc_id,
                        "vt_id": vt_id,
                        "symbol": symbol,
                        "side": str(record.get("side") or ""),
                        "exchange_order_id": execution.order_id,
                    }
                )
                converted += 1
                if active_count is not None:
                    active_count += 1
                if position_count is not None:
                    position_count += 1
                active_symbols.add(symbol)
                continue

        reason = "; ".join(review_reasons[:3]) if review_reasons else "Pending setup no longer passes scan"
        if candidate is None:
            reason = _missing_candidate_reason(record, candidates_by_symbol)
        if exchange is None and config.get("mode") != "dry_run" and exchange_order_id:
            warnings.append(f"{symbol}: pending order was not canceled because OKX sync is unavailable")
            kept += 1
            continue
        if exchange is not None and exchange_order_id:
            try:
                exchange.cancel_order(exchange_order_id, symbol)
            except Exception as exc:
                warnings.append(f"{symbol}: pending cancel failed: {exc}")
                kept += 1
                continue
        close_pending_order(config, local_id, "CANCELED", reason)
        events.append(
            {
                "type": "pending_canceled",
                "lc_id": lc_id,
                "symbol": symbol,
                "side": str(record.get("side") or ""),
                "reason": reason,
            }
        )
        canceled += 1

    return {
        "enabled": True,
        "reviewed": reviewed,
        "kept": kept,
        "canceled": canceled,
        "closed": closed,
        "converted": converted,
        "submitted": submitted,
        "events": events,
        "warnings": warnings,
    }
