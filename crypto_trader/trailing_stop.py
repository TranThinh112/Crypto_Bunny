from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .market import create_exchange
from .storage import get_journal_state, list_trade_execution_rows, set_journal_state, update_trade_execution


STATE_KEY = "trailing_stop:last_status"
PARTIAL_TP_NOTIFICATION_PREFIX = "trailing_stop:partial_tp_notified"
PROFIT_STEP_NOTIFICATION_PREFIX = "trailing_stop:profit_step_notified"


def _float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("trailing_stop", {})
    partial = raw.get("partial_take_profit", {}) if isinstance(raw.get("partial_take_profit"), dict) else {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "activation_r_multiple": float(raw.get("activation_r_multiple", 1.0) or 1.0),
        "atr_multiplier": float(raw.get("atr_multiplier", 1.5) or 1.5),
        "atr_period": max(1, int(raw.get("atr_period", 14) or 14)),
        "atr_timeframe": str(raw.get("atr_timeframe", "1m") or "1m"),
        "min_improvement_price": max(0.0, float(raw.get("min_improvement_price", 0.0) or 0.0)),
        "trigger_price_type": str(raw.get("trigger_price_type", "last") or "last"),
        "algo_order_types": list(raw.get("algo_order_types") or ["oco", "conditional", "trigger"]),
        "symbol_overrides": raw.get("symbol_overrides", {}) if isinstance(raw.get("symbol_overrides"), dict) else {},
        "partial_take_profit": {
            "enabled": bool(partial.get("enabled", False)),
            "trigger_tp_progress": min(0.95, max(0.05, float(partial.get("trigger_tp_progress", 0.7) or 0.7))),
            "close_fraction": min(0.9, max(0.01, float(partial.get("close_fraction", 0.3) or 0.3))),
            "remaining_sl_buffer_r": max(0.0, float(partial.get("remaining_sl_buffer_r", 0.1) or 0.1)),
            "tp_extension_fraction": max(0.0, float(partial.get("tp_extension_fraction", partial.get("close_fraction", 0.3)) or 0.0)),
            "max_extension_steps": max(1, min(3, int(partial.get("max_extension_steps", 3) or 3))),
            "sl_buffer_r_by_step": list(partial.get("sl_buffer_r_by_step") or [0.1, 0.5, 1.0]),
        },
    }


def _position_side(position: dict[str, Any]) -> str:
    info = position.get("info", {}) if isinstance(position.get("info"), dict) else {}
    side = str(position.get("side") or info.get("posSide") or "").strip().lower()
    if side in {"long", "short"}:
        return side
    contracts = _float(position.get("contracts") or info.get("pos") or info.get("availPos")) or 0.0
    if contracts < 0:
        return "short"
    return "long"


def _position_symbol(position: dict[str, Any]) -> str:
    info = position.get("info", {}) if isinstance(position.get("info"), dict) else {}
    return str(position.get("symbol") or info.get("instId") or "").strip()


def _position_contracts(position: dict[str, Any]) -> float:
    info = position.get("info", {}) if isinstance(position.get("info"), dict) else {}
    return abs(_float(position.get("contracts") or info.get("pos") or info.get("availPos")) or 0.0)


def _position_contract_size(position: dict[str, Any]) -> float:
    info = position.get("info", {}) if isinstance(position.get("info"), dict) else {}
    return abs(_float(position.get("contractSize") or info.get("ctVal")) or 1.0)


def _position_entry(position: dict[str, Any]) -> float | None:
    info = position.get("info", {}) if isinstance(position.get("info"), dict) else {}
    return _float(position.get("entry_price") or position.get("entryPrice") or info.get("avgPx"))


def _position_mark(position: dict[str, Any]) -> float | None:
    info = position.get("info", {}) if isinstance(position.get("info"), dict) else {}
    return _float(position.get("mark_price") or position.get("markPrice") or info.get("markPx") or position.get("last"))


def _base_symbol(symbol: str) -> str:
    return str(symbol or "").split("/")[0].split("-")[0].upper()


