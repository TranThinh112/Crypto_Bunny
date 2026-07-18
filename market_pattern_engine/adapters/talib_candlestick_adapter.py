from __future__ import annotations

from market_pattern_engine.detectors.candlestick import NativeCandlestickDetector


class TalibCandlestickAdapter(NativeCandlestickDetector):
    name = "talib_candlestick_adapter"
    detector_source = "EXTERNAL_ADAPTER"

    def detect(self, market_context):
        try:
            import talib  # noqa: F401
        except Exception:
            return []
        # TA-Lib installation varies by platform; native validation remains authoritative.
        return super().detect(market_context)
