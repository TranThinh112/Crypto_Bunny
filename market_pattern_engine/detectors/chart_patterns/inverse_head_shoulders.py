from __future__ import annotations

from market_pattern_engine.detectors.base import MarketContext, clamp01
from market_pattern_engine.detectors.market_structure.swing_detector import detect_swings
from market_pattern_engine.domain.enums import PatternDirection, PatternStatus
from market_pattern_engine.domain.models import ChartPatternDetection


def detect_inverse_head_shoulders(context: MarketContext) -> list[ChartPatternDetection]:
    pivots = detect_swings(context)[-12:]
    lows = [p for p in pivots if p["type"] == "swing_low"]
    highs = [p for p in pivots if p["type"] == "swing_high"]
    if len(lows) < 3 or len(highs) < 2:
        return []
    left, head, right = lows[-3], lows[-2], lows[-1]
    shoulder_diff = abs(float(left["price"]) - float(right["price"]))
    tol = context.atr * 1.2
    if float(head["price"]) < float(left["price"]) and float(head["price"]) < float(right["price"]) and shoulder_diff <= tol:
        neckline = (float(highs[-1]["price"]) + float(highs[-2]["price"])) / 2
        confirmed = context.last_close > neckline
        return [ChartPatternDetection(pattern="inverse_head_and_shoulders", direction=PatternDirection.BULLISH, start_index=int(left["index"]), end_index=int(right["index"]), pivots=[left, head, right], neckline=neckline, breakout_level=neckline, theoretical_target=neckline + (neckline - float(head["price"])), invalidation_level=float(head["price"]), confidence=clamp01(0.55 + (1 - shoulder_diff / max(tol, 1e-12)) * 0.35), status=PatternStatus.CONFIRMED if confirmed else PatternStatus.FORMING)]
    return []
