from __future__ import annotations

import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .codex_features import apply_system_validation_to_candidate
from .config import project_path
from .ledger import read_events
from .market import create_exchange
from .models import RiskCheck, TradeCandidate


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _private_exchange_available(config: dict[str, Any]) -> bool:
    load_dotenv()
    key = os.getenv(config["exchange"].get("api_key_env", "OKX_API_KEY"), "")
    secret = os.getenv(config["exchange"].get("secret_env", "OKX_SECRET"), "")
    password = os.getenv(config["exchange"].get("passphrase_env", "OKX_PASSPHRASE"), "")
    return bool(key and secret and password)


def _capital_reserve_check_is_advisory(config: dict[str, Any]) -> bool:
    return str(config.get("mode") or "").strip().lower() == "dry_run" or bool(config.get("_atlas_test_mode"))


ActiveSummary = tuple[int | None, set[str], list[str]]


def _risk_settings(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("trading_risk", {}) if isinstance(config.get("trading_risk"), dict) else {}


def _market_guard_blocks_entry(candidate: TradeCandidate) -> bool:
    warnings = " | ".join(str(item) for item in candidate.warnings)
    return "avoid_new_entry" in warnings or "Bunny Health Monitor dang pause" in warnings


def _health_warning_present(candidate: TradeCandidate) -> bool:
    return any("Bunny Health Monitor dang o trang thai warning" in str(item) for item in candidate.warnings)


def _candidate_news_score(candidate: TradeCandidate) -> float:
    try:
        return float(candidate.news_score or 0.0)
    except (TypeError, ValueError):
        return 0.0

def _coerce_candidate_quality(config: dict[str, Any], candidate: TradeCandidate) -> None:
    settings = _risk_settings(config)
    fallback = settings.get("confidence_fallback", {})
    if not isinstance(fallback, dict) or not fallback.get("enabled", True):
        return
    if float(candidate.confidence or 0.0) > 0:
        return
    values: list[float] = []
    for value in (
        candidate.win_probability_pct,
        candidate.rule_score,
        getattr(candidate, "score", None),
        (candidate.decision_metadata or {}).get("entry_quality"),
        (candidate.decision_metadata or {}).get("setup_score"),
    ):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            values.append(parsed)
    if not values:
        return
    floor = float(fallback.get("floor", 55.0) or 55.0)
    ceiling = float(fallback.get("ceiling", 82.0) or 82.0)
    candidate.confidence = round(max(floor, min(max(values), ceiling)), 2)
    candidate.decision_metadata["confidence_fallback"] = {
        "applied": True,
        "source_values": [round(item, 2) for item in values[:5]],
        "confidence": candidate.confidence,
    }


def _effective_min_win_probability(
    config: dict[str, Any],
    candidate: TradeCandidate,
    base_threshold: float,
) -> tuple[float, dict[str, Any]]:
    settings = _risk_settings(config)
    dynamic = settings.get("dynamic_win_probability_thresholds", {})
    if not isinstance(dynamic, dict) or not dynamic.get("enabled", True):
        return base_threshold, {"enabled": False, "base_threshold": base_threshold, "effective_threshold": base_threshold}

    regime = str(candidate.market_regime or "").upper()
    effective = base_threshold
    reason = "base"
    if regime == "LOW_VOLATILITY":
        low_vol = dynamic.get("low_volatility", {})
        if isinstance(low_vol, dict):
            candidate_rr = float(candidate.risk_reward or 0)
            candidate_confidence = float(candidate.confidence or 0)
            required_rr = float(low_vol.get("min_risk_reward", 2.0) or 2.0)
            required_confidence = float(low_vol.get("min_confidence", 90.0) or 90.0)
            if candidate_rr >= required_rr and candidate_confidence >= required_confidence and not _market_guard_blocks_entry(candidate):
                effective = min(base_threshold, float(low_vol.get("min_win_probability_pct", 67.0) or 67.0))
                reason = "low_volatility_quality_setup"
    elif regime in {"HIGH_VOLATILITY", "VOLATILE"}:
        high_vol = dynamic.get("high_volatility", {})
        if isinstance(high_vol, dict):
            effective = max(base_threshold, float(high_vol.get("min_win_probability_pct", base_threshold) or base_threshold))
            reason = "high_volatility_protection"
    if _health_warning_present(candidate):
        effective = max(effective, float(dynamic.get("health_warning_floor_pct", base_threshold) or base_threshold))
        reason = "health_warning_floor"

    return effective, {
        "enabled": True,
        "base_threshold": base_threshold,
        "effective_threshold": effective,
        "reason": reason,
        "market_regime": candidate.market_regime,
    }


def _apply_probation_entry_if_allowed(
    config: dict[str, Any],
    candidate: TradeCandidate,
    reasons: list[str],
    threshold_meta: dict[str, Any],
) -> bool:
    settings = _risk_settings(config)
    probation = settings.get("probation_entry", {})
    if not isinstance(probation, dict) or not probation.get("enabled", False):
        return False
    if candidate.win_probability_pct is None:
        return False
    if _market_guard_blocks_entry(candidate) or _health_warning_present(candidate):
        return False
    min_win = float(probation.get("min_win_probability_pct", 66.0) or 66.0)
    min_conf = float(probation.get("min_confidence", 90.0) or 90.0)
    min_rr = float(probation.get("min_risk_reward", 2.0) or 2.0)
    if candidate.win_probability_pct < min_win or candidate.confidence < min_conf or candidate.risk_reward < min_rr:
        return False
    allowed_prefixes = (
        "Win probability ",
        "Confidence ",
        "Order size is not positive",
    )
    if any(not str(reason).startswith(allowed_prefixes) for reason in reasons):
        return False
    max_margin = float(probation.get("margin_usdt", 1.0) or 1.0)
    leverage = float(config.get("exchange", {}).get("leverage", 1) or 1)
    max_order_usdt = max_margin * max(leverage, 1.0)
    current_order = float(candidate.order_usdt or 0.0)
    scale = 1.0
    if current_order <= 0 or current_order > max_order_usdt:
        scale = max_order_usdt / max(current_order, max_order_usdt, 1e-12)
        candidate.order_usdt = max_order_usdt
        if candidate.quantity is not None and current_order > 0:
            candidate.quantity = candidate.quantity * scale
        candidate.margin_usdt = max_margin
    candidate.decision_metadata["probation_entry_sizing"] = {
        "enabled": True,
        "margin_usdt": max_margin,
        "order_usdt": max_order_usdt,
        "scale": scale,
        "reason": "small_size_probation_entry",
    }
    candidate.decision_metadata["probation_entry"] = {
        "enabled": True,
        "reason": "win_probability_below_normal_but_probation_quality_passed",
        "threshold": threshold_meta,
    }
    reasons.clear()
    return True


def mini_pending_risk_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return the validation profile used by Mini-approved pending setups."""
    pending_config = config.get("pending_orders", {})
    review_config = pending_config.get("review", {})
    risk_config = deepcopy(config)
    risk_config.setdefault("strategy", {})
    risk_config.setdefault("news", {})
    risk_config["strategy"]["min_confidence"] = float(
        review_config.get("min_confidence", risk_config["strategy"].get("min_confidence", 75)) or 75
    )
    risk_config["strategy"]["min_win_probability_pct"] = float(
        review_config.get("min_win_probability_pct", 50) or 50
    )
    risk_config["strategy"]["min_risk_reward"] = float(
        review_config.get("min_risk_reward", risk_config["strategy"].get("min_risk_reward", 1.5)) or 1.5
    )
    risk_config["news"]["require_symbol_news"] = bool(
        pending_config.get("require_symbol_news_for_mini_lc", False)
    )
    return risk_config


def active_trades_summary(config: dict[str, Any]) -> ActiveSummary:
    warnings: list[str] = []
    if config.get("mode") == "dry_run" or not _private_exchange_available(config):
        warnings.append("Private OKX checks skipped because API credentials are unavailable or mode is dry_run")
        return None, set(), warnings
    try:
        exchange = create_exchange(config, authenticated=True)
        positions = exchange.fetch_positions()
        open_positions = []
        active_symbols: set[str] = set()
        for item in positions:
            raw_size = item.get("contracts")
            if raw_size is None:
                raw_size = item.get("info", {}).get("pos")
            if raw_size is None:
                raw_size = item.get("info", {}).get("availPos")
            if abs(float(raw_size or 0)) > 0:
                open_positions.append(item)
                symbol = item.get("symbol") or item.get("info", {}).get("instId")
                if symbol:
                    active_symbols.add(str(symbol))
        orders = exchange.fetch_open_orders()
        for order in orders:
            symbol = order.get("symbol") or order.get("info", {}).get("instId")
            if symbol:
                active_symbols.add(str(symbol))
        return len(open_positions) + len(orders), active_symbols, warnings
    except Exception as exc:
        warnings.append(f"Private OKX active trade check failed: {exc}")
        return None, set(), warnings


def evaluate_candidate(
    config: dict[str, Any],
    candidate: TradeCandidate | None,
    *,
    active_summary: ActiveSummary | None = None,
    enforce_active_limit: bool = True,
    check_active_trades: bool = True,
    check_order_limits: bool = True,
    extra_active_symbols: set[str] | None = None,
) -> RiskCheck:
    if candidate is None:
        return RiskCheck(False, ["No candidate was produced"])

    _coerce_candidate_quality(config, candidate)
    risk_config = config["risk"]
    strategy_config = config["strategy"]
    execution_config = config["execution"]
    now = datetime.now(timezone.utc)
    reasons: list[str] = []
    warnings: list[str] = list(candidate.warnings)

    if float(candidate.order_usdt or 0) <= 0:
        reasons.append("Order size is not positive")
    if config.get("capital_reserve", {}).get("enabled", True):
        required_margin = candidate.margin_usdt
        if required_margin is None:
            leverage = float(config.get("exchange", {}).get("leverage", 1) or 1)
            required_margin = float(candidate.order_usdt or 0) / max(leverage, 1e-12)
        try:
            from .capital import check_capital_allocation

            allocation = check_capital_allocation(config, required_margin)
        except Exception as exc:
            allocation = {"allowed": False, "reason": f"Capital reserve check failed: {exc}"}
        if not allocation.get("allowed"):
            reason = str(allocation.get("reason") or "Insufficient trading capital after reserve protection")
            if _capital_reserve_check_is_advisory(config):
                warnings.append(f"Capital reserve check advisory: {reason}")
            else:
                reasons.append(reason)
        elif allocation.get("reason") and str(allocation.get("reason")) != "OK":
            warnings.append(str(allocation.get("reason")))

    min_confidence = float(strategy_config.get("min_confidence", 75))
    if candidate.confidence < min_confidence:
        reasons.append(f"Confidence {candidate.confidence:.2f} is below minimum {min_confidence:.2f}")

    min_win_probability = float(strategy_config.get("min_win_probability_pct", 0) or 0)
    threshold_meta: dict[str, Any] = {}
    if min_win_probability > 0:
        min_win_probability, threshold_meta = _effective_min_win_probability(config, candidate, min_win_probability)
        candidate.decision_metadata["risk_win_probability_threshold"] = threshold_meta
        if candidate.win_probability_pct is None:
            reasons.append(f"Win probability is unavailable; minimum is {min_win_probability:.2f}%")
        elif candidate.win_probability_pct < min_win_probability:
            reasons.append(
                f"Win probability {candidate.win_probability_pct:.2f}% is below minimum {min_win_probability:.2f}%"
            )

    min_rr = float(strategy_config.get("min_risk_reward", 2.0))
    rr_hard_block_enabled = bool(config.get("trading_risk", {}).get("rr_hard_block_enabled", False))
    if candidate.risk_reward < min_rr:
        message = f"Risk/reward {candidate.risk_reward:.2f} is below reference {min_rr:.2f}"
        if rr_hard_block_enabled:
            reasons.append(message)
        else:
            warnings.append(message)

    max_spread = float(risk_config.get("max_spread_pct", 0.15))
    if candidate.spread_pct is not None and candidate.spread_pct > max_spread:
        reasons.append(f"Spread {candidate.spread_pct:.4f}% exceeds maximum {max_spread:.4f}%")
    elif candidate.spread_pct is None:
        warnings.append("Spread unavailable")

    stop_distance_pct = abs(candidate.entry - candidate.stop_loss) / candidate.entry * 100
    min_stop = float(risk_config.get("min_stop_distance_pct", 0.35))
    max_stop = float(risk_config.get("max_stop_distance_pct", 3.0))
    if stop_distance_pct < min_stop:
        reasons.append(f"Stop distance {stop_distance_pct:.2f}% is below minimum {min_stop:.2f}%")
    if stop_distance_pct > max_stop:
        reasons.append(f"Stop distance {stop_distance_pct:.2f}% exceeds maximum {max_stop:.2f}%")

    conflict_threshold = float(risk_config.get("news_conflict_threshold", 2.0))
    news_score = _candidate_news_score(candidate)
    if candidate.side == "long" and news_score <= -conflict_threshold:
        warnings.append(f"News sentiment conflicts with LONG setup ({news_score:+.2f})")
    if candidate.side == "short" and news_score >= conflict_threshold:
        warnings.append(f"News sentiment conflicts with SHORT setup ({news_score:+.2f})")

    mode = config.get("mode", "dry_run")
    if mode == "live":
        live_confirm = project_path(config, execution_config.get("live_confirm_file", ".allow-live-trading"))
        if not execution_config.get("enable_live", False):
            reasons.append("Live mode blocked because execution.enable_live is false")
        if not Path(live_confirm).exists():
            reasons.append(f"Live mode blocked because {live_confirm} does not exist")

    if check_order_limits:
        events = read_events(config)
        today_events: list[dict[str, Any]] = []
        last_trade_at: datetime | None = None
        for event in events:
            created_at = _parse_time(str(event.get("created_at", "")))
            if not created_at:
                continue
            if created_at.date() == now.date() and event.get("submitted"):
                today_events.append(event)
            if event.get("submitted") and (last_trade_at is None or created_at > last_trade_at):
                last_trade_at = created_at

        cooldown_minutes = int(risk_config.get("cooldown_minutes", 60))
        if last_trade_at and now - last_trade_at < timedelta(minutes=cooldown_minutes):
            remaining = timedelta(minutes=cooldown_minutes) - (now - last_trade_at)
            reasons.append(f"Cooldown active for another {int(remaining.total_seconds() // 60)} minute(s)")

        max_daily_orders = int(risk_config.get("max_daily_orders", 3))
        if len(today_events) >= max_daily_orders:
            reasons.append(f"Daily order limit reached: {len(today_events)}/{max_daily_orders}")

        planned_risk_today = sum(float(event.get("planned_risk_usdt", 0)) for event in today_events)
        max_daily_risk = float(risk_config.get("max_daily_planned_risk_usdt", 10))
        if planned_risk_today + candidate.planned_risk_usdt > max_daily_risk:
            reasons.append(
                f"Daily planned risk would be {planned_risk_today + candidate.planned_risk_usdt:.2f} USDT, above {max_daily_risk:.2f}"
            )

    if check_active_trades:
        if active_summary is None:
            active_count, active_symbols, private_warnings = active_trades_summary(config)
        else:
            active_count, active_symbols, private_warnings = active_summary
        if extra_active_symbols:
            active_symbols = set(active_symbols) | set(extra_active_symbols)
        warnings.extend(private_warnings)
        max_active = int(risk_config.get("max_active_trades", 1))
        if enforce_active_limit and active_count is not None and active_count >= max_active:
            reasons.append(f"Active trade limit reached: {active_count}/{max_active}")
        if active_count is not None and candidate.symbol in active_symbols:
            reasons.append(f"Active OKX position/order already exists for {candidate.symbol}")
        if mode in {"demo", "live"} and active_count is None:
            reasons.append("Cannot verify active OKX positions/orders")

    system_reasons, system_warnings = apply_system_validation_to_candidate(config, candidate)
    reasons.extend(system_reasons)
    warnings.extend(system_warnings)
    _apply_probation_entry_if_allowed(config, candidate, reasons, threshold_meta)

    return RiskCheck(passed=not reasons, reasons=reasons, warnings=warnings)
