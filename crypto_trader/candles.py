from __future__ import annotations

from typing import Any


PATTERN_LIBRARY: dict[str, dict[str, str]] = {
    "doji": {
        "bias": "neutral",
        "category": "indecision",
        "meaning": "Doji shows hesitation and balance between buyers and sellers.",
        "learning_note": "A doji near support or resistance often warns that the current move is losing momentum.",
    },
    "dragonfly_doji": {
        "bias": "bullish_reversal",
        "category": "reversal",
        "meaning": "Dragonfly doji often signals buyers defended lower prices.",
        "learning_note": "After a decline, a dragonfly doji can hint at a bullish reversal when confirmation follows.",
    },
    "gravestone_doji": {
        "bias": "bearish_reversal",
        "category": "reversal",
        "meaning": "Gravestone doji often signals sellers rejected higher prices.",
        "learning_note": "After an advance, a gravestone doji can hint at a bearish reversal when the next candle confirms weakness.",
    },
    "bullish_engulfing": {
        "bias": "bullish_reversal",
        "category": "reversal",
        "meaning": "Bullish engulfing shows strong buying pressure after weakness.",
        "learning_note": "A bullish engulfing candle is stronger when it appears after a short decline or near support.",
    },
    "bearish_engulfing": {
        "bias": "bearish_reversal",
        "category": "reversal",
        "meaning": "Bearish engulfing shows strong selling pressure after strength.",
        "learning_note": "A bearish engulfing candle is stronger when it appears after an advance or near resistance.",
    },
    "hammer": {
        "bias": "bullish_reversal",
        "category": "reversal",
        "meaning": "Hammer suggests demand stepped in aggressively after lower prices were rejected.",
        "learning_note": "A hammer after a pullback is a common bullish reversal signal, especially on 15m and 1h charts.",
    },
    "hanging_man": {
        "bias": "bearish_reversal",
        "category": "reversal",
        "meaning": "Hanging man warns that buyers may be losing control after an up move.",
        "learning_note": "The signal is stronger when the next candle confirms downside follow-through.",
    },
    "inverted_hammer": {
        "bias": "bullish_reversal",
        "category": "reversal",
        "meaning": "Inverted hammer suggests bullish reaction after a decline.",
        "learning_note": "An inverted hammer becomes more useful when trend is down and the next candle confirms upside intent.",
    },
    "shooting_star": {
        "bias": "bearish_reversal",
        "category": "reversal",
        "meaning": "Shooting star suggests rejection of higher prices after an up move.",
        "learning_note": "A shooting star near resistance is a classic bearish reversal warning.",
    },
    "piercing_line": {
        "bias": "bullish_reversal",
        "category": "reversal",
        "meaning": "Piercing line shows buyers recovered a large part of the previous bearish candle.",
        "learning_note": "On wider frames, piercing line often hints that downside momentum is fading.",
    },
    "dark_cloud_cover": {
        "bias": "bearish_reversal",
        "category": "reversal",
        "meaning": "Dark cloud cover shows sellers pushed back into the prior bullish candle.",
        "learning_note": "This pattern is stronger when it appears after an extended rise.",
    },
    "bullish_harami": {
        "bias": "bullish_reversal",
        "category": "reversal",
        "meaning": "Bullish harami suggests downside momentum is slowing.",
        "learning_note": "Harami patterns are weaker than engulfing patterns, so confirmation matters more.",
    },
    "bearish_harami": {
        "bias": "bearish_reversal",
        "category": "reversal",
        "meaning": "Bearish harami suggests upside momentum is slowing.",
        "learning_note": "Bearish harami near resistance can act as an early warning before a reversal.",
    },
    "tweezer_bottom": {
        "bias": "bullish_reversal",
        "category": "reversal",
        "meaning": "Tweezer bottom suggests repeated defense of the same low.",
        "learning_note": "Matching lows across candles can show demand is absorbing sell pressure.",
    },
    "tweezer_top": {
        "bias": "bearish_reversal",
        "category": "reversal",
        "meaning": "Tweezer top suggests repeated rejection of the same high.",
        "learning_note": "Matching highs across candles can show supply is capping the move.",
    },
    "morning_star": {
        "bias": "bullish_reversal",
        "category": "reversal",
        "meaning": "Morning star is a strong bullish reversal pattern after weakness.",
        "learning_note": "Morning star on 15m or 1h often gives mini a stronger reversal clue than a single candle pattern.",
    },
    "evening_star": {
        "bias": "bearish_reversal",
        "category": "reversal",
        "meaning": "Evening star is a strong bearish reversal pattern after strength.",
        "learning_note": "Evening star is more reliable on wider frames where momentum exhaustion is clearer.",
    },
    "three_white_soldiers": {
        "bias": "bullish_continuation",
        "category": "continuation",
        "meaning": "Three white soldiers show persistent aggressive buying.",
        "learning_note": "This pattern often confirms trend continuation rather than early reversal.",
    },
    "three_black_crows": {
        "bias": "bearish_continuation",
        "category": "continuation",
        "meaning": "Three black crows show persistent aggressive selling.",
        "learning_note": "This pattern often confirms trend continuation rather than early reversal.",
    },
    "bullish_marubozu": {
        "bias": "bullish_continuation",
        "category": "continuation",
        "meaning": "Bullish marubozu shows strong control by buyers.",
        "learning_note": "A bullish marubozu is stronger when it breaks structure with volume support.",
    },
    "bearish_marubozu": {
        "bias": "bearish_continuation",
        "category": "continuation",
        "meaning": "Bearish marubozu shows strong control by sellers.",
        "learning_note": "A bearish marubozu is stronger when it breaks support with volume support.",
    },
    "bullish_pin_bar": {
        "bias": "bullish_reversal",
        "category": "reversal",
        "meaning": "Bullish pin bar shows sharp rejection of lower prices.",
        "learning_note": "On 15m and 1h, bullish pin bars near support often improve reversal quality for mini analysis.",
    },
    "bearish_pin_bar": {
        "bias": "bearish_reversal",
        "category": "reversal",
        "meaning": "Bearish pin bar shows sharp rejection of higher prices.",
        "learning_note": "On 15m and 1h, bearish pin bars near resistance often improve reversal quality for mini analysis.",
    },
}


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _candle(row: list[Any]) -> dict[str, float]:
    open_price = _float(row[1])
    high = _float(row[2])
    low = _float(row[3])
    close = _float(row[4])
    volume = _float(row[5]) if len(row) > 5 else 0.0
    body = abs(close - open_price)
    range_size = max(high - low, 1e-12)
    upper_wick = max(0.0, high - max(open_price, close))
    lower_wick = max(0.0, min(open_price, close) - low)
    return {
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "body": body,
        "range": range_size,
        "upper_wick": upper_wick,
        "lower_wick": lower_wick,
        "body_ratio": body / range_size,
        "bullish": 1.0 if close > open_price else 0.0,
        "bearish": 1.0 if close < open_price else 0.0,
    }


