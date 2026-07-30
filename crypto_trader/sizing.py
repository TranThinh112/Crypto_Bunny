from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .market import create_exchange
from .models import TradeCandidate
from .storage import get_journal_state, set_journal_state, storage_stats


STATE_KEY = "position_sizing:recovery_cycle"


def _float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _event_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _canonical_position_symbol(value: Any) -> str:
    symbol = str(value or "").strip()
    if symbol.endswith("-SWAP"):
        parts = symbol[:-5].split("-")
        if len(parts) >= 2:
            base = "-".join(parts[:-1])
            quote = parts[-1]
            return f"{base}/{quote}:{quote}"
    return symbol

def _position_key(row: dict[str, Any]) -> str:
    info = row.get("info", {}) if isinstance(row.get("info"), dict) else {}
    symbol = _canonical_position_symbol(row.get("symbol") or row.get("instId") or info.get("instId") or "")
    pos_id = row.get("id") or row.get("posId") or info.get("posId")
    updated = row.get("lastUpdateTimestamp") or row.get("uTime") or info.get("uTime") or info.get("closeTime") or row.get("timestamp") or info.get("cTime")
    return f"{symbol}:{pos_id or 'unknown'}:{updated or 'unknown'}"


def _position_symbol(row: dict[str, Any]) -> str:
    info = row.get("info", {}) if isinstance(row.get("info"), dict) else {}
    return _canonical_position_symbol(row.get("symbol") or row.get("instId") or info.get("instId") or "")


def _normalize_side(value: Any) -> str:
    side = str(value or "").strip().lower()
    if side == "long":
        return "long"
    if side == "short":
        return "short"
    return ""


def _position_side(row: dict[str, Any]) -> str:
    info = row.get("info", {}) if isinstance(row.get("info"), dict) else {}
    for value in (
        row.get("posSide"),
        info.get("posSide"),
        row.get("side"),
        info.get("side"),
        row.get("direction"),
        info.get("direction"),
    ):
        side = _normalize_side(value)
        if side:
            return side
    for value in (row.get("contracts"), row.get("contractSize"), info.get("pos"), info.get("availPos")):
        numeric = _float(value)
        if numeric is None or numeric == 0:
            continue
        return "short" if numeric < 0 else "long"
    return ""


def _position_pnl(row: dict[str, Any]) -> float | None:
    info = row.get("info", {}) if isinstance(row.get("info"), dict) else {}
    for key in ("realizedPnl", "realisedPnl", "netPnl", "netProfit"):
        value = _float(row.get(key))
        if value is not None:
            return value
    for key in ("realizedPnl", "realisedPnl", "netPnl", "netProfit"):
        value = _float(info.get(key))
        if value is not None:
            return value
    for payload in (row, info):
        value = _float(payload.get("pnl"))
        if value is not None:
            adjustments = 0.0
            adjusted = False
            for adjust_payload in (row, info):
                for key in ("fee", "fundingFee", "funding", "settledPnl"):
                    adjustment = _float(adjust_payload.get(key))
                    if adjustment is None:
                        continue
                    adjustments += adjustment
                    adjusted = True
            return round(value + adjustments, 6) if adjusted else value
    pnl = _float(row.get("upl") or info.get("upl"))
    if pnl is None:
        return None
    adjustments = 0.0
    adjusted = False
    for payload in (row, info):
        for key in ("fee", "fundingFee", "funding", "settledPnl"):
            value = _float(payload.get(key))
            if value is None:
                continue
            adjustments += value
            adjusted = True
    return round(pnl + adjustments, 6) if adjusted else pnl


def _position_time(row: dict[str, Any]) -> datetime | None:
    info = row.get("info", {}) if isinstance(row.get("info"), dict) else {}
    timestamp = row.get("lastUpdateTimestamp") or row.get("uTime") or info.get("uTime") or info.get("closeTime") or row.get("timestamp") or info.get("cTime")
    numeric = _float(timestamp)
    if numeric is not None:
        if numeric > 10_000_000_000:
            numeric = numeric / 1000
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    return _event_time(timestamp)

def _position_history_is_full_close(row: dict[str, Any]) -> bool:
    info = row.get("info", {}) if isinstance(row.get("info"), dict) else {}
    open_max = _float(row.get("openMaxPos") or info.get("openMaxPos"))
    close_total = _float(row.get("closeTotalPos") or info.get("closeTotalPos"))
    if open_max is None or close_total is None or open_max <= 0:
        return True
    return close_total + 1e-12 >= open_max

