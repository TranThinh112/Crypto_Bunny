from __future__ import annotations


def classify_structure(swings: list[dict]) -> tuple[str, str, float]:
    highs = [item for item in swings if item["type"] == "swing_high"][-3:]
    lows = [item for item in swings if item["type"] == "swing_low"][-3:]
    if len(highs) < 2 or len(lows) < 2:
        return "range", "insufficient_swings", 0.0
    higher_highs = sum(1 for left, right in zip(highs[:-1], highs[1:]) if float(right["price"]) > float(left["price"]))
    higher_lows = sum(1 for left, right in zip(lows[:-1], lows[1:]) if float(right["price"]) > float(left["price"]))
    lower_highs = sum(1 for left, right in zip(highs[:-1], highs[1:]) if float(right["price"]) < float(left["price"]))
    lower_lows = sum(1 for left, right in zip(lows[:-1], lows[1:]) if float(right["price"]) < float(left["price"]))
    total = max(len(highs) - 1 + len(lows) - 1, 1)
    bull = (higher_highs + higher_lows) / total
    bear = (lower_highs + lower_lows) / total
    if bull >= 0.75:
        return "bullish", "hh_hl", bull
    if bear >= 0.75:
        return "bearish", "lh_ll", bear
    if max(bull, bear) >= 0.5:
        return "transition", "mixed", max(bull, bear)
    return "range", "range", 1 - max(bull, bear)
