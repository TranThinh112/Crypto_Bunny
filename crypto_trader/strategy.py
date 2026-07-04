from __future__ import annotations

from typing import Any

from .market import amount_for_notional, create_exchange
from .models import MarketSnapshot, NewsDigest, TradeCandidate
from .news import base_from_symbol


def _round_price(value: float) -> float:
    if value >= 1000:
        return round(value, 2)
    if value >= 1:
        return round(value, 4)
    return round(value, 8)


def _estimate_win_probability(
    side: str,
    confidence: float,
    risk_reward: float,
    news_score: float,
    news_count: int,
    warnings: list[str],
) -> float:
    break_even = 100 / (1 + max(risk_reward, 0.01))
    confidence_edge = (confidence - 60) * 0.65
    news_aligned = (side == "long" and news_score > 0) or (side == "short" and news_score < 0)
    if news_count <= 0:
        news_edge = -1.0
    elif news_aligned:
        news_edge = min(abs(news_score), 5.0) * 0.7
    else:
        news_edge = -min(abs(news_score), 5.0) * 0.7
    warning_penalty = min(5.0, len(warnings) * 1.5)
    estimate = break_even + confidence_edge + news_edge - warning_penalty
    return round(max(25.0, min(80.0, estimate)), 2)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _market_context(snapshots: list[MarketSnapshot], strategy_config: dict[str, Any]) -> dict[str, float | str]:
    if not snapshots:
        return {
            "breadth": 0.5,
            "momentum": 0.5,
            "btc_trend": 0.0,
            "long_bias_points": 0.0,
            "regime_points": 0.0,
            "label": "neutral",
        }

    bias_config = strategy_config.get("long_short_bias", {})
    target_long_ratio = _clamp(float(bias_config.get("target_long_ratio", 0.6) or 0.6), 0.4, 0.75)
    strength = _clamp(float(bias_config.get("strength", 10) or 10), 0, 20)
    trend_count = sum(1 for item in snapshots if item.last >= item.ema_slow)
    momentum_count = sum(1 for item in snapshots if item.ema_fast >= item.ema_slow)
    breadth = trend_count / len(snapshots)
    momentum = momentum_count / len(snapshots)
    btc = next((item for item in snapshots if base_from_symbol(item.symbol) == "BTC"), None)
    btc_trend = 0.0
    if btc and btc.ema_slow:
        btc_trend = ((btc.last - btc.ema_slow) / btc.ema_slow) * 100

    long_bias_points = (target_long_ratio - 0.5) * strength * 5
    regime_raw = ((breadth + momentum) / 2 - 0.5) * 14 + _clamp(btc_trend, -3, 3)
    regime_points = _clamp(regime_raw, -8, 8)
    if regime_points >= 3:
        label = "bullish"
    elif regime_points <= -3:
        label = "bearish"
    else:
        label = "neutral"
    return {
        "breadth": round(breadth, 4),
        "momentum": round(momentum, 4),
        "btc_trend": round(btc_trend, 4),
        "long_bias_points": round(long_bias_points, 4),
        "regime_points": round(regime_points, 4),
        "label": label,
    }


def _guard_adjustment_note(layer: dict[str, Any], label: str) -> str:
    return (
        f"Market guard {label}: action={layer.get('action', 'normal')}, "
        f"risk={float(layer.get('risk_score') or 0):.1f}, "
        f"alerts={int(layer.get('alert_count') or 0)}, "
        f"vol={float(layer.get('max_volume_ratio') or 0):.2f}x, "
        f"move={float(layer.get('window_move_pct') or 0):+.2f}%"
    )


