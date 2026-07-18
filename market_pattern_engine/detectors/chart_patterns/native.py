from __future__ import annotations

from market_pattern_engine.detectors.base import Detector, MarketContext
from market_pattern_engine.detectors.chart_patterns.channel import detect_channel_or_range
from market_pattern_engine.detectors.chart_patterns.double_top import detect_double_patterns
from market_pattern_engine.detectors.chart_patterns.head_shoulders import detect_head_shoulders
from market_pattern_engine.detectors.chart_patterns.inverse_head_shoulders import detect_inverse_head_shoulders
from market_pattern_engine.detectors.chart_patterns.triangle import detect_triangles
from market_pattern_engine.detectors.chart_patterns.wedge import detect_wedges
from market_pattern_engine.domain.models import ChartPatternDetection


class NativeChartPatternDetector(Detector):
    name = "chart_patterns"

    def detect(self, context: MarketContext) -> list[ChartPatternDetection]:
        rows: list[ChartPatternDetection] = []
        for fn in (detect_double_patterns, detect_head_shoulders, detect_inverse_head_shoulders, detect_triangles, detect_wedges, detect_channel_or_range):
            rows.extend(fn(context))
        return rows
