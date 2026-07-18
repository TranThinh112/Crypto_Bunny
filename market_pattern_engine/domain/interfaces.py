from __future__ import annotations

from typing import Protocol

from .models import ChartPatternDetection, PatternDetection


class CandlestickDetectionProvider(Protocol):
    def detect(self, market_context: object) -> list[PatternDetection]:
        ...


class TrendlineDetectionProvider(Protocol):
    def detect(self, market_context: object) -> list[dict]:
        ...


class ChartPatternDetectionProvider(Protocol):
    def detect(self, market_context: object) -> list[ChartPatternDetection]:
        ...
