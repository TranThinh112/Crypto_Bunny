from __future__ import annotations

from market_pattern_engine.detectors.base import MarketContext, clamp01
from market_pattern_engine.detectors.market_structure.swing_detector import detect_swings
from market_pattern_engine.domain.enums import PatternDirection, PatternStatus
from market_pattern_engine.domain.models import ChartPatternDetection


def detect_double_patterns(context: MarketContext) -> list[ChartPatternDetection]:
    pivots = detect_swings(context)[-10:]
    highs = [p for p in pivots if p["type"] == "swing_high"]
    lows = [p for p in pivots if p["type"] == "swing_low"]
    tol = context.atr * float(context.config.get("chart_patterns", {}).get("pivot_tolerance_atr", 0.75) or 0.75)
    out: list[ChartPatternDetection] = []
    if len(highs) >= 2:
        a, b = highs[-2], highs[-1]
        if abs(float(a["price"]) - float(b["price"])) <= tol:
            neckline = min(float(p["price"]) for p in lows[-3:]) if lows else None
            confirmed = neckline is not None and context.last_close < neckline
            out.append(ChartPatternDetection(pattern="double_top", direction=PatternDirection.BEARISH, start_index=int(a["index"]), end_index=int(b["index"]), pivots=[a, b], neckline=neckline, breakout_level=neckline, theoretical_target=(neckline - (float(b["price"]) - neckline)) if neckline else None, invalidation_level=max(float(a["price"]), float(b["price"])) + tol, confidence=clamp01(1 - abs(float(a["price"]) - float(b["price"])) / max(tol, 1e-12)), status=PatternStatus.CONFIRMED if confirmed else PatternStatus.FORMING))
    if len(lows) >= 2:
        a, b = lows[-2], lows[-1]
        if abs(float(a["price"]) - float(b["price"])) <= tol:
            neckline = max(float(p["price"]) for p in highs[-3:]) if highs else None
            confirmed = neckline is not None and context.last_close > neckline
            out.append(ChartPatternDetection(pattern="double_bottom", direction=PatternDirection.BULLISH, start_index=int(a["index"]), end_index=int(b["index"]), pivots=[a, b], neckline=neckline, breakout_level=neckline, theoretical_target=(neckline + (neckline - float(b["price"]))) if neckline else None, invalidation_level=min(float(a["price"]), float(b["price"])) - tol, confidence=clamp01(1 - abs(float(a["price"]) - float(b["price"])) / max(tol, 1e-12)), status=PatternStatus.CONFIRMED if confirmed else PatternStatus.FORMING))
    return out
