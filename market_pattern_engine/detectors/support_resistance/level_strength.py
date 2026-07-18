from __future__ import annotations

from market_pattern_engine.detectors.base import clamp01


def level_strength(
    *,
    touch_count: int,
    successful_reactions: int,
    failed_reactions: int,
    volume_score: float,
    freshness_score: float,
    distance_atr: float,
) -> float:
    touch_score = clamp01(touch_count / 6)
    reaction_score = successful_reactions / max(1, successful_reactions + failed_reactions)
    distance_score = clamp01(1 - min(abs(distance_atr), 5) / 5)
    return clamp01(
        touch_score * 0.25
        + reaction_score * 0.25
        + volume_score * 0.20
        + freshness_score * 0.15
        + distance_score * 0.15
    )
