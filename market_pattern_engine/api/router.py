from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from market_pattern_engine.api.request_models import BatchMarketAnalysisRequest, RecheckMarketAnalysisRequest
from market_pattern_engine.api.response_models import MarketPatternResponse
from market_pattern_engine.domain.enums import AnalysisMode
from market_pattern_engine.domain.models import MarketAnalysisRequest
from market_pattern_engine.infrastructure.config_loader import load_engine_config
from market_pattern_engine.infrastructure.metrics import metrics
from market_pattern_engine.repositories.analysis_repository import AnalysisRepository
from market_pattern_engine.services.analysis_service import AnalysisService
from market_pattern_engine.services.batch_analysis_service import BatchAnalysisService


router = APIRouter(prefix="/api/v1/market-pattern", tags=["market-pattern"])


def _repository(request: Request) -> AnalysisRepository:
    config = load_engine_config()
    db = getattr(request.app.state, "market_pattern_db", None)
    return AnalysisRepository(db=db, config=config)


def _service(request: Request) -> AnalysisService:
    config = load_engine_config()
    return AnalysisService(config, _repository(request))


@router.post("/analyze", response_model=MarketPatternResponse)
def analyze(payload: MarketAnalysisRequest, request: Request) -> MarketPatternResponse:
    try:
        result, snapshot_id, elapsed = _service(request).analyze(payload)
        return MarketPatternResponse(success=True, correlation_id=payload.correlation_id, snapshot_id=snapshot_id, symbol=payload.symbol, timeframe=payload.timeframe, analysis_mode=payload.mode.value, result=result.model_dump(mode="json"), warnings=result.warnings, processing_time_ms=round(elapsed, 3))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/analyze-batch")
async def analyze_batch(payload: BatchMarketAnalysisRequest, request: Request) -> dict[str, Any]:
    metrics.inc("batch_requests")
    config = load_engine_config()
    service = AnalysisService(config, _repository(request))
    rows = await BatchAnalysisService(service, max_concurrency=int(config.get("engine", {}).get("max_concurrency", 8))).analyze_batch(payload.requests)
    return {"success": True, "items": rows}


@router.post("/recheck", response_model=MarketPatternResponse)
def recheck(payload: RecheckMarketAnalysisRequest, request: Request) -> MarketPatternResponse:
    try:
        request_payload = MarketAnalysisRequest(**{**payload.model_dump(), "mode": AnalysisMode.RECHECK_MODE})
        result, snapshot_id, elapsed = _service(request).analyze(request_payload, setup=payload.setup)
        return MarketPatternResponse(success=True, correlation_id=payload.correlation_id, snapshot_id=snapshot_id, symbol=payload.symbol, timeframe=payload.timeframe, analysis_mode=AnalysisMode.RECHECK_MODE.value, result=result.model_dump(mode="json"), warnings=result.warnings, processing_time_ms=round(elapsed, 3))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/snapshot/{snapshot_id}")
def snapshot(snapshot_id: str, request: Request) -> dict[str, Any]:
    try:
        return _repository(request).get_snapshot(snapshot_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/latest")
def latest(request: Request, symbol: str | None = None, timeframe: str | None = None, limit: int = 20) -> dict[str, Any]:
    return {"items": _repository(request).latest(symbol=symbol, timeframe=timeframe, limit=limit)}


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    return _repository(request).health()


@router.get("/metrics")
def metric_snapshot() -> dict[str, Any]:
    return metrics.snapshot()
