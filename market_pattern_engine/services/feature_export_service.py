from __future__ import annotations

from market_pattern_engine.domain.enums import PatternDirection
from market_pattern_engine.domain.models import ChartPatternDetection, ConfluenceResult, FeatureVector, MarketStructure, PatternDetection, SmartMoneyDetection, SupportResistanceZone


class FeatureExportService:
    def build(
        self,
        *,
        structure: MarketStructure,
        support_zones: list[SupportResistanceZone],
        resistance_zones: list[SupportResistanceZone],
        candles: list[PatternDetection],
        chart_patterns: list[ChartPatternDetection],
        smart_money: list[SmartMoneyDetection],
        confluence: ConfluenceResult,
        atr: float,
        last_close: float,
        engine_version: str,
        config_version: str,
    ) -> FeatureVector:
        nearest_support = min(support_zones, key=lambda z: abs(last_close - z.center_price), default=None)
        nearest_resistance = min(resistance_zones, key=lambda z: abs(last_close - z.center_price), default=None)
        bullish_pattern_score = max((item.confidence for item in candles if item.direction == PatternDirection.BULLISH), default=0.0)
        bearish_pattern_score = max((item.confidence for item in candles if item.direction == PatternDirection.BEARISH), default=0.0)
        sweep = next((item for item in smart_money if "liquidity_sweep" in item.type), None)
        fvg_near = any(item.zone_low is not None and item.zone_high is not None and item.zone_low <= last_close <= item.zone_high for item in smart_money if "fvg" in item.type)
        return FeatureVector(
            trend_regime=structure.trend_regime,
            trend_strength=structure.trend_strength,
            distance_to_support_atr=((last_close - nearest_support.center_price) / max(atr, 1e-12)) if nearest_support else None,
            distance_to_resistance_atr=((nearest_resistance.center_price - last_close) / max(atr, 1e-12)) if nearest_resistance else None,
            nearest_support_strength=nearest_support.strength_score if nearest_support else 0.0,
            nearest_resistance_strength=nearest_resistance.strength_score if nearest_resistance else 0.0,
            bullish_pattern_score=bullish_pattern_score,
            bearish_pattern_score=bearish_pattern_score,
            bos_bullish=1 if structure.bos.detected and structure.bos.direction == PatternDirection.BULLISH else 0,
            bos_bearish=1 if structure.bos.detected and structure.bos.direction == PatternDirection.BEARISH else 0,
            choch_bullish=1 if structure.choch.detected and structure.choch.direction == PatternDirection.BULLISH else 0,
            choch_bearish=1 if structure.choch.detected and structure.choch.direction == PatternDirection.BEARISH else 0,
            liquidity_sweep_side=sweep.type if sweep else None,
            fvg_near_price=1 if fvg_near else 0,
            chart_pattern=chart_patterns[0].pattern if chart_patterns else None,
            technical_confluence_score=confluence.confluence_score,
            data_quality_score=confluence.data_quality,
            detector_version=engine_version,
            config_version=config_version,
            metadata={"look_ahead_bias_guard": "closed candles only in SCAN_MODE"},
        )