def _is_bullish(candle: dict[str, float]) -> bool:
    return bool(candle["bullish"])


def _is_bearish(candle: dict[str, float]) -> bool:
    return bool(candle["bearish"])


def _trend_bias(candles: list[dict[str, float]]) -> str:
    if len(candles) < 3:
        return "neutral"
    closes = [item["close"] for item in candles[-3:]]
    highs = [item["high"] for item in candles[-3:]]
    lows = [item["low"] for item in candles[-3:]]
    rising = sum(1 for previous, current in zip(closes[:-1], closes[1:]) if current > previous)
    falling = sum(1 for previous, current in zip(closes[:-1], closes[1:]) if current < previous)
    if rising >= 2 and highs[-1] >= highs[0] and lows[-1] >= lows[0]:
        return "up"
    if falling >= 2 and highs[-1] <= highs[0] and lows[-1] <= lows[0]:
        return "down"
    return "neutral"


def _inside_body(inner: dict[str, float], outer: dict[str, float]) -> bool:
    outer_low = min(outer["open"], outer["close"])
    outer_high = max(outer["open"], outer["close"])
    inner_low = min(inner["open"], inner["close"])
    inner_high = max(inner["open"], inner["close"])
    return inner_low >= outer_low and inner_high <= outer_high


def _same_level(value_a: float, value_b: float, last_close: float, avg_range: float) -> bool:
    tolerance = max(last_close * 0.0015, avg_range * 0.12)
    return abs(value_a - value_b) <= tolerance


def _pattern_detail(name: str, score: float) -> dict[str, Any]:
    metadata = PATTERN_LIBRARY.get(
        name,
        {
            "bias": "neutral",
            "category": "unknown",
            "meaning": name.replace("_", " "),
            "learning_note": "",
        },
    )
    return {
        "name": name,
        "bias": metadata["bias"],
        "category": metadata["category"],
        "meaning": metadata["meaning"],
        "learning_note": metadata["learning_note"],
        "score": round(score, 2),
    }


