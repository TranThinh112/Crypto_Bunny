from __future__ import annotations

from enum import Enum


class AnalysisMode(str, Enum):
    SCAN_MODE = "SCAN_MODE"
    RECHECK_MODE = "RECHECK_MODE"


class PatternDirection(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    MIXED = "mixed"


class PatternStatus(str, Enum):
    FORMING = "forming"
    CONFIRMED = "confirmed"
    INVALIDATED = "invalidated"
    COMPLETED = "completed"
    PROVISIONAL = "provisional"


class SetupStatus(str, Enum):
    ACTIVE = "ACTIVE"
    WEAKENED = "WEAKENED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    COMPLETED = "COMPLETED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class DetectorSource(str, Enum):
    NATIVE = "NATIVE"
    EXTERNAL_ADAPTER = "EXTERNAL_ADAPTER"
