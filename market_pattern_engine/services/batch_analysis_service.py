from __future__ import annotations

import asyncio
from typing import Any

from market_pattern_engine.domain.models import MarketAnalysisRequest
from .analysis_service import AnalysisService


class BatchAnalysisService:
    def __init__(self, service: AnalysisService, max_concurrency: int = 8) -> None:
        self.service = service
        self.max_concurrency = max(1, int(max_concurrency or 8))

    async def analyze_batch(self, requests: list[MarketAnalysisRequest]) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def run_one(request: MarketAnalysisRequest) -> dict[str, Any]:
            async with semaphore:
                try:
                    result, snapshot_id, elapsed = await asyncio.to_thread(self.service.analyze, request)
                    return {"success": True, "correlation_id": request.correlation_id, "snapshot_id": snapshot_id, "symbol": request.symbol, "timeframe": request.timeframe, "analysis_mode": request.mode.value, "result": result.model_dump(mode="json"), "warnings": result.warnings, "processing_time_ms": round(elapsed, 3)}
                except Exception as exc:
                    return {"success": False, "correlation_id": request.correlation_id, "symbol": request.symbol, "timeframe": request.timeframe, "error": str(exc), "warnings": []}

        return await asyncio.gather(*(run_one(request) for request in requests))
