from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from market_pattern_engine.domain.models import Candle, DataQuality, finite_float
from market_pattern_engine.infrastructure.metrics import metrics


@dataclass(frozen=True)
class MarketContext:
    exchange: str
    symbol: str
    timeframe: str
    candles: list[Candle]
    closed_candles: list[Candle]
    frame: pd.DataFrame
    config: dict[str, Any]
    mode: str
    data_quality: DataQuality

    @property
    def last_close(self) -> float:
        return float(self.frame["close"].iloc[-1])

    @property
    def atr(self) -> float:
        return float(self.frame["atr"].iloc[-1] or 0.0)


class Detector:
    name = "base"
    detector_source = "NATIVE"
    detector_version = "1.0.0"
    min_candles = 20

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def detect(self, context: MarketContext) -> list[Any]:
        raise NotImplementedError

    def run(self, context: MarketContext) -> tuple[list[Any], list[str]]:
        started = time.perf_counter()
        try:
            result = self.detect(context)
            metrics.observe_ms(f"detector.{self.name}.ms", (time.perf_counter() - started) * 1000)
            return result, []
        except Exception as exc:
            metrics.inc(f"detector.{self.name}.errors")
            return [], [f"{self.name}: {exc}"]


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value) if math.isfinite(float(value)) else 0.0))


def build_market_context(
    *,
    exchange: str,
    symbol: str,
    timeframe: str,
    candles: list[Candle],
    mode: str,
    config: dict[str, Any],
) -> MarketContext:
    closed = [item for item in candles if item.is_closed]
    rows = [
        {
            "timestamp": candle.timestamp,
            "open": finite_float(candle.open),
            "high": finite_float(candle.high),
            "low": finite_float(candle.low),
            "close": finite_float(candle.close),
            "volume": finite_float(candle.volume),
            "is_closed": candle.is_closed,
        }
        for candle in (closed if mode == "SCAN_MODE" else candles)
    ]
    frame = pd.DataFrame(rows)
    frame["prev_close"] = frame["close"].shift(1)
    tr_components = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - frame["prev_close"]).abs(),
            (frame["low"] - frame["prev_close"]).abs(),
        ],
        axis=1,
    )
    frame["true_range"] = tr_components.max(axis=1).fillna(frame["high"] - frame["low"])
    frame["atr"] = frame["true_range"].rolling(14, min_periods=1).mean()
    frame["ema20"] = frame["close"].ewm(span=20, adjust=False).mean()
    frame["ema50"] = frame["close"].ewm(span=50, adjust=False).mean()
    frame["sma_volume20"] = frame["volume"].rolling(20, min_periods=1).mean()
    diff = frame["close"].diff()
    gain = diff.clip(lower=0).rolling(14, min_periods=1).mean()
    loss = (-diff.clip(upper=0)).rolling(14, min_periods=1).mean()
    rs = gain / loss.replace(0, np.nan)
    frame["rsi"] = (100 - (100 / (1 + rs))).fillna(50)
    warnings: list[str] = []
    if len(closed) < len(candles):
        warnings.append("Request includes provisional candles")
    if (frame["volume"] == 0).any():
        warnings.append("Some candles have zero volume")
    score = 1.0
    dq = config.get("data_quality", {})
    if (frame["volume"] == 0).any():
        score -= float(dq.get("zero_volume_penalty", 0.15) or 0.15)
    if len(closed) < len(candles):
        score -= float(dq.get("provisional_penalty", 0.15) or 0.15)
    return MarketContext(
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        candles=candles,
        closed_candles=closed,
        frame=frame,
        config=config,
        mode=mode,
        data_quality=DataQuality(
            score=clamp01(score),
            warnings=warnings,
            candle_count=len(candles),
            closed_candle_count=len(closed),
            provisional=len(closed) < len(candles),
        ),
    )


def candle_ratios(row: pd.Series) -> dict[str, float]:
    body = abs(float(row.close) - float(row.open))
    candle_range = max(float(row.high) - float(row.low), 1e-12)
    upper = max(0.0, float(row.high) - max(float(row.open), float(row.close)))
    lower = max(0.0, min(float(row.open), float(row.close)) - float(row.low))
    return {
        "body": body,
        "range": candle_range,
        "body_ratio": body / candle_range,
        "upper_shadow_ratio": upper / candle_range,
        "lower_shadow_ratio": lower / candle_range,
        "upper_to_body": upper / max(body, 1e-12),
        "lower_to_body": lower / max(body, 1e-12),
    }


def direction_of(row: pd.Series) -> str:
    if float(row.close) > float(row.open):
        return "bullish"
    if float(row.close) < float(row.open):
        return "bearish"
    return "neutral"
