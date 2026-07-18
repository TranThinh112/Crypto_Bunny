from __future__ import annotations

from market_pattern_engine.detectors.base import MarketContext, clamp01
from market_pattern_engine.detectors.market_structure.swing_detector import detect_swings
from market_pattern_engine.domain.enums import PatternDirection
from market_pattern_engine.domain.models import SmartMoneyDetection


def detect_equal_high_low(context: MarketContext) -> list[SmartMoneyDetection]:
    tolerance = context.atr * float(context.config.get("smart_money", {}).get("equal_level_tolerance_atr", 0.2) or 0.2)
    swings = detect_swings(context)[-12:]
    rows: list[SmartMoneyDetection] = []
    for kind, name, direction in (
        ("swing_high", "equal_high", PatternDirection.BEARISH),
        ("swing_low", "equal_low", PatternDirection.BULLISH),
    ):
        pivots = [p for p in swings if p["type"] == kind]
        if len(pivots) < 2:
            continue
        a, b = pivots[-2], pivots[-1]
        diff = abs(float(a["price"]) - float(b["price"]))
        if diff <= tolerance:
            price = (float(a["price"]) + float(b["price"])) / 2
            rows.append(SmartMoneyDetection(type=name, direction=direction, zone_low=price - tolerance / 2, zone_high=price + tolerance / 2, created_at=b["time"], confidence=clamp01(1 - diff / max(tolerance, 1e-12)), evidence={"touches": 2, "price_diff": diff}))
    return rows
