from __future__ import annotations

from market_pattern_engine.domain.enums import PatternDirection
from market_pattern_engine.domain.models import ConfluenceResult, MarketAnalysisResult, MarketStructure, PatternDetection, SmartMoneyDetection, SupportResistanceZone


class ConfluenceService:
    def build(
        self,
        *,
        structure: MarketStructure,
        candles: list[PatternDetection],
        support_zones: list[SupportResistanceZone],
        resistance_zones: list[SupportResistanceZone],
        smart_money: list[SmartMoneyDetection],
        data_quality_score: float,
    ) -> ConfluenceResult:
        bullish: list[str] = []
        bearish: list[str] = []
        for item in candles:
            if item.direction == PatternDirection.BULLISH:
                bullish.append(item.pattern_type)
            elif item.direction == PatternDirection.BEARISH:
                bearish.append(item.pattern_type)
        if structure.trend_regime == "bullish":
            bullish.append("bullish_structure")
        elif structure.trend_regime == "bearish":
            bearish.append("bearish_structure")
        if structure.bos.detected and structure.bos.direction == PatternDirection.BULLISH:
            bullish.append("bullish_bos")
        if structure.bos.detected and structure.bos.direction == PatternDirection.BEARISH:
            bearish.append("bearish_bos")
        for item in smart_money:
            if item.direction == PatternDirection.BULLISH:
                bullish.append(item.type)
            elif item.direction == PatternDirection.BEARISH:
                bearish.append(item.type)
        if support_zones:
            bullish.append("near_support_zone")
        if resistance_zones:
            bearish.append("near_resistance_zone")
        bull_score = len(bullish)
        bear_score = len(bearish)
        if bull_score > bear_score:
            bias = PatternDirection.BULLISH
            supporting, conflicting = bullish, bearish
        elif bear_score > bull_score:
            bias = PatternDirection.BEARISH
            supporting, conflicting = bearish, bullish
        else:
            bias = PatternDirection.NEUTRAL
            supporting, conflicting = [], bullish + bearish
        raw = abs(bull_score - bear_score) / max(bull_score + bear_score, 1)
        return ConfluenceResult(
            bias=bias,
            confluence_score=max(0.0, min(1.0, raw * data_quality_score)),
            supporting_factors=list(dict.fromkeys(supporting)),
            conflicting_factors=list(dict.fromkeys(conflicting)),
            data_quality=data_quality_score,
        )