def _apply_market_guard_context(
    long_score: float,
    short_score: float,
    long_adjustments: list[str],
    short_adjustments: list[str],
    warnings: list[str],
    layer: dict[str, Any],
) -> tuple[float, float]:
    layer_5m = layer.get("layer_5m") or {}
    layer_20m = layer.get("layer_20m") or {}
    if not layer_5m.get("sample_count") and not layer_20m.get("sample_count"):
        return long_score, short_score

    penalty = 0.0
    action_5m = str(layer_5m.get("action") or "normal")
    action_20m = str(layer_20m.get("action") or "normal")
    if action_5m == "avoid_new_entry":
        penalty += 12.0
        warnings.append(_guard_adjustment_note(layer_5m, "5m"))
    elif action_5m == "wait_confirmation":
        penalty += 5.0
        warnings.append(_guard_adjustment_note(layer_5m, "5m"))

    if action_20m == "avoid_new_entry":
        penalty += 8.0
        warnings.append(_guard_adjustment_note(layer_20m, "20m"))
    elif action_20m == "wait_confirmation":
        penalty += 3.0
        warnings.append(_guard_adjustment_note(layer_20m, "20m"))

    if penalty:
        long_score -= penalty
        short_score -= penalty
        long_adjustments.append(f"Market guard reduces entry score by {penalty:.1f} point(s)")
        short_adjustments.append(f"Market guard reduces entry score by {penalty:.1f} point(s)")

    direction = str(layer_20m.get("direction") or layer_5m.get("direction") or "neutral")
    if penalty < 10 and direction == "up":
        long_score += 2.0
        short_score -= 1.0
        long_adjustments.append("Market guard 20m flow is upward")
    elif penalty < 10 and direction == "down":
        short_score += 2.0
        long_score -= 1.0
        short_adjustments.append("Market guard 20m flow is downward")
    return long_score, short_score


