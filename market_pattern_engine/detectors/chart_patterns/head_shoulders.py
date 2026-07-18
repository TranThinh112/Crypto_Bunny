from __future__ import annotations

from market_pattern_engine.detectors.base import MarketContext, clamp01
from market_pattern_engine.detectors.market_structure.swing_detector import detect_swings
from market_pattern_engine.domain.enums import PatternDirection, PatternStatus
from market_pattern_engine.domain.models import ChartPatternDetection


def detect_head_shoulders(context: MarketContext) -> list[ChartPatternDetection]:
    pivots = detect_swings(context)[-12:]
    highs = [p for p in pivots if p["type"] == "swing_high"]
    lows = [p for p in pivots if p["type"] == "swing_low"]
    if len(highs) < 3 or len(lows) < 2:
        return []
    left, head, right = highs[-3], highs[-2], highs[-1]
    shoulder_diff = abs(float(left["price"]) - float(right["price"]))
    tol = context.atr * 1.2
    if float(head["price"]) > float(left["price"]) and float(head["price"]) > float(right["price"]) and shoulder_diff <= tol:
        neckline = (float(lows[-1]["price"]) + float(lows[-2]["price"])) / 2
        confirmed = context.last_close < neckline
        return [ChartPatternDetection(pattern="head_and_shoulders", direction=PatternDirection.BEARISH, start_index=int(left["index"]), end_index=int(right["index"]), pivots=[left, head, right], neckline=neckline, breakout_level=neckline, theoretical_target=neckline - (float(head["price"]) - neckline), invalidation_level=float(head["price"]), confidence=clamp01(0.55 + (1 - shoulder_diff / max(tol, 1e-12)) * 0.35), status=PatternStatus.CONFIRMED if confirmed else PatternStatus.FORMING)]
    return []
