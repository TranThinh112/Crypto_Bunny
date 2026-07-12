from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .reporting import fetch_account_snapshot
from .storage import (
    _ensure_mongo_write_allowed,
    _mongo_collection,
    _mongo_find_many,
    _mongo_next_id,
    list_pending_orders,
)


CAPITAL_MODES = {"HEALTHY", "WARNING", "RECOVERY", "CRITICAL"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _round(value: Any, digits: int = 6) -> float:
    return round(_float(value), digits)


def _mode(value: Any) -> str:
    mode = str(value or "HEALTHY").strip().upper()
    return mode if mode in CAPITAL_MODES else "HEALTHY"


def _capital_sync_options(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("capital_sync", {})
    return {
        "enabled": bool(raw.get("enabled", True)),
        "capital_source": str(raw.get("capital_source") or "OKX"),
        "refresh_interval_seconds": max(10, _int(raw.get("refresh_interval_seconds"), 60)),
        "use_realized_capital_only": bool(raw.get("use_realized_capital_only", True)),
        "exclude_unrealized_pnl": bool(raw.get("exclude_unrealized_pnl", True)),
        "quote_currency": str(raw.get("quote_currency") or "USDT").upper(),
    }


def _capital_reserve_options(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("capital_reserve", {})
    return {
        "enabled": bool(raw.get("enabled", True)),
        "base_reserve_percent": _float(raw.get("base_reserve_percent"), 20.0),
        "warning_reserve_percent": _float(raw.get("warning_reserve_percent"), 25.0),
        "recovery_reserve_percent": _float(raw.get("recovery_reserve_percent"), 30.0),
        "critical_reserve_percent": _float(raw.get("critical_reserve_percent"), 40.0),
        "allow_reserve_usage": bool(raw.get("allow_reserve_usage", False)),
        "emergency_allow_reserve_usage": bool(raw.get("emergency_allow_reserve_usage", True)),
        "min_trading_capital": _float(raw.get("min_trading_capital"), 10.0),
    }


def _position_sizing_options(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("position_sizing", {})
    leverage = _float(config.get("exchange", {}).get("leverage"), 1.0)
    return {
        "enabled": bool(raw.get("enabled", True)),
        "normal_risk_percent": _float(raw.get("normal_risk_percent"), 1.0),
        "warning_risk_percent": _float(raw.get("warning_risk_percent"), 0.7),
        "recovery_risk_percent": _float(raw.get("recovery_risk_percent"), 0.5),
        "critical_risk_percent": _float(raw.get("critical_risk_percent"), 0.0),
        "min_order_size": _float(raw.get("min_order_size"), 1.0),
        "max_order_size_percent_of_trading_capital": _float(
            raw.get("max_order_size_percent_of_trading_capital"),
            20.0,
        ),
        "min_leverage": _float(raw.get("min_leverage"), 1.0),
        "max_leverage": _float(raw.get("max_leverage"), max(20.0, leverage)),
        "default_stop_loss_percent": _float(raw.get("default_stop_loss_percent"), _float(raw.get("sl_roi"), 0.5) * 100),
        "default_take_profit_percent": _float(raw.get("default_take_profit_percent"), _float(raw.get("tp_roi"), 0.75) * 100),
        "round_order_size_decimals": _int(raw.get("round_order_size_decimals"), 2),
        "round_margin_decimals": _int(raw.get("round_margin_decimals"), 2),
        "base_margin_usdt": _float(raw.get("base_margin_usdt"), 2.0),
        "target_profit_usdt": _float(raw.get("target_profit_usdt"), 0.30),
        "max_recovery_step": _int(raw.get("max_recovery_step"), 4),
        "leverage": leverage,
    }


def calculate_realized_capital(wallet_balance: Any, unrealized_pnl: Any) -> float:
    return _round(_float(wallet_balance) - _float(unrealized_pnl), 6)


def _unrealized_pnl_from_account(account: dict[str, Any]) -> float:
    return _round(sum(_float(position.get("pnl_usdt")) for position in account.get("positions") or []), 6)


def build_capital_snapshot(config: dict[str, Any], *, use_cache: bool = True) -> dict[str, Any]:
    options = _capital_sync_options(config)
    account = fetch_account_snapshot(config, use_cache=use_cache)
    if not account.get("ok"):
        return {
            "ok": False,
            "error": account.get("error") or "account snapshot unavailable",
            "source": options["capital_source"],
            "quote_currency": options["quote_currency"],
            "created_at": _now_iso(),
        }
    wallet_balance = _float(account.get("balance_usdt"))
    unrealized_pnl = _unrealized_pnl_from_account(account)
    realized_capital = calculate_realized_capital(wallet_balance, unrealized_pnl)
    return {
        "ok": True,
        "created_at": account.get("created_at") or _now_iso(),
        "source": options["capital_source"],
        "quote_currency": options["quote_currency"],
        "wallet_balance": _round(wallet_balance),
        "available_balance": _round(wallet_balance),
        "equity": _round(wallet_balance),
        "unrealized_pnl": unrealized_pnl,
        "realized_capital": realized_capital,
        "note": "realized_capital = wallet_balance - unrealized_pnl",
    }


def save_capital_snapshot(config: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    _ensure_mongo_write_allowed(config)
    row = dict(snapshot)
    row_id = _mongo_next_id(config, "capital_snapshots")
    row.update({"_id": row_id, "id": row_id, "created_at": row.get("created_at") or _now_iso()})
    _mongo_collection(config, "capital_snapshots").replace_one({"id": row_id}, row, upsert=True)
    return row


def latest_capital_snapshot(config: dict[str, Any]) -> dict[str, Any] | None:
    row = _mongo_collection(config, "capital_snapshots").find_one({}, sort=[("created_at", -1), ("id", -1)])
    return dict(row) if isinstance(row, dict) else None


def sync_capital_from_okx(config: dict[str, Any]) -> dict[str, Any]:
    snapshot = build_capital_snapshot(config, use_cache=False)
    if not snapshot.get("ok"):
        return snapshot
    return save_capital_snapshot(config, snapshot)


def _reserve_percent_for_mode(options: dict[str, Any], mode: str) -> float:
    return {
        "HEALTHY": options["base_reserve_percent"],
        "WARNING": options["warning_reserve_percent"],
        "RECOVERY": options["recovery_reserve_percent"],
        "CRITICAL": options["critical_reserve_percent"],
    }.get(mode, options["base_reserve_percent"])


def _used_trading_capital(config: dict[str, Any]) -> float:
    total = 0.0
    for record in list_pending_orders(config, status="ACTIVE", limit=500):
        total += _float(record.get("margin_usdt") or record.get("order_usdt"))
    return _round(total)


def infer_capital_mode(
    *,
    health: dict[str, Any] | None = None,
    risk_state: dict[str, Any] | None = None,
    sizing_state: dict[str, Any] | None = None,
) -> str:
    health = health or {}
    risk_state = risk_state or {}
    sizing_state = sizing_state or {}
    if health.get("isCritical"):
        return "CRITICAL"
    if risk_state.get("isRecoveryMode") or sizing_state.get("blocked") or _int(sizing_state.get("recovery_step")) > 0:
        return "RECOVERY"
    if health.get("isWarning"):
        return "WARNING"
    return "HEALTHY"


def calculate_capital_reserve_state(
    config: dict[str, Any],
    *,
    mode: str = "HEALTHY",
    used_trading_capital: float | None = None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    options = _capital_reserve_options(config)
    snapshot = snapshot or latest_capital_snapshot(config) or build_capital_snapshot(config, use_cache=True)
    if not snapshot or not snapshot.get("ok", True):
        return {
            "ok": False,
            "mode": _mode(mode),
            "reason": (snapshot or {}).get("error") or "capital snapshot unavailable",
        }
    mode = _mode(mode)
    realized_capital = _float(snapshot.get("realized_capital"))
    reserve_percent = _reserve_percent_for_mode(options, mode)
    reserve_amount = realized_capital * reserve_percent / 100.0
    trading_capital = realized_capital - reserve_amount
    used = _used_trading_capital(config) if used_trading_capital is None else _float(used_trading_capital)
    available = trading_capital - used
    allow_reserve_usage = bool(options["allow_reserve_usage"])
    reason = "OK"
    if trading_capital < options["min_trading_capital"]:
        reason = "Trading capital is below minimum"
    if mode == "CRITICAL" and not options["emergency_allow_reserve_usage"]:
        reason = "Critical mode disables new positions"
    return {
        "ok": True,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "mode": mode,
        "realized_capital": _round(realized_capital),
        "reserve_percent": _round(reserve_percent, 4),
        "reserve_amount": _round(reserve_amount),
        "trading_capital": _round(trading_capital),
        "used_trading_capital": _round(used),
        "available_trading_capital": _round(available),
        "allow_reserve_usage": allow_reserve_usage,
        "reason": reason,
    }


def save_capital_reserve_state(config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    _ensure_mongo_write_allowed(config)
    row = dict(state)
    row_id = _mongo_next_id(config, "capital_reserve_states")
    row.update({"_id": row_id, "id": row_id, "created_at": row.get("created_at") or _now_iso(), "updated_at": _now_iso()})
    _mongo_collection(config, "capital_reserve_states").replace_one({"id": row_id}, row, upsert=True)
    return row


def latest_capital_reserve_state(config: dict[str, Any]) -> dict[str, Any] | None:
    row = _mongo_collection(config, "capital_reserve_states").find_one({}, sort=[("created_at", -1), ("id", -1)])
    return dict(row) if isinstance(row, dict) else None


def refresh_capital_reserve_state(
    config: dict[str, Any],
    *,
    mode: str = "HEALTHY",
    used_trading_capital: float | None = None,
) -> dict[str, Any]:
    snapshot = latest_capital_snapshot(config) or sync_capital_from_okx(config)
    state = calculate_capital_reserve_state(
        config,
        mode=mode,
        used_trading_capital=used_trading_capital,
        snapshot=snapshot,
    )
    if not state.get("ok"):
        return state
    return save_capital_reserve_state(config, state)


def check_capital_allocation(config: dict[str, Any], required_margin: Any, *, mode: str = "HEALTHY") -> dict[str, Any]:
    state = latest_capital_reserve_state(config) or calculate_capital_reserve_state(config, mode=mode)
    if not state.get("ok"):
        return {"allowed": False, "reason": state.get("reason") or "capital state unavailable", **state}
    required = _float(required_margin)
    allowed = True
    reason = "OK"
    if state.get("mode") == "CRITICAL":
        allowed = False
        reason = "Critical mode: new positions are disabled"
    elif _float(state.get("trading_capital")) <= 0:
        allowed = False
        reason = "Trading capital is not positive"
    elif _float(state.get("available_trading_capital")) < required:
        allowed = False
        reason = "Insufficient trading capital after reserve protection"
    return {
        "allowed": allowed,
        "reason": reason,
        "required_margin": _round(required),
        "available_trading_capital": state.get("available_trading_capital"),
        "reserve_amount": state.get("reserve_amount"),
        "realized_capital": state.get("realized_capital"),
        "mode": state.get("mode"),
    }


def calculate_position_size(config: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    options = _position_sizing_options(config)
    mode = _mode(request.get("mode"))
    state = latest_capital_reserve_state(config) or calculate_capital_reserve_state(config, mode=mode)
    if not state.get("ok"):
        return {"allowed": False, "reason": state.get("reason") or "capital state unavailable"}
    leverage = _float(request.get("leverage"), options["leverage"])
    stop_loss_percent = _float(request.get("stop_loss_percent"), options["default_stop_loss_percent"])
    take_profit_percent = _float(request.get("take_profit_percent"), options["default_take_profit_percent"])
    risk_percent = {
        "HEALTHY": options["normal_risk_percent"],
        "WARNING": options["warning_risk_percent"],
        "RECOVERY": options["recovery_risk_percent"],
        "CRITICAL": options["critical_risk_percent"],
    }.get(mode, options["normal_risk_percent"])
    trading_capital = _float(state.get("trading_capital"))
    available = _float(state.get("available_trading_capital"))
    reason = "OK"
    allowed = True
    if mode == "CRITICAL":
        allowed = False
        reason = "Critical mode: new positions are disabled"
    if leverage < options["min_leverage"] or leverage > options["max_leverage"]:
        allowed = False
        reason = "Leverage is outside allowed range"
    if stop_loss_percent <= 0 or take_profit_percent <= 0:
        allowed = False
        reason = "Stop loss and take profit percent must be positive"
    risk_amount = trading_capital * risk_percent / 100.0
    max_by_risk = risk_amount / max(stop_loss_percent / 100.0, 1e-12)
    max_by_capital = trading_capital * options["max_order_size_percent_of_trading_capital"] / 100.0
    raw_order_size = max(0.0, min(max_by_risk, max_by_capital))
    requested = request.get("requested_order_size")
    order_size = _float(requested) if requested not in (None, "") else raw_order_size
    if requested not in (None, "") and order_size > raw_order_size:
        allowed = False
        reason = "Requested order size exceeds risk limit"
    required_margin = order_size / max(leverage, 1e-12)
    if required_margin > available:
        if requested in (None, ""):
            order_size = max(0.0, available * leverage)
            required_margin = order_size / max(leverage, 1e-12)
        else:
            allowed = False
            reason = "Insufficient trading capital after reserve protection"
    if order_size < options["min_order_size"]:
        allowed = False
        reason = "Suggested order size is below minimum"
    result = {
        "allowed": bool(allowed),
        "reason": reason,
        "created_at": _now_iso(),
        "mode": mode,
        "symbol": request.get("symbol"),
        "side": str(request.get("side") or "").upper(),
        "realized_capital": state.get("realized_capital"),
        "reserve_amount": state.get("reserve_amount"),
        "trading_capital": state.get("trading_capital"),
        "used_trading_capital": state.get("used_trading_capital"),
        "available_trading_capital": state.get("available_trading_capital"),
        "risk_percent": _round(risk_percent, 4),
        "risk_amount": _round(risk_amount),
        "stop_loss_percent": _round(stop_loss_percent, 4),
        "take_profit_percent": _round(take_profit_percent, 4),
        "leverage": _round(leverage, 4),
        "max_order_size_by_risk": _round(max_by_risk, 2),
        "max_order_size_by_capital": _round(max_by_capital, 2),
        "suggested_order_size": round(order_size, options["round_order_size_decimals"]),
        "required_margin": round(required_margin, options["round_margin_decimals"]),
        "estimated_loss": _round(order_size * stop_loss_percent / 100.0, 2),
        "estimated_profit": _round(order_size * take_profit_percent / 100.0, 2),
    }
    return result


def save_position_size_calculation(config: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    _ensure_mongo_write_allowed(config)
    row = dict(result)
    row_id = _mongo_next_id(config, "position_size_calculations")
    row.update({"_id": row_id, "id": row_id, "created_at": row.get("created_at") or _now_iso()})
    _mongo_collection(config, "position_size_calculations").replace_one({"id": row_id}, row, upsert=True)
    return row


def latest_position_size_calculation(config: dict[str, Any]) -> dict[str, Any] | None:
    row = _mongo_collection(config, "position_size_calculations").find_one({}, sort=[("created_at", -1), ("id", -1)])
    return dict(row) if isinstance(row, dict) else None


def position_size_history(config: dict[str, Any], *, limit: int = 50) -> list[dict[str, Any]]:
    return [dict(row) for row in _mongo_find_many(config, "position_size_calculations", sort=[("created_at", -1), ("id", -1)], limit=max(1, min(limit, 200)))]


def _current_trading_config(config: dict[str, Any]) -> dict[str, Any]:
    sizing = _position_sizing_options(config)
    return {
        "max_concurrent_positions": _int(config.get("risk", {}).get("max_active_trades"), 1),
        "initial_order_size": _round(_float(config.get("risk", {}).get("order_usdt"), sizing["base_margin_usdt"] * sizing["leverage"]), 2),
        "max_recovery_level": sizing["max_recovery_step"],
        "stop_loss_percent": sizing["default_stop_loss_percent"],
        "take_profit_percent": sizing["default_take_profit_percent"],
        "target_net_profit": sizing["target_profit_usdt"],
        "reserve_percent": _capital_reserve_options(config)["base_reserve_percent"],
        "leverage": sizing["leverage"],
    }


def _required_capital_for_config(cfg: dict[str, Any]) -> float:
    order_size = _float(cfg.get("initial_order_size"))
    max_positions = max(1, _int(cfg.get("max_concurrent_positions"), 1))
    levels = max(0, _int(cfg.get("max_recovery_level"), 0))
    sl_pct = max(_float(cfg.get("stop_loss_percent")), 1e-12)
    tp_pct = max(_float(cfg.get("take_profit_percent")), 1e-12)
    target = _float(cfg.get("target_net_profit"))
    per_position = order_size
    previous_losses = 0.0
    for _ in range(levels):
        previous_losses += order_size * sl_pct / 100.0
        recovery_size = (previous_losses + target) / (tp_pct / 100.0)
        per_position += recovery_size
    return _round(per_position * max_positions, 6)


def _max_safe_order_size(cfg: dict[str, Any], trading_capital: float) -> float:
    low = 0.0
    high = max(_float(cfg.get("initial_order_size")), trading_capital)
    test = dict(cfg)
    for _ in range(40):
        mid = (low + high) / 2.0
        test["initial_order_size"] = mid
        if _required_capital_for_config(test) <= trading_capital:
            low = mid
        else:
            high = mid
    return round(low, 2)


def _safety_score(trading_capital: float, required_capital: float) -> int:
    if required_capital <= 0:
        return 0
    ratio = trading_capital / required_capital
    if ratio >= 1.5:
        return 100
    if ratio >= 1.2:
        return 80
    if ratio >= 1.0:
        return 60
    if ratio >= 0.8:
        return 40
    return 20


def _risk_level(score: int) -> str:
    if score >= 85:
        return "LOW"
    if score >= 65:
        return "MEDIUM"
    if score >= 45:
        return "HIGH"
    return "CRITICAL"


def analyze_configuration_change(config: dict[str, Any], proposed_config: dict[str, Any] | None = None) -> dict[str, Any]:
    current = _current_trading_config(config)
    proposed = {**current, **(proposed_config or {})}
    snapshot = latest_capital_snapshot(config) or build_capital_snapshot(config, use_cache=True)
    realized = _float(snapshot.get("realized_capital")) if snapshot and snapshot.get("ok", True) else 0.0
    before_trading = realized * (1 - _float(current.get("reserve_percent")) / 100.0)
    after_trading = realized * (1 - _float(proposed.get("reserve_percent")) / 100.0)
    before_required = _required_capital_for_config(current)
    after_required = _required_capital_for_config(proposed)
    max_safe = _max_safe_order_size(proposed, after_trading)
    suggested = _float(proposed.get("initial_order_size"))
    if suggested > max_safe:
        suggested = max_safe
    score = _safety_score(after_trading, after_required)
    risk_level = _risk_level(score)
    warnings: list[str] = []
    recommendations: list[str] = []
    if _int(proposed.get("max_concurrent_positions")) > _int(current.get("max_concurrent_positions")):
        warnings.append("Increasing max positions can reduce safe order size.")
    if _float(proposed.get("initial_order_size")) > max_safe:
        warnings.append("Initial order size exceeds max safe order size.")
        recommendations.append(f"Reduce initial_order_size to {max_safe:.2f} USDT or lower.")
    if after_required > after_trading:
        warnings.append("Required capital after change exceeds trading capital.")
    if _float(proposed.get("reserve_percent")) < 10:
        warnings.append("Reserve percent is below 10%.")
    if _int(proposed.get("max_recovery_level")) > _int(config.get("configuration_impact", {}).get("high_recovery_level_threshold"), 4):
        warnings.append("Max recovery level is high.")
        recommendations.append("Reduce max_recovery_level.")
    if after_trading < 10:
        warnings.append("Trading capital after reserve is below 10 USDT.")
        recommendations.append("Increase realized capital before applying this config.")
    if risk_level in {"HIGH", "CRITICAL"}:
        warnings.append(f"Risk level is {risk_level}.")
    if _float(proposed.get("leverage")) > _float(config.get("configuration_impact", {}).get("high_leverage_threshold"), 20):
        warnings.append("Leverage is high.")
        recommendations.append("Reduce leverage.")
    if risk_level == "CRITICAL":
        recommendations.append("Do not apply this configuration unless force is explicitly required.")
    return {
        "created_at": _now_iso(),
        "current_config": current,
        "proposed_config": proposed,
        "realized_capital": _round(realized),
        "trading_capital_before": _round(before_trading),
        "trading_capital_after": _round(after_trading),
        "required_capital_before": _round(before_required),
        "required_capital_after": _round(after_required),
        "suggested_order_size": _round(suggested, 2),
        "max_safe_order_size": _round(max_safe, 2),
        "safety_score": score,
        "is_safe": after_required <= after_trading and risk_level != "CRITICAL",
        "risk_level": risk_level,
        "summary": (
            f"With realized capital {realized:.2f} USDT and reserve {proposed.get('reserve_percent')}%, "
            f"trading capital is {after_trading:.2f} USDT. Suggested order size is {suggested:.2f} USDT."
        ),
        "warnings": warnings,
        "recommendations": recommendations,
    }


def save_configuration_impact_report(config: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    _ensure_mongo_write_allowed(config)
    row_id = _mongo_next_id(config, "configuration_impact_reports")
    row = {
        "_id": row_id,
        "id": row_id,
        "created_at": report.get("created_at") or _now_iso(),
        "current_config_json": json.dumps(report.get("current_config") or {}, ensure_ascii=False),
        "proposed_config_json": json.dumps(report.get("proposed_config") or {}, ensure_ascii=False),
        "realized_capital": report.get("realized_capital"),
        "trading_capital_before": report.get("trading_capital_before"),
        "trading_capital_after": report.get("trading_capital_after"),
        "required_capital_before": report.get("required_capital_before"),
        "required_capital_after": report.get("required_capital_after"),
        "suggested_order_size": report.get("suggested_order_size"),
        "max_safe_order_size": report.get("max_safe_order_size"),
        "safety_score": report.get("safety_score"),
        "is_safe": report.get("is_safe"),
        "risk_level": report.get("risk_level"),
        "summary": report.get("summary"),
        "warnings_json": json.dumps(report.get("warnings") or [], ensure_ascii=False),
        "recommendations_json": json.dumps(report.get("recommendations") or [], ensure_ascii=False),
    }
    _mongo_collection(config, "configuration_impact_reports").replace_one({"id": row_id}, row, upsert=True)
    return row


def configuration_impact_history(config: dict[str, Any], *, limit: int = 50) -> list[dict[str, Any]]:
    return [dict(row) for row in _mongo_find_many(config, "configuration_impact_reports", sort=[("created_at", -1), ("id", -1)], limit=max(1, min(limit, 200)))]


def configuration_versions(config: dict[str, Any], *, limit: int = 50) -> list[dict[str, Any]]:
    return [dict(row) for row in _mongo_find_many(config, "trading_config_versions", sort=[("created_at", -1), ("id", -1)], limit=max(1, min(limit, 200)))]


def current_trading_config(config: dict[str, Any]) -> dict[str, Any]:
    row = _mongo_collection(config, "trading_config_versions").find_one({"is_active": True}, sort=[("created_at", -1), ("id", -1)])
    if isinstance(row, dict):
        return dict(row)
    return _current_trading_config(config)


def apply_trading_config(
    config: dict[str, Any],
    proposed_config: dict[str, Any],
    *,
    confirm: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    report = analyze_configuration_change(config, proposed_config)
    if not confirm:
        return {"applied": False, "reason": "confirm=false; configuration was analyzed only", "impact": report}
    if report["risk_level"] == "CRITICAL" and not force:
        return {"applied": False, "reason": "CRITICAL risk requires force=true", "impact": report}
    _ensure_mongo_write_allowed(config)
    collection = _mongo_collection(config, "trading_config_versions")
    collection.update_many({"is_active": True}, {"$set": {"is_active": False, "updated_at": _now_iso()}})
    row_id = _mongo_next_id(config, "trading_config_versions")
    row = {
        "_id": row_id,
        "id": row_id,
        "created_at": _now_iso(),
        "version": f"capital-config-v{row_id}",
        "is_active": True,
        "note": report["summary"],
        **report["proposed_config"],
    }
    collection.replace_one({"id": row_id}, row, upsert=True)
    save_configuration_impact_report(config, report)
    return {"applied": True, "config": row, "impact": report}

