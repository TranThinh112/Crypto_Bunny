from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from typing import Any

from .codex_features import record_trade_execution
from .ledger import append_event
from .market import create_exchange
from .models import ExecutionResult, TradeCandidate, to_jsonable


def _order_side(candidate: TradeCandidate) -> str:
    return "buy" if candidate.side == "long" else "sell"


def candidate_client_order_id(candidate: TradeCandidate, *, entry_type: str) -> str | None:
    metadata = candidate.decision_metadata if isinstance(candidate.decision_metadata, dict) else {}
    stored = str(metadata.get("okx_client_order_id") or "").strip()
    if stored:
        return stored[:32]
    mini_setup = metadata.get("mini_setup")
    if not isinstance(mini_setup, dict):
        return None
    setup_id = str(mini_setup.get("setup_id") or "").strip()
    if not setup_id:
        return None
    digest = hashlib.sha256(f"{setup_id}|{entry_type}".encode("utf-8")).hexdigest()[:30]
    return f"CB{digest}"


def with_candidate_client_order_id(candidate: TradeCandidate, *, entry_type: str) -> TradeCandidate:
    client_order_id = candidate_client_order_id(candidate, entry_type=entry_type)
    if not client_order_id:
        return candidate
    prepared = deepcopy(candidate)
    metadata = prepared.decision_metadata if isinstance(prepared.decision_metadata, dict) else {}
    prepared.decision_metadata = {**metadata, "okx_client_order_id": client_order_id}
    return prepared


def _order_client_id(order: dict[str, Any]) -> str:
    info = order.get("info") if isinstance(order.get("info"), dict) else {}
    return str(order.get("clientOrderId") or info.get("clOrdId") or "").strip()


def _order_exchange_id(order: dict[str, Any]) -> str:
    info = order.get("info") if isinstance(order.get("info"), dict) else {}
    return str(order.get("id") or info.get("ordId") or "").strip()


def _is_ambiguous_submit_error(exc: Exception) -> bool:
    class_names = {item.__name__.lower() for item in type(exc).__mro__}
    if class_names & {
        "timeouterror",
        "requesttimeout",
        "networkerror",
        "exchangenotavailable",
        "ddosprotection",
    }:
        return True
    message = str(exc).lower()
    return any(
        token in message
        for token in (
            "timeout",
            "timed out",
            "connection reset",
            "connection aborted",
            "remote end closed",
            "temporarily unavailable",
            "bad gateway",
            "gateway timeout",
        )
    )


def _recover_order_after_submit_error(
    exchange: Any,
    *,
    symbol: str,
    client_order_id: str,
) -> dict[str, Any] | None:
    if not client_order_id:
        return None
    fetch_order = getattr(exchange, "fetch_order", None)
    if callable(fetch_order):
        try:
            order = fetch_order(client_order_id, symbol, {"clOrdId": client_order_id})
            if isinstance(order, dict) and _order_client_id(order) == client_order_id:
                return order
        except Exception:
            pass
    for method_name in ("fetch_open_orders", "fetch_closed_orders"):
        fetch_orders = getattr(exchange, method_name, None)
        if not callable(fetch_orders):
            continue
        try:
            orders = fetch_orders(symbol)
        except TypeError:
            try:
                orders = fetch_orders()
            except Exception:
                continue
        except Exception:
            continue
        for order in orders or []:
            if isinstance(order, dict) and _order_client_id(order) == client_order_id:
                return order
    return None


def _okx_params(
    config: dict[str, Any],
    candidate: TradeCandidate,
    *,
    client_order_id: str | None = None,
) -> dict[str, Any]:
    exchange_config = config["exchange"]
    execution_config = config["execution"]
    params: dict[str, Any] = {
        "tdMode": exchange_config.get("td_mode", "isolated"),
    }
    if exchange_config.get("position_side_mode") == "long_short":
        params["posSide"] = "long" if candidate.side == "long" else "short"
    if client_order_id:
        params["clOrdId"] = client_order_id
    if execution_config.get("attach_tp_sl", True):
        params["attachAlgoOrds"] = [
            {
                "tpTriggerPx": str(candidate.take_profit),
                "tpOrdPx": "-1",
                "tpTriggerPxType": "last",
                "slTriggerPx": str(candidate.stop_loss),
                "slOrdPx": "-1",
                "slTriggerPxType": "last",
            }
        ]
    return params


def _is_okx_pos_side_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "posside" in message and ("51000" in message or "parameter" in message or "error" in message)


def _pos_side_retry_params(params: dict[str, Any], candidate: TradeCandidate) -> dict[str, Any]:
    retry = deepcopy(params)
    if "posSide" in retry:
        retry.pop("posSide", None)
    else:
        retry["posSide"] = "long" if candidate.side == "long" else "short"
    return retry


def _readable_submit_error(exc: Exception) -> str:
    message = re.sub(r"\s+", " ", str(exc or "").strip())
    lower = message.lower()
    if "posside" in lower:
        return (
            "OKX từ chối lệnh: chế độ vị thế posSide không khớp tài khoản "
            "(Net/Hedge). Bot đã thử tự điều chỉnh, nhưng OKX vẫn từ chối."
        )
    if "insufficient" in lower or "balance" in lower:
        return "OKX từ chối lệnh: số dư hoặc margin không đủ."
    if "timeout" in lower or "timed out" in lower:
        return "OKX chưa xác nhận trạng thái lệnh do timeout kết nối."
    if not message:
        return "OKX từ chối lệnh nhưng không trả lý do rõ ràng."
    return f"OKX từ chối lệnh: {message[:220]}"


