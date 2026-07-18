from __future__ import annotations

from market_pattern_engine.detectors.chart_patterns import NativeChartPatternDetector


class PatternPyAdapter(NativeChartPatternDetector):
    name = "patternpy_adapter"
    detector_source = "EXTERNAL_ADAPTER"

    def detect(self, market_context):
        try:
            import patternpy  # type: ignore  # noqa: F401
        except Exception:
            return []
        return super().detect(market_context)
