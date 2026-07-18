from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .enums import AnalysisMode, PatternDirection, PatternStatus, SetupStatus
from .exceptions import InvalidOHLCVError


VALID_TIMEFRAMES = {"1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def finite_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


class Candle(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_closed: bool = True

    @field_validator("open", "high", "low", "close", "volume")
    @classmethod
    def decimal_must_be_finite(cls, value: Decimal) -> Decimal:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Candle contains NaN or Infinity")
        return value

    @model_validator(mode="after")
    def validate_ohlcv(self) -> "Candle":
        if self.volume < 0:
            raise ValueError("Volume cannot be negative")
        high = max(self.open, self.close, self.high)
        low = min(self.open, self.close, self.low)
        if self.high != high:
            raise ValueError("High must be >= open, close and low")
        if self.low != low:
            raise ValueError("Low must be <= open, close and high")
        return self


class MarketAnalysisRequest(BaseModel):
    symbol: str
    timeframe: str
    exchange: str = "OKX"
    candles: list[Candle]
    mode: AnalysisMode = AnalysisMode.SCAN_MODE
    previous_snapshot_id: str | None = None
    requested_detectors: list[str] | None = None
    correlation_id: str

    @model_validator(mode="after")
    def validate_request(self) -> "MarketAnalysisRequest":
        if not self.symbol or "/" not in self.symbol and "-" not in self.symbol:
            raise ValueError("Symbol must be a non-empty trading symbol")
        if self.timeframe not in VALID_TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe: {self.timeframe}")
        timestamps = [candle.timestamp for candle in self.candles]
        if timestamps != sorted(timestamps):
            raise ValueError("Candles must be sorted ascending by timestamp")
        if len(set(timestamps)) != len(timestamps):
            raise ValueError("Duplicate candle timestamp")
        if len(self.candles) < 20:
            raise ValueError("At least 20 candles are required")
        return self


class DataQuality(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    warnings: list[str] = Field(default_factory=list)
    candle_count: int = 0
    closed_candle_count: int = 0
    provisional: bool = False


class PatternDetection(BaseModel):
    detector_name: str
    detector_source: str
    detector_version: str = "1.0.0"
    pattern_type: str
    direction: PatternDirection
    start_index: int
    end_index: int
    start_time: datetime
    end_time: datetime
    confidence: float = Field(ge=0.0, le=1.0)
    status: PatternStatus
    evidence: dict[str, Any] = Field(default_factory=dict)
    detected_by: list[str] = Field(default_factory=list)
    provider_results: list[dict[str, Any]] = Field(default_factory=list)
    consensus_score: float | None = Field(default=None, ge=0.0, le=1.0)


class SupportResistanceZone(BaseModel):
    type: str
    zone_low: float
    zone_high: float
    center_price: float
    touch_count: int
    successful_reactions: int
    failed_reactions: int
    strength_score: float = Field(ge=0.0, le=1.0)
    freshness_score: float = Field(ge=0.0, le=1.0)
    volume_score: float = Field(ge=0.0, le=1.0)
    last_touch_at: datetime | None = None
    status: str = "active"
    evidence: dict[str, Any] = Field(default_factory=dict)


class StructureBreak(BaseModel):
    detected: bool = False
    direction: PatternDirection | None = None
    broken_level: float | None = None
    wick_break: bool = False
    confirmed_by_close: bool = False
    volume_confirmation: bool = False
    retested: bool = False
    false_breakout: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: dict[str, Any] = Field(default_factory=dict)


class MarketStructure(BaseModel):
    trend_regime: str = "range"
    structure_state: str = "range"
    trend_strength: float = Field(default=0.0, ge=0.0, le=1.0)
    last_swing_high: float | None = None
    last_swing_low: float | None = None
    swings: list[dict[str, Any]] = Field(default_factory=list)
    bos: StructureBreak = Field(default_factory=StructureBreak)
    choch: StructureBreak = Field(default_factory=StructureBreak)


class ChartPatternDetection(BaseModel):
    pattern: str
    direction: PatternDirection
    start_index: int
    end_index: int
    pivots: list[dict[str, Any]] = Field(default_factory=list)
    neckline: float | None = None
    breakout_level: float | None = None
    theoretical_target: float | None = None
    invalidation_level: float | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    status: PatternStatus
    evidence: dict[str, Any] = Field(default_factory=dict)


class SmartMoneyDetection(BaseModel):
    type: str
    direction: PatternDirection
    zone_low: float | None = None
    zone_high: float | None = None
    created_at: datetime | None = None
    fill_percentage: float | None = Field(default=None, ge=0.0, le=1.0)
    status: str = "active"
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: dict[str, Any] = Field(default_factory=dict)


class ConfluenceResult(BaseModel):
    bias: PatternDirection
    confluence_score: float = Field(ge=0.0, le=1.0)
    supporting_factors: list[str] = Field(default_factory=list)
    conflicting_factors: list[str] = Field(default_factory=list)
    data_quality: float = Field(ge=0.0, le=1.0)
    note: str = "Technical confluence score only; not a win probability."


class RecheckResult(BaseModel):
    setup_status: SetupStatus = SetupStatus.ACTIVE
    should_keep_setup: bool = True
    invalidation_reasons: list[str] = Field(default_factory=list)
    changes: list[dict[str, Any]] = Field(default_factory=list)


class FeatureVector(BaseModel):
    schema_version: str = "1.0"
    trend_regime: str = "range"
    trend_strength: float = 0.0
    distance_to_support_atr: float | None = None
    distance_to_resistance_atr: float | None = None
    nearest_support_strength: float = 0.0
    nearest_resistance_strength: float = 0.0
    bullish_pattern_score: float = 0.0
    bearish_pattern_score: float = 0.0
    bos_bullish: int = 0
    bos_bearish: int = 0
    choch_bullish: int = 0
    choch_bearish: int = 0
    liquidity_sweep_side: str | None = None
    fvg_near_price: int = 0
    chart_pattern: str | None = None
    technical_confluence_score: float = 0.0
    data_quality_score: float = 0.0
    detector_version: str = "1.0.0"
    config_version: str = "2026.07.18"
    metadata: dict[str, Any] = Field(default_factory=dict)


class MarketAnalysisResult(BaseModel):
    symbol: str
    timeframe: str
    exchange: str
    candle_close_time: datetime
    analysis_mode: AnalysisMode
    market_structure: MarketStructure
    support_zones: list[SupportResistanceZone] = Field(default_factory=list)
    resistance_zones: list[SupportResistanceZone] = Field(default_factory=list)
    candlestick_patterns: list[PatternDetection] = Field(default_factory=list)
    chart_patterns: list[ChartPatternDetection] = Field(default_factory=list)
    smart_money: list[SmartMoneyDetection] = Field(default_factory=list)
    confluence: ConfluenceResult
    recheck: RecheckResult | None = None
    feature_vector: FeatureVector
    data_quality: DataQuality
    warnings: list[str] = Field(default_factory=list)
    provider_versions: dict[str, str] = Field(default_factory=dict)
    engine_version: str = "1.0.0"
    config_version: str = "2026.07.18"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


def validate_closed_candle_policy(request: MarketAnalysisRequest) -> list[Candle]:
    closed = [candle for candle in request.candles if candle.is_closed]
    if request.mode == AnalysisMode.SCAN_MODE:
        if len(closed) < 20:
            raise InvalidOHLCVError("SCAN_MODE requires at least 20 closed candles")
        return closed
    if len(closed) < 20:
        raise InvalidOHLCVError("RECHECK_MODE requires at least 20 closed candles")
    return request.candles
