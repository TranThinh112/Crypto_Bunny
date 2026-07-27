from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .models import AnalysisResult, MarketContext, TradeCandidate, TradeIntent, to_jsonable

TREND_FOLLOWING = "TrendFollowing"
COUNTER_TREND = "CounterTrend"
BREAKOUT = "Breakout"
RANGE_TRADING = "RangeTrading"
NO_TRADE = "NoTrade"

def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    return max(lower, min(upper, value))

def _grade(score: float) -> str:
    if score >= 92:
        return "S"
    if score >= 84:
        return "A"
    if score >= 76:
        return "B"
    if score >= 66:
        return "C"
    return "D"

def _speed(atr_pct: float, volume_ratio: float) -> str:
    if atr_pct >= 2.2 or volume_ratio >= 2.5:
        return "Fast"
    if atr_pct <= 0.7 and volume_ratio <= 0.8:
        return "Slow"
    return "Medium"

def _volatility(atr_pct: float) -> str:
    if atr_pct >= 2.2:
        return "High"
    if atr_pct <= 0.7:
        return "Low"
    return "Medium"

def _news_risk(news_score: float, side: str) -> str:
    if abs(news_score) < 1:
        return "Low"
    aligned = (side == "long" and news_score > 0) or (side == "short" and news_score < 0)
    return "Medium" if aligned else "High"

def _mapping_to_candidate(row: dict[str, Any]) -> TradeCandidate | None:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
    if not isinstance(payload, dict):
        return None
    symbol = str(payload.get("symbol") or row.get("symbol") or "").strip()
    side = str(payload.get("side") or row.get("side") or "").strip().lower()
    if not symbol or side not in {"long", "short"}:
        return None
    base = str(payload.get("base") or symbol.split("/")[0] or "").upper()
    entry = _float(payload.get("entry") or payload.get("price") or row.get("entry") or row.get("price"))
    if entry <= 0:
        return None
    stop_loss = _float(payload.get("stop_loss"), entry * (0.98 if side == "long" else 1.02))
    take_profit = _float(payload.get("take_profit"), entry * (1.03 if side == "long" else 0.97))
    indicator_summary = payload.get("indicator_summary") if isinstance(payload.get("indicator_summary"), dict) else {}
    higher_timeframes = payload.get("higher_timeframes") if isinstance(payload.get("higher_timeframes"), dict) else {}
    candlestick_patterns = payload.get("candlestick_patterns") if isinstance(payload.get("candlestick_patterns"), dict) else {}
    return TradeCandidate(
        symbol=symbol,
        base=base,
        side=side,  # type: ignore[arg-type]
        confidence=_float(payload.get("confidence") or row.get("confidence"), 0.0),
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        risk_reward=_float(payload.get("risk_reward"), 1.5),
        order_usdt=_float(payload.get("order_usdt"), 0.0),
        quantity=payload.get("quantity"),
        spread_pct=payload.get("spread_pct"),
        news_score=_float(payload.get("news_score")),
        news_count=int(_float(payload.get("news_count"), 0.0)),
        higher_timeframes=higher_timeframes,
        indicator_summary=indicator_summary,
        candlestick_patterns=candlestick_patterns,
        rule_score=payload.get("rule_score"),
        margin_usdt=payload.get("margin_usdt"),
        win_probability_pct=payload.get("win_probability_pct") or row.get("win_probability_pct"),
        reasons=list(payload.get("reasons") or row.get("reasons") or []),
        warnings=list(payload.get("warnings") or row.get("warnings") or []),
        market_regime=payload.get("market_regime") or row.get("market_regime"),
        regime_confidence=payload.get("regime_confidence") or row.get("regime_confidence"),
    )

def _timeframe_trend(candidate: TradeCandidate, timeframe: str) -> str | None:
    frame = candidate.higher_timeframes.get(timeframe) if isinstance(candidate.higher_timeframes, dict) else None
    if isinstance(frame, dict):
        trend = str(frame.get("trend") or "").strip().lower()
        return trend or None
    return None

def _trend_alignment_score(candidate: TradeCandidate) -> tuple[float, list[str]]:
    side = str(candidate.side or "").lower()
    weights = {"4h": 36.0, "1h": 28.0, "15m": 18.0, "5m": 12.0}
    score = 50.0
    evidence: list[str] = []
    frames = candidate.higher_timeframes if isinstance(candidate.higher_timeframes, dict) else {}
    for timeframe, weight in weights.items():
        frame = frames.get(timeframe)
        if not isinstance(frame, dict):
            continue
        trend = str(frame.get("trend") or "mixed").lower()
        ema_gap = _float(frame.get("ema_gap_pct"))
        price_vs_ema = _float(frame.get("price_vs_ema_slow_pct"))
        aligned = (side == "long" and trend == "up") or (side == "short" and trend == "down")
        opposite = (side == "long" and trend == "down") or (side == "short" and trend == "up")
        if aligned:
            score += weight * 0.62
            evidence.append(f"{timeframe.upper()} trend aligns with {side}: ema_gap={ema_gap:+.2f}%, price_vs_ema={price_vs_ema:+.2f}%")
        elif opposite:
            score -= weight * 0.55
            evidence.append(f"{timeframe.upper()} trend opposes {side}: ema_gap={ema_gap:+.2f}%, price_vs_ema={price_vs_ema:+.2f}%")
        else:
            evidence.append(f"{timeframe.upper()} trend is mixed")
    rule_score = candidate.rule_score if candidate.rule_score is not None else candidate.confidence
    score = (score * 0.65) + (_clamp(_float(rule_score), 0, 120) * 0.35)
    return round(_clamp(score), 2), evidence

