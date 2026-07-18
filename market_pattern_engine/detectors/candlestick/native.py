from __future__ import annotations

from typing import Any

from market_pattern_engine.detectors.base import Detector, MarketContext, candle_ratios, clamp01, direction_of
from market_pattern_engine.domain.enums import PatternDirection, PatternStatus
from market_pattern_engine.domain.models import PatternDetection


def _detection(context: MarketContext, name: str, direction: str, start: int, end: int, confidence: float, evidence: dict[str, Any]) -> PatternDetection:
    frame = context.frame
    provisional = not bool(frame.iloc[end].is_closed)
    return PatternDetection(
        detector_name=f"native_{name}",
        detector_source="NATIVE",
        pattern_type=name,
        direction=PatternDirection(direction),
        start_index=start,
        end_index=end,
        start_time=frame.iloc[start].timestamp,
        end_time=frame.iloc[end].timestamp,
        confidence=clamp01(confidence),
        status=PatternStatus.PROVISIONAL if provisional else PatternStatus.CONFIRMED,
        evidence=evidence,
        detected_by=[f"native_{name}"],
        provider_results=[{"provider": "native", "confidence": clamp01(confidence)}],
        consensus_score=clamp01(confidence),
    )


class NativeCandlestickDetector(Detector):
    name = "candlestick"

    def detect(self, context: MarketContext) -> list[PatternDetection]:
        frame = context.frame.reset_index(drop=True)
        cfg = context.config.get("candlestick", {})
        out: list[PatternDetection] = []
        if len(frame) < 3:
            return out
        i = len(frame) - 1
        last = frame.iloc[i]
        prev = frame.iloc[i - 1]
        r = candle_ratios(last)
        prev_r = candle_ratios(prev)
        last_dir = direction_of(last)
        prev_dir = direction_of(prev)
        doji_cfg = cfg.get("doji", {})
        if r["body_ratio"] <= float(doji_cfg.get("max_body_to_range_ratio", 0.1)):
            confidence = 1 - r["body_ratio"] / max(float(doji_cfg.get("max_body_to_range_ratio", 0.1)), 1e-12)
            out.append(_detection(context, "doji", "neutral", i, i, confidence, r))
            if r["lower_shadow_ratio"] >= float(doji_cfg.get("dragonfly_min_lower_shadow_to_range_ratio", 0.55)):
                out.append(_detection(context, "dragonfly_doji", "bullish", i, i, r["lower_shadow_ratio"], r))
            if r["upper_shadow_ratio"] >= float(doji_cfg.get("gravestone_min_upper_shadow_to_range_ratio", 0.55)):
                out.append(_detection(context, "gravestone_doji", "bearish", i, i, r["upper_shadow_ratio"], r))
        spin_cfg = cfg.get("spinning_top", {})
        if (
            r["body_ratio"] <= float(spin_cfg.get("max_body_to_range_ratio", 0.35))
            and r["body_ratio"] > float(doji_cfg.get("max_body_to_range_ratio", 0.1))
            and r["upper_shadow_ratio"] >= float(spin_cfg.get("min_each_shadow_to_range_ratio", 0.25))
            and r["lower_shadow_ratio"] >= float(spin_cfg.get("min_each_shadow_to_range_ratio", 0.25))
        ):
            out.append(_detection(context, "spinning_top", "neutral", i, i, 1 - r["body_ratio"], r))
        hammer_cfg = cfg.get("hammer", {})
        if (
            r["body_ratio"] <= float(hammer_cfg.get("max_body_to_range_ratio", 0.4))
            and r["lower_to_body"] >= float(hammer_cfg.get("min_lower_shadow_to_body_ratio", 2.0))
            and r["upper_shadow_ratio"] <= float(hammer_cfg.get("max_upper_shadow_to_range_ratio", 0.22))
        ):
            out.append(_detection(context, "hammer", "bullish", i, i, min(r["lower_to_body"] / 4, 1), r))
        inv_cfg = cfg.get("inverted_hammer", {})
        if (
            r["body_ratio"] <= float(inv_cfg.get("max_body_to_range_ratio", 0.4))
            and r["upper_to_body"] >= float(inv_cfg.get("min_upper_shadow_to_body_ratio", 2.0))
            and r["lower_shadow_ratio"] <= float(inv_cfg.get("max_lower_shadow_to_range_ratio", 0.22))
        ):
            out.append(_detection(context, "inverted_hammer", "bullish", i, i, min(r["upper_to_body"] / 4, 1), r))
            out.append(_detection(context, "shooting_star", "bearish", i, i, min(r["upper_to_body"] / 4, 1), r))
        engulf_cfg = cfg.get("engulfing", {})
        prev_low = min(float(prev.open), float(prev.close))
        prev_high = max(float(prev.open), float(prev.close))
        last_low = min(float(last.open), float(last.close))
        last_high = max(float(last.open), float(last.close))
        cover = (last_high - last_low) / max(prev_high - prev_low, 1e-12)
        if last_dir == "bullish" and prev_dir == "bearish" and last_low <= prev_low and last_high >= prev_high:
            out.append(_detection(context, "bullish_engulfing", "bullish", i - 1, i, min(cover / 1.5, 1), {"previous_body_covered_ratio": cover, **r}))
        if last_dir == "bearish" and prev_dir == "bullish" and last_low <= prev_low and last_high >= prev_high:
            out.append(_detection(context, "bearish_engulfing", "bearish", i - 1, i, min(cover / 1.5, 1), {"previous_body_covered_ratio": cover, **r}))
        harami_cfg = cfg.get("harami", {})
        if prev_r["body"] >= r["body"] * float(harami_cfg.get("min_previous_body_ratio", 1.5)) and last_low >= prev_low and last_high <= prev_high:
            if prev_dir == "bearish" and last_dir == "bullish":
                out.append(_detection(context, "bullish_harami", "bullish", i - 1, i, min(prev_r["body"] / max(r["body"], 1e-12) / 3, 1), r))
            if prev_dir == "bullish" and last_dir == "bearish":
                out.append(_detection(context, "bearish_harami", "bearish", i - 1, i, min(prev_r["body"] / max(r["body"], 1e-12) / 3, 1), r))
        first = frame.iloc[i - 2]
        middle = frame.iloc[i - 1]
        third = frame.iloc[i]
        middle_r = candle_ratios(middle)
        midpoint = (float(first.open) + float(first.close)) / 2
        star_max = float(cfg.get("star", {}).get("max_middle_body_to_range_ratio", 0.35))
        if direction_of(first) == "bearish" and middle_r["body_ratio"] <= star_max and direction_of(third) == "bullish" and float(third.close) > midpoint:
            out.append(_detection(context, "morning_star", "bullish", i - 2, i, clamp01((float(third.close) - midpoint) / max(float(first.open) - midpoint, 1e-12)), {"middle_body_ratio": middle_r["body_ratio"]}))
        if direction_of(first) == "bullish" and middle_r["body_ratio"] <= star_max and direction_of(third) == "bearish" and float(third.close) < midpoint:
            out.append(_detection(context, "evening_star", "bearish", i - 2, i, clamp01((midpoint - float(third.close)) / max(midpoint - float(first.open), 1e-12)), {"middle_body_ratio": middle_r["body_ratio"]}))
        if all(direction_of(frame.iloc[j]) == "bullish" and candle_ratios(frame.iloc[j])["body_ratio"] >= 0.45 for j in range(i - 2, i + 1)):
            out.append(_detection(context, "three_white_soldiers", "bullish", i - 2, i, 0.78, {"body_ratios": [candle_ratios(frame.iloc[j])["body_ratio"] for j in range(i - 2, i + 1)]}))
        if all(direction_of(frame.iloc[j]) == "bearish" and candle_ratios(frame.iloc[j])["body_ratio"] >= 0.45 for j in range(i - 2, i + 1)):
            out.append(_detection(context, "three_black_crows", "bearish", i - 2, i, 0.78, {"body_ratios": [candle_ratios(frame.iloc[j])["body_ratio"] for j in range(i - 2, i + 1)]}))
        maru_min = float(cfg.get("marubozu", {}).get("min_body_to_range_ratio", 0.75))
        if r["body_ratio"] >= maru_min:
            out.append(_detection(context, "marubozu", last_dir if last_dir in {"bullish", "bearish"} else "neutral", i, i, r["body_ratio"], r))
        return out