def _frame_float(frame: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(frame.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _apply_higher_timeframe_context(
    snapshot: MarketSnapshot,
    strategy_config: dict[str, Any],
    long_score: float,
    short_score: float,
    long_adjustments: list[str],
    short_adjustments: list[str],
    warnings: list[str],
) -> tuple[float, float]:
    config = strategy_config.get("confirmation_timeframes", {})
    if isinstance(config, dict) and not config.get("enabled", True):
        return long_score, short_score

    weights = {"15m": 4.0, "1h": 6.5, "4h": 9.0}
    raw_weights = config.get("weights", {}) if isinstance(config, dict) else {}
    if isinstance(raw_weights, dict):
        for frame, value in raw_weights.items():
            try:
                weights[str(frame)] = float(value)
            except (TypeError, ValueError):
                continue

    for frame_name, frame in (snapshot.higher_timeframes or {}).items():
        if not isinstance(frame, dict):
            continue
        weight = weights.get(str(frame_name), 4.0)
        trend = str(frame.get("trend") or "mixed")
        rsi_value = _frame_float(frame, "rsi", 50.0)
        range_position = _frame_float(frame, "range_position", 0.5)
        ema_gap = _frame_float(frame, "ema_gap_pct")
        price_vs_ema = _frame_float(frame, "price_vs_ema_slow_pct")
        label = str(frame_name).upper()

        if trend == "up":
            long_score += weight
            short_score -= weight * 0.7
            long_adjustments.append(
                f"{label} trend confirms long (EMA gap {ema_gap:+.2f}%, price vs EMA50 {price_vs_ema:+.2f}%)"
            )
        elif trend == "down":
            short_score += weight
            long_score -= weight * 0.7
            short_adjustments.append(
                f"{label} trend confirms short (EMA gap {ema_gap:+.2f}%, price vs EMA50 {price_vs_ema:+.2f}%)"
            )
        else:
            warnings.append(f"{label} trend is mixed")

        if trend == "up" and rsi_value >= 78 and range_position >= 0.82:
            penalty = weight * 0.65
            long_score -= penalty
            long_adjustments.append(
                f"{label} is hot: RSI {rsi_value:.1f}, range {range_position * 100:.0f}%"
            )
        if trend == "down" and rsi_value <= 28 and range_position <= 0.18:
            penalty = weight * 0.65
            short_score -= penalty
            short_adjustments.append(
                f"{label} is oversold: RSI {rsi_value:.1f}, range {range_position * 100:.0f}%"
            )

        if str(frame_name) == "4h":
            if trend == "down" and snapshot.last > snapshot.ema_fast and snapshot.volume_ratio >= 1.15:
                warnings.append("15m bounce is fighting the 4H downtrend")
            if trend == "up" and snapshot.last < snapshot.ema_fast and snapshot.volume_ratio >= 1.15:
                warnings.append("15m pullback is fighting the 4H uptrend")

    return long_score, short_score


def _pattern_names(patterns: dict[str, Any]) -> str:
    raw = patterns.get("patterns")
    if not isinstance(raw, list) or not raw:
        return "none"
    return ", ".join(str(item) for item in raw[:4])


def _apply_candlestick_context(
    snapshot: MarketSnapshot,
    strategy_config: dict[str, Any],
    long_score: float,
    short_score: float,
    long_adjustments: list[str],
    short_adjustments: list[str],
    warnings: list[str],
) -> tuple[float, float]:
    config = strategy_config.get("candlestick_patterns", {})
    if isinstance(config, dict) and not config.get("enabled", True):
        return long_score, short_score

    weights = {"1m": 3.0, "5m": 4.5, "15m": 6.5, "1h": 8.5, "4h": 10.0}
    raw_weights = config.get("weights", {}) if isinstance(config, dict) else {}
    if isinstance(raw_weights, dict):
        for frame, value in raw_weights.items():
            try:
                weights[str(frame)] = float(value)
            except (TypeError, ValueError):
                continue

    frames: dict[str, dict[str, Any]] = {}
    for frame_name, pattern_data in (snapshot.candlestick_patterns or {}).items():
        if isinstance(pattern_data, dict):
            frames[str(frame_name)] = pattern_data
    for frame_name, frame in (snapshot.higher_timeframes or {}).items():
        pattern_data = frame.get("candlestick_patterns") if isinstance(frame, dict) else None
        if isinstance(pattern_data, dict):
            frames[str(frame_name)] = pattern_data

    for frame_name, pattern_data in frames.items():
        bullish = float(pattern_data.get("bullish_score") or 0)
        bearish = float(pattern_data.get("bearish_score") or 0)
        weight = weights.get(frame_name, 4.0)
        if bullish > bearish:
            boost = min(weight, (bullish - bearish) * weight / 3.0)
            long_score += boost
            short_score -= boost * 0.45
            long_adjustments.append(
                f"{frame_name.upper()} candlestick supports LONG: {_pattern_names(pattern_data)}"
            )
            if pattern_data.get("signal_summary"):
                long_adjustments.append(f"{frame_name.upper()} candlestick lesson: {pattern_data.get('signal_summary')}")
        elif bearish > bullish:
            boost = min(weight, (bearish - bullish) * weight / 3.0)
            short_score += boost
            long_score -= boost * 0.45
            short_adjustments.append(
                f"{frame_name.upper()} candlestick supports SHORT: {_pattern_names(pattern_data)}"
            )
            if pattern_data.get("signal_summary"):
                short_adjustments.append(f"{frame_name.upper()} candlestick lesson: {pattern_data.get('signal_summary')}")
        elif "doji" in pattern_data.get("patterns", []):
            warnings.append(f"{frame_name.upper()} doji shows hesitation")

    return long_score, short_score


def _indicator_summary(snapshot: MarketSnapshot) -> dict[str, Any]:
    return {
        "last": snapshot.last,
        "ema_fast": snapshot.ema_fast,
        "ema_slow": snapshot.ema_slow,
        "rsi": snapshot.rsi,
        "atr": snapshot.atr,
        "atr_pct": snapshot.atr_pct,
        "volume_ratio": snapshot.volume_ratio,
        "support": snapshot.support,
        "resistance": snapshot.resistance,
        "spread_pct": snapshot.spread_pct,
        "candlestick_patterns": snapshot.candlestick_patterns,
        "higher_timeframes": snapshot.higher_timeframes,
    }


def _candidate_for_side(
    snapshot: MarketSnapshot,
    base: str,
    side: str,
    score: float,
    news_score: float,
    news_count: int,
    order_usdt: float,
    min_rr: float,
    target_config: dict[str, Any],
    leverage: float,
    reasons: list[str],
    warnings: list[str],
) -> TradeCandidate:
    entry = snapshot.last
    target_mode = str(target_config.get("mode", "atr_rr"))
    take_profit_pct = float(target_config.get("take_profit_pct", 0) or 0)
    stop_loss_pct = float(target_config.get("stop_loss_pct", 0) or 0)
    price_take_profit_pct: float | None = None
    price_stop_loss_pct: float | None = None

    if target_mode in {"roi_percent", "price_percent"} and take_profit_pct > 0 and stop_loss_pct > 0:
        divisor = max(leverage, 1.0) if target_mode == "roi_percent" else 1.0
        price_take_profit_pct = take_profit_pct / divisor
        price_stop_loss_pct = stop_loss_pct / divisor
        if side == "long":
            stop = entry * (1 - price_stop_loss_pct / 100)
            take_profit = entry * (1 + price_take_profit_pct / 100)
        else:
            stop = entry * (1 + price_stop_loss_pct / 100)
            take_profit = max(entry * (1 - price_take_profit_pct / 100), 1e-12)
        risk = abs(entry - stop)
        reward = abs(take_profit - entry)
        rr = reward / max(risk, 1e-12)
        reasons.append(
            f"TP/SL target: TP {take_profit_pct:.0f}%, SL {stop_loss_pct:.0f}% "
            f"({target_mode}, {leverage:.0f}x)"
        )
    else:
        minimum_stop = entry * 0.006
        atr_stop = max(snapshot.atr * 1.4, minimum_stop)
        if side == "long":
            stop = min(entry - atr_stop, snapshot.support * 0.998)
            risk = entry - stop
            take_profit = entry + risk * min_rr
        else:
            stop = max(entry + atr_stop, snapshot.resistance * 1.002)
            risk = stop - entry
            take_profit = entry - risk * min_rr
        rr = abs(take_profit - entry) / max(abs(entry - stop), 1e-12)

    confidence = max(0.0, min(100.0, score))
    win_probability_pct = _estimate_win_probability(
        side,
        confidence,
        rr,
        news_score,
        news_count,
        warnings,
    )
    return TradeCandidate(
        symbol=snapshot.symbol,
        base=base,
        side=side,  # type: ignore[arg-type]
        confidence=round(confidence, 2),
        entry=_round_price(entry),
        stop_loss=_round_price(stop),
        take_profit=_round_price(take_profit),
        risk_reward=round(rr, 2),
        order_usdt=order_usdt,
        quantity=None,
        spread_pct=round(snapshot.spread_pct, 4) if snapshot.spread_pct is not None else None,
        news_score=news_score,
        news_count=news_count,
        higher_timeframes=snapshot.higher_timeframes,
        indicator_summary=_indicator_summary(snapshot),
        candlestick_patterns=snapshot.candlestick_patterns,
        rule_score=round(score, 2),
        win_probability_pct=win_probability_pct,
        target_mode=target_mode,
        take_profit_pct=take_profit_pct if take_profit_pct > 0 else None,
        stop_loss_pct=stop_loss_pct if stop_loss_pct > 0 else None,
        price_take_profit_pct=round(price_take_profit_pct, 4) if price_take_profit_pct is not None else None,
        price_stop_loss_pct=round(price_stop_loss_pct, 4) if price_stop_loss_pct is not None else None,
        reasons=reasons,
        warnings=warnings,
    )


def build_candidates(
    config: dict[str, Any],
    snapshots: list[MarketSnapshot],
    digest: NewsDigest,
    limit: int | None = 5,
    market_layers: dict[str, dict[str, Any]] | None = None,
) -> list[TradeCandidate]:
    risk_config = config["risk"]
    strategy_config = config["strategy"]
    min_rr = float(strategy_config.get("min_risk_reward", 2.0))
    target_config = strategy_config.get("target", {})
    leverage = float(config.get("exchange", {}).get("leverage", 1) or 1)
    order_usdt = float(risk_config.get("order_usdt", 20))
    candidates: list[TradeCandidate] = []
    context = _market_context(snapshots, strategy_config)
    bias_config = strategy_config.get("long_short_bias", {})
    bias_enabled = bool(bias_config.get("enabled", True))
    long_bias_points = float(context["long_bias_points"]) if bias_enabled else 0.0
    regime_points = float(context["regime_points"])
    market_label = str(context["label"])

    for snapshot in snapshots:
        base = base_from_symbol(snapshot.symbol)
        news_score = float(digest.by_symbol_score.get(base, 0.0))
        news_count = int(digest.by_symbol_count.get(base, 0))
        long_score = 35.0
        short_score = 35.0
        long_reasons: list[str] = []
        short_reasons: list[str] = []
        warnings: list[str] = []
        long_adjustments: list[str] = []
        short_adjustments: list[str] = []

        if bias_enabled and long_bias_points:
            long_score += long_bias_points
            short_score -= long_bias_points * 0.55
            long_adjustments.append(f"Strategic long bias target 60/40 adds {long_bias_points:.1f} point(s)")
            short_adjustments.append(f"Strategic long bias requires stronger short evidence ({long_bias_points:.1f} point edge)")

        if regime_points > 0:
            long_score += regime_points
            short_score -= regime_points * 0.4
            long_adjustments.append(f"Market regime is {market_label}: breadth {float(context['breadth']) * 100:.0f}% favors longs")
        elif regime_points < 0:
            short_score += abs(regime_points) * 0.85
            long_score -= abs(regime_points) * 0.35
            short_adjustments.append(f"Market regime is {market_label}: breadth {float(context['breadth']) * 100:.0f}% allows shorts")

        if snapshot.last > snapshot.ema_slow:
            long_score += 15
            long_reasons.append("Price is above EMA50")
        else:
            short_score += 15
            short_reasons.append("Price is below EMA50")

        if snapshot.ema_fast > snapshot.ema_slow:
            long_score += 12
            long_reasons.append("EMA20 is above EMA50")
        else:
            short_score += 12
            short_reasons.append("EMA20 is below EMA50")

        ema_gap_pct = ((snapshot.ema_fast - snapshot.ema_slow) / snapshot.ema_slow) * 100 if snapshot.ema_slow else 0.0
        price_gap_pct = ((snapshot.last - snapshot.ema_fast) / snapshot.ema_fast) * 100 if snapshot.ema_fast else 0.0
        if ema_gap_pct > 0.15 and price_gap_pct > -0.3:
            long_score += min(6.0, ema_gap_pct * 4)
            long_reasons.append(f"EMA trend quality favors long ({ema_gap_pct:+.2f}% gap)")
        elif ema_gap_pct < -0.15 and price_gap_pct < 0.3:
            short_score += min(6.0, abs(ema_gap_pct) * 4)
            short_reasons.append(f"EMA trend quality favors short ({ema_gap_pct:+.2f}% gap)")

        range_size = max(snapshot.resistance - snapshot.support, 1e-12)
        range_position = (snapshot.last - snapshot.support) / range_size
        if 0.25 <= range_position <= 0.7 and snapshot.last >= snapshot.ema_slow:
            long_score += 4
            long_reasons.append(f"Price is in a healthier long zone ({range_position * 100:.0f}% of support-resistance range)")
        elif range_position >= 0.82 and snapshot.rsi > 68:
            long_score -= 5
            long_reasons.append("Long entry is near resistance with elevated RSI")
        if range_position <= 0.18 and snapshot.rsi < 38:
            short_score -= 5
            short_reasons.append("Short entry is near support with low RSI")

        if 50 <= snapshot.rsi <= 68:
            long_score += 10
            long_reasons.append(f"RSI is constructive at {snapshot.rsi:.1f}")
        elif 32 <= snapshot.rsi <= 50:
            short_score += 10
            short_reasons.append(f"RSI is weak at {snapshot.rsi:.1f}")
        elif snapshot.rsi > 76:
            short_score += 5
            short_reasons.append(f"RSI is extended at {snapshot.rsi:.1f}")
        elif snapshot.rsi < 26:
            long_score += 5
            long_reasons.append(f"RSI is deeply oversold at {snapshot.rsi:.1f}")

        if snapshot.volume_ratio >= 1.15:
            long_score += 6 if snapshot.last > snapshot.ema_fast else 0
            short_score += 6 if snapshot.last < snapshot.ema_fast else 0
            direction = "above" if snapshot.last > snapshot.ema_fast else "below"
            reason = f"Volume is {snapshot.volume_ratio:.2f}x recent average with price {direction} EMA20"
            if snapshot.last > snapshot.ema_fast:
                long_reasons.append(reason)
            else:
                short_reasons.append(reason)

        if snapshot.last > snapshot.resistance * 0.995:
            long_score += 7
            long_reasons.append("Price is pressing recent resistance")
        if snapshot.last < snapshot.support * 1.005:
            short_score += 7
            short_reasons.append("Price is pressing recent support")

        if news_score > 0:
            boost = min(18.0, news_score * 2.5 + min(news_count, 4))
            long_score += boost
            long_reasons.append(f"News sentiment is bullish ({news_score:+.2f}, {news_count} item(s))")
        elif news_score < 0:
            boost = min(18.0, abs(news_score) * 2.5 + min(news_count, 4))
            short_score += boost
            short_reasons.append(f"News sentiment is bearish ({news_score:+.2f}, {news_count} item(s))")
        elif news_count:
            warnings.append(f"{news_count} related news item(s), but sentiment is neutral")

        if snapshot.atr_pct > 4:
            warnings.append(f"ATR is high at {snapshot.atr_pct:.2f}%")

        long_score, short_score = _apply_higher_timeframe_context(
            snapshot,
            strategy_config,
            long_score,
            short_score,
            long_adjustments,
            short_adjustments,
            warnings,
        )

        long_score, short_score = _apply_candlestick_context(
            snapshot,
            strategy_config,
            long_score,
            short_score,
            long_adjustments,
            short_adjustments,
            warnings,
        )

        long_score, short_score = _apply_market_guard_context(
            long_score,
            short_score,
            long_adjustments,
            short_adjustments,
            warnings,
            (market_layers or {}).get(snapshot.symbol) or {},
        )

        long_reasons = long_adjustments + long_reasons
        short_reasons = short_adjustments + short_reasons

        if long_score >= short_score:
            candidates.append(
                _candidate_for_side(
                    snapshot,
                    base,
                    "long",
                    long_score,
                    news_score,
                    news_count,
                    order_usdt,
                    min_rr,
                    target_config,
                    leverage,
                    long_reasons,
                    warnings,
                )
            )
        else:
            candidates.append(
                _candidate_for_side(
                    snapshot,
                    base,
                    "short",
                    short_score,
                    news_score,
                    news_count,
                    order_usdt,
                    min_rr,
                    target_config,
                    leverage,
                    short_reasons,
                    warnings,
                )
            )

    ranked = sorted(
        candidates,
        key=lambda item: (item.win_probability_pct or 0, item.confidence),
        reverse=True,
    )
    if limit is None:
        return ranked
    return ranked[:limit]


def enrich_quantities(config: dict[str, Any], candidates: list[TradeCandidate]) -> list[str]:
    warnings: list[str] = []
    try:
        exchange = create_exchange(config, authenticated=False)
        exchange.load_markets()
    except Exception as exc:
        return [f"quantity precision unavailable: {exc}"]

    for candidate in candidates:
        try:
            candidate.quantity = amount_for_notional(
                exchange,
                candidate.symbol,
                candidate.order_usdt,
                candidate.entry,
            )
        except Exception as exc:
            warnings.append(f"{candidate.symbol}: quantity calculation failed: {exc}")
    return warnings
