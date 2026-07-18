from __future__ import annotations

from market_pattern_engine.domain.models import SupportResistanceZone
from market_pattern_engine.detectors.base import MarketContext


def breakout_status(context: MarketContext, zones: list[SupportResistanceZone]) -> list[dict]:
    close = context.last_close
    buffer = context.atr * float(context.config.get("support_resistance", {}).get("breakout_close_atr_buffer", 0.15) or 0.15)
    rows = []
    for zone in zones:
        if zone.type == "resistance" and close > zone.zone_high + buffer:
            rows.append({"type": "breakout", "direction": "bullish", "level": zone.center_price, "confirmed_by_close": True})
        if zone.type == "support" and close < zone.zone_low - buffer:
            rows.append({"type": "breakout", "direction": "bearish", "level": zone.center_price, "confirmed_by_close": True})
    return rows