def _signal_summary(direction: str, strongest_pattern: str | None, trend: str) -> str:
    if not strongest_pattern:
        return "No clear candlestick edge"
    label = strongest_pattern.replace("_", " ")
    if direction == "bullish":
        return f"{label} supports a bullish read against {trend} trend context"
    if direction == "bearish":
        return f"{label} supports a bearish read against {trend} trend context"
    return f"{label} is present, but the overall candlestick read is mixed"


def detect_candlestick_patterns(ohlcv: list[list[Any]]) -> dict[str, Any]:
    if len(ohlcv) < 2:
        return {
            "patterns": [],
            "bullish_score": 0.0,
            "bearish_score": 0.0,
            "direction": "neutral",
            "trend_context": "neutral",
            "reversal_patterns": {"bullish": [], "bearish": []},
            "continuation_patterns": [],
            "indecision_patterns": [],
            "strongest_pattern": None,
            "pattern_details": [],
            "signal_summary": "No clear candlestick edge",
        }

    candles = [_candle(row) for row in ohlcv[-5:]]
    last = candles[-1]
    previous = candles[-2]
    setup = candles[:-1]
    trend = _trend_bias(setup)
    patterns: list[str] = []
    bullish_score = 0.0
    bearish_score = 0.0
    bullish_reversals: list[str] = []
    bearish_reversals: list[str] = []
    continuation_patterns: list[str] = []
    indecision_patterns: list[str] = []
    pattern_scores: dict[str, float] = {}

    def add_pattern(
        name: str,
        *,
        bullish: float = 0.0,
        bearish: float = 0.0,
        bucket: str = "reversal",
    ) -> None:
        nonlocal bullish_score, bearish_score
        patterns.append(name)
        if bullish:
            bullish_score += bullish
            pattern_scores[name] = bullish
            if bucket == "reversal":
                bullish_reversals.append(name)
            elif bucket == "continuation":
                continuation_patterns.append(name)
        if bearish:
            bearish_score += bearish
            pattern_scores[name] = bearish
            if bucket == "reversal":
                bearish_reversals.append(name)
            elif bucket == "continuation":
                continuation_patterns.append(name)
        if bucket == "indecision":
            indecision_patterns.append(name)
            pattern_scores[name] = max(bullish, bearish, pattern_scores.get(name, 0.0))

    if last["body_ratio"] <= 0.1:
        add_pattern("doji", bucket="indecision")
        if last["lower_wick"] >= last["range"] * 0.55 and last["upper_wick"] <= last["range"] * 0.12:
            add_pattern("dragonfly_doji", bullish=1.6)
        if last["upper_wick"] >= last["range"] * 0.55 and last["lower_wick"] <= last["range"] * 0.12:
            add_pattern("gravestone_doji", bearish=1.6)

    if (
        _is_bullish(last)
        and _is_bearish(previous)
        and last["open"] <= previous["close"]
        and last["close"] >= previous["open"]
        and last["body"] >= previous["body"] * 0.8
    ):
        add_pattern("bullish_engulfing", bullish=3.0)

    if (
        _is_bearish(last)
        and _is_bullish(previous)
        and last["open"] >= previous["close"]
        and last["close"] <= previous["open"]
        and last["body"] >= previous["body"] * 0.8
    ):
        add_pattern("bearish_engulfing", bearish=3.0)

    long_lower_shadow = (
        last["lower_wick"] >= last["body"] * 2.2
        and last["upper_wick"] <= max(last["body"] * 0.6, last["range"] * 0.18)
    )
    long_upper_shadow = (
        last["upper_wick"] >= last["body"] * 2.2
        and last["lower_wick"] <= max(last["body"] * 0.6, last["range"] * 0.18)
    )

    if long_lower_shadow:
        if trend == "up":
            add_pattern("hanging_man", bearish=2.0)
        else:
            add_pattern("hammer", bullish=1.8)
        if last["body_ratio"] <= 0.35:
            if trend == "up":
                add_pattern("bearish_pin_bar", bearish=1.4)
            else:
                add_pattern("bullish_pin_bar", bullish=1.4)

    if long_upper_shadow:
        if trend == "down":
            add_pattern("inverted_hammer", bullish=1.7)
        else:
            add_pattern("shooting_star", bearish=1.8)
        if last["body_ratio"] <= 0.35:
            if trend == "down":
                add_pattern("bullish_pin_bar", bullish=1.2)
            else:
                add_pattern("bearish_pin_bar", bearish=1.4)

    midpoint = (previous["open"] + previous["close"]) / 2
    if (
        _is_bearish(previous)
        and _is_bullish(last)
        and last["open"] <= previous["close"] * 1.002
        and midpoint < last["close"] < previous["open"]
    ):
        add_pattern("piercing_line", bullish=2.4)
    if (
        _is_bullish(previous)
        and _is_bearish(last)
        and last["open"] >= previous["close"] * 0.998
        and previous["open"] < last["close"] < midpoint
    ):
        add_pattern("dark_cloud_cover", bearish=2.4)

    if (
        previous["body"] >= last["body"] * 1.5
        and _inside_body(last, previous)
        and _is_bearish(previous)
        and _is_bullish(last)
    ):
        add_pattern("bullish_harami", bullish=2.0)
    if (
        previous["body"] >= last["body"] * 1.5
        and _inside_body(last, previous)
        and _is_bullish(previous)
        and _is_bearish(last)
    ):
        add_pattern("bearish_harami", bearish=2.0)

    avg_range = sum(item["range"] for item in candles[-3:]) / min(len(candles), 3)
    if _same_level(last["low"], previous["low"], last["close"], avg_range) and _is_bearish(previous) and _is_bullish(last):
        add_pattern("tweezer_bottom", bullish=1.6)
    if _same_level(last["high"], previous["high"], last["close"], avg_range) and _is_bullish(previous) and _is_bearish(last):
        add_pattern("tweezer_top", bearish=1.6)

    if len(candles) >= 3:
        first, middle, third = candles[-3], candles[-2], candles[-1]
        midpoint = (first["open"] + first["close"]) / 2
        if (
            _is_bearish(first)
            and middle["body_ratio"] <= 0.35
            and _is_bullish(third)
            and third["close"] > midpoint
        ):
            add_pattern("morning_star", bullish=3.5)
        if (
            _is_bullish(first)
            and middle["body_ratio"] <= 0.35
            and _is_bearish(third)
            and third["close"] < midpoint
        ):
            add_pattern("evening_star", bearish=3.5)

        if all(_is_bullish(item) and item["body_ratio"] >= 0.45 for item in candles[-3:]):
            add_pattern("three_white_soldiers", bullish=2.5, bucket="continuation")
        if all(_is_bearish(item) and item["body_ratio"] >= 0.45 for item in candles[-3:]):
            add_pattern("three_black_crows", bearish=2.5, bucket="continuation")

    if _is_bullish(last) and last["body_ratio"] >= 0.75:
        add_pattern("bullish_marubozu", bullish=1.4, bucket="continuation")
    if _is_bearish(last) and last["body_ratio"] >= 0.75:
        add_pattern("bearish_marubozu", bearish=1.4, bucket="continuation")

    if bullish_score > bearish_score:
        direction = "bullish"
    elif bearish_score > bullish_score:
        direction = "bearish"
    else:
        direction = "neutral"

    unique_patterns = list(dict.fromkeys(patterns))
    strongest_pattern = None
    if pattern_scores:
        strongest_pattern = max(pattern_scores.items(), key=lambda item: item[1])[0]
    detail_keys = list(dict.fromkeys(unique_patterns))
    pattern_details = [_pattern_detail(name, pattern_scores.get(name, 0.0)) for name in detail_keys]

    return {
        "patterns": unique_patterns,
        "bullish_score": round(bullish_score, 2),
        "bearish_score": round(bearish_score, 2),
        "direction": direction,
        "trend_context": trend,
        "reversal_patterns": {
            "bullish": list(dict.fromkeys(bullish_reversals)),
            "bearish": list(dict.fromkeys(bearish_reversals)),
        },
        "continuation_patterns": list(dict.fromkeys(continuation_patterns)),
        "indecision_patterns": list(dict.fromkeys(indecision_patterns)),
        "strongest_pattern": strongest_pattern,
        "pattern_details": pattern_details,
        "signal_summary": _signal_summary(direction, strongest_pattern, trend),
        "last_body_pct": round(last["body_ratio"] * 100, 2),
        "last_upper_wick_pct": round(last["upper_wick"] / last["range"] * 100, 2),
        "last_lower_wick_pct": round(last["lower_wick"] / last["range"] * 100, 2),
    }