def _add_position_history_rows(rows: Any, target: list[dict[str, Any]], seen: set[str]) -> None:
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _position_key(row)
        if key in seen:
            continue
        seen.add(key)
        target.append(row)

def _fetch_positions_history_rows(exchange: Any, limit: int) -> list[dict[str, Any]]:
    fetch_raw = getattr(exchange, "privateGetAccountPositionsHistory", None)
    if not callable(fetch_raw):
        fetch_raw = getattr(exchange, "private_get_account_positions_history", None)
    if callable(fetch_raw):
        try:
            response = fetch_raw({"instType": "SWAP", "limit": str(max(1, min(int(limit or 100), 100)))})
            raw_rows = response.get("data") if isinstance(response, dict) else response
            rows: list[dict[str, Any]] = []
            _add_position_history_rows(raw_rows, rows, set())
            if rows:
                return rows
        except Exception:
            pass

    rows = []
    fetch_history = getattr(exchange, "fetch_positions_history", None)
    if callable(fetch_history):
        try:
            _add_position_history_rows(fetch_history(None, None, limit), rows, set())
        except Exception:
            pass
    return rows


def _sizing_config(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("position_sizing", {})
    leverage = float(config.get("exchange", {}).get("leverage", 1) or 1)
    return {
        "enabled": bool(raw.get("enabled", False)),
        "cycle_start_at": raw.get("cycle_start_at"),
        "base_margin_usdt": float(raw.get("base_margin_usdt", 2.0) or 2.0),
        "target_profit_usdt": float(raw.get("target_profit_usdt", 0.30) or 0.30),
        "tp_roi": float(raw.get("tp_roi", 0.75) or 0.75),
        "sl_roi": float(raw.get("sl_roi", 0.50) or 0.50),
        "open_fee": float(raw.get("open_fee", 0.0005) or 0.0005),
        "close_fee": float(raw.get("close_fee", 0.0005) or 0.0005),
        "safety_buffer": float(raw.get("safety_buffer", 0.02) or 0.02),
        "max_recovery_step": int(raw.get("max_recovery_step", 4) or 4),
        "max_margin_usdt": float(raw.get("max_margin_usdt", 20) or 20),
        "max_cycle_loss_usdt": float(raw.get("max_cycle_loss_usdt", 10) or 10),
        "hard_loss_streak_threshold": int(raw.get("hard_loss_streak_threshold", 2)),
        "hard_loss_usdt_threshold": float(raw.get("hard_loss_usdt_threshold", 10)),
        "history_limit": int(raw.get("history_limit", 100) or 100),
        "reset_orphaned_blocked_state": bool(raw.get("reset_orphaned_blocked_state", True)),
        "min_recovery_confidence": float(raw.get("min_recovery_confidence", 88) or 88),
        "min_recovery_win_probability_pct": float(raw.get("min_recovery_win_probability_pct", 58) or 58),
        "block_recovery_on_market_guard": bool(raw.get("block_recovery_on_market_guard", True)),
        "block_recovery_same_symbol_side": bool(raw.get("block_recovery_same_symbol_side", True)),
        "max_recovery_4h_rsi_long": float(raw.get("max_recovery_4h_rsi_long", 76) or 76),
        "min_recovery_4h_rsi_short": float(raw.get("min_recovery_4h_rsi_short", 24) or 24),
        "leverage": leverage,
    }


def _default_state(base_margin: float) -> dict[str, Any]:
    return {
        "cycle_start_at": None,
        "cycle_pnl_usdt": 0.0,
        "recovery_step": 0,
        "recovery_band": "normal",
        "next_margin_usdt": base_margin,
        "processed_keys": [],
        "processed_pnl_by_key": {},
        "blocked": False,
        "block_reason": None,
        "hard_started_at": None,
        "hard_start_pnl_usdt": None,
        "hard_peak_loss_usdt": None,
        "soft_return_pnl_usdt": None,
        "hard_soft_recovered_at": None,
        "last_processed_key": None,
        "last_realized_net_pnl": None,
        "last_loss_symbol": None,
        "last_loss_side": None,
        "last_loss_key": None,
        "loss_streak": 0,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _state_cycle_pnl(state: dict[str, Any]) -> float:
    try:
        return float(state.get("cycle_pnl_usdt") or 0)
    except (TypeError, ValueError):
        return 0.0


def _state_recovery_step(state: dict[str, Any]) -> int:
    try:
        return int(state.get("recovery_step") or 0)
    except (TypeError, ValueError):
        return 0


def _state_is_idle(state: dict[str, Any]) -> bool:
    return not bool(state.get("blocked")) and _state_recovery_step(state) <= 0 and abs(_state_cycle_pnl(state)) <= 1e-9


def _normalize_idle_state(state: dict[str, Any], base_margin: float) -> None:
    if not _state_is_idle(state):
        return
    state["recovery_band"] = "normal"
    state["next_margin_usdt"] = round(base_margin, 4)
    state["block_reason"] = None
    state["hard_started_at"] = None
    state["hard_start_pnl_usdt"] = None
    state["hard_peak_loss_usdt"] = None
    state["soft_return_pnl_usdt"] = None
    state["hard_soft_recovered_at"] = None

def _configured_cycle_start(settings: dict[str, Any]) -> datetime | None:
    value = settings.get("cycle_start_at")
    if not value:
        return None
    return _event_time(value)

def _cycle_start_state_value(settings: dict[str, Any]) -> str | None:
    start_at = _configured_cycle_start(settings)
    return start_at.isoformat() if start_at else None


def _has_runtime_trade_records(config: dict[str, Any]) -> bool:
    try:
        row_counts = storage_stats(config).get("row_counts", {})
    except Exception:
        return True
    for key in ("trade_executions", "pending_orders", "internal_pending_orders", "paper_trades", "trade_memory"):
        try:
            if int(row_counts.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            return True
    return False


def _reset_orphaned_blocked_state(
    config: dict[str, Any],
    state: dict[str, Any],
    base_margin: float,
) -> tuple[dict[str, Any], bool]:
    settings = config.get("position_sizing", {})
    if not bool(settings.get("reset_orphaned_blocked_state", True)):
        return state, False
    if not bool(state.get("blocked")):
        return state, False
    if _has_runtime_trade_records(config):
        return state, False
    clean = _default_state(base_margin)
    clean["auto_reset_reason"] = (
        "Blocked recovery state had no runtime trade, pending order, paper trade, or trade memory records"
    )
    return clean, True


def _load_state(config: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    base_margin = float(settings["base_margin_usdt"])
    cycle_start_value = _cycle_start_state_value(settings)
    raw = get_journal_state(config, STATE_KEY)
    if not raw:
        state = _default_state(base_margin)
        state["cycle_start_at"] = cycle_start_value
        state["_is_new"] = True
        state["_bootstrap_configured_history"] = bool(cycle_start_value)
        return state
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        state = _default_state(base_margin)
        state["cycle_start_at"] = cycle_start_value
        state["_is_new"] = True
        state["_bootstrap_configured_history"] = bool(cycle_start_value)
        return state
    default = _default_state(base_margin)
    default.update({key: value for key, value in state.items() if key in default})
    if cycle_start_value and default.get("cycle_start_at") != cycle_start_value:
        default = _default_state(base_margin)
        default["cycle_start_at"] = cycle_start_value
        default["_is_new"] = True
        default["_bootstrap_configured_history"] = True
        return default
    default["cycle_start_at"] = cycle_start_value
    if not isinstance(default.get("processed_keys"), list):
        default["processed_keys"] = []
    if not isinstance(default.get("processed_pnl_by_key"), dict):
        default["processed_pnl_by_key"] = {}
    default, was_orphaned_reset = _reset_orphaned_blocked_state(config, default, base_margin)
    _normalize_idle_state(default, base_margin)
    default["_is_new"] = was_orphaned_reset
    return default


def _save_state(config: dict[str, Any], state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    state["processed_keys"] = list(dict.fromkeys([str(item) for item in state.get("processed_keys", [])]))[-200:]
    processed = state.get("processed_pnl_by_key")
    if isinstance(processed, dict):
        keep = set(state["processed_keys"])
        state["processed_pnl_by_key"] = {str(key): value for key, value in processed.items() if str(key) in keep}
    clean_state = {key: value for key, value in state.items() if not str(key).startswith("_")}
    set_journal_state(config, STATE_KEY, json.dumps(clean_state, ensure_ascii=False))


def _closed_positions(config: dict[str, Any], settings: dict[str, Any]) -> list[dict[str, Any]]:
    if config.get("mode") == "dry_run":
        return []
    exchange = create_exchange(config, authenticated=True)
    exchange.load_markets()
    limit = int(settings["history_limit"])
    cycle_start_at = _configured_cycle_start(settings)
    rows = _fetch_positions_history_rows(exchange, limit)
    closed: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _position_key(row)
        symbol = _position_symbol(row)
        pnl = _position_pnl(row)
        closed_at = _position_time(row)
        if not key or not symbol or pnl is None:
            continue
        if not _position_history_is_full_close(row):
            continue
        if cycle_start_at is not None and (closed_at is None or closed_at < cycle_start_at):
            continue
        closed.append(
            {
                "key": key,
                "symbol": symbol,
                "side": _position_side(row),
                "pnl_usdt": pnl,
                "closed_at": closed_at,
            }
        )
    closed.sort(key=lambda item: item.get("closed_at") or datetime.min.replace(tzinfo=timezone.utc))
    return closed

def _refresh_state_from_closed_positions(
    state: dict[str, Any],
    settings: dict[str, Any],
    closed: list[dict[str, Any]],
) -> dict[str, Any]:
    base_margin = float(settings["base_margin_usdt"])
    cycle_start_at = state.get("cycle_start_at")
    state = _default_state(base_margin)
    state["cycle_start_at"] = cycle_start_at
    total_pnl = 0.0
    for row in closed:
        key = str(row["key"])
        pnl = float(row.get("pnl_usdt") or 0.0)
        total_pnl = round(total_pnl + pnl, 6)
        state["cycle_pnl_usdt"] = total_pnl
        state["last_realized_net_pnl"] = round(pnl, 6)
        state["blocked"] = False
        state["block_reason"] = None
        _set_loss_streak_fields(
            state,
            pnl,
            str(row.get("symbol") or ""),
            str(row.get("side") or ""),
            key,
        )
        hard_reason = _hard_loss_rule_reason(state, settings, total_pnl)
        if hard_reason:
            _enter_hard_recovery(state, total_pnl)
            _stop_state(state, hard_reason)
        elif state.get("recovery_band") == "hard":
            if _hard_recovery_returned_to_soft(state, total_pnl):
                state["recovery_step"] = min(
                    int(state.get("recovery_step") or 0),
                    max(0, int(settings["max_recovery_step"]) - 1),
                )
            else:
                soft_return = _float(state.get("soft_return_pnl_usdt"))
                reason = "Hard recovery remains active until 50% of the hard loss is recovered"
                if soft_return is not None:
                    reason = f"{reason}: cycle pnl {total_pnl:.4f} < soft threshold {soft_return:.4f}"
                _stop_state(state, reason)
        elif total_pnl < 0:
            state["recovery_band"] = "soft"
            state["next_margin_usdt"] = base_margin
        else:
            _clear_hard_recovery_state(state)
            state["next_margin_usdt"] = base_margin
        state["processed_keys"].append(key)
        state.setdefault("processed_pnl_by_key", {})[key] = round(pnl, 6)
        state["last_processed_key"] = key
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    return state


def _expected_net_tp(settings: dict[str, Any]) -> float:
    leverage = float(settings["leverage"] or 1)
    tp_roi = float(settings["tp_roi"])
    price_move_tp = tp_roi / max(leverage, 1e-12)
    return (
        tp_roi
        - float(settings["open_fee"]) * leverage
        - float(settings["close_fee"]) * leverage * (1 + price_move_tp)
        - float(settings["safety_buffer"])
    )


def _stop_state(state: dict[str, Any], reason: str) -> None:
    state["blocked"] = True
    state["block_reason"] = reason
    state["next_margin_usdt"] = 0.0


def _clear_hard_recovery_state(state: dict[str, Any]) -> None:
    state["recovery_band"] = "normal"
    state["hard_started_at"] = None
    state["hard_start_pnl_usdt"] = None
    state["hard_peak_loss_usdt"] = None
    state["soft_return_pnl_usdt"] = None
    state["hard_soft_recovered_at"] = None


def _set_loss_streak_fields(state: dict[str, Any], pnl: float, symbol: str, side: str, key: str) -> None:
    if pnl < 0:
        state["loss_streak"] = int(state.get("loss_streak") or 0) + 1
        state["last_loss_symbol"] = symbol or None
        state["last_loss_side"] = _normalize_side(side) or None
        state["last_loss_key"] = key or None
        return
    if pnl > 0 and state.get("recovery_band") != "hard":
        state["loss_streak"] = 0

def _enter_hard_recovery(state: dict[str, Any], cycle_pnl: float) -> None:
    now = datetime.now(timezone.utc).isoformat()
    if state.get("recovery_band") != "hard":
        state["hard_started_at"] = now
        state["hard_start_pnl_usdt"] = round(cycle_pnl, 6)
        state["hard_soft_recovered_at"] = None
    previous_peak = _float(state.get("hard_peak_loss_usdt"))
    peak = cycle_pnl if previous_peak is None or previous_peak >= 0 or cycle_pnl < previous_peak else previous_peak
    state["recovery_band"] = "hard"
    state["hard_peak_loss_usdt"] = round(peak, 6)
    state["soft_return_pnl_usdt"] = round(peak * 0.5, 6) if peak < 0 else 0.0


def _hard_recovery_returned_to_soft(state: dict[str, Any], cycle_pnl: float) -> bool:
    if state.get("recovery_band") != "hard":
        return False
    previous_peak = _float(state.get("hard_peak_loss_usdt"))
    if previous_peak is None or previous_peak >= 0:
        return False
    if cycle_pnl < previous_peak:
        state["hard_peak_loss_usdt"] = round(cycle_pnl, 6)
        state["soft_return_pnl_usdt"] = round(cycle_pnl * 0.5, 6)
        return False
    soft_return = _float(state.get("soft_return_pnl_usdt"))
    if soft_return is not None and cycle_pnl >= soft_return:
        state["recovery_band"] = "soft"
        state["hard_soft_recovered_at"] = datetime.now(timezone.utc).isoformat()
        state["blocked"] = False
        state["block_reason"] = None
        return True
    return False


def _hard_loss_rule_reason(state: dict[str, Any], settings: dict[str, Any], cycle_pnl: float) -> str | None:
    if state.get("recovery_band") == "hard":
        return None
    loss_streak_threshold = int(settings.get("hard_loss_streak_threshold") or 0)
    loss_streak = int(state.get("loss_streak") or 0)
    if loss_streak_threshold > 0 and loss_streak >= loss_streak_threshold:
        return f"Hard recovery triggered by loss streak: {loss_streak}/{loss_streak_threshold}"
    loss_threshold = float(settings.get("hard_loss_usdt_threshold") or 0)
    if loss_threshold > 0 and cycle_pnl <= -loss_threshold:
        return f"Hard recovery triggered by cycle loss: {cycle_pnl:.4f} <= -{loss_threshold:.4f}"
    return None

def _apply_realized_pnl(
    state: dict[str, Any],
    settings: dict[str, Any],
    pnl: float,
    *,
    symbol: str = "",
    side: str = "",
    key: str = "",
) -> str:
    target_profit = float(settings["target_profit_usdt"])
    base_margin = float(settings["base_margin_usdt"])
    max_step = int(settings["max_recovery_step"])
    max_cycle_loss = float(settings["max_cycle_loss_usdt"])
    max_margin = float(settings["max_margin_usdt"])

    cycle_pnl = float(state.get("cycle_pnl_usdt") or 0) + pnl
    state["cycle_pnl_usdt"] = round(cycle_pnl, 6)
    state["last_realized_net_pnl"] = round(pnl, 6)
    state["blocked"] = False
    state["block_reason"] = None
    _set_loss_streak_fields(state, pnl, symbol, side, key)

    if cycle_pnl >= target_profit:
        state["cycle_pnl_usdt"] = 0.0
        state["recovery_step"] = 0
        state["next_margin_usdt"] = base_margin
        _clear_hard_recovery_state(state)
        state["last_loss_symbol"] = None
        state["last_loss_side"] = None
        state["last_loss_key"] = None
        state["loss_streak"] = 0
        return f"Cycle target reached: pnl {cycle_pnl:.4f} >= {target_profit:.4f}; reset to base size"

    hard_reason = _hard_loss_rule_reason(state, settings, cycle_pnl)
    if hard_reason:
        _enter_hard_recovery(state, cycle_pnl)
        _stop_state(state, hard_reason)
        return str(state["block_reason"])

    if max_cycle_loss > 0 and cycle_pnl <= -max_cycle_loss:
        _enter_hard_recovery(state, cycle_pnl)
        _stop_state(state, f"Recovery cycle loss limit reached: {cycle_pnl:.4f} <= -{max_cycle_loss:.4f}")
        return str(state["block_reason"])

    recovery_step = int(state.get("recovery_step") or 0)
    returned_to_soft = _hard_recovery_returned_to_soft(state, cycle_pnl)
    if returned_to_soft:
        recovery_step = min(recovery_step, max(0, max_step - 1))
        state["recovery_step"] = recovery_step
    elif state.get("recovery_band") == "hard":
        soft_return = _float(state.get("soft_return_pnl_usdt"))
        reason = "Hard recovery remains active until 50% of the hard loss is recovered"
        if soft_return is not None:
            reason = f"{reason}: cycle pnl {cycle_pnl:.4f} < soft threshold {soft_return:.4f}"
        _stop_state(state, reason)
        return str(state["block_reason"])
    soft_after_hard_recovery = state.get("recovery_band") == "soft" and bool(state.get("hard_soft_recovered_at"))
    if recovery_step >= max_step and not soft_after_hard_recovery:
        _enter_hard_recovery(state, cycle_pnl)
        if state.get("recovery_band") == "hard":
            _stop_state(state, f"Recovery step limit reached: {recovery_step}/{max_step}")
            return str(state["block_reason"])

    expected_net_tp = _expected_net_tp(settings)
    if expected_net_tp <= 0:
        _stop_state(state, f"Expected net TP is not positive: {expected_net_tp:.4f}")
        return str(state["block_reason"])

    required_profit = target_profit - cycle_pnl
    next_margin = max(base_margin, required_profit / expected_net_tp)
    if max_margin > 0 and next_margin > max_margin:
        state["next_margin_usdt"] = round(next_margin, 4)
        _enter_hard_recovery(state, cycle_pnl)
        _stop_state(state, f"Recovery margin limit reached: {next_margin:.4f} > {max_margin:.4f}")
        return str(state["block_reason"])

    state["next_margin_usdt"] = round(next_margin, 4)
    state["recovery_step"] = recovery_step if returned_to_soft else recovery_step + 1
    soft_after_hard_recovery = state.get("recovery_band") == "soft" and bool(state.get("hard_soft_recovered_at"))
    if state["recovery_step"] >= max_step and not soft_after_hard_recovery:
        _enter_hard_recovery(state, cycle_pnl)
    elif cycle_pnl < 0:
        state["recovery_band"] = "soft"
    return (
        f"Cycle pnl {cycle_pnl:.4f}; required {required_profit:.4f}; "
        f"next margin {next_margin:.4f} USDT; step {state['recovery_step']}/{max_step}"
    )


def _update_cycle_state(config: dict[str, Any], settings: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    state = _load_state(config, settings)
    notes: list[str] = []
    try:
        closed = _closed_positions(config, settings)
    except Exception as exc:
        notes.append(f"Recovery history unavailable: {exc}")
        return state, notes

    bootstrap_configured_history = bool(state.get("_bootstrap_configured_history"))
    if (
        state.get("_is_new")
        and not bootstrap_configured_history
        and not config.get("position_sizing", {}).get("bootstrap_existing_history", False)
    ):
        state["processed_keys"] = [str(row["key"]) for row in closed]
        notes.append("Recovery cycle initialized; existing closed positions marked as processed")
        _save_state(config, state)
        return state, notes

    if config.get("position_sizing", {}).get("cycle_start_at"):
        state = _refresh_state_from_closed_positions(state, settings, closed)
        notes.append("Recovery cycle refreshed from OKX history window")
        _save_state(config, state)
        return state, notes

    processed = {str(item) for item in state.get("processed_keys", [])}
    for row in closed:
        key = str(row["key"])
        if key in processed:
            continue
        note = _apply_realized_pnl(
            state,
            settings,
            float(row["pnl_usdt"]),
            symbol=str(row.get("symbol") or ""),
            side=str(row.get("side") or ""),
            key=key,
        )
        notes.append(f"{row['symbol']} closed {float(row['pnl_usdt']):+.4f} USDT. {note}")
        state["processed_keys"].append(key)
        state.setdefault("processed_pnl_by_key", {})[key] = round(float(row["pnl_usdt"]), 6)
        state["last_processed_key"] = key
        processed.add(key)

    _save_state(config, state)
    return state, notes

def rebuild_recovery_cycle_state(config: dict[str, Any]) -> dict[str, Any]:
    settings = _sizing_config(config)
    base_margin = float(settings["base_margin_usdt"])
    state = _default_state(base_margin)
    state["cycle_start_at"] = _cycle_start_state_value(settings)
    notes: list[str] = []
    try:
        closed = _closed_positions(config, settings)
    except Exception as exc:
        state["rebuild_error"] = str(exc)
        return {"state": state, "notes": [f"Recovery history unavailable: {exc}"], "closed_count": 0}

    state = _refresh_state_from_closed_positions(state, settings, closed)
    for row in closed:
        notes.append(f"{row['symbol']} closed {float(row['pnl_usdt']):+.4f} USDT.")

    state["rebuilt_at"] = datetime.now(timezone.utc).isoformat()
    state["rebuild_closed_count"] = len(closed)
    _save_state(config, state)
    return {"state": {key: value for key, value in state.items() if key != "_is_new"}, "notes": notes, "closed_count": len(closed)}


def _candidate_4h_rsi(candidate: TradeCandidate) -> float | None:
    frames = candidate.higher_timeframes or {}
    frame = frames.get("4h") or frames.get("4H")
    if not isinstance(frame, dict):
        return None
    return _float(frame.get("rsi"))


def _recovery_active(state: dict[str, Any], margin: float, base_margin: float) -> bool:
    if bool(state.get("blocked")):
        return True
    if _state_recovery_step(state) > 0:
        return True
    return abs(_state_cycle_pnl(state)) > 1e-9

def _enforce_recovery_limits(state: dict[str, Any], settings: dict[str, Any], margin: float) -> tuple[float, bool, str]:
    cycle_pnl = _state_cycle_pnl(state)
    max_cycle_loss = float(settings["max_cycle_loss_usdt"])
    max_margin = float(settings["max_margin_usdt"])
    if max_cycle_loss > 0 and cycle_pnl <= -max_cycle_loss:
        reason = f"Recovery cycle loss limit reached: {cycle_pnl:.4f} <= -{max_cycle_loss:.4f}"
        _stop_state(state, reason)
        return 0.0, True, reason
    if max_margin > 0 and margin > max_margin:
        reason = f"Recovery margin limit reached: {margin:.4f} > {max_margin:.4f}"
        _stop_state(state, reason)
        return 0.0, True, reason
    return margin, bool(state.get("blocked")), str(state.get("block_reason") or "")


def _candidate_recovery_guard_reasons(
    candidate: TradeCandidate,
    state: dict[str, Any],
    settings: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    min_confidence = float(settings["min_recovery_confidence"])
    if candidate.confidence < min_confidence:
        reasons.append(f"Recovery confidence {candidate.confidence:.2f} below {min_confidence:.2f}")

    min_win_probability = float(settings["min_recovery_win_probability_pct"])
    if candidate.win_probability_pct is None:
        reasons.append("Recovery win probability is unavailable")
    elif candidate.win_probability_pct < min_win_probability:
        reasons.append(
            f"Recovery win probability {candidate.win_probability_pct:.2f}% below {min_win_probability:.2f}%"
        )

    if settings["block_recovery_on_market_guard"] and any("Market guard" in str(item) for item in candidate.warnings):
        reasons.append("Market Guard is reporting strong movement")

    if settings["block_recovery_same_symbol_side"]:
        last_loss_symbol = str(state.get("last_loss_symbol") or "")
        last_loss_side = _normalize_side(state.get("last_loss_side"))
        if last_loss_symbol == candidate.symbol and last_loss_side == candidate.side:
            reasons.append(f"Last loss was also {candidate.symbol} {candidate.side.upper()}")

    rsi_4h = _candidate_4h_rsi(candidate)
    if candidate.side == "long" and rsi_4h is not None and rsi_4h >= float(settings["max_recovery_4h_rsi_long"]):
        reasons.append(f"4H RSI is too hot for recovery LONG: {rsi_4h:.1f}")
    if candidate.side == "short" and rsi_4h is not None and rsi_4h <= float(settings["min_recovery_4h_rsi_short"]):
        reasons.append(f"4H RSI is too low for recovery SHORT: {rsi_4h:.1f}")

    return reasons


def apply_position_sizing(config: dict[str, Any], candidates: list[TradeCandidate]) -> dict[str, Any]:
    settings = _sizing_config(config)
    leverage = float(settings["leverage"] or 1)
    if not settings["enabled"]:
        margin = float(config.get("risk", {}).get("order_usdt", 20)) / max(leverage, 1)
        for candidate in candidates:
            candidate.margin_usdt = round(margin, 4)
            candidate.order_usdt = round(float(candidate.order_usdt), 4)
        return {
            "enabled": False,
            "base_margin_usdt": round(margin, 4),
            "order_usdt": candidates[0].order_usdt if candidates else None,
        }

    state, notes = _update_cycle_state(config, settings)
    base_margin = float(settings["base_margin_usdt"])
    margin = float(state.get("next_margin_usdt") or base_margin)
    margin, blocked, block_reason = _enforce_recovery_limits(state, settings, margin)
    if blocked:
        _save_state(config, state)

    sizing_notes = [
        f"Base margin {base_margin:.2f} USDT, leverage {leverage:.0f}x",
        f"Cycle PnL {float(state.get('cycle_pnl_usdt') or 0):+.4f} USDT",
        f"Recovery step {int(state.get('recovery_step') or 0)}/{int(settings['max_recovery_step'])}",
    ] + notes

    if blocked:
        sizing_notes.append(f"Trading stopped by recovery guard: {block_reason}")
        margin = 0.0

    notional = margin * leverage
    recovery_amount = max(0.0, margin - base_margin)
    source_key = str(state.get("last_processed_key") or "") or None
    guard_active = _recovery_active(state, margin, base_margin)
    blocked_candidates: list[dict[str, Any]] = []

    for candidate in candidates:
        candidate.margin_usdt = round(margin, 4)
        candidate.order_usdt = round(notional, 4)
        candidate.recovery_margin_usdt = round(recovery_amount, 4) if recovery_amount > 0 else None
        candidate.recovery_source_key = source_key
        candidate.sizing_notes = list(sizing_notes)
        if blocked:
            candidate.confidence = 0.0
            candidate.warnings.append(f"Recovery guard stopped trading: {block_reason}")
            continue

        if guard_active:
            guard_reasons = _candidate_recovery_guard_reasons(candidate, state, settings)
            if guard_reasons:
                blocked_candidates.append(
                    {
                        "symbol": candidate.symbol,
                        "side": candidate.side,
                        "reasons": guard_reasons,
                    }
                )
                candidate.margin_usdt = 0.0
                candidate.order_usdt = 0.0
                candidate.recovery_margin_usdt = None
                candidate.confidence = 0.0
                candidate.sizing_notes.append("Recovery guard blocked this candidate: " + " | ".join(guard_reasons))
                candidate.warnings.append("Recovery guard blocked: " + " | ".join(guard_reasons))
            else:
                candidate.sizing_notes.append("Recovery guard passed for this candidate")

    return {
        "enabled": True,
        "blocked": blocked,
        "block_reason": block_reason if blocked else None,
        "recovery_guard_active": guard_active,
        "blocked_candidates": blocked_candidates,
        "cycle_start_at": state.get("cycle_start_at"),
        "cycle_pnl_usdt": round(float(state.get("cycle_pnl_usdt") or 0), 6),
        "last_realized_net_pnl": state.get("last_realized_net_pnl"),
        "target_profit_usdt": round(float(settings["target_profit_usdt"]), 4),
        "recovery_step": int(state.get("recovery_step") or 0),
        "recovery_band": state.get("recovery_band") or "normal",
        "max_recovery_step": int(settings["max_recovery_step"]),
        "hard_start_pnl_usdt": state.get("hard_start_pnl_usdt"),
        "hard_peak_loss_usdt": state.get("hard_peak_loss_usdt"),
        "soft_return_pnl_usdt": state.get("soft_return_pnl_usdt"),
        "base_margin_usdt": round(base_margin, 4),
        "recovery_margin_usdt": round(recovery_amount, 4),
        "margin_usdt": round(margin, 4),
        "max_margin_usdt": round(float(settings["max_margin_usdt"]), 4),
        "max_cycle_loss_usdt": round(float(settings["max_cycle_loss_usdt"]), 4),
        "expected_net_tp": round(_expected_net_tp(settings), 6),
        "leverage": leverage,
        "order_usdt": round(notional, 4),
        "recovery_source_key": source_key,
        "last_loss_symbol": state.get("last_loss_symbol"),
        "last_loss_side": state.get("last_loss_side"),
        "loss_streak": int(state.get("loss_streak") or 0),
        "notes": sizing_notes,
    }
