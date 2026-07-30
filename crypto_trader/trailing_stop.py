from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .market import create_exchange
from .storage import append_trade_execution_event, get_journal_state, list_trade_execution_rows, set_journal_state, update_trade_execution


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

def _loss_guard_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("loss_guard", {}) if isinstance(config.get("loss_guard"), dict) else {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "effective_from": str(raw.get("effective_from") or ""),
        "apply_to_existing_positions": bool(raw.get("apply_to_existing_positions", False)),
        "auto_close_enabled": bool(raw.get("auto_close_enabled", False)),
        "partial_close_r": float(raw.get("partial_close_r", -0.8) or -0.8),
        "partial_close_fraction": min(0.9, max(0.01, float(raw.get("partial_close_fraction", 0.25) or 0.25))),
    }

def _loss_guard_applies(row: dict[str, Any], settings: dict[str, Any]) -> bool:
    if settings.get("apply_to_existing_positions"):
        return True
    effective_raw = str(settings.get("effective_from") or "").strip()
    if not effective_raw:
        return False
    created_raw = row.get("created_at")
    if not created_raw:
        return False
    try:
        effective_at = datetime.fromisoformat(effective_raw.replace("Z", "+00:00"))
        created_at = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if effective_at.tzinfo is None:
        effective_at = effective_at.replace(tzinfo=timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at >= effective_at

def _loss_guard_trigger_price(side: str, entry: float, initial_r: float, partial_close_r: float) -> float:
    return entry + initial_r * partial_close_r if side == "long" else entry - initial_r * partial_close_r

def _loss_guard_trigger_reached(side: str, mark: float, trigger_price: float) -> bool:
    return mark <= trigger_price if side == "long" else mark >= trigger_price


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
    for key in ("snapshot_json", "payload_json"):
        try:
            payload = json.loads(str(row.get(key) or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return {}


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

def _initial_contracts(row: dict[str, Any]) -> float | None:
    payload = _json_payload(row)
    position = payload.get("position") if isinstance(payload.get("position"), dict) else {}
    info = position.get("info") if isinstance(position.get("info"), dict) else {}
    values = (
        row.get("initial_quantity"),
        row.get("max_contracts_seen"),
        row.get("original_quantity"),
        row.get("quantity"),
        row.get("contracts"),
        payload.get("initial_quantity"),
        payload.get("quantity"),
        payload.get("contracts"),
        position.get("contracts"),
        info.get("pos"),
    )
    numbers = [abs(value) for raw in values if (value := (_float(raw) or 0.0)) > 0]
    return max(numbers) if numbers else None


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

def _stop_loss_trigger_valid(side: str, stop_loss: float | None, mark: float | None) -> bool:
    if stop_loss is None or mark is None:
        return False
    if side == "long":
        return stop_loss < mark
    if side == "short":
        return stop_loss > mark
    return False


def _extract_position_algos(position: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(position, dict):
        return []
    info = position.get("info") if isinstance(position.get("info"), dict) else {}
    raw_values = (
        position.get("closeOrderAlgo"),
        position.get("close_order_algo"),
        info.get("closeOrderAlgo"),
        info.get("close_order_algo"),
    )
    rows: list[dict[str, Any]] = []
    for raw in raw_values:
        value = raw
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                continue
        if isinstance(value, dict):
            rows.append(value)
        elif isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, dict))
    return rows

def _find_stop_loss_algo(
    exchange: Any,
    symbol: str,
    side: str,
    current_sl: float | None,
    settings: dict[str, Any],
    position: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    market = exchange.market(symbol) if hasattr(exchange, "market") else {"id": symbol}
    inst_id = str(market.get("id") or symbol)
    fetch_algos = getattr(exchange, "privateGetTradeOrdersAlgoPending", None)
    if not callable(fetch_algos):
        fetch_algos = getattr(exchange, "private_get_trade_orders_algo_pending", None)
    rows: list[Any] = _extract_position_algos(position)
    if callable(fetch_algos):
        ord_types = [str(item) for item in (settings.get("algo_order_types") or ["oco", "conditional", "trigger"]) if str(item).strip()]
        requests = [{"instId": inst_id, "ordType": ord_type} for ord_type in ord_types]
        requests.append({"instId": inst_id})
        seen_requests: set[tuple[tuple[str, str], ...]] = set()
        for request in requests:
            request_key = tuple(sorted((str(key), str(value)) for key, value in request.items()))
            if request_key in seen_requests:
                continue
            seen_requests.add(request_key)
            try:
                response = fetch_algos(request)
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


def _is_pos_side_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "posside" in message
        or "pos side" in message
        or "position side" in message
        or "don't have any positions in this direction" in message
        or "no positions in this direction" in message
    )

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
    position: dict[str, Any] | None = None,
) -> dict[str, Any]:
    close_side = "sell" if side == "long" else "buy"
    info = position.get("info") if isinstance(position, dict) and isinstance(position.get("info"), dict) else {}
    position_td_mode = (
        (position or {}).get("marginMode")
        or (position or {}).get("margin_mode")
        or info.get("mgnMode")
        or info.get("tdMode")
    )
    position_pos_side = str((position or {}).get("side") or info.get("posSide") or side).strip().lower()
    base_params: dict[str, Any] = {
        "tdMode": position_td_mode or config.get("exchange", {}).get("td_mode", "isolated"),
        "reduceOnly": True,
    }
    if position_pos_side in {"long", "short"}:
        base_params["posSide"] = position_pos_side
    elif config.get("exchange", {}).get("position_side_mode") == "long_short":
        base_params["posSide"] = side

    def submit(params: dict[str, Any]) -> dict[str, Any]:
        return exchange.create_order(
            symbol,
            "market",
            close_side,
            _amount_to_precision(exchange, symbol, amount),
            None,
            params,
        )

    variants: list[dict[str, Any]] = [dict(base_params)]
    without_pos_side = dict(base_params)
    without_pos_side.pop("posSide", None)
    variants.append(without_pos_side)
    with_pos_side = dict(base_params)
    with_pos_side["posSide"] = side
    variants.append(with_pos_side)
    with_net_pos_side = dict(base_params)
    with_net_pos_side["posSide"] = "net"
    variants.append(with_net_pos_side)

    seen: set[tuple[tuple[str, str], ...]] = set()
    last_exc: Exception | None = None
    for params in variants:
        key = tuple(sorted((str(item_key), str(item_value)) for item_key, item_value in params.items()))
        if key in seen:
            continue
        seen.add(key)
        try:
            result = submit(params)
        except Exception as exc:
            last_exc = exc
            if _is_pos_side_error(exc):
                continue
            raise
        if params != base_params and isinstance(result, dict):
            result = dict(result)
            result["pos_side_retry"] = {"original": base_params, "used": params}
        return result
    if last_exc is not None:
        raise last_exc
    return submit(base_params)


def _live_position_for_symbol(exchange: Any, symbol: str, side: str) -> dict[str, Any] | None:
    try:
        positions = [item for item in (exchange.fetch_positions() or []) if isinstance(item, dict)]
    except Exception:
        return None
    for position in positions:
        if _position_symbol(position) != symbol:
            continue
        if _position_side(position) != side:
            continue
        if _position_contracts(position) <= 0:
            continue
        return position
    return None

def _partial_already_reduced_amount(
    *,
    stored_contracts: float,
    live_contracts: float,
    close_fraction: float,
) -> float | None:
    if stored_contracts <= 0 or live_contracts <= 0:
        return None
    reduced = stored_contracts - live_contracts
    if reduced <= 0:
        return None
    expected = stored_contracts * max(0.0, close_fraction)
    tolerance = max(stored_contracts * 0.08, 1e-8)
    if expected > 0 and abs(reduced - expected) <= tolerance:
        return reduced
    if live_contracts < stored_contracts:
        return reduced
    return None

def _parse_exchange_time(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        return datetime.fromtimestamp(number, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def _trade_amount(value: Any) -> float | None:
    number = _float(value)
    return abs(number) if number is not None else None

def _manual_partial_close_history(
    exchange: Any,
    *,
    symbol: str,
    side: str,
    entry: float,
    amount: float,
    contract_size: float,
    since: datetime | None,
) -> dict[str, Any] | None:
    if amount <= 0:
        return None
    close_side = "sell" if side == "long" else "buy"
    since_ms = int(since.timestamp() * 1000) if since else None
    trades: list[dict[str, Any]] = []
    fetch_my_trades = getattr(exchange, "fetch_my_trades", None)
    if callable(fetch_my_trades):
        try:
            rows = fetch_my_trades(symbol, since=since_ms, limit=100)
            if isinstance(rows, list):
                trades.extend(item for item in rows if isinstance(item, dict))
        except Exception:
            pass
    fetch_raw = getattr(exchange, "private_get_trade_fills", None)
    if not callable(fetch_raw):
        fetch_raw = getattr(exchange, "privateGetTradeFills", None)
    if callable(fetch_raw):
        try:
            market = exchange.market(symbol) if hasattr(exchange, "market") else {"id": symbol}
            response = fetch_raw({"instId": str(market.get("id") or symbol), "limit": "100"})
            rows = response.get("data") if isinstance(response, dict) else response
            if isinstance(rows, list):
                trades.extend(item for item in rows if isinstance(item, dict))
        except Exception:
            pass
    if not trades:
        return None
    candidates: list[dict[str, Any]] = []
    for trade in trades:
        info = trade.get("info") if isinstance(trade.get("info"), dict) else {}
        trade_side = str(trade.get("side") or info.get("side") or "").strip().lower()
        pos_side = str(trade.get("posSide") or info.get("posSide") or "").strip().lower()
        reduce_only = str(trade.get("reduceOnly") or info.get("reduceOnly") or "").strip().lower() in {"1", "true", "yes"}
        if trade_side and trade_side != close_side:
            continue
        if pos_side and pos_side not in {side, "net"}:
            continue
        price = _float(trade.get("price") or info.get("fillPx") or info.get("px"))
        qty = _trade_amount(trade.get("amount") or info.get("fillSz") or info.get("sz"))
        closed_at = _parse_exchange_time(trade.get("timestamp") or trade.get("datetime") or info.get("ts") or info.get("uTime") or info.get("cTime"))
        if price is None or qty is None or qty <= 0:
            continue
        if since and closed_at and closed_at < since:
            continue
        if not reduce_only and not pos_side:
            continue
        pnl = _float(trade.get("realizedPnl") or trade.get("pnl") or info.get("fillPnl") or info.get("realizedPnl") or info.get("pnl"))
        if pnl is None:
            gross = price - entry if side == "long" else entry - price
            pnl = gross * qty * contract_size
        candidates.append({"price": price, "amount": qty, "closed_at": closed_at, "pnl": pnl, "raw": trade})
    if not candidates:
        return None
    candidates.sort(key=lambda item: item["closed_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    selected: list[dict[str, Any]] = []
    total_amount = 0.0
    for candidate in candidates:
        selected.append(candidate)
        total_amount += float(candidate["amount"])
        if total_amount + max(amount * 0.02, 1e-8) >= amount:
            break
    if not selected:
        return None
    weighted_price = sum(float(item["price"]) * float(item["amount"]) for item in selected) / max(total_amount, 1e-12)
    pnl_total = sum(float(item["pnl"]) for item in selected)
    closed_at_values = [item["closed_at"] for item in selected if item.get("closed_at") is not None]
    return {
        "source": "okx_fills",
        "price": weighted_price,
        "amount": total_amount,
        "pnl": pnl_total,
        "closed_at": min(closed_at_values).isoformat() if closed_at_values else None,
        "fills": [item["raw"] for item in selected],
    }


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


def _is_profitable_close(side: str, entry: float | None, price: float | None) -> bool:
    if entry is None or price is None:
        return False
    if side == "long":
        return price > entry
    if side == "short":
        return price < entry
    return False


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
        algo = _find_stop_loss_algo(exchange, symbol, side, current_sl, settings, position)
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
        loss_settings = _loss_guard_settings(config)
        if (
            loss_settings["enabled"]
            and loss_settings["auto_close_enabled"]
            and _loss_guard_applies(execution, loss_settings)
            and not bool(execution.get("loss_guard_partial_done"))
        ):
            loss_trigger_price = _loss_guard_trigger_price(side, initial_entry, initial_r, float(loss_settings["partial_close_r"]))
            if _loss_guard_trigger_reached(side, mark, loss_trigger_price):
                live_position = _live_position_for_symbol(exchange, symbol, side)
                live_contracts = _position_contracts(live_position) if live_position is not None else _position_contracts(position)
                loss_amount = live_contracts * float(loss_settings["partial_close_fraction"])
                if live_position is None or live_contracts <= 0 or loss_amount <= 0 or loss_amount >= live_contracts:
                    skipped += 1
                    rows.append(
                        _status_row(
                            symbol,
                            side,
                            "skipped",
                            "invalid loss guard partial close amount",
                            live_contracts=live_contracts,
                            partial_amount=loss_amount,
                        )
                    )
                    continue
                loss_order = _close_partial_position(exchange, config, symbol=symbol, side=side, amount=loss_amount, position=live_position)
                updated_at = datetime.now(timezone.utc).isoformat()
                update_trade_execution(
                    config,
                    int(execution["id"]),
                    {
                        "updated_at": updated_at,
                        "loss_guard_partial_done": True,
                        "loss_guard_partial_at": updated_at,
                        "loss_guard_partial_fraction": float(loss_settings["partial_close_fraction"]),
                        "loss_guard_partial_amount": loss_amount,
                        "loss_guard_partial_price": mark,
                        "loss_guard_partial_trigger_price": loss_trigger_price,
                        "loss_guard_partial_r": float(loss_settings["partial_close_r"]),
                        "loss_guard_partial_order_json": json.dumps(loss_order, ensure_ascii=False),
                    },
                )
                append_trade_execution_event(
                    config,
                    int(execution["id"]),
                    {
                        "type": "loss_guard_partial_close",
                        "created_at": updated_at,
                        "symbol": symbol,
                        "side": side,
                        "mark_price": mark,
                        "trigger_price": loss_trigger_price,
                        "partial_close_r": float(loss_settings["partial_close_r"]),
                        "partial_fraction": float(loss_settings["partial_close_fraction"]),
                        "partial_amount": loss_amount,
                        "remaining_amount": max(0.0, live_contracts - loss_amount),
                        "contract_size": _position_contract_size(live_position or position),
                        "exchange_order": loss_order,
                    },
                )
                partial_closed += 1
                rows.append(
                    _status_row(
                        symbol,
                        side,
                        "loss_guard_partial_closed",
                        "loss guard partial close executed",
                        r_multiple=round(r_multiple, 4),
                        trigger_price=round(loss_trigger_price, 8),
                        mark_price=mark,
                        partial_amount=round(loss_amount, 8),
                    )
                )
                continue
        take_profit = _float(execution.get("take_profit"))
        partial_settings = settings.get("partial_take_profit", {}) if isinstance(settings.get("partial_take_profit"), dict) else {}
        partial_enabled = bool(partial_settings.get("enabled"))
        partial_done = bool(execution.get("partial_take_profit_done"))
        initial_take_profit = _float(execution.get("partial_take_profit_original_tp")) or take_profit
        if partial_enabled and not partial_done:
            live_position = _live_position_for_symbol(exchange, symbol, side)
            live_contracts = _position_contracts(live_position) if live_position is not None else 0.0
            initial_contracts = _initial_contracts(execution) or 0.0
            live_snapshot_contracts = _position_contracts(position)
            stored_contracts = max(initial_contracts, live_snapshot_contracts)
            close_fraction = float(partial_settings.get("close_fraction", 0.3) or 0.3)
            manually_reduced_amount = _partial_already_reduced_amount(
                stored_contracts=stored_contracts,
                live_contracts=live_contracts,
                close_fraction=close_fraction,
            )
            if bool(execution.get("loss_guard_partial_done")):
                manually_reduced_amount = None
            progress = _tp_progress(side, initial_entry, take_profit, mark)
            trigger_progress = float(partial_settings.get("trigger_tp_progress", 0.7) or 0.7)
            if progress is None:
                skipped += 1
                rows.append(_status_row(symbol, side, "skipped", "TP progress unavailable", take_profit=take_profit))
                continue
            if progress < trigger_progress and manually_reduced_amount is None:
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
            if live_position is None:
                skipped += 1
                rows.append(_status_row(symbol, side, "skipped", "position no longer open before partial close"))
                continue
            if live_contracts <= 0:
                skipped += 1
                rows.append(_status_row(symbol, side, "skipped", "position size already zero"))
                continue
            contracts = max(stored_contracts, live_contracts)
            close_partial_on_exchange = manually_reduced_amount is None
            active_contracts = live_contracts if manually_reduced_amount is not None else min(contracts, live_contracts)
            partial_amount = manually_reduced_amount if manually_reduced_amount is not None else active_contracts * close_fraction
            if partial_amount <= 0 or partial_amount >= contracts:
                skipped += 1
                rows.append(_status_row(symbol, side, "skipped", "invalid partial close amount", contracts=contracts, live_contracts=live_contracts, partial_amount=partial_amount))
                continue
            manual_partial_history = None
            if manually_reduced_amount is not None:
                manual_partial_history = _manual_partial_close_history(
                    exchange,
                    symbol=symbol,
                    side=side,
                    entry=initial_entry,
                    amount=partial_amount,
                    contract_size=_position_contract_size(position),
                    since=_parse_exchange_time(execution.get("created_at")),
                )
            partial_event_price = _float(manual_partial_history.get("price")) if isinstance(manual_partial_history, dict) else None
            if partial_event_price is None:
                partial_event_price = mark
            partial_event_at = str(manual_partial_history.get("closed_at") or "") if isinstance(manual_partial_history, dict) else ""
            manual_partial_pnl = _float(manual_partial_history.get("pnl")) if isinstance(manual_partial_history, dict) else None
            if manually_reduced_amount is not None and not (
                (manual_partial_pnl is not None and manual_partial_pnl > 0)
                or _is_profitable_close(side, initial_entry, partial_event_price)
            ):
                skipped += 1
                rows.append(
                    _status_row(
                        symbol,
                        side,
                        "waiting",
                        "manual reduction is not profitable partial TP",
                        manual_partial_detected=True,
                        partial_price=round(partial_event_price, 8) if partial_event_price is not None else None,
                        manual_partial_pnl=manual_partial_pnl,
                        tp_progress=round(progress, 4),
                        trigger_tp_progress=trigger_progress,
                    )
                )
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
            invalid_protection_reason = None
            if not _stop_loss_trigger_valid(side, new_sl, mark):
                invalid_protection_reason = "proposed SL trigger is invalid for current mark price"
            partial_order = (
                {
                    "source": "manual_partial_detected",
                    "stored_contracts": contracts,
                    "live_contracts": live_contracts,
                    "partial_amount": partial_amount,
                    "history": manual_partial_history,
                }
                if not close_partial_on_exchange
                else _close_partial_position(exchange, config, symbol=symbol, side=side, amount=partial_amount, position=live_position)
            )
            protection_error = None
            amend_result: dict[str, Any] = {}
            if algo is None:
                protection_error = "OKX SL/TP algo order not found"
                amend_result = {"error": protection_error}
            elif invalid_protection_reason:
                protection_error = invalid_protection_reason
                amend_result = {
                    "error": protection_error,
                    "proposed_stop_loss": new_sl,
                    "mark_price": mark,
                }
            else:
                amend_result = _amend_stop_loss(exchange, symbol, algo, new_sl, settings, new_tp=new_tp)
            updated_at = datetime.now(timezone.utc).isoformat()
            updates = {
                "updated_at": updated_at,
                "initial_entry_price": initial_entry,
                "initial_stop_loss": initial_sl,
                "partial_take_profit_done": True,
                "partial_take_profit_at": updated_at,
                "partial_take_profit_fraction": float(partial_settings.get("close_fraction", 0.3) or 0.3),
                "partial_take_profit_amount": partial_amount,
                "partial_take_profit_price": partial_event_price,
                "partial_take_profit_order_json": json.dumps(partial_order, ensure_ascii=False),
                "partial_take_profit_original_tp": take_profit,
                "profit_extension_step": 0 if protection_error else 1,
                "trailing_stop_updated_at": updated_at,
                "trailing_stop_r_multiple": round(r_multiple, 6),
            }
            if protection_error:
                updates["partial_take_profit_protection_error"] = protection_error
            else:
                updates.update(
                    {
                        "stop_loss": new_sl,
                        "take_profit": new_tp if new_tp is not None else take_profit,
                        "partial_take_profit_extended_tp": new_tp,
                    }
                )
            update_trade_execution(config, int(execution["id"]), updates)
            append_trade_execution_event(
                config,
                int(execution["id"]),
                {
                    "type": "partial_close",
                    "created_at": updated_at,
                    "symbol": symbol,
                    "side": side,
                    "mark_price": mark,
                    "partial_price": partial_event_price,
                    "partial_closed_at": partial_event_at or None,
                    "close_fraction": float(partial_settings.get("close_fraction", 0.3) or 0.3),
                    "partial_amount": partial_amount,
                    "remaining_amount": max(0.0, live_contracts if manually_reduced_amount is not None else active_contracts - partial_amount),
                    "manual_partial_detected": manually_reduced_amount is not None,
                    "manual_partial_source": manual_partial_history.get("source") if isinstance(manual_partial_history, dict) else "position_size_delta",
                    "manual_partial_history": manual_partial_history,
                    "contract_size": _position_contract_size(position),
                    "old_stop_loss": current_sl,
                    "new_stop_loss": None if protection_error else new_sl,
                    "old_take_profit": take_profit,
                    "new_take_profit": None if protection_error else new_tp,
                    "protection_error": protection_error,
                    "r_multiple": round(r_multiple, 6),
                    "exchange_order": partial_order,
                    "amend_request": amend_result.get("request"),
                    "amend_error": amend_result.get("error"),
                },
            )
            notification_sent = _notify_partial_take_profit(
                config,
                {
                    "trade_execution_id": execution.get("id"),
                    "symbol": symbol,
                    "side": side,
                    "entry": initial_entry,
                    "trigger_price": partial_event_price,
                    "detected_price": mark,
                    "partial_closed_at": partial_event_at or None,
                    "close_fraction": float(partial_settings.get("close_fraction", 0.3) or 0.3),
                    "partial_amount": partial_amount,
                    "remaining_amount": max(0.0, live_contracts if manually_reduced_amount is not None else active_contracts - partial_amount),
                    "contract_size": _position_contract_size(position),
                    "manual_partial_detected": manually_reduced_amount is not None,
                    "manual_partial_source": manual_partial_history.get("source") if isinstance(manual_partial_history, dict) else "position_size_delta",
                    "manual_partial_pnl": manual_partial_history.get("pnl") if isinstance(manual_partial_history, dict) else None,
                    "old_stop_loss": current_sl,
                    "new_stop_loss": None if protection_error else new_sl,
                    "old_take_profit": take_profit,
                    "new_take_profit": None if protection_error else new_tp,
                    "protection_error": protection_error,
                    "partial_at": updated_at,
                },
            )
            partial_closed += 1
            if not protection_error:
                amended += 1
            rows.append(
                _status_row(
                    symbol,
                    side,
                    "partial_closed",
                    "partial TP closed; SL protected and TP extended",
                    tp_progress=round(progress, 4),
                    partial_amount=round(partial_amount, 8),
                    manual_partial_detected=manually_reduced_amount is not None,
                    manual_partial_source=manual_partial_history.get("source") if isinstance(manual_partial_history, dict) else "position_size_delta",
                    partial_price=round(partial_event_price, 8) if partial_event_price is not None else None,
                    partial_closed_at=partial_event_at or None,
                    new_stop_loss=None if protection_error else round(new_sl, 8),
                    new_take_profit=None if protection_error or new_tp is None else round(new_tp, 8),
                    protection_error=protection_error,
                    notification_sent=notification_sent,
                    amend_request=amend_result.get("request"),
                    amend_error=amend_result.get("error"),
                )
            )
            continue
        if partial_enabled and partial_done:
            current_step_value = _float(execution.get("profit_extension_step"))
            current_step = int(current_step_value) if current_step_value is not None else 1
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
                    if not _stop_loss_trigger_valid(side, new_sl, mark):
                        skipped += 1
                        rows.append(
                            _status_row(
                                symbol,
                                side,
                                "waiting",
                                "proposed SL trigger is invalid for current mark price",
                                mark_price=mark,
                                proposed_stop_loss=round(new_sl, 8),
                                current_stop_loss=current_sl,
                                next_step=next_step,
                            )
                        )
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
                    append_trade_execution_event(
                        config,
                        int(execution["id"]),
                        {
                            "type": "profit_step_extend",
                            "created_at": updated_at,
                            "symbol": symbol,
                            "side": side,
                            "step": next_step,
                            "mark_price": mark,
                            "old_stop_loss": current_sl,
                            "new_stop_loss": new_sl,
                            "old_take_profit": take_profit,
                            "new_take_profit": new_tp,
                            "r_multiple": round(r_multiple, 6),
                            "amend_request": amend_result.get("request"),
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
                skipped += 1
                rows.append(
                    _status_row(
                        symbol,
                        side,
                        "waiting",
                        "next profit step not reached",
                        tp_progress=round(progress, 4),
                        trigger_tp_progress=trigger_progress,
                        current_step=current_step,
                        max_steps=max_steps,
                    )
                )
                continue
            skipped += 1
            rows.append(
                _status_row(
                    symbol,
                    side,
                    "waiting",
                    "max profit steps reached",
                    current_step=current_step,
                    max_steps=max_steps,
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
        if not _stop_loss_trigger_valid(side, new_sl, mark):
            skipped += 1
            rows.append(
                _status_row(
                    symbol,
                    side,
                    "waiting",
                    "proposed SL trigger is invalid for current mark price",
                    mark_price=mark,
                    proposed_stop_loss=round(new_sl, 8),
                    current_stop_loss=current_sl,
                )
            )
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
        append_trade_execution_event(
            config,
            int(execution["id"]),
            {
                "type": "trailing_stop_update",
                "created_at": updated_at,
                "symbol": symbol,
                "side": side,
                "mark_price": mark,
                "old_stop_loss": current_sl,
                "new_stop_loss": new_sl,
                "r_multiple": round(r_multiple, 6),
                "atr": round(atr, 8),
                "amend_request": amend_result.get("request"),
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
