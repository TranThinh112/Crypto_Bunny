from __future__ import annotations

from market_pattern_engine.detectors.base import Detector, MarketContext, clamp01
from market_pattern_engine.detectors.support_resistance.level_clustering import cluster_levels
from market_pattern_engine.detectors.support_resistance.level_strength import level_strength
from market_pattern_engine.detectors.support_resistance.pivot_detector import detect_pivots
from market_pattern_engine.domain.models import SupportResistanceZone


class SupportResistanceDetector(Detector):
    name = "support_resistance"

    def detect(self, context: MarketContext) -> list[SupportResistanceZone]:
        cfg = context.config.get("support_resistance", {})
        tolerance = max(context.atr * float(cfg.get("clustering_atr_multiplier", 0.35) or 0.35), context.last_close * 0.0005)
        min_touches = int(cfg.get("minimum_touch_count", 2) or 2)
        pivots = detect_pivots(context)
        zones: list[SupportResistanceZone] = []
        avg_volume = float(context.frame["volume"].tail(20).mean() or 0.0)
        for cluster in cluster_levels(pivots, tolerance):
            if len(cluster) < min_touches:
                continue
            prices = [float(item["price"]) for item in cluster]
            zone_low = min(prices) - tolerance * 0.25
            zone_high = max(prices) + tolerance * 0.25
            center = sum(prices) / len(prices)
            zone_type = "support" if center <= context.last_close else "resistance"
            last_touch = max(item["time"] for item in cluster)
            freshness_score = clamp01(1 - (len(context.frame) - max(int(item["index"]) for item in cluster)) / max(len(context.frame), 1))
            volume_score = clamp01(sum(float(context.frame.iloc[int(item["index"])].volume) for item in cluster) / max(avg_volume * len(cluster), 1e-12))
            successful = 0
            failed = 0
            reaction_size = context.atr * float(cfg.get("reaction_atr_multiplier", 0.5) or 0.5)
            for item in cluster:
                idx = int(item["index"])
                future = context.frame.iloc[idx + 1:min(idx + 4, len(context.frame))]
                if future.empty:
                    continue
                if zone_type == "support" and float(future["high"].max()) - center >= reaction_size:
                    successful += 1
                elif zone_type == "resistance" and center - float(future["low"].min()) >= reaction_size:
                    successful += 1
                else:
                    failed += 1
            distance_atr = (context.last_close - center) / max(context.atr, 1e-12)
            strength = level_strength(
                touch_count=len(cluster),
                successful_reactions=successful,
                failed_reactions=failed,
                volume_score=volume_score,
                freshness_score=freshness_score,
                distance_atr=distance_atr,
            )
            zones.append(
                SupportResistanceZone(
                    type=zone_type,
                    zone_low=round(zone_low, 8),
                    zone_high=round(zone_high, 8),
                    center_price=round(center, 8),
                    touch_count=len(cluster),
                    successful_reactions=successful,
                    failed_reactions=failed,
                    strength_score=strength,
                    freshness_score=freshness_score,
                    volume_score=volume_score,
                    last_touch_at=last_touch,
                    evidence={"tolerance": tolerance, "pivot_types": list({item["type"] for item in cluster})},
                )
            )
        return sorted(zones, key=lambda item: item.strength_score, reverse=True)[:8]
