from __future__ import annotations

from market_pattern_engine.detectors.base import MarketContext, clamp01, direction_of
from market_pattern_engine.domain.enums import PatternDirection
from market_pattern_engine.domain.models import SmartMoneyDetection


def detect_order_block(context: MarketContext) -> list[SmartMoneyDetection]:
    lookback = int(context.config.get("smart_money", {}).get("order_block_lookback", 5) or 5)
    frame = context.frame.tail(max(lookback + 1, 2)).reset_index(drop=True)
    if len(frame) < 2:
        return []
    last = frame.iloc[-1]
    direction = direction_of(last)
    rows: list[SmartMoneyDetection] = []
    if direction == "bullish":
        candidates = [row for _, row in frame.iloc[:-1].iterrows() if direction_of(row) == "bearish"]
        sm_direction = PatternDirection.BULLISH
        name = "bullish_order_block_heuristic"
    elif direction == "bearish":
        candidates = [row for _, row in frame.iloc[:-1].iterrows() if direction_of(row) == "bullish"]
        sm_direction = PatternDirection.BEARISH
        name = "bearish_order_block_heuristic"
    else:
        return []
    if not candidates:
        return []
    candle = candidates[-1]
    rows.append(SmartMoneyDetection(type=name, direction=sm_direction, zone_low=float(candle.low), zone_high=float(candle.high), created_at=candle.timestamp, confidence=clamp01(abs(float(last.close) - float(last.open)) / max(context.atr * 2, 1e-12)), evidence={"heuristic": True, "note": "Order Block is heuristic, not an absolute fact."}))
    return rows