def execute_candidate(
    config: dict[str, Any],
    candidate: TradeCandidate,
    *,
    order_type_override: str | None = None,
    entry_type: str = "market",
    journal_type: str | None = None,
    journal_id: int | None = None,
    linked_journal_id: int | None = None,
) -> ExecutionResult:
    mode = config.get("mode", "dry_run")
    if mode == "dry_run":
        return ExecutionResult(
            mode=mode,
            submitted=False,
            order_id=None,
            message="dry_run: order was not submitted",
            journal_type=journal_type,
            journal_id=journal_id,
            linked_journal_id=linked_journal_id,
        )

    if not candidate.quantity or candidate.quantity <= 0:
        return ExecutionResult(
            mode=mode,
            submitted=False,
            order_id=None,
            message="quantity is missing or invalid",
            journal_type=journal_type,
            journal_id=journal_id,
            linked_journal_id=linked_journal_id,
        )

    exchange = create_exchange(config, authenticated=True)
    exchange.load_markets()

    if config["exchange"].get("account_type") == "swap":
        try:
            exchange.set_leverage(
                int(config["exchange"].get("leverage", 1)),
                candidate.symbol,
                {"mgnMode": config["exchange"].get("td_mode", "isolated")},
            )
        except Exception:
            pass

    order_type = order_type_override or config["execution"].get("order_type", "market")
    side = _order_side(candidate)
    price = None if order_type == "market" else candidate.entry
    client_order_id = candidate_client_order_id(candidate, entry_type=entry_type)
    params = _okx_params(config, candidate, client_order_id=client_order_id)
    order: dict[str, Any] | None = None
    recovered: dict[str, Any] | None = None
    pos_side_retry = False
    first_error: Exception | None = None
    try:
        order = exchange.create_order(
            candidate.symbol,
            order_type,
            side,
            candidate.quantity,
            price,
            params,
        )
    except Exception as exc:
        first_error = exc
        if _is_okx_pos_side_error(exc):
            retry_params = _pos_side_retry_params(params, candidate)
            try:
                order = exchange.create_order(
                    candidate.symbol,
                    order_type,
                    side,
                    candidate.quantity,
                    price,
                    retry_params,
                )
                params = retry_params
                pos_side_retry = True
            except Exception as retry_exc:
                exc = retry_exc
        if order is None:
            ambiguous = _is_ambiguous_submit_error(exc)
            recovered = (
                _recover_order_after_submit_error(
                    exchange,
                    symbol=candidate.symbol,
                    client_order_id=client_order_id,
                )
                if ambiguous and client_order_id
                else None
            )
            recovered_order_id = _order_exchange_id(recovered or {})
            if recovered is not None and recovered_order_id:
                order = recovered
            else:
                return ExecutionResult(
                    mode=mode,
                    submitted=False,
                    order_id=None,
                    message=_readable_submit_error(exc),
                    raw={
                        "error": str(exc),
                        "first_error": str(first_error) if first_error and first_error is not exc else None,
                        "client_order_id": client_order_id,
                        "submission_status": "unknown" if ambiguous else "not_submitted",
                    },
                    journal_type=journal_type,
                    journal_id=journal_id,
                    linked_journal_id=linked_journal_id,
                )
    if order is None:
        return ExecutionResult(
            mode=mode,
            submitted=False,
            order_id=None,
            message="OKX từ chối lệnh nhưng bot không nhận được phản hồi order.",
            raw={"client_order_id": client_order_id, "submission_status": "not_submitted"},
            journal_type=journal_type,
            journal_id=journal_id,
            linked_journal_id=linked_journal_id,
        )
    order_id = _order_exchange_id(order)
    if not order_id:
        return ExecutionResult(
            mode=mode,
            submitted=False,
            order_id=None,
            message="OKX response did not include an exchange order id",
            raw={
                "order": order,
                "client_order_id": client_order_id,
                "submission_status": "unknown",
            },
            journal_type=journal_type,
            journal_id=journal_id,
            linked_journal_id=linked_journal_id,
        )
    result = ExecutionResult(
        mode=mode,
        submitted=True,
        order_id=order_id,
        message=f"{mode}: {order_type} order submitted",
        raw={
            **order,
            "client_order_id": client_order_id,
            "submission_status": "recovered" if order is recovered else "submitted",
            "pos_side_retry": pos_side_retry,
        },
        journal_type=journal_type,
        journal_id=journal_id,
        linked_journal_id=linked_journal_id,
    )
    append_event(
        config,
        {
            "mode": mode,
            "submitted": True,
            "order_id": order_id,
            "entry_type": entry_type,
            "order_type": order_type,
            "journal_type": journal_type,
            "journal_id": journal_id,
            "linked_journal_id": linked_journal_id,
            "symbol": candidate.symbol,
            "side": candidate.side,
            "confidence": candidate.confidence,
            "entry": candidate.entry,
            "stop_loss": candidate.stop_loss,
            "take_profit": candidate.take_profit,
            "quantity": candidate.quantity,
            "margin_usdt": candidate.margin_usdt,
            "notional_usdt": candidate.order_usdt,
            "leverage": config.get("exchange", {}).get("leverage", 1),
            "recovery_margin_usdt": candidate.recovery_margin_usdt,
            "recovery_source_key": candidate.recovery_source_key,
            "planned_risk_usdt": round(candidate.planned_risk_usdt, 4),
        },
    )
    try:
        execution_payload = {
            **to_jsonable(result),
            "entry_type": entry_type,
            "order_type": order_type,
            "journal_type": journal_type,
            "journal_id": journal_id,
            "linked_journal_id": linked_journal_id,
        }
        record_trade_execution(config, candidate, execution=execution_payload)
    except Exception as exc:
        raw = result.raw if isinstance(result.raw, dict) else {}
        result.raw = {**raw, "trade_execution_record_error": str(exc)}
    return result
