from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .market import create_exchange
from .storage import get_journal_state, list_trade_execution_rows, set_journal_state, update_trade_execution


STATE_KEY = "trailing_stop:last_status"


def _float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("trailing_stop", {})
    return {
        "enabled": bool(raw.get("enabled", False)),
        "activation_r_multiple": float(raw.get("activation_r_multiple", 1.0) or 1.0),
        "atr_multiplier": float(raw.get("atr_multiplier", 1.5) or 1.5),
        "atr_period": max(1, int(raw.get("atr_period", 14) or 14)),
        "atr_timeframe": str(raw.get("atr_timeframe", "1m") or "1m"),
        "min_improvement_price": max(0.0, float(raw.get("min_improvement_price", 0.0) or 0.0)),
        "trigger_price_type": str(raw.get("trigger_price_type", "last") or "last"),
        "symbol_overrides": raw.get("symbol_overrides", {}) if isinstance(raw.get("symbol_overrides"), dict) else {},
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


def _find_stop_loss_algo(exchange: Any, symbol: str, side: str, current_sl: float | None) -> dict[str, Any] | None:
    market = exchange.market(symbol) if hasattr(exchange, "market") else {"id": symbol}
    inst_id = str(market.get("id") or symbol)
    fetch_algos = getattr(exchange, "privateGetTradeOrdersAlgoPending", None)
    if not callable(fetch_algos):
        fetch_algos = getattr(exchange, "private_get_trade_orders_algo_pending", None)
    if not callable(fetch_algos):
        return None
    response = fetch_algos({"instId": inst_id})
    rows = response.get("data") if isinstance(response, dict) else response
    if not isinstance(rows, list):
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


def _amend_stop_loss(exchange: Any, symbol: str, algo: dict[str, Any], new_sl: float, settings: dict[str, Any]) -> dict[str, Any]:
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
    amend = getattr(exchange, "privatePostTradeAmendAlgos", None)
    if not callable(amend):
        amend = getattr(exchange, "private_post_trade_amend_algos", None)
    if not callable(amend):
        raise RuntimeError("OKX amend algo endpoint is unavailable")
    response = amend(payload)
    return {"request": payload, "response": response}


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
        algo = _find_stop_loss_algo(exchange, symbol, side, current_sl)
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
        if r_multiple < float(settings["activation_r_multiple"]):
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
        "skipped": skipped,
        "items": rows[-20:],
        "previous_status": get_journal_state(config, STATE_KEY),
    }
    set_journal_state(config, STATE_KEY, json.dumps({k: v for k, v in result.items() if k != "previous_status"}, ensure_ascii=False))
    return result
