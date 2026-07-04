from __future__ import annotations

from typing import Any

from .codex_features import record_trade_execution
from .ledger import append_event
from .market import create_exchange
from .models import ExecutionResult, TradeCandidate, to_jsonable


def _order_side(candidate: TradeCandidate) -> str:
    return "buy" if candidate.side == "long" else "sell"


def _okx_params(config: dict[str, Any], candidate: TradeCandidate) -> dict[str, Any]:
    exchange_config = config["exchange"]
    execution_config = config["execution"]
    params: dict[str, Any] = {
        "tdMode": exchange_config.get("td_mode", "isolated"),
    }
    if exchange_config.get("position_side_mode") == "long_short":
        params["posSide"] = "long" if candidate.side == "long" else "short"
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
    params = _okx_params(config, candidate)
    order = exchange.create_order(
        candidate.symbol,
        order_type,
        side,
        candidate.quantity,
        price,
        params,
    )
    order_id = str(order.get("id") or order.get("clientOrderId") or "")
    result = ExecutionResult(
        mode=mode,
        submitted=True,
        order_id=order_id,
        message=f"{mode}: {order_type} order submitted",
        raw=order,
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
        record_trade_execution(config, candidate, execution=to_jsonable(result))
    except Exception as exc:
        raw = result.raw if isinstance(result.raw, dict) else {}
        result.raw = {**raw, "trade_execution_record_error": str(exc)}
    return result
