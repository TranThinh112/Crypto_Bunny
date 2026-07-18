from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MarketPatternResponse(BaseModel):
    success: bool
    correlation_id: str
    snapshot_id: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    analysis_mode: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    processing_time_ms: float = 0.0
    error: str | None = None
