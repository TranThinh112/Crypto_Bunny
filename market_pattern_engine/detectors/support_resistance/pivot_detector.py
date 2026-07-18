from __future__ import annotations

from typing import Any

from market_pattern_engine.detectors.base import MarketContext


def detect_pivots(context: MarketContext) -> list[dict[str, Any]]:
    cfg = context.config.get("support_resistance", {})
    left = int(cfg.get("pivot_left_bars", 3) or 3)
    right = int(cfg.get("pivot_right_bars", 3) or 3)
    frame = context.frame.reset_index(drop=True)
    pivots: list[dict[str, Any]] = []
    for index in range(left, len(frame) - right):
        window = frame.iloc[index - left:index + right + 1]
        row = frame.iloc[index]
        if float(row.high) >= float(window["high"].max()):
            pivots.append({"type": "pivot_high", "index": index, "price": float(row.high), "time": row.timestamp})
        if float(row.low) <= float(window["low"].min()):
            pivots.append({"type": "pivot_low", "index": index, "price": float(row.low), "time": row.timestamp})
    return pivots
