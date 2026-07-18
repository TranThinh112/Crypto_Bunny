from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from market_pattern_engine.domain.enums import AnalysisMode
from market_pattern_engine.domain.models import Candle, MarketAnalysisRequest, MarketAnalysisResult
from market_pattern_engine.infrastructure.config_loader import load_engine_config
from market_pattern_engine.repositories.analysis_repository import AnalysisRepository
from market_pattern_engine.services.analysis_service import AnalysisService

from .models import MarketSnapshot, TradeCandidate


def _market_pattern_settings(config: dict[str, Any]) -> dict[str, Any]:
    settings = config.get("market_pattern_engine", {})
    return settings if isinstance(settings, dict) else {}


def market_pattern_enabled(config: dict[str, Any]) -> bool:
    return bool(_market_pattern_settings(config).get("enabled", True))


def _row_timestamp(value: Any) -> datetime:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    if number > 10_000_000_000:
        return datetime.fromtimestamp(number / 1000.0, tz=timezone.utc)
    return datetime.fromtimestamp(max(0.0, number), tz=timezone.utc)


def _ohlcv_to_candles(rows: list[list[Any]], *, max_candles: int) -> list[Candle]:
    candles: list[Candle] = []
    for row in rows[-max(20, max_candles) :]:
        if len(row) < 6:
            continue
        candles.append(
            Candle(
                timestamp=_row_timestamp(row[0]),
                open=Decimal(str(row[1])),
                high=Decimal(str(row[2])),
                low=Decimal(str(row[3])),
                close=Decimal(str(row[4])),
                volume=Decimal(str(row[5])),
                is_closed=True,
            )
        )
    return candles


def _compact_market_pattern_result(result: MarketAnalysisResult, snapshot_id: str | None) -> dict[str, Any]:
    structure = result.market_structure
    confluence = result.confluence
    return {
        "snapshot_id": snapshot_id,
        "symbol": result.symbol,
        "timeframe": result.timeframe,
        "candle_close_time": result.candle_close_time.isoformat(),
        "trend_regime": structure.trend_regime,
        "structure_state": structure.structure_state,
        "trend_strength": round(float(structure.trend_strength or 0.0), 4),
        "bos_detected": bool(structure.bos.detected),
        "bos_direction": structure.bos.direction.value if structure.bos.direction else None,
        "choch_detected": bool(structure.choch.detected),
        "choch_direction": structure.choch.direction.value if structure.choch.direction else None,
        "confluence_bias": confluence.bias.value,
        "confluence_score": round(float(confluence.confluence_score or 0.0), 4),
        "data_quality_score": round(float(result.data_quality.score or 0.0), 4),
        "candlestick_count": len(result.candlestick_patterns),
        "chart_pattern_count": len(result.chart_patterns),
        "smart_money_count": len(result.smart_money),
        "support_zone_count": len(result.support_zones),
        "resistance_zone_count": len(result.resistance_zones),
        "feature_vector": result.feature_vector.model_dump(mode="json"),
        "warnings": result.warnings[:5],
    }


def analyze_market_pattern_snapshots(
    config: dict[str, Any],
    snapshots: list[MarketSnapshot],
    *,
    correlation_id: str,
    source: str,
    mode: AnalysisMode = AnalysisMode.SCAN_MODE,
    repository: AnalysisRepository | None = None,
) -> dict[str, Any]:
    if not market_pattern_enabled(config):
        return {"enabled": False, "source": source, "analyzed": 0, "by_symbol": {}, "warnings": []}
    settings = _market_pattern_settings(config)
    max_symbols = max(1, min(30, int(settings.get("max_snapshots_per_scan", 3) or 3)))
    max_candles = max(20, min(500, int(settings.get("max_candles", 220) or 220)))
    requested = settings.get("requested_detectors")
    requested_detectors = [str(item) for item in requested] if isinstance(requested, list) else None
    usable_snapshots = [snapshot for snapshot in snapshots if snapshot and snapshot.ohlcv][:max_symbols]
    if not usable_snapshots:
        return {"enabled": True, "source": source, "analyzed": 0, "by_symbol": {}, "warnings": ["No OHLCV snapshots available for Market Pattern Engine"]}
    try:
        engine_config = load_engine_config()
        service = AnalysisService(engine_config, repository or AnalysisRepository(config=engine_config))
    except Exception as exc:
        return {"enabled": True, "source": source, "analyzed": 0, "by_symbol": {}, "warnings": [f"Market Pattern Engine unavailable: {exc}"]}

    by_symbol: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for snapshot in usable_snapshots:
        try:
            candles = _ohlcv_to_candles(snapshot.ohlcv, max_candles=max_candles)
            request = MarketAnalysisRequest(
                symbol=snapshot.symbol,
                timeframe=snapshot.ohlcv_timeframe or config.get("strategy", {}).get("timeframe", "15m"),
                exchange=str(config.get("exchange", {}).get("name") or "OKX"),
                candles=candles,
                mode=mode,
                requested_detectors=requested_detectors,
                correlation_id=f"{correlation_id}:{snapshot.symbol}",
            )
            result, snapshot_id, _elapsed = service.analyze(request)
            compact = _compact_market_pattern_result(result, snapshot_id)
            snapshot.market_pattern_analysis = compact
            by_symbol[snapshot.symbol] = compact
        except Exception as exc:
            warnings.append(f"{snapshot.symbol}: Market Pattern Engine failed: {exc}")
    return {
        "enabled": True,
        "source": source,
        "analyzed": len(by_symbol),
        "by_symbol": by_symbol,
        "warnings": warnings[:20],
    }


def attach_market_pattern_features_to_candidates(candidates: list[TradeCandidate], by_symbol: dict[str, dict[str, Any]]) -> None:
    if not isinstance(by_symbol, dict) or not by_symbol:
        return
    for candidate in candidates:
        analysis = by_symbol.get(candidate.symbol)
        if not analysis:
            continue
        candidate.indicator_summary["market_pattern"] = {
            "snapshot_id": analysis.get("snapshot_id"),
            "timeframe": analysis.get("timeframe"),
            "trend_regime": analysis.get("trend_regime"),
            "structure_state": analysis.get("structure_state"),
            "trend_strength": analysis.get("trend_strength"),
            "bos_detected": analysis.get("bos_detected"),
            "bos_direction": analysis.get("bos_direction"),
            "choch_detected": analysis.get("choch_detected"),
            "choch_direction": analysis.get("choch_direction"),
            "confluence_bias": analysis.get("confluence_bias"),
            "confluence_score": analysis.get("confluence_score"),
            "data_quality_score": analysis.get("data_quality_score"),
            "candlestick_count": analysis.get("candlestick_count"),
            "chart_pattern_count": analysis.get("chart_pattern_count"),
            "smart_money_count": analysis.get("smart_money_count"),
            "support_zone_count": analysis.get("support_zone_count"),
            "resistance_zone_count": analysis.get("resistance_zone_count"),
        }
        candidate.decision_metadata["market_pattern_snapshot_id"] = analysis.get("snapshot_id")
