from __future__ import annotations

from market_pattern_engine.domain.models import SupportResistanceZone
from market_pattern_engine.detectors.base import MarketContext


def retest_status(context: MarketContext, zones: list[SupportResistanceZone]) -> list[dict]:
    recent = context.frame.tail(5)
    rows = []
    for zone in zones:
        touched = ((recent["low"] <= zone.zone_high) & (recent["high"] >= zone.zone_low)).any()
        if touched:
            rows.append({"type": "retest", "zone": zone.center_price, "zone_type": zone.type, "status": "tested"})
    return rows
