from __future__ import annotations

from market_pattern_engine.detectors.base import MarketContext
from market_pattern_engine.detectors.support_resistance.pivot_detector import detect_pivots


def detect_swings(context: MarketContext) -> list[dict]:
    min_distance = context.atr * float(context.config.get("market_structure", {}).get("minimum_swing_distance_atr", 0.5) or 0.5)
    swings: list[dict] = []
    last_price: float | None = None
    for pivot in sorted(detect_pivots(context), key=lambda item: int(item["index"])):
        if last_price is not None and abs(float(pivot["price"]) - last_price) < min_distance:
            continue
        swings.append(
            {
                "type": "swing_high" if pivot["type"] == "pivot_high" else "swing_low",
                "index": pivot["index"],
                "price": pivot["price"],
                "time": pivot["time"],
            }
        )
        last_price = float(pivot["price"])
    return swings
