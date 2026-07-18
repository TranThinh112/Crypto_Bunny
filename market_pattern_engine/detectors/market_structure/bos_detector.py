from __future__ import annotations

from market_pattern_engine.detectors.base import MarketContext, clamp01
from market_pattern_engine.domain.enums import PatternDirection
from market_pattern_engine.domain.models import StructureBreak


def detect_bos(context: MarketContext, swings: list[dict]) -> StructureBreak:
    highs = [item for item in swings if item["type"] == "swing_high"]
    lows = [item for item in swings if item["type"] == "swing_low"]
    if not highs or not lows:
        return StructureBreak()
    last = context.frame.iloc[-1]
    avg_volume = float(context.frame["sma_volume20"].iloc[-1] or 0.0)
    volume_confirmation = float(last.volume) >= avg_volume if avg_volume else False
    high_level = float(highs[-1]["price"])
    low_level = float(lows[-1]["price"])
    buffer = context.atr * float(context.config.get("market_structure", {}).get("breakout_atr_buffer", 0.15) or 0.15)
    if float(last.high) > high_level and float(last.close) > high_level + buffer:
        return StructureBreak(detected=True, direction=PatternDirection.BULLISH, broken_level=high_level, wick_break=True, confirmed_by_close=True, volume_confirmation=volume_confirmation, confidence=clamp01(0.55 + (0.2 if volume_confirmation else 0) + min((float(last.close) - high_level) / max(context.atr, 1e-12), 1) * 0.25))
    if float(last.low) < low_level and float(last.close) < low_level - buffer:
        return StructureBreak(detected=True, direction=PatternDirection.BEARISH, broken_level=low_level, wick_break=True, confirmed_by_close=True, volume_confirmation=volume_confirmation, confidence=clamp01(0.55 + (0.2 if volume_confirmation else 0) + min((low_level - float(last.close)) / max(context.atr, 1e-12), 1) * 0.25))
    if float(last.high) > high_level or float(last.low) < low_level:
        direction = PatternDirection.BULLISH if float(last.high) > high_level else PatternDirection.BEARISH
        level = high_level if direction == PatternDirection.BULLISH else low_level
        return StructureBreak(detected=True, direction=direction, broken_level=level, wick_break=True, false_breakout=True, confidence=0.35)
    return StructureBreak()
