from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .market import create_exchange
from .models import TradeCandidate
from .storage import get_journal_state, set_journal_state


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


def _position_key(row: dict[str, Any]) -> str:
    info = row.get("info", {}) if isinstance(row.get("info"), dict) else {}
    symbol = row.get("symbol") or info.get("instId") or ""
    pos_id = row.get("id") or info.get("posId")
    updated = row.get("timestamp") or info.get("uTime") or info.get("cTime") or info.get("closeTime")
    return f"{symbol}:{pos_id or updated or 'unknown'}"


def _position_symbol(row: dict[str, Any]) -> str:
    info = row.get("info", {}) if isinstance(row.get("info"), dict) else {}
    return str(row.get("symbol") or info.get("instId") or "")


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
    for key in ("pnl", "realizedPnl", "realisedPnl", "upl"):
        value = _float(row.get(key))
        if value is not None:
            return value
    for key in ("pnl", "realizedPnl", "realisedPnl"):
        value = _float(info.get(key))
        if value is not None:
            return value
    return None


def _position_time(row: dict[str, Any]) -> datetime | None:
    info = row.get("info", {}) if isinstance(row.get("info"), dict) else {}
    timestamp = row.get("timestamp") or info.get("uTime") or info.get("cTime") or info.get("closeTime")
    numeric = _float(timestamp)
    if numeric is not None:
        if numeric > 10_000_000_000:
            numeric = numeric / 1000
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    return _event_time(timestamp)


def _sizing_config(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("position_sizing", {})
    leverage = float(config.get("exchange", {}).get("leverage", 1) or 1)
    return {
        "enabled": bool(raw.get("enabled", False)),
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
        "history_limit": int(raw.get("history_limit", 100) or 100),
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
        "cycle_pnl_usdt": 0.0,
        "recovery_step": 0,
        "next_margin_usdt": base_margin,
        "processed_keys": [],
        "blocked": False,
        "block_reason": None,
        "last_processed_key": None,
        "last_realized_net_pnl": None,
        "last_loss_symbol": None,
        "last_loss_side": None,
        "last_loss_key": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _load_state(config: dict[str, Any], base_margin: float) -> dict[str, Any]:
    raw = get_journal_state(config, STATE_KEY)
    if not raw:
        state = _default_state(base_margin)
        state["_is_new"] = True
        return state
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        state = _default_state(base_margin)
        state["_is_new"] = True
        return state
    default = _default_state(base_margin)
    default.update({key: value for key, value in state.items() if key in default})
    if not isinstance(default.get("processed_keys"), list):
        default["processed_keys"] = []
    default["_is_new"] = False
    return default


def _save_state(config: dict[str, Any], state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    state["processed_keys"] = list(dict.fromkeys([str(item) for item in state.get("processed_keys", [])]))[-200:]
    clean_state = {key: value for key, value in state.items() if key != "_is_new"}
    set_journal_state(config, STATE_KEY, json.dumps(clean_state, ensure_ascii=False))


def _closed_positions(config: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    if config.get("mode") == "dry_run":
        return []
    exchange = create_exchange(config, authenticated=True)
    exchange.load_markets()
    rows = exchange.fetch_positions_history(None, None, limit)
    closed: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _position_key(row)
        symbol = _position_symbol(row)
        pnl = _position_pnl(row)
        if not key or not symbol or pnl is None:
            continue
        closed.append(
            {
                "key": key,
                "symbol": symbol,
                "side": _position_side(row),
                "pnl_usdt": pnl,
                "closed_at": _position_time(row),
            }
        )
    closed.sort(key=lambda item: item.get("closed_at") or datetime.min.replace(tzinfo=timezone.utc))
    return closed


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

    cycle_pnl = float(state.get("cycle_pnl_usdt") or 0) + pnl
    state["cycle_pnl_usdt"] = round(cycle_pnl, 6)
    state["last_realized_net_pnl"] = round(pnl, 6)
    state["blocked"] = False
    state["block_reason"] = None
    if pnl < 0:
        state["last_loss_symbol"] = symbol or None
        state["last_loss_side"] = _normalize_side(side) or None
        state["last_loss_key"] = key or None

    if cycle_pnl >= target_profit:
        state["cycle_pnl_usdt"] = 0.0
        state["recovery_step"] = 0
        state["next_margin_usdt"] = base_margin
        state["last_loss_symbol"] = None
        state["last_loss_side"] = None
        state["last_loss_key"] = None
        return f"Cycle target reached: pnl {cycle_pnl:.4f} >= {target_profit:.4f}; reset to base size"

    recovery_step = int(state.get("recovery_step") or 0)
    if recovery_step >= max_step:
        _stop_state(state, f"Recovery step limit reached: {recovery_step}/{max_step}")
        return str(state["block_reason"])

    expected_net_tp = _expected_net_tp(settings)
    if expected_net_tp <= 0:
        _stop_state(state, f"Expected net TP is not positive: {expected_net_tp:.4f}")
        return str(state["block_reason"])

    required_profit = target_profit - cycle_pnl
    next_margin = max(base_margin, required_profit / expected_net_tp)
    state["next_margin_usdt"] = round(next_margin, 4)
    state["recovery_step"] = recovery_step + 1
    return (
        f"Cycle pnl {cycle_pnl:.4f}; required {required_profit:.4f}; "
        f"next margin {next_margin:.4f} USDT; step {state['recovery_step']}/{max_step}"
    )


def _update_cycle_state(config: dict[str, Any], settings: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    state = _load_state(config, float(settings["base_margin_usdt"]))
    notes: list[str] = []
    try:
        closed = _closed_positions(config, int(settings["history_limit"]))
    except Exception as exc:
        notes.append(f"Recovery history unavailable: {exc}")
        return state, notes

    if state.get("_is_new") and not config.get("position_sizing", {}).get("bootstrap_existing_history", False):
        state["processed_keys"] = [str(row["key"]) for row in closed]
        notes.append("Recovery cycle initialized; existing closed positions marked as processed")
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
        state["last_processed_key"] = key
        processed.add(key)

    _save_state(config, state)
    return state, notes


def _candidate_4h_rsi(candidate: TradeCandidate) -> float | None:
    frames = candidate.higher_timeframes or {}
    frame = frames.get("4h") or frames.get("4H")
    if not isinstance(frame, dict):
        return None
    return _float(frame.get("rsi"))


def _recovery_active(state: dict[str, Any], margin: float, base_margin: float) -> bool:
    if bool(state.get("blocked")):
        return True
    if int(state.get("recovery_step") or 0) > 0:
        return True
    if margin > base_margin + 1e-9:
        return True
    return abs(float(state.get("cycle_pnl_usdt") or 0)) > 1e-9


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
    blocked = bool(state.get("blocked"))
    block_reason = str(state.get("block_reason") or "")

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
        "cycle_pnl_usdt": round(float(state.get("cycle_pnl_usdt") or 0), 6),
        "target_profit_usdt": round(float(settings["target_profit_usdt"]), 4),
        "recovery_step": int(state.get("recovery_step") or 0),
        "max_recovery_step": int(settings["max_recovery_step"]),
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
        "notes": sizing_notes,
    }
