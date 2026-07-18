from __future__ import annotations

import time
from typing import Any

from market_pattern_engine import ENGINE_VERSION
from market_pattern_engine.detectors.base import build_market_context
from market_pattern_engine.detectors.candlestick import NativeCandlestickDetector
from market_pattern_engine.detectors.chart_patterns import NativeChartPatternDetector
from market_pattern_engine.detectors.market_structure import MarketStructureDetector
from market_pattern_engine.detectors.smart_money import NativeSmartMoneyDetector
from market_pattern_engine.detectors.support_resistance import SupportResistanceDetector
from market_pattern_engine.domain.enums import AnalysisMode
from market_pattern_engine.domain.models import MarketAnalysisRequest, MarketAnalysisResult, MarketStructure, validate_closed_candle_policy
from market_pattern_engine.infrastructure.logging import log_json
from market_pattern_engine.infrastructure.metrics import metrics
from market_pattern_engine.repositories.analysis_repository import AnalysisRepository
from .confluence_service import ConfluenceService
from .feature_export_service import FeatureExportService
from .invalidation_service import InvalidationService


class AnalysisService:
    def __init__(self, config: dict[str, Any], repository: AnalysisRepository | None = None) -> None:
        self.config = config
        self.repository = repository

    def analyze(self, request: MarketAnalysisRequest, *, setup: dict[str, Any] | None = None) -> tuple[MarketAnalysisResult, str | None, float]:
        started = time.perf_counter()
        usable_candles = validate_closed_candle_policy(request)
        context = build_market_context(exchange=request.exchange, symbol=request.symbol, timeframe=request.timeframe, candles=usable_candles, mode=request.mode.value, config=self.config)
        warnings: list[str] = []
        requested = set(request.requested_detectors or ["candlestick", "support_resistance", "market_structure", "chart_patterns", "smart_money"])
        candles, sr_zones, structures, chart_patterns, smart_money = [], [], [], [], []
        detector_plan = [
            ("candlestick", NativeCandlestickDetector(self.config), candles),
            ("support_resistance", SupportResistanceDetector(self.config), sr_zones),
            ("market_structure", MarketStructureDetector(self.config), structures),
            ("chart_patterns", NativeChartPatternDetector(self.config), chart_patterns),
            ("smart_money", NativeSmartMoneyDetector(self.config), smart_money),
        ]
        ran_detectors: list[str] = []
        skipped_detectors: list[str] = []
        for key, detector, target in detector_plan:
            if key not in requested:
                skipped_detectors.append(key)
                continue
            rows, detector_warnings = detector.run(context)
            ran_detectors.append(key)
            target.extend(rows)
            warnings.extend(detector_warnings)
        structure = structures[0] if structures else MarketStructure()
        support = [zone for zone in sr_zones if zone.type == "support"]
        resistance = [zone for zone in sr_zones if zone.type == "resistance"]
        confluence = ConfluenceService().build(structure=structure, candles=candles, support_zones=support, resistance_zones=resistance, smart_money=smart_money, data_quality_score=context.data_quality.score)
        config_version = str(self.config.get("engine", {}).get("config_version", "2026.07.18"))
        feature_vector = FeatureExportService().build(structure=structure, support_zones=support, resistance_zones=resistance, candles=candles, chart_patterns=chart_patterns, smart_money=smart_money, confluence=confluence, atr=context.atr, last_close=context.last_close, engine_version=ENGINE_VERSION, config_version=config_version)
        previous = self.repository.get_snapshot(request.previous_snapshot_id) if self.repository and request.previous_snapshot_id else None
        result = MarketAnalysisResult(symbol=request.symbol, timeframe=request.timeframe, exchange=request.exchange, candle_close_time=context.closed_candles[-1].timestamp, analysis_mode=request.mode, market_structure=structure, support_zones=support, resistance_zones=resistance, candlestick_patterns=candles, chart_patterns=chart_patterns, smart_money=smart_money, confluence=confluence, recheck=None, feature_vector=feature_vector, data_quality=context.data_quality, warnings=warnings + context.data_quality.warnings, provider_versions={"native": ENGINE_VERSION}, engine_version=ENGINE_VERSION, config_version=config_version)
        if request.mode == AnalysisMode.RECHECK_MODE:
            result.recheck = InvalidationService(self.config).recheck(previous, result, setup=setup)
        snapshot_id = self.repository.save_snapshot(result) if self.repository else None
        elapsed = (time.perf_counter() - started) * 1000
        metrics.inc("analysis_requests")
        metrics.observe_ms("analysis.processing.ms", elapsed)
        log_json("market_analysis_completed", correlation_id=request.correlation_id, symbol=request.symbol, timeframe=request.timeframe, mode=request.mode.value, candle_count=len(request.candles), detectors_ran=ran_detectors, detectors_skipped=skipped_detectors, processing_time_ms=round(elapsed, 3), patterns_detected=len(candles) + len(chart_patterns), support_zones=len(support), resistance_zones=len(resistance), data_quality=context.data_quality.score, config_version=config_version, engine_version=ENGINE_VERSION)
        return result, snapshot_id, elapsed
