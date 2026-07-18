from __future__ import annotations

from market_pattern_engine.detectors.base import MarketContext, clamp01
from market_pattern_engine.domain.enums import PatternDirection
from market_pattern_engine.domain.models import SmartMoneyDetection


def detect_fair_value_gaps(context: MarketContext) -> list[SmartMoneyDetection]:
    cfg = context.config.get("smart_money", {})
    min_gap = context.atr * float(cfg.get("fvg_min_gap_atr", 0.12) or 0.12)
    frame = context.frame.reset_index(drop=True)
    rows: list[SmartMoneyDetection] = []
    for i in range(2, len(frame)):
        a, _, c = frame.iloc[i - 2], frame.iloc[i - 1], frame.iloc[i]
        if float(c.low) - float(a.high) >= min_gap:
            zone_low, zone_high = float(a.high), float(c.low)
            fill = clamp01((zone_high - min(context.last_close, zone_high)) / max(zone_high - zone_low, 1e-12)) if context.last_close < zone_high else 0.0
            rows.append(SmartMoneyDetection(type="bullish_fvg", direction=PatternDirection.BULLISH, zone_low=zone_low, zone_high=zone_high, created_at=c.timestamp, fill_percentage=fill, status="partially_filled" if fill else "active", confidence=clamp01((zone_high - zone_low) / max(context.atr, 1e-12) / 2), evidence={"middle_candle_body": abs(float(_.close) - float(_.open))}))
        if float(a.low) - float(c.high) >= min_gap:
            zone_low, zone_high = float(c.high), float(a.low)
            fill = clamp01((max(context.last_close, zone_low) - zone_low) / max(zone_high - zone_low, 1e-12)) if context.last_close > zone_low else 0.0
            rows.append(SmartMoneyDetection(type="bearish_fvg", direction=PatternDirection.BEARISH, zone_low=zone_low, zone_high=zone_high, created_at=c.timestamp, fill_percentage=fill, status="partially_filled" if fill else "active", confidence=clamp01((zone_high - zone_low) / max(context.atr, 1e-12) / 2), evidence={"middle_candle_body": abs(float(_.close) - float(_.open))}))
    return rows[-8:]
