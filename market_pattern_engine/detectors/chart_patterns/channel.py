from __future__ import annotations

from market_pattern_engine.detectors.base import MarketContext
from market_pattern_engine.domain.enums import PatternDirection, PatternStatus
from market_pattern_engine.domain.models import ChartPatternDetection


def detect_channel_or_range(context: MarketContext) -> list[ChartPatternDetection]:
    frame = context.frame.tail(40)
    high_slope = float(frame.high.iloc[-1] - frame.high.iloc[0])
    low_slope = float(frame.low.iloc[-1] - frame.low.iloc[0])
    tolerance = context.atr * 2
    if abs(high_slope) <= tolerance and abs(low_slope) <= tolerance:
        name, direction = "range", PatternDirection.NEUTRAL
    elif high_slope > tolerance and low_slope > tolerance:
        name, direction = "channel_up", PatternDirection.BULLISH
    elif high_slope < -tolerance and low_slope < -tolerance:
        name, direction = "channel_down", PatternDirection.BEARISH
    else:
        return []
    return [ChartPatternDetection(pattern=name, direction=direction, start_index=max(0, len(context.frame) - len(frame)), end_index=len(context.frame) - 1, confidence=0.58, status=PatternStatus.FORMING, evidence={"high_slope": high_slope, "low_slope": low_slope})]
