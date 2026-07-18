from __future__ import annotations

from market_pattern_engine.detectors.base import Detector, MarketContext
from market_pattern_engine.detectors.market_structure.bos_detector import detect_bos
from market_pattern_engine.detectors.market_structure.choch_detector import detect_choch
from market_pattern_engine.detectors.market_structure.hh_hl_lh_ll import classify_structure
from market_pattern_engine.detectors.market_structure.swing_detector import detect_swings
from market_pattern_engine.domain.models import MarketStructure


class MarketStructureDetector(Detector):
    name = "market_structure"

    def detect(self, context: MarketContext) -> list[MarketStructure]:
        swings = detect_swings(context)
        trend, state, strength = classify_structure(swings)
        bos = detect_bos(context, swings)
        choch = detect_choch(trend, bos)
        highs = [item for item in swings if item["type"] == "swing_high"]
        lows = [item for item in swings if item["type"] == "swing_low"]
        return [
            MarketStructure(
                trend_regime=trend,
                structure_state=state,
                trend_strength=strength,
                last_swing_high=float(highs[-1]["price"]) if highs else None,
                last_swing_low=float(lows[-1]["price"]) if lows else None,
                swings=swings[-12:],
                bos=bos,
                choch=choch,
            )
        ]