def _continuation_score(candidate: TradeCandidate, trend_score: float) -> tuple[float, list[str]]:
    evidence: list[str] = []
    volume_ratio = _float(candidate.indicator_summary.get("volume_ratio"), _float(candidate.volume_ratio if hasattr(candidate, "volume_ratio") else 0))
    rsi = _float(candidate.indicator_summary.get("rsi"), 50.0)
    atr_pct = _float(candidate.indicator_summary.get("atr_pct"))
    score = trend_score * 0.7
    if volume_ratio >= 1.2:
        score += min(12.0, volume_ratio * 3.0)
        evidence.append(f"Volume supports continuation: {volume_ratio:.2f}x")
    elif volume_ratio <= 0.5:
        score -= 8.0
        evidence.append(f"Volume is weak: {volume_ratio:.2f}x")
    if candidate.side == "long" and rsi >= 78:
        score -= 10.0
        evidence.append(f"Long is hot: RSI {rsi:.1f}")
    elif candidate.side == "short" and rsi <= 24:
        score -= 10.0
        evidence.append(f"Short is oversold: RSI {rsi:.1f}")
    elif 38 <= rsi <= 68:
        score += 5.0
        evidence.append(f"RSI remains constructive: {rsi:.1f}")
    if atr_pct >= 2.5:
        score += 4.0
        evidence.append(f"ATR allows movement: {atr_pct:.2f}%")
    return round(_clamp(score), 2), evidence

def _entry_quality(candidate: TradeCandidate) -> tuple[float, list[str]]:
    spread = _float(candidate.spread_pct)
    confidence = _float(candidate.confidence)
    win_probability = _float(candidate.win_probability_pct, confidence)
    score = (confidence * 0.45) + (win_probability * 0.45)
    evidence: list[str] = [f"Confidence={confidence:.2f}", f"Win probability={win_probability:.2f}%"]
    if spread <= 0.05:
        score += 8.0
        evidence.append(f"Spread is tight: {spread:.4f}%")
    elif spread >= 0.18:
        score -= 8.0
        evidence.append(f"Spread is wide: {spread:.4f}%")
    if candidate.warnings:
        penalty = min(12.0, len(candidate.warnings) * 3.0)
        score -= penalty
        evidence.append(f"Warnings penalty: -{penalty:.1f}")
    return round(_clamp(score), 2), evidence

def build_market_context(candidate: TradeCandidate, *, market_regime: str | None = None) -> MarketContext:
    trend_score, trend_evidence = _trend_alignment_score(candidate)
    continuation, continuation_evidence = _continuation_score(candidate, trend_score)
    entry_quality, entry_evidence = _entry_quality(candidate)
    atr_pct = _float(candidate.indicator_summary.get("atr_pct"))
    volume_ratio = _float(candidate.indicator_summary.get("volume_ratio"))
    breakout_probability = round(_clamp((trend_score * 0.45) + (entry_quality * 0.35) + min(20.0, volume_ratio * 5.0)), 2)
    pullback_quality = round(_clamp((100.0 - abs(_float(candidate.indicator_summary.get("rsi"), 50.0) - 50.0) * 2.0) * 0.6 + entry_quality * 0.4), 2)
    regime = market_regime or candidate.market_regime or "UNKNOWN"
    return MarketContext(
        symbol=candidate.symbol,
        side_hint=candidate.side,
        market_regime=str(regime),
        regime_confidence=candidate.regime_confidence,
        trend_4h=_timeframe_trend(candidate, "4h"),
        trend_1h=_timeframe_trend(candidate, "1h"),
        trend_15m=_timeframe_trend(candidate, "15m"),
        trend_score=trend_score,
        continuation_score=continuation,
        entry_quality=entry_quality,
        breakout_probability=breakout_probability,
        pullback_quality=pullback_quality,
        volatility=_volatility(atr_pct),
        news_risk=_news_risk(_float(candidate.news_score), str(candidate.side)),
        evidence=[*trend_evidence, *continuation_evidence, *entry_evidence],
    )

