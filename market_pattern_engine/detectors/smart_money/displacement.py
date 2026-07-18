from __future__ import annotations

from market_pattern_engine.detectors.base import MarketContext, clamp01, direction_of
from market_pattern_engine.domain.enums import PatternDirection
from market_pattern_engine.domain.models import SmartMoneyDetection


def detect_displacement(context: MarketContext) -> list[SmartMoneyDetection]:
    last = context.frame.iloc[-1]
    body = abs(float(last.close) - float(last.open))
    threshold = context.atr * float(context.config.get("smart_money", {}).get("displacement_body_atr", 1.2) or 1.2)
    if body < threshold:
        return []
    direction = PatternDirection.BULLISH if direction_of(last) == "bullish" else PatternDirection.BEARISH
    return [SmartMoneyDetection(type="displacement_candle", direction=direction, created_at=last.timestamp, confidence=clamp01(body / max(threshold * 1.8, 1e-12)), evidence={"body": body, "threshold": threshold})]
