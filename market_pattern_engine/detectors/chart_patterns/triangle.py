from __future__ import annotations

from market_pattern_engine.detectors.base import MarketContext
from market_pattern_engine.detectors.market_structure.swing_detector import detect_swings
from market_pattern_engine.domain.enums import PatternDirection, PatternStatus
from market_pattern_engine.domain.models import ChartPatternDetection


def detect_triangles(context: MarketContext) -> list[ChartPatternDetection]:
    pivots = detect_swings(context)[-10:]
    highs = [p for p in pivots if p["type"] == "swing_high"][-3:]
    lows = [p for p in pivots if p["type"] == "swing_low"][-3:]
    if len(highs) < 2 or len(lows) < 2:
        return []
    high_slope = float(highs[-1]["price"]) - float(highs[0]["price"])
    low_slope = float(lows[-1]["price"]) - float(lows[0]["price"])
    tol = context.atr * 0.5
    if abs(high_slope) <= tol and low_slope > tol:
        name, direction = "ascending_triangle", PatternDirection.BULLISH
    elif abs(low_slope) <= tol and high_slope < -tol:
        name, direction = "descending_triangle", PatternDirection.BEARISH
    elif high_slope < -tol and low_slope > tol:
        name, direction = "symmetrical_triangle", PatternDirection.MIXED
    else:
        return []
    return [ChartPatternDetection(pattern=name, direction=direction, start_index=min(int(highs[0]["index"]), int(lows[0]["index"])), end_index=max(int(highs[-1]["index"]), int(lows[-1]["index"])), pivots=[*highs, *lows], breakout_level=float(highs[-1]["price"]) if direction != PatternDirection.BEARISH else float(lows[-1]["price"]), confidence=0.64, status=PatternStatus.FORMING)]
