from __future__ import annotations

import os
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
    if min_win_probability > 0:
        if candidate.win_probability_pct is None:
            reasons.append(f"Win probability is unavailable; minimum is {min_win_probability:.2f}%")
        elif candidate.win_probability_pct < min_win_probability:
            reasons.append(
                f"Win probability {candidate.win_probability_pct:.2f}% is below minimum {min_win_probability:.2f}%"
            )

    min_rr = float(strategy_config.get("min_risk_reward", 2.0))
    if candidate.risk_reward < min_rr:
        reasons.append(f"Risk/reward {candidate.risk_reward:.2f} is below minimum {min_rr:.2f}")

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

    if config["news"].get("require_symbol_news", True) and candidate.news_count <= 0:
        reasons.append("No recent symbol-specific news confirmed the setup")
    conflict_threshold = float(risk_config.get("news_conflict_threshold", 2.0))
    if candidate.side == "long" and candidate.news_score <= -conflict_threshold:
        reasons.append(f"News sentiment conflicts with LONG setup ({candidate.news_score:+.2f})")
    if candidate.side == "short" and candidate.news_score >= conflict_threshold:
        reasons.append(f"News sentiment conflicts with SHORT setup ({candidate.news_score:+.2f})")

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

    return RiskCheck(passed=not reasons, reasons=reasons, warnings=warnings)