def build_analysis_result(context: MarketContext, candidate: TradeCandidate) -> AnalysisResult:
    blended_score = (context.trend_score * 0.35) + (context.continuation_score * 0.35) + (context.entry_quality * 0.30)
    warnings = list(candidate.warnings or [])
    if context.news_risk == "High":
        warnings.append("News risk conflicts with side hint")
    return AnalysisResult(
        symbol=context.symbol,
        market_regime=context.market_regime,
        trend_score=context.trend_score,
        continuation_score=context.continuation_score,
        entry_quality=context.entry_quality,
        setup_grade=_grade(blended_score),
        expected_speed=_speed(_float(candidate.indicator_summary.get("atr_pct")), _float(candidate.indicator_summary.get("volume_ratio"))),
        evidence=context.evidence[:12],
        warnings=warnings[:8],
    )

def select_strategy(context: MarketContext, analysis: AnalysisResult, candidate: TradeCandidate) -> tuple[str, str]:
    if analysis.setup_grade == "D" or analysis.entry_quality < 55:
        return NO_TRADE, "entry quality or setup grade is too weak"
    if context.breakout_probability >= 85 and analysis.entry_quality >= 78:
        return BREAKOUT, "breakout probability and entry quality are high"
    if analysis.trend_score >= 80 and analysis.continuation_score >= 75:
        return TREND_FOLLOWING, "trend and continuation are strong"
    if analysis.trend_score >= 72 and analysis.continuation_score <= 48 and analysis.entry_quality >= 76:
        return COUNTER_TREND, "trend is extended while continuation weakened"
    if analysis.trend_score < 55 and context.volatility == "Low" and analysis.entry_quality >= 70:
        return RANGE_TRADING, "trend is weak and volatility is low"
    if analysis.trend_score >= 68 and analysis.continuation_score >= 60:
        return TREND_FOLLOWING, "trend is acceptable with enough continuation"
    return NO_TRADE, "no deterministic strategy rule matched"

def build_trade_intent(context: MarketContext, analysis: AnalysisResult, candidate: TradeCandidate) -> TradeIntent:
    strategy, reason = select_strategy(context, analysis, candidate)
    if strategy == TREND_FOLLOWING or strategy == BREAKOUT:
        holding_profile = "Long"
    elif strategy == COUNTER_TREND or strategy == RANGE_TRADING:
        holding_profile = "Short"
    else:
        holding_profile = "None"
    risk_profile = "Normal"
    if analysis.setup_grade in {"C", "D"} or analysis.entry_quality < 70:
        risk_profile = "Reduced"
    return TradeIntent(
        symbol=candidate.symbol,
        side=candidate.side,
        strategy=strategy,
        setup_grade=analysis.setup_grade,
        holding_profile=holding_profile,
        risk_profile=risk_profile,
        entry_quality=analysis.entry_quality,
        continuation_score=analysis.continuation_score,
        status="shadow",
        reason=reason,
    )

def build_shadow_trade_intent(candidate: TradeCandidate, *, market_regime: str | None = None) -> dict[str, Any]:
    context = build_market_context(candidate, market_regime=market_regime)
    analysis = build_analysis_result(context, candidate)
    intent = build_trade_intent(context, analysis, candidate)
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "phase": "shadow_phase_1_4",
        "enabled_for_execution": False,
        "market_context": to_jsonable(context),
        "analysis_result": to_jsonable(analysis),
        "trade_intent": to_jsonable(intent),
    }

def build_shadow_trade_intents(candidates: list[TradeCandidate], *, limit: int = 5, market_regime: str | None = None) -> dict[str, Any]:
    rows = [build_shadow_trade_intent(candidate, market_regime=market_regime) for candidate in candidates[: max(0, limit)]]
    strategy_counts: dict[str, int] = {}
    for row in rows:
        strategy = str((row.get("trade_intent") or {}).get("strategy") or NO_TRADE)
        strategy_counts[strategy] = strategy_counts.get(strategy, 0) + 1
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "shadow_mode": True,
        "phase_completion": {
            "phase_1_schema": 100,
            "phase_2_context_builder": 100,
            "phase_3_analysis_result": 100,
            "phase_4_strategy_selector": 100,
        },
        "candidate_count": len(candidates),
        "intent_count": len(rows),
        "strategy_counts": strategy_counts,
        "items": rows,
    }

def build_shadow_trade_intents_from_rows(rows: list[dict[str, Any]], *, limit: int = 5, market_regime: str | None = None) -> dict[str, Any]:
    candidates: list[TradeCandidate] = []
    skipped = 0
    for row in rows:
        if not isinstance(row, dict):
            skipped += 1
            continue
        candidate = _mapping_to_candidate(row)
        if candidate is None:
            skipped += 1
            continue
        candidates.append(candidate)
        if len(candidates) >= max(1, limit):
            break
    payload = build_shadow_trade_intents(candidates, limit=limit, market_regime=market_regime)
    payload["source_row_count"] = len(rows)
    payload["skipped_row_count"] = skipped
    return payload
