from __future__ import annotations

from market_pattern_engine.domain.enums import PatternDirection
from market_pattern_engine.domain.models import StructureBreak


def detect_choch(trend_regime: str, bos: StructureBreak) -> StructureBreak:
    if not bos.detected or not bos.direction:
        return StructureBreak()
    if trend_regime == "bullish" and bos.direction == PatternDirection.BEARISH:
        return StructureBreak(**{**bos.model_dump(), "confidence": min(1.0, bos.confidence + 0.08)})
    if trend_regime == "bearish" and bos.direction == PatternDirection.BULLISH:
        return StructureBreak(**{**bos.model_dump(), "confidence": min(1.0, bos.confidence + 0.08)})
    return StructureBreak()
