from __future__ import annotations

from pydantic import BaseModel, Field

from market_pattern_engine.domain.models import MarketAnalysisRequest


class BatchMarketAnalysisRequest(BaseModel):
    requests: list[MarketAnalysisRequest] = Field(default_factory=list)


class RecheckMarketAnalysisRequest(MarketAnalysisRequest):
    setup: dict | None = None
