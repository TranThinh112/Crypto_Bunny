from __future__ import annotations

from typing import Any

from market_pattern_engine.domain.enums import PatternDirection, SetupStatus
from market_pattern_engine.domain.models import MarketAnalysisResult, RecheckResult
from .snapshot_comparison_service import compare_snapshots


class InvalidationService:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def recheck(self, previous_snapshot: dict[str, Any] | None, current: MarketAnalysisResult, setup: dict[str, Any] | None = None) -> RecheckResult:
        reasons: list[str] = []
        if current.data_quality.score < 0.5:
            reasons.append("insufficient_data_quality")
        if current.market_structure.choch.detected:
            reasons.append(f"{current.market_structure.choch.direction.value}_choch_detected")
        side = str((setup or {}).get("side") or "").lower()
        if side == "long" and current.market_structure.choch.direction == PatternDirection.BEARISH:
            reasons.append("opposite_choch_against_long")
        if side == "short" and current.market_structure.choch.direction == PatternDirection.BULLISH:
            reasons.append("opposite_choch_against_short")
        changes = compare_snapshots(previous_snapshot, current.model_dump(mode="json"))
        if reasons:
            status = SetupStatus.INVALIDATED if any("opposite" in item for item in reasons) else SetupStatus.WEAKENED
        elif changes:
            status = SetupStatus.WEAKENED
        else:
            status = SetupStatus.ACTIVE
        return RecheckResult(setup_status=status, should_keep_setup=status != SetupStatus.INVALIDATED, invalidation_reasons=list(dict.fromkeys(reasons)), changes=changes)
