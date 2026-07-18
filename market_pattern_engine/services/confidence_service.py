from __future__ import annotations

from typing import Any

from market_pattern_engine.detectors.base import clamp01


class ConfidenceService:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def weighted(self, **parts: float) -> tuple[float, dict[str, float]]:
        weights = self.config.get("confidence", {})
        total_weight = 0.0
        total = 0.0
        breakdown: dict[str, float] = {}
        for key, value in parts.items():
            weight = float(weights.get(key, 0.0) or 0.0)
            score = clamp01(float(value or 0.0))
            total += score * weight
            total_weight += weight
            breakdown[key] = score
        if total_weight <= 0:
            return 0.0, breakdown
        return clamp01(total / total_weight), breakdown
