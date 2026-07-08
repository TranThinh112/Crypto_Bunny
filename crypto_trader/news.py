from __future__ import annotations

import re
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser

from .models import NewsDigest, NewsItem


ALIASES: dict[str, list[str]] = {
    "BTC": ["btc", "bitcoin"],
    "ETH": ["eth", "ethereum", "ether"],
    "SOL": ["sol", "solana"],
    "BNB": ["bnb", "binance coin", "binance"],
    "XRP": ["xrp", "ripple"],
    "ADA": ["ada", "cardano"],
    "DOGE": ["doge", "dogecoin"],
    "AVAX": ["avax", "avalanche"],
    "LINK": ["link", "chainlink"],
    "TON": ["ton", "toncoin"],
    "SUI": ["sui"],
}

BULLISH_TERMS: dict[str, float] = {
    "etf approval": 7,
    "approved": 3,
    "approval": 3,
    "institutional inflow": 5,
    "inflows": 3,
    "partnership": 4,
    "integrates": 3,
    "listing": 4,
    "upgrade": 3,
    "mainnet": 3,
    "breakout": 3,
    "rally": 3,
    "surge": 3,
    "record high": 4,
    "accumulat": 3,
    "bullish": 3,
}

BEARISH_TERMS: dict[str, float] = {
    "hack": -8,
    "exploit": -8,
    "breach": -6,
    "lawsuit": -6,
    "sues": -6,
    "sec charges": -7,
    "investigation": -4,
    "outflows": -4,
    "delisting": -7,
    "downtime": -5,
    "halt": -4,
    "token unlock": -4,
    "sell-off": -4,
    "selloff": -4,
    "plunge": -4,
    "crash": -5,
    "bearish": -3,
}


def base_from_symbol(symbol: str) -> str:
    return symbol.split("/")[0].split(":")[0].upper()


def _published_at(entry: Any) -> datetime:
    for attr in ("published", "updated", "created"):
        raw = getattr(entry, attr, None) or entry.get(attr)
        if raw:
            try:
                parsed = parsedate_to_datetime(raw)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except (TypeError, ValueError):
                pass
    if getattr(entry, "published_parsed", None):
        parsed_tuple = entry.published_parsed
        return datetime(*parsed_tuple[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip()


def detect_symbols(text: str, symbols: list[str]) -> list[str]:
    lowered = text.lower()
    bases = [base_from_symbol(symbol) for symbol in symbols]
    detected: list[str] = []
    for base in bases:
        aliases = ALIASES.get(base, [base.lower()])
        for alias in aliases:
            pattern = rf"(?<![a-z0-9]){re.escape(alias.lower())}(?![a-z0-9])"
            if re.search(pattern, lowered):
                detected.append(base)
                break
    return sorted(set(detected))


def score_sentiment(text: str) -> tuple[float, str]:
    lowered = text.lower()
    score = 0.0
    for term, points in BULLISH_TERMS.items():
        if term in lowered:
            score += points
    for term, points in BEARISH_TERMS.items():
        if term in lowered:
            score += points
    score = max(-10.0, min(10.0, score))
    if score >= 2:
        return score, "bullish"
    if score <= -2:
        return score, "bearish"
    return score, "neutral"


def _parse_feed(feed_url: str, *, timeout_seconds: float) -> Any:
    request = urllib.request.Request(
        feed_url,
        headers={"User-Agent": "Crypto_Bunny/1.0"},
    )
    with urllib.request.urlopen(request, timeout=max(1.0, float(timeout_seconds))) as response:
        return feedparser.parse(response.read())


def collect_news(config: dict[str, Any]) -> NewsDigest:
    news_config = config["news"]
    symbols = config["strategy"]["symbols"]
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=float(news_config["lookback_hours"]))
    max_items = int(news_config.get("max_items_per_feed", 40))
    timeout_seconds = float(news_config.get("timeout_seconds", 5) or 5)
    seen: set[str] = set()
    items: list[NewsItem] = []

    for feed_url in news_config.get("feeds", []):
        try:
            parsed_feed = _parse_feed(str(feed_url), timeout_seconds=timeout_seconds)
        except Exception:
            continue
        source = parsed_feed.feed.get("title", feed_url)
        for entry in parsed_feed.entries[:max_items]:
            title = _clean_text(entry.get("title", ""))
            summary = _clean_text(entry.get("summary", ""))
            url = entry.get("link", "")
            key = (url or title).lower()
            if not title or key in seen:
                continue
            seen.add(key)
            published = _published_at(entry)
            if published < cutoff:
                continue
            joined = f"{title} {summary}"
            detected = detect_symbols(joined, symbols)
            if news_config.get("require_symbol_news", True) and not detected:
                continue
            score, label = score_sentiment(joined)
            items.append(
                NewsItem(
                    title=title,
                    source=source,
                    url=url,
                    published_at=published,
                    summary=summary[:500],
                    symbols=detected,
                    sentiment_score=score,
                    sentiment_label=label,
                )
            )

    weighted_scores: dict[str, float] = defaultdict(float)
    weights: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for item in items:
        age_hours = max(0.0, (now - item.published_at).total_seconds() / 3600)
        recency_weight = max(0.25, 1.0 - age_hours / max(1.0, float(news_config["lookback_hours"])))
        for symbol in item.symbols:
            weighted_scores[symbol] += item.sentiment_score * recency_weight
            weights[symbol] += recency_weight
            counts[symbol] += 1

    by_symbol_score = {
        symbol: round(weighted_scores[symbol] / weights[symbol], 2)
        for symbol in weighted_scores
        if weights[symbol]
    }
    return NewsDigest(items=items, by_symbol_score=by_symbol_score, by_symbol_count=dict(counts))
