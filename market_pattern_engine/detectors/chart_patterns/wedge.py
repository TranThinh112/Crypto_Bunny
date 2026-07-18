from __future__ import annotations

from market_pattern_engine.detectors.chart_patterns.triangle import detect_triangles
from market_pattern_engine.domain.enums import PatternDirection


def detect_wedges(context):
    rows = []
    for item in detect_triangles(context):
        if item.pattern == "symmetrical_triangle":
            continue
        dumped = item.model_dump()
        if item.direction == PatternDirection.BULLISH:
            dumped["pattern"] = "falling_wedge"
        elif item.direction == PatternDirection.BEARISH:
            dumped["pattern"] = "rising_wedge"
        rows.append(type(item)(**dumped))
    return rows