def _json_payload(row: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(str(row.get("payload_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _initial_entry(row: dict[str, Any], fallback: float | None) -> float | None:
    payload = _json_payload(row)
    return (
        _float(row.get("initial_entry_price"))
        or _float(payload.get("entry"))
        or _float(payload.get("entry_price"))
        or _float(row.get("entry_price"))
        or fallback
    )


def _initial_stop_loss(row: dict[str, Any]) -> float | None:
    payload = _json_payload(row)
    return (
        _float(row.get("initial_stop_loss"))
        or _float(payload.get("stop_loss"))
        or _float(payload.get("stopLoss"))
        or _float(row.get("stop_loss"))
    )


def _matching_execution(rows: list[dict[str, Any]], symbol: str, side: str) -> dict[str, Any] | None:
    side_key = side.upper()
    matches = [
        row
        for row in rows
        if str(row.get("symbol") or "") == symbol and str(row.get("side") or "").upper() == side_key
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda item: (str(item.get("created_at") or ""), int(item.get("id") or 0)))[0]


def _atr_from_ohlcv(ohlcv: list[list[float]], period: int) -> float | None:
    if len(ohlcv) < period + 1:
        return None
    ranges: list[float] = []
    rows = ohlcv[-(period + 1) :]
    for index in range(1, len(rows)):
        previous_close = float(rows[index - 1][4])
        high = float(rows[index][2])
        low = float(rows[index][3])
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    if not ranges:
        return None
    return sum(ranges[-period:]) / min(period, len(ranges))


def _symbol_min_improvement(symbol: str, settings: dict[str, Any]) -> float:
    overrides = settings.get("symbol_overrides") if isinstance(settings.get("symbol_overrides"), dict) else {}
    override = overrides.get(symbol) or overrides.get(_base_symbol(symbol)) or {}
    minimum = float(settings["min_improvement_price"])
    if isinstance(override, dict):
        points = _float(override.get("min_improvement_points"))
        point_value = _float(override.get("point_value"))
        if points is not None and point_value is not None:
            minimum = max(minimum, points * point_value)
        price = _float(override.get("min_improvement_price"))
        if price is not None:
            minimum = max(minimum, price)
    return max(0.0, minimum)


def _position_r_multiple(side: str, entry: float, initial_stop: float, mark: float) -> tuple[float | None, float | None]:
    initial_r = entry - initial_stop if side == "long" else initial_stop - entry
    if initial_r <= 0:
        return None, None
    open_profit = mark - entry if side == "long" else entry - mark
    return initial_r, open_profit / initial_r


def _find_stop_loss_algo(exchange: Any, symbol: str, side: str, current_sl: float | None, settings: dict[str, Any]) -> dict[str, Any] | None:
    market = exchange.market(symbol) if hasattr(exchange, "market") else {"id": symbol}
    inst_id = str(market.get("id") or symbol)
    fetch_algos = getattr(exchange, "privateGetTradeOrdersAlgoPending", None)
    if not callable(fetch_algos):
        fetch_algos = getattr(exchange, "private_get_trade_orders_algo_pending", None)
    if not callable(fetch_algos):
        return None
    rows: list[Any] = []
    for ord_type in settings.get("algo_order_types") or ["oco", "conditional", "trigger"]:
        try:
            response = fetch_algos({"instId": inst_id, "ordType": str(ord_type)})
        except Exception:
            continue
        chunk = response.get("data") if isinstance(response, dict) else response
        if isinstance(chunk, list):
            rows.extend(chunk)
    if not rows:
        return None
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_inst = str(row.get("instId") or inst_id)
        if row_inst != inst_id:
            continue
        sl = _float(row.get("slTriggerPx") or row.get("slOrdPx"))
        if sl is None:
            continue
        pos_side = str(row.get("posSide") or "").strip().lower()
        if pos_side and pos_side not in {side, "net"}:
            continue
        candidates.append(row)
    if not candidates:
        return None
    if current_sl is None:
        return candidates[0]
    return min(
        candidates,
        key=lambda item: abs((_float(item.get("slTriggerPx") or item.get("slOrdPx")) or current_sl) - current_sl),
    )


def _price_to_precision(exchange: Any, symbol: str, price: float) -> str:
    method = getattr(exchange, "price_to_precision", None)
    if callable(method):
        return str(method(symbol, price))
    return f"{price:.8f}".rstrip("0").rstrip(".")


def _amount_to_precision(exchange: Any, symbol: str, amount: float) -> str:
    method = getattr(exchange, "amount_to_precision", None)
    if callable(method):
        return str(method(symbol, amount))
    return f"{amount:.8f}".rstrip("0").rstrip(".")


def _amend_stop_loss(
    exchange: Any,
    symbol: str,
    algo: dict[str, Any],
    new_sl: float,
    settings: dict[str, Any],
    *,
    new_tp: float | None = None,
) -> dict[str, Any]:
    market = exchange.market(symbol) if hasattr(exchange, "market") else {"id": symbol}
    inst_id = str(market.get("id") or symbol)
    algo_id = str(algo.get("algoId") or algo.get("id") or "").strip()
    if not algo_id:
        raise RuntimeError("OKX SL algoId is unavailable")
    payload = {
        "instId": inst_id,
        "algoId": algo_id,
        "newSlTriggerPx": _price_to_precision(exchange, symbol, new_sl),
        "newSlOrdPx": "-1",
        "newSlTriggerPxType": str(settings.get("trigger_price_type") or "last"),
    }
    if new_tp is not None:
        payload.update(
            {
                "newTpTriggerPx": _price_to_precision(exchange, symbol, new_tp),
                "newTpOrdPx": "-1",
                "newTpTriggerPxType": str(settings.get("trigger_price_type") or "last"),
            }
        )
    amend = getattr(exchange, "privatePostTradeAmendAlgos", None)
    if not callable(amend):
        amend = getattr(exchange, "private_post_trade_amend_algos", None)
    if not callable(amend):
        raise RuntimeError("OKX amend algo endpoint is unavailable")
    response = amend(payload)
    return {"request": payload, "response": response}


def _close_partial_position(
    exchange: Any,
    config: dict[str, Any],
    *,
    symbol: str,
    side: str,
    amount: float,
) -> dict[str, Any]:
    close_side = "sell" if side == "long" else "buy"
    params: dict[str, Any] = {
        "tdMode": config.get("exchange", {}).get("td_mode", "isolated"),
        "reduceOnly": True,
    }
    if config.get("exchange", {}).get("position_side_mode") == "long_short":
        params["posSide"] = side
    return exchange.create_order(
        symbol,
        "market",
        close_side,
        _amount_to_precision(exchange, symbol, amount),
        None,
        params,
    )


def _evaluate_new_stop(
    *,
    side: str,
    mark: float,
    atr: float,
    current_sl: float,
    settings: dict[str, Any],
) -> float:
    distance = atr * float(settings["atr_multiplier"])
    return mark - distance if side == "long" else mark + distance


def _tp_progress(side: str, entry: float, take_profit: float | None, mark: float) -> float | None:
    if take_profit is None:
        return None
    reward = take_profit - entry if side == "long" else entry - take_profit
    if reward <= 0:
        return None
    gained = mark - entry if side == "long" else entry - mark
    return gained / reward


def _positive_stop_from_entry(side: str, entry: float, initial_r: float, buffer_r: float) -> float:
    buffer = max(0.0, initial_r * buffer_r)
    return entry + buffer if side == "long" else entry - buffer


def _extended_take_profit(side: str, entry: float, take_profit: float | None, extension_fraction: float) -> float | None:
    if take_profit is None:
        return None
    reward = take_profit - entry if side == "long" else entry - take_profit
    if reward <= 0:
        return None
    extension = reward * max(0.0, extension_fraction)
    return take_profit + extension if side == "long" else take_profit - extension

def _step_sl_buffer(partial_settings: dict[str, Any], step: int) -> float:
    buffers = partial_settings.get("sl_buffer_r_by_step")
    if not isinstance(buffers, list) or not buffers:
        buffers = [partial_settings.get("remaining_sl_buffer_r", 0.1), 0.5, 1.0]
    index = max(0, min(len(buffers) - 1, step - 1))
    return max(0.0, float(buffers[index] or 0.0))

def _step_take_profit(side: str, initial_entry: float, initial_tp: float | None, step: int, extension_fraction: float) -> float | None:
    if initial_tp is None:
        return None
    reward = initial_tp - initial_entry if side == "long" else initial_entry - initial_tp
    if reward <= 0:
        return None
    extension = reward * max(0.0, extension_fraction) * max(0, step)
    return initial_tp + extension if side == "long" else initial_tp - extension

def _profit_step_notification_key(execution_id: Any, step: int, updated_at: str) -> str:
    return f"{PROFIT_STEP_NOTIFICATION_PREFIX}:{execution_id}:{step}:{updated_at}"

def _notify_profit_extension_step(config: dict[str, Any], event: dict[str, Any]) -> bool:
    key = _profit_step_notification_key(event.get("trade_execution_id"), int(event.get("step") or 0), str(event.get("updated_at") or ""))
    if get_journal_state(config, key):
        return False
    from .notifier import send_telegram_message
    from .reporting import format_profit_extension_step_message

    sent = send_telegram_message(
        config,
        format_profit_extension_step_message(config, event),
        with_buttons=False,
        replace_previous=False,
        allow_during_startup_quiet=True,
    )
    if sent:
        set_journal_state(config, key, json.dumps({"sent_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False))
    return bool(sent)


def _partial_tp_notification_key(execution_id: Any, partial_at: str) -> str:
    return f"{PARTIAL_TP_NOTIFICATION_PREFIX}:{execution_id}:{partial_at}"

def _notify_partial_take_profit(config: dict[str, Any], event: dict[str, Any]) -> bool:
    key = _partial_tp_notification_key(event.get("trade_execution_id"), str(event.get("partial_at") or ""))
    if get_journal_state(config, key):
        return False
    from .notifier import send_telegram_message
    from .reporting import format_partial_take_profit_message

    sent = send_telegram_message(
        config,
        format_partial_take_profit_message(config, event),
        with_buttons=False,
        replace_previous=False,
        allow_during_startup_quiet=True,
    )
    if sent:
        set_journal_state(config, key, json.dumps({"sent_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False))
    return bool(sent)

def _status_row(symbol: str, side: str, status: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "side": side.upper(),
        "status": status,
        "reason": reason,
        **extra,
    }


def run_trailing_stop_cycle(config: dict[str, Any]) -> dict[str, Any]:
    settings = _settings(config)
    now = datetime.now(timezone.utc).isoformat()
    if not settings["enabled"]:
        result = {"enabled": False, "created_at": now, "reason": "disabled"}
        set_journal_state(config, STATE_KEY, json.dumps(result, ensure_ascii=False))
        return result
    if config.get("mode") == "dry_run":
        result = {"enabled": False, "created_at": now, "reason": "dry_run"}
        set_journal_state(config, STATE_KEY, json.dumps(result, ensure_ascii=False))
        return result

    exchange = create_exchange(config, authenticated=True)
    exchange.load_markets()
    positions = [item for item in (exchange.fetch_positions() or []) if isinstance(item, dict)]
    executions = list_trade_execution_rows(config, statuses=["OPEN"], limit=1000)
    rows: list[dict[str, Any]] = []
    amended = 0
    partial_closed = 0
    skipped = 0
    for position in positions:
        if _position_contracts(position) <= 0:
            continue
        symbol = _position_symbol(position)
        side = _position_side(position)
        if not symbol or side not in {"long", "short"}:
            continue
        mark = _position_mark(position)
        entry = _position_entry(position)
        if mark is None or entry is None:
            skipped += 1
            rows.append(_status_row(symbol, side, "skipped", "missing entry or mark price"))
            continue
        execution = _matching_execution(executions, symbol, side)
        if execution is None:
            skipped += 1
            rows.append(_status_row(symbol, side, "skipped", "missing OPEN trade execution for initial SL"))
            continue
        initial_entry = _initial_entry(execution, entry)
        initial_sl = _initial_stop_loss(execution)
        current_sl = _float(execution.get("stop_loss")) or initial_sl
        algo = _find_stop_loss_algo(exchange, symbol, side, current_sl, settings)
        if algo is not None:
            algo_sl = _float(algo.get("slTriggerPx") or algo.get("slOrdPx"))
            if algo_sl is not None:
                current_sl = algo_sl
                if initial_sl is None:
                    initial_sl = algo_sl
        if initial_entry is None or initial_sl is None:
            skipped += 1
            rows.append(_status_row(symbol, side, "skipped", "missing initial entry or initial SL"))
            continue
        initial_r, r_multiple = _position_r_multiple(side, initial_entry, initial_sl, mark)
        if initial_r is None or r_multiple is None:
            skipped += 1
            rows.append(_status_row(symbol, side, "skipped", "invalid initial R", entry=initial_entry, initial_stop_loss=initial_sl))
            continue
        take_profit = _float(execution.get("take_profit"))
        partial_settings = settings.get("partial_take_profit", {}) if isinstance(settings.get("partial_take_profit"), dict) else {}
        partial_enabled = bool(partial_settings.get("enabled"))
        partial_done = bool(execution.get("partial_take_profit_done"))
        initial_take_profit = _float(execution.get("partial_take_profit_original_tp")) or take_profit
        if partial_enabled and not partial_done:
            progress = _tp_progress(side, initial_entry, take_profit, mark)
            trigger_progress = float(partial_settings.get("trigger_tp_progress", 0.7) or 0.7)
            if progress is None:
                skipped += 1
                rows.append(_status_row(symbol, side, "skipped", "TP progress unavailable", take_profit=take_profit))
                continue
            if progress < trigger_progress:
                skipped += 1
                rows.append(
                    _status_row(
                        symbol,
                        side,
                        "waiting",
                        "partial TP trigger not reached",
                        tp_progress=round(progress, 4),
                        trigger_tp_progress=trigger_progress,
                    )
                )
                continue
            if algo is None:
                skipped += 1
                rows.append(_status_row(symbol, side, "skipped", "OKX SL/TP algo order not found"))
                continue
            contracts = _position_contracts(position)
            partial_amount = contracts * float(partial_settings.get("close_fraction", 0.3) or 0.3)
            if partial_amount <= 0 or partial_amount >= contracts:
                skipped += 1
                rows.append(_status_row(symbol, side, "skipped", "invalid partial close amount", contracts=contracts, partial_amount=partial_amount))
                continue
            positive_sl = _positive_stop_from_entry(
                side,
                initial_entry,
                initial_r,
                _step_sl_buffer(partial_settings, 1),
            )
            new_sl = max(current_sl, positive_sl) if side == "long" else min(current_sl, positive_sl)
            new_tp = _step_take_profit(
                side,
                initial_entry,
                take_profit,
                1,
                float(partial_settings.get("tp_extension_fraction", partial_settings.get("close_fraction", 0.3)) or 0.0),
            )
            partial_order = _close_partial_position(exchange, config, symbol=symbol, side=side, amount=partial_amount)
            amend_result = _amend_stop_loss(exchange, symbol, algo, new_sl, settings, new_tp=new_tp)
            updated_at = datetime.now(timezone.utc).isoformat()
            update_trade_execution(
                config,
                int(execution["id"]),
                {
                    "updated_at": updated_at,
                    "stop_loss": new_sl,
                    "take_profit": new_tp if new_tp is not None else take_profit,
                    "initial_entry_price": initial_entry,
                    "initial_stop_loss": initial_sl,
                    "partial_take_profit_done": True,
                    "partial_take_profit_at": updated_at,
                    "partial_take_profit_fraction": float(partial_settings.get("close_fraction", 0.3) or 0.3),
                    "partial_take_profit_amount": partial_amount,
                    "partial_take_profit_price": mark,
                    "partial_take_profit_order_json": json.dumps(partial_order, ensure_ascii=False),
                    "partial_take_profit_original_tp": take_profit,
                    "partial_take_profit_extended_tp": new_tp,
                    "profit_extension_step": 1,
                    "trailing_stop_updated_at": updated_at,
                    "trailing_stop_r_multiple": round(r_multiple, 6),
                },
            )
            notification_sent = _notify_partial_take_profit(
                config,
                {
                    "trade_execution_id": execution.get("id"),
                    "symbol": symbol,
                    "side": side,
                    "entry": initial_entry,
                    "trigger_price": mark,
                    "close_fraction": float(partial_settings.get("close_fraction", 0.3) or 0.3),
                    "partial_amount": partial_amount,
                    "remaining_amount": max(0.0, contracts - partial_amount),
                    "contract_size": _position_contract_size(position),
                    "old_stop_loss": current_sl,
                    "new_stop_loss": new_sl,
                    "old_take_profit": take_profit,
                    "new_take_profit": new_tp,
                    "partial_at": updated_at,
                },
            )
            partial_closed += 1
            amended += 1
            rows.append(
                _status_row(
                    symbol,
                    side,
                    "partial_closed",
                    "partial TP closed; SL protected and TP extended",
                    tp_progress=round(progress, 4),
                    partial_amount=round(partial_amount, 8),
                    new_stop_loss=round(new_sl, 8),
                    new_take_profit=None if new_tp is None else round(new_tp, 8),
                    notification_sent=notification_sent,
                    amend_request=amend_result.get("request"),
                )
            )
            continue
        if partial_enabled and partial_done:
            current_step = int(_float(execution.get("profit_extension_step")) or 1)
            max_steps = int(partial_settings.get("max_extension_steps", 3) or 3)
            if current_step < max_steps:
                progress = _tp_progress(side, initial_entry, take_profit, mark)
                trigger_progress = float(partial_settings.get("trigger_tp_progress", 0.7) or 0.7)
                if progress is None:
                    skipped += 1
                    rows.append(_status_row(symbol, side, "skipped", "TP step progress unavailable", take_profit=take_profit))
                    continue
                if progress >= trigger_progress:
                    if algo is None:
                        skipped += 1
                        rows.append(_status_row(symbol, side, "skipped", "OKX SL/TP algo order not found"))
                        continue
                    next_step = current_step + 1
                    step_sl = _positive_stop_from_entry(side, initial_entry, initial_r, _step_sl_buffer(partial_settings, next_step))
                    new_sl = max(current_sl, step_sl) if side == "long" else min(current_sl, step_sl)
                    new_tp = _step_take_profit(
                        side,
                        initial_entry,
                        initial_take_profit,
                        next_step,
                        float(partial_settings.get("tp_extension_fraction", partial_settings.get("close_fraction", 0.3)) or 0.0),
                    )
                    if new_tp is None:
                        skipped += 1
                        rows.append(_status_row(symbol, side, "skipped", "next TP unavailable"))
                        continue
                    amend_result = _amend_stop_loss(exchange, symbol, algo, new_sl, settings, new_tp=new_tp)
                    updated_at = datetime.now(timezone.utc).isoformat()
                    update_trade_execution(
                        config,
                        int(execution["id"]),
                        {
                            "updated_at": updated_at,
                            "stop_loss": new_sl,
                            "take_profit": new_tp,
                            "partial_take_profit_extended_tp": new_tp,
                            "profit_extension_step": next_step,
                            "trailing_stop_updated_at": updated_at,
                            "trailing_stop_r_multiple": round(r_multiple, 6),
                        },
                    )
                    notification_sent = _notify_profit_extension_step(
                        config,
                        {
                            "trade_execution_id": execution.get("id"),
                            "symbol": symbol,
                            "side": side,
                            "step": next_step,
                            "entry": initial_entry,
                            "trigger_price": mark,
                            "remaining_amount": _position_contracts(position),
                            "contract_size": _position_contract_size(position),
                            "old_stop_loss": current_sl,
                            "new_stop_loss": new_sl,
                            "old_take_profit": take_profit,
                            "new_take_profit": new_tp,
                            "updated_at": updated_at,
                        },
                    )
                    amended += 1
                    rows.append(
                        _status_row(
                            symbol,
                            side,
                            "profit_step_extended",
                            f"profit step {next_step} extended TP and SL",
                            tp_progress=round(progress, 4),
                            new_stop_loss=round(new_sl, 8),
                            new_take_profit=round(new_tp, 8),
                            notification_sent=notification_sent,
                            amend_request=amend_result.get("request"),
                        )
                    )
                    continue
        if not partial_done and r_multiple < float(settings["activation_r_multiple"]):
            skipped += 1
            rows.append(
                _status_row(
                    symbol,
                    side,
                    "waiting",
                    "activation R not reached",
                    r_multiple=round(r_multiple, 4),
                    activation_r_multiple=settings["activation_r_multiple"],
                )
            )
            continue
        ohlcv = exchange.fetch_ohlcv(symbol, settings["atr_timeframe"], limit=int(settings["atr_period"]) + 1)
        atr = _atr_from_ohlcv(ohlcv or [], int(settings["atr_period"]))
        if atr is None or atr <= 0:
            skipped += 1
            rows.append(_status_row(symbol, side, "skipped", "ATR unavailable"))
            continue
        new_sl = _evaluate_new_stop(side=side, mark=mark, atr=atr, current_sl=current_sl, settings=settings)
        improvement = new_sl - current_sl if side == "long" else current_sl - new_sl
        if improvement <= 0:
            skipped += 1
            rows.append(
                _status_row(symbol, side, "waiting", "new SL is not better", current_stop_loss=current_sl, proposed_stop_loss=round(new_sl, 8))
            )
            continue
        min_improvement = _symbol_min_improvement(symbol, settings)
        if improvement < min_improvement:
            skipped += 1
            rows.append(
                _status_row(
                    symbol,
                    side,
                    "waiting",
                    "minimum improvement not reached",
                    current_stop_loss=current_sl,
                    proposed_stop_loss=round(new_sl, 8),
                    improvement=round(improvement, 8),
                    min_improvement=min_improvement,
                )
            )
            continue
        if algo is None:
            skipped += 1
            rows.append(_status_row(symbol, side, "skipped", "OKX SL algo order not found"))
            continue
        amend_result = _amend_stop_loss(exchange, symbol, algo, new_sl, settings)
        updated_at = datetime.now(timezone.utc).isoformat()
        update_trade_execution(
            config,
            int(execution["id"]),
            {
                "updated_at": updated_at,
                "stop_loss": new_sl,
                "initial_entry_price": initial_entry,
                "initial_stop_loss": initial_sl,
                "trailing_stop_updated_at": updated_at,
                "trailing_stop_r_multiple": round(r_multiple, 6),
                "trailing_stop_atr": round(atr, 8),
            },
        )
        amended += 1
        rows.append(
            _status_row(
                symbol,
                side,
                "amended",
                "SL trailed",
                current_stop_loss=current_sl,
                new_stop_loss=round(new_sl, 8),
                r_multiple=round(r_multiple, 4),
                atr=round(atr, 8),
                amend_request=amend_result.get("request"),
            )
        )

    result = {
        "enabled": True,
        "created_at": now,
        "positions_seen": len(positions),
        "amended": amended,
        "partial_closed": partial_closed,
        "skipped": skipped,
        "items": rows[-20:],
        "previous_status": get_journal_state(config, STATE_KEY),
    }
    set_journal_state(config, STATE_KEY, json.dumps({k: v for k, v in result.items() if k != "previous_status"}, ensure_ascii=False))
    return result
