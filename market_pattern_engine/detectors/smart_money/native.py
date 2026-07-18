from __future__ import annotations

from market_pattern_engine.detectors.base import Detector, MarketContext
from market_pattern_engine.detectors.smart_money.displacement import detect_displacement
from market_pattern_engine.detectors.smart_money.equal_high_low import detect_equal_high_low
from market_pattern_engine.detectors.smart_money.fair_value_gap import detect_fair_value_gaps
from market_pattern_engine.detectors.smart_money.liquidity_sweep import detect_liquidity_sweep
from market_pattern_engine.detectors.smart_money.order_block import detect_order_block
from market_pattern_engine.domain.models import SmartMoneyDetection


class NativeSmartMoneyDetector(Detector):
    name = "smart_money"

    def detect(self, context: MarketContext) -> list[SmartMoneyDetection]:
        rows: list[SmartMoneyDetection] = []
        for fn in (detect_fair_value_gaps, detect_liquidity_sweep, detect_equal_high_low, detect_displacement, detect_order_block):
            rows.extend(fn(context))
        return rows
