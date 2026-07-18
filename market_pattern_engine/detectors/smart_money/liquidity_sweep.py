from __future__ import annotations

from market_pattern_engine.detectors.base import MarketContext, clamp01
from market_pattern_engine.detectors.market_structure.swing_detector import detect_swings
from market_pattern_engine.domain.enums import PatternDirection
from market_pattern_engine.domain.models import SmartMoneyDetection


def detect_liquidity_sweep(context: MarketContext) -> list[SmartMoneyDetection]:
    swings = detect_swings(context)
    highs = [item for item in swings if item["type"] == "swing_high"]
    lows = [item for item in swings if item["type"] == "swing_low"]
    last = context.frame.iloc[-1]
    rows: list[SmartMoneyDetection] = []
    if highs:
        level = float(highs[-1]["price"])
        if float(last.high) > level and float(last.close) < level:
            rows.append(SmartMoneyDetection(type="buy_side_liquidity_sweep", direction=PatternDirection.BEARISH, zone_low=level, zone_high=float(last.high), created_at=last.timestamp, confidence=clamp01((float(last.high) - level) / max(context.atr, 1e-12)), evidence={"swept_level": level, "close_back_inside": True}))
    if lows:
        level = float(lows[-1]["price"])
        if float(last.low) < level and float(last.close) > level:
            rows.append(SmartMoneyDetection(type="sell_side_liquidity_sweep", direction=PatternDirection.BULLISH, zone_low=float(last.low), zone_high=level, created_at=last.timestamp, confidence=clamp01((level - float(last.low)) / max(context.atr, 1e-12)), evidence={"swept_level": level, "close_back_inside": True}))
    return rows
