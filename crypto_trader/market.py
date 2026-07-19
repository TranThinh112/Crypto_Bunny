from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any
import urllib.request
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .candles import detect_candlestick_patterns
from .indicators import adx, atr, ema, rsi, volume_ratio, vwap
from .models import MarketSnapshot


NON_CRYPTO_BASES = {
    "AAPL",
    "AMD",
    "AMZN",
    "BRENT",
    "COPPER",
    "COIN",
    "DJI",
    "DXY",
    "GOLD",
    "GOOGL",
    "META",
    "MSTR",
    "NASDAQ",
    "NDX",
    "NFLX",
    "NVDA",
    "OIL",
    "PAXG",
    "SILVER",
    "SPX",
    "TSLA",
    "UKOIL",
    "USOIL",
    "WTI",
    "XBR",
    "XAG",
    "XAU",
    "XAUT",
    "XTI",
}
NON_CRYPTO_KEYWORDS = {
    "STOCK",
    "EQUITY",
    "SHARE",
    "COMMODITY",
    "METAL",
    "FOREX",
}
DEFAULT_PRIORITY_SYMBOLS = [
    "BTC/USDT:USDT",
    "SOL/USDT:USDT",
    "ETH/USDT:USDT",
    "BNB/USDT:USDT",
    "XRP/USDT:USDT",
]
DEFAULT_WEEKDAY_PRIORITY_SYMBOLS = ["XAU/USDT:USDT"]


def prefetch_market_data(
    config: dict[str, Any],
    *,
    symbols: list[str] | None = None,
    require_all_tickers: bool = False,
) -> dict[str, Any]:
    exchange = create_exchange(config, authenticated=False)
    markets = exchange.load_markets() or getattr(exchange, "markets", {}) or {}
    warnings: list[str] = []
    tickers: dict[str, Any] = {}
    ticker_symbols = None if require_all_tickers else [str(symbol) for symbol in (symbols or []) if str(symbol)]
    try:
        tickers = _fetch_tickers_batch(exchange, ticker_symbols)
        if not isinstance(tickers, dict):
            warnings.append("Market prefetch returned invalid ticker data")
            tickers = {}
    except Exception as exc:
        warnings.append(f"Market prefetch ticker fetch failed: {exc}")
        tickers = {}
    return {
        "exchange": exchange,
        "markets": markets if isinstance(markets, dict) else {},
        "currencies": getattr(exchange, "currencies", {}) or {},
        "tickers": tickers,
        "warnings": warnings,
    }


def create_exchange(config: dict[str, Any], authenticated: bool = False) -> Any:
    import ccxt

    load_dotenv()
    exchange_name = config["exchange"].get("name", "okx")
    exchange_class = getattr(ccxt, exchange_name)
    account_type = str(config["exchange"].get("account_type", "swap") or "swap").lower()
    configured_market_types = config["exchange"].get("fetch_market_types")
    if isinstance(configured_market_types, list) and configured_market_types:
        fetch_market_types = [str(item).lower() for item in configured_market_types if str(item).strip()]
    else:
        fetch_market_types = [account_type]
    params: dict[str, Any] = {
        "enableRateLimit": True,
        "timeout": int(config["exchange"].get("timeout_ms", 10000) or 10000),
        "options": {
            "defaultType": account_type,
            "fetchMarkets": {"types": fetch_market_types},
        },
    }

    if authenticated:
        key_env = config["exchange"].get("api_key_env", "OKX_API_KEY")
        secret_env = config["exchange"].get("secret_env", "OKX_SECRET")
        passphrase_env = config["exchange"].get("passphrase_env", "OKX_PASSPHRASE")
        params.update(
            {
                "apiKey": os.getenv(key_env, ""),
                "secret": os.getenv(secret_env, ""),
                "password": os.getenv(passphrase_env, ""),
            }
        )

    exchange = exchange_class(params)
    if config.get("mode") == "demo":
        exchange.headers = {**getattr(exchange, "headers", {}), "x-simulated-trading": "1"}
        try:
            exchange.set_sandbox_mode(True)
        except Exception:
            pass
    return exchange


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _ticker_number(ticker: dict[str, Any], *keys: str) -> float | None:
    info = ticker.get("info") if isinstance(ticker.get("info"), dict) else {}
    for key in keys:
        value = _as_float(ticker.get(key))
        if value is not None:
            return value
        value = _as_float(info.get(key))
        if value is not None:
            return value
    return None


def _first_number(source: dict[str, Any] | None, *keys: str) -> float | None:
    if not isinstance(source, dict):
        return None
    info = source.get("info") if isinstance(source.get("info"), dict) else {}
    for key in keys:
        value = _as_float(source.get(key))
        if value is not None:
            return value
        value = _as_float(info.get(key))
        if value is not None:
            return value
    return None


def _ticker_quote_volume(ticker: dict[str, Any]) -> float:
    quote_volume = _ticker_number(
        ticker,
        "quoteVolume",
        "quote_volume",
        "volCcyQuote24h",
        "volUsd24h",
        "turnover",
        "turnover24h",
    )
    if quote_volume is not None:
        return max(0.0, quote_volume)

    base_volume = _ticker_number(ticker, "volCcy24h", "baseVolume", "base_volume", "vol24h")
    last = _ticker_number(ticker, "last", "close")
    if base_volume is not None and last is not None:
        return max(0.0, base_volume * last)
    return 0.0


def _market_regime_metrics_enabled(config: dict[str, Any], key: str, default: bool = False) -> bool:
    settings = config.get("market_regime", {})
    if not isinstance(settings, dict):
        return default
    return bool(settings.get(key, default))


def _fetch_derivative_metrics(exchange: Any, symbol: str) -> tuple[dict[str, float], list[str]]:
    metrics: dict[str, float] = {}
    warnings: list[str] = []
    capabilities = getattr(exchange, "has", {}) if isinstance(getattr(exchange, "has", {}), dict) else {}
    if capabilities.get("fetchFundingRate") is not False and hasattr(exchange, "fetch_funding_rate"):
        try:
            funding = exchange.fetch_funding_rate(symbol)
            funding_rate = _first_number(funding, "fundingRate", "funding_rate", "funding")
            if funding_rate is not None:
                metrics["funding_rate"] = funding_rate
        except Exception as exc:
            warnings.append(f"{symbol}: funding rate fetch failed: {exc}")
    if capabilities.get("fetchOpenInterest") is not False and hasattr(exchange, "fetch_open_interest"):
        try:
            interest = exchange.fetch_open_interest(symbol)
            open_interest = _first_number(
                interest,
                "openInterestAmount",
                "openInterestValue",
                "openInterest",
                "open_interest",
                "oi",
                "oiCcy",
                "oiUsd",
            )
            if open_interest is not None:
                metrics["open_interest"] = open_interest
        except Exception as exc:
            warnings.append(f"{symbol}: open interest fetch failed: {exc}")
    return metrics, warnings


def _fetch_fear_greed(config: dict[str, Any]) -> tuple[float | None, list[str]]:
    if not _market_regime_metrics_enabled(config, "fear_greed_enabled", False):
        return None, []
    settings = config.get("market_regime", {})
    url = str(settings.get("fear_greed_url") or "https://api.alternative.me/fng/?limit=1&format=json")
    timeout = float(settings.get("fear_greed_timeout_seconds", 3) or 3)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Crypto_Bunny/1.0"})
        with urllib.request.urlopen(request, timeout=max(1.0, timeout)) as response:
            payload = json.loads(response.read().decode("utf-8"))
        rows = payload.get("data") if isinstance(payload, dict) else None
        item = rows[0] if isinstance(rows, list) and rows else None
        value = _as_float(item.get("value")) if isinstance(item, dict) else None
        return value, [] if value is not None else ["Fear & Greed response did not include a numeric value"]
    except Exception as exc:
        return None, [f"Fear & Greed fetch failed: {exc}"]


def apply_news_scores_to_snapshots(snapshots: list[MarketSnapshot], digest: Any) -> None:
    scores = getattr(digest, "by_symbol_score", {}) or {}
    if not isinstance(scores, dict):
        return
    for snapshot in snapshots:
        base = str(snapshot.symbol or "").split("/", 1)[0].split(":", 1)[0].upper()
        value = _as_float(scores.get(base))
        snapshot.news_score = 0.0 if value is None else value


def _symbol_base(symbol: str, market: dict[str, Any]) -> str:
    base = str(market.get("base") or "").upper().strip()
    if base:
        return base
    return symbol.split("/")[0].split("-")[0].upper().strip()


def _is_crypto_market(
    symbol: str,
    market: dict[str, Any],
    *,
    excluded_bases: set[str] | None = None,
    excluded_keywords: set[str] | None = None,
) -> bool:
    base = _symbol_base(symbol, market)
    if base in (excluded_bases or set()):
        return False
    info = market.get("info") if isinstance(market.get("info"), dict) else {}
    category = str(info.get("instCategory") or info.get("category") or "").strip()
    if category and category != "1":
        return False
    text = " ".join(
        str(value or "").upper()
        for value in (
            symbol,
            market.get("id"),
            market.get("type"),
            market.get("base"),
            market.get("quote"),
            market.get("settle"),
            info.get("instType"),
            info.get("instFamily"),
            info.get("uly"),
        )
    )
    return not any(keyword in text for keyword in (excluded_keywords or set()))


def _market_matches_universe(
    symbol: str,
    market: dict[str, Any],
    quote: str,
    account_type: str,
    *,
    asset_class: str = "crypto",
    excluded_bases: set[str] | None = None,
    excluded_keywords: set[str] | None = None,
) -> bool:
    if market.get("active") is False:
        return False
    if asset_class == "crypto" and not _is_crypto_market(
        symbol,
        market,
        excluded_bases=excluded_bases,
        excluded_keywords=excluded_keywords,
    ):
        return False
    quote = quote.upper()
    quote_values = {
        str(market.get("quote") or "").upper(),
        str(market.get("settle") or "").upper(),
    }
    upper_symbol = symbol.upper()
    if quote and quote not in quote_values and f"/{quote}" not in upper_symbol:
        return False

    desired_type = account_type.lower()
    market_type = str(market.get("type") or "").lower()
    if desired_type and market_type and market_type != desired_type:
        return False
    if desired_type == "swap" and market_type != "swap" and not bool(market.get("swap")):
        return False
    return True


def _symbol_list(value: Any, fallback: list[str] | None = None) -> list[str]:
    if value is None:
        return list(fallback or [])
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return list(fallback or [])


def _local_scan_now(now: datetime | None, timezone_name: str | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    name = str(timezone_name or "Asia/Ho_Chi_Minh")
    try:
        return value.astimezone(ZoneInfo(name))
    except Exception:
        if name in {"Asia/Ho_Chi_Minh", "Asia/Saigon", "UTC+7", "+07:00"}:
            return value.astimezone(timezone(timedelta(hours=7)))
        return value.astimezone(timezone.utc)


def _append_priority_symbols(
    selected: list[str],
    markets: dict[str, Any],
    symbols: list[str],
    *,
    quote: str,
    account_type: str,
    asset_class: str,
    excluded_bases: set[str],
    excluded_keywords: set[str],
    limit: int,
) -> None:
    seen = {symbol.upper() for symbol in selected}
    for symbol in symbols:
        if len(selected) >= limit:
            return
        market = markets.get(symbol)
        if not isinstance(market, dict):
            continue
        if not _market_matches_universe(
            symbol,
            market,
            quote,
            account_type,
            asset_class=asset_class,
            excluded_bases=excluded_bases,
            excluded_keywords=excluded_keywords,
        ):
            continue
        key = symbol.upper()
        if key in seen:
            continue
        selected.append(symbol)
        seen.add(key)


def select_top_volume_symbols_from_tickers(
    markets: dict[str, Any],
    tickers: dict[str, Any],
    *,
    limit: int = 50,
    quote: str = "USDT",
    account_type: str = "swap",
    asset_class: str = "crypto",
    excluded_bases: list[str] | set[str] | None = None,
    excluded_keywords: list[str] | set[str] | None = None,
    priority_symbols: list[str] | set[str] | None = None,
    weekday_priority_symbols: list[str] | set[str] | None = None,
    priority_symbols_enabled: bool = True,
    weekday_priority_enabled: bool = True,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> list[str]:
    max_symbols = max(1, min(40, int(limit or 40)))
    ranked: list[tuple[float, str]] = []
    excluded_base_set = {item.upper() for item in (excluded_bases or NON_CRYPTO_BASES)}
    excluded_keyword_set = {item.upper() for item in (excluded_keywords or NON_CRYPTO_KEYWORDS)}
    for symbol, market in markets.items():
        if not isinstance(market, dict):
            continue
        if not _market_matches_universe(
            str(symbol),
            market,
            quote,
            account_type,
            asset_class=asset_class,
            excluded_bases=excluded_base_set,
            excluded_keywords=excluded_keyword_set,
        ):
            continue
        ticker = tickers.get(symbol) or tickers.get(str(market.get("id") or ""))
        if not isinstance(ticker, dict):
            continue
        volume = _ticker_quote_volume(ticker)
        if volume <= 0:
            continue
        ranked.append((volume, str(symbol)))

    ranked.sort(key=lambda item: item[0], reverse=True)
    selected: list[str] = []
    if priority_symbols_enabled:
        _append_priority_symbols(
            selected,
            markets,
            _symbol_list(priority_symbols),
            quote=quote,
            account_type=account_type,
            asset_class=asset_class,
            excluded_bases=excluded_base_set,
            excluded_keywords=excluded_keyword_set,
            limit=max_symbols,
        )
    if weekday_priority_enabled and _local_scan_now(now, timezone_name).weekday() < 5:
        _append_priority_symbols(
            selected,
            markets,
            _symbol_list(weekday_priority_symbols),
            quote=quote,
            account_type=account_type,
            asset_class="all",
            excluded_bases=set(),
            excluded_keywords=set(),
            limit=max_symbols,
        )
    seen = {symbol.upper() for symbol in selected}
    for _, symbol in ranked:
        if len(selected) >= max_symbols:
            break
        key = symbol.upper()
        if key in seen:
            continue
        selected.append(symbol)
        seen.add(key)
    return selected


def _fetch_tickers_batch(exchange: Any, symbols: list[str] | None = None) -> dict[str, Any]:
    fetch_symbols = [str(symbol) for symbol in (symbols or []) if str(symbol)]
    if fetch_symbols:
        try:
            tickers = exchange.fetch_tickers(fetch_symbols)
            if isinstance(tickers, dict):
                return tickers
        except TypeError:
            pass
    tickers = exchange.fetch_tickers()
    return tickers if isinstance(tickers, dict) else {}


def fetch_top_volume_symbols(
    config: dict[str, Any],
    *,
    market_data: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    strategy_config = config.get("strategy", {})
    universe = strategy_config.get("universe", {})
    limit = max(1, min(40, int(universe.get("max_symbols", 40) or 40)))
    quote = str(universe.get("quote", config.get("exchange", {}).get("default_settle", "USDT")) or "USDT")
    account_type = str(config.get("exchange", {}).get("account_type", "swap") or "swap")
    asset_class = str(universe.get("asset_class", "crypto") or "crypto")
    excluded_bases = universe.get("exclude_bases") or list(NON_CRYPTO_BASES)
    excluded_keywords = universe.get("exclude_keywords") or list(NON_CRYPTO_KEYWORDS)
    priority_symbols = _symbol_list(universe.get("priority_symbols"), DEFAULT_PRIORITY_SYMBOLS)
    weekday_priority_symbols = _symbol_list(universe.get("weekday_priority_symbols"), DEFAULT_WEEKDAY_PRIORITY_SYMBOLS)
    timezone_name = (
        universe.get("weekday_priority_timezone")
        or config.get("ai", {}).get("internal", {}).get("market_scan_timezone")
        or config.get("timezone")
        or "Asia/Ho_Chi_Minh"
    )
    warnings: list[str] = list((market_data or {}).get("warnings") or [])
    try:
        if market_data:
            exchange = market_data.get("exchange")
            markets = market_data.get("markets") or {}
            tickers = market_data.get("tickers") or {}
        else:
            prefetched = prefetch_market_data(config, require_all_tickers=True)
            exchange = prefetched.get("exchange")
            markets = prefetched.get("markets") or {}
            tickers = prefetched.get("tickers") or {}
            warnings.extend(prefetched.get("warnings") or [])
        if exchange is None:
            raise RuntimeError("Exchange is unavailable")
        if not tickers:
            tickers = _fetch_tickers_batch(exchange)
        if not isinstance(markets, dict) or not isinstance(tickers, dict):
            return [], warnings + ["Top-volume universe fetch returned invalid market data"]
        symbols = select_top_volume_symbols_from_tickers(
            markets,
            tickers,
            limit=limit,
            quote=quote,
            account_type=account_type,
            asset_class=asset_class,
            excluded_bases=excluded_bases,
            excluded_keywords=excluded_keywords,
            priority_symbols=priority_symbols,
            weekday_priority_symbols=weekday_priority_symbols,
            priority_symbols_enabled=bool(universe.get("priority_symbols_enabled", True)),
            weekday_priority_enabled=bool(universe.get("weekday_priority_enabled", True)),
            timezone_name=str(timezone_name),
        )
        if not symbols:
            return [], warnings + ["Top-volume universe returned no eligible symbols"]
        return symbols, warnings
    except Exception as exc:
        return [], warnings + [f"Top-volume universe fetch failed: {exc}"]


def _spread_pct(ticker: dict[str, Any], last: float) -> float | None:
    bid = ticker.get("bid")
    ask = ticker.get("ask")
    if not bid or not ask or not last:
        return None
    return ((ask - bid) / last) * 100


def _frame_summary(timeframe: str, symbol: str, ohlcv: list[list[float]], last: float) -> dict[str, Any]:
    if len(ohlcv) < 60:
        raise ValueError(f"{symbol} does not have enough {timeframe} OHLCV rows")
    closes = [float(row[4]) for row in ohlcv]
    highs = [float(row[2]) for row in ohlcv]
    lows = [float(row[3]) for row in ohlcv]
    ema_fast = ema(closes, 20)
    ema_slow = ema(closes, 50)
    ema_long = ema(closes, 200) if len(closes) >= 200 else None
    current_vwap = vwap(ohlcv, 200)
    current_atr = atr(ohlcv, 14)
    current_adx = adx(ohlcv, 14)
    recent_lows = lows[-40:]
    recent_highs = highs[-40:]
    support = min(recent_lows)
    resistance = max(recent_highs)
    range_size = max(resistance - support, 1e-12)
    range_position = (last - support) / range_size
    if last >= ema_slow and ema_fast >= ema_slow:
        trend = "up"
    elif last <= ema_slow and ema_fast <= ema_slow:
        trend = "down"
    else:
        trend = "mixed"
    return {
        "timeframe": timeframe,
        "last": last,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "ema200": ema_long,
        "vwap": current_vwap,
        "ema_gap_pct": ((ema_fast - ema_slow) / ema_slow) * 100 if ema_slow else 0.0,
        "price_vs_ema_slow_pct": ((last - ema_slow) / ema_slow) * 100 if ema_slow else 0.0,
        "price_vs_ema200_pct": ((last - ema_long) / ema_long) * 100 if ema_long else None,
        "price_vs_vwap_pct": ((last - current_vwap) / current_vwap) * 100 if current_vwap else None,
        "rsi": rsi(closes, 14),
        "adx": current_adx,
        "atr_pct": (current_atr / last) * 100 if last else 0.0,
        "volume_ratio": volume_ratio(ohlcv, 20),
        "support": support,
        "resistance": resistance,
        "range_position": range_position,
        "trend": trend,
        "candlestick_patterns": detect_candlestick_patterns(ohlcv),
    }


def snapshot_from_ohlcv(
    symbol: str,
    ohlcv: list[list[float]],
    ticker: dict[str, Any],
    higher_timeframes: dict[str, dict[str, Any]] | None = None,
    timeframe: str = "primary",
    market_metrics: dict[str, Any] | None = None,
) -> MarketSnapshot:
    if len(ohlcv) < 60:
        raise ValueError(f"{symbol} does not have enough OHLCV rows")
    closes = [float(row[4]) for row in ohlcv]
    highs = [float(row[2]) for row in ohlcv]
    lows = [float(row[3]) for row in ohlcv]
    last = float(ticker.get("last") or closes[-1])
    current_atr = atr(ohlcv, 14)
    recent_lows = lows[-40:]
    recent_highs = highs[-40:]
    ema_long = ema(closes, 200) if len(closes) >= 200 else None
    current_adx = adx(ohlcv, 14)
    metrics = market_metrics if isinstance(market_metrics, dict) else {}
    return MarketSnapshot(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        last=last,
        bid=ticker.get("bid"),
        ask=ticker.get("ask"),
        spread_pct=_spread_pct(ticker, last),
        ema_fast=ema(closes, 20),
        ema_slow=ema(closes, 50),
        ema200=ema_long,
        vwap=vwap(ohlcv, 200),
        rsi=rsi(closes, 14),
        adx=current_adx,
        atr=current_atr,
        atr_pct=(current_atr / last) * 100 if last else 0.0,
        volume_ratio=volume_ratio(ohlcv, 20),
        support=min(recent_lows),
        resistance=max(recent_highs),
        higher_timeframes=higher_timeframes or {},
        candlestick_patterns={timeframe: detect_candlestick_patterns(ohlcv)},
        ohlcv_timeframe=timeframe,
        ohlcv=[list(row) for row in ohlcv],
        funding_rate=_as_float(metrics.get("funding_rate")),
        open_interest=_as_float(metrics.get("open_interest")),
        open_interest_change=_as_float(metrics.get("open_interest_change")),
        fear_greed=_as_float(metrics.get("fear_greed")),
        news_score=_as_float(metrics.get("news_score")),
    )


def _confirmation_timeframes(config: dict[str, Any]) -> tuple[bool, list[str], int]:
    strategy_config = config.get("strategy", {})
    raw = strategy_config.get("confirmation_timeframes", {})
    if isinstance(raw, dict):
        enabled = bool(raw.get("enabled", True))
        frames = [str(item) for item in raw.get("frames", ["5m", "15m", "1h", "4h"]) if str(item)]
        limit = int(raw.get("ohlcv_limit", strategy_config.get("ohlcv_limit", 180)) or 180)
        return enabled, frames, max(60, limit)
    if isinstance(raw, list):
        return True, [str(item) for item in raw if str(item)], int(strategy_config.get("ohlcv_limit", 180))
    return False, [], int(strategy_config.get("ohlcv_limit", 180))


def _snapshot_worker_count(config: dict[str, Any], symbol_count: int) -> int:
    raw = config.get("exchange", {}).get("snapshot_workers", 4)
    try:
        workers = int(raw or 4)
    except (TypeError, ValueError):
        workers = 4
    return max(1, min(max(1, symbol_count), workers, 8))


def _build_snapshot_exchange(
    config: dict[str, Any],
    *,
    markets: dict[str, Any],
    currencies: dict[str, Any],
) -> Any:
    exchange = create_exchange(config, authenticated=False)
    if markets and hasattr(exchange, "set_markets"):
        exchange.set_markets(markets, currencies if isinstance(currencies, dict) else None)
    else:
        exchange.load_markets()
    return exchange


def _fetch_single_snapshot(
    exchange: Any,
    symbol: str,
    *,
    markets: dict[str, Any],
    tickers: dict[str, Any],
    timeframe: str,
    limit: int,
    higher_enabled: bool,
    higher_frames: list[str],
    higher_limit: int,
    derivatives_metrics_enabled: bool = False,
    fear_greed_value: float | None = None,
) -> tuple[MarketSnapshot | None, list[str]]:
    warnings: list[str] = []
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        market = markets.get(symbol) if isinstance(markets, dict) else {}
        ticker = None
        if isinstance(tickers, dict):
            ticker = tickers.get(symbol)
            if ticker is None and isinstance(market, dict):
                ticker = tickers.get(str(market.get("id") or ""))
        if not isinstance(ticker, dict):
            ticker = exchange.fetch_ticker(symbol)
        last = float(ticker.get("last") or float(ohlcv[-1][4]))
        higher_timeframes: dict[str, dict[str, Any]] = {}
        if higher_enabled:
            for frame in higher_frames:
                if frame == timeframe:
                    continue
                try:
                    frame_ohlcv = exchange.fetch_ohlcv(symbol, timeframe=frame, limit=higher_limit)
                    higher_timeframes[frame] = _frame_summary(frame, symbol, frame_ohlcv, last)
                except Exception as exc:
                    warnings.append(f"{symbol}: {frame} confirmation fetch failed: {exc}")
        market_metrics: dict[str, Any] = {}
        if derivatives_metrics_enabled:
            derivative_metrics, derivative_warnings = _fetch_derivative_metrics(exchange, symbol)
            market_metrics.update(derivative_metrics)
            warnings.extend(derivative_warnings)
        if fear_greed_value is not None:
            market_metrics["fear_greed"] = fear_greed_value
        return snapshot_from_ohlcv(
            symbol,
            ohlcv,
            ticker,
            higher_timeframes,
            timeframe=timeframe,
            market_metrics=market_metrics,
        ), warnings
    except Exception as exc:
        return None, [f"{symbol}: market fetch failed: {exc}"]


def fetch_market_snapshots(
    config: dict[str, Any],
    symbols: list[str] | None = None,
    *,
    market_data: dict[str, Any] | None = None,
) -> tuple[list[MarketSnapshot], list[str]]:
    timeframe = config["strategy"].get("timeframe", "15m")
    limit = max(200, int(config["strategy"].get("ohlcv_limit", 180)))
    higher_enabled, higher_frames, higher_limit = _confirmation_timeframes(config)
    snapshots: list[MarketSnapshot] = []
    warnings: list[str] = list((market_data or {}).get("warnings") or [])
    active_symbols = [str(symbol) for symbol in (symbols or config["strategy"]["symbols"]) if str(symbol)]
    if not active_symbols:
        return [], warnings
    if market_data:
        exchange = market_data.get("exchange")
        markets = market_data.get("markets") or {}
        tickers = market_data.get("tickers") or {}
    else:
        prefetched = prefetch_market_data(config, symbols=active_symbols)
        exchange = prefetched.get("exchange")
        markets = prefetched.get("markets") or {}
        tickers = prefetched.get("tickers") or {}
        warnings.extend(prefetched.get("warnings") or [])
    if exchange is None:
        return [], warnings + ["Market snapshot exchange is unavailable"]
    if not markets:
        markets = exchange.load_markets() or getattr(exchange, "markets", {}) or {}
    derivatives_metrics_enabled = _market_regime_metrics_enabled(config, "derivatives_metrics_enabled", False)
    fear_greed_value, fear_greed_warnings = _fetch_fear_greed(config)
    warnings.extend(fear_greed_warnings)
    worker_count = _snapshot_worker_count(config, len(active_symbols))
    if worker_count <= 1 or len(active_symbols) <= 1:
        for symbol in active_symbols:
            snapshot, symbol_warnings = _fetch_single_snapshot(
                exchange,
                symbol,
                markets=markets,
                tickers=tickers if isinstance(tickers, dict) else {},
                timeframe=timeframe,
                limit=limit,
                higher_enabled=higher_enabled,
                higher_frames=higher_frames,
                higher_limit=higher_limit,
                derivatives_metrics_enabled=derivatives_metrics_enabled,
                fear_greed_value=fear_greed_value,
            )
            warnings.extend(symbol_warnings)
            if snapshot is not None:
                snapshots.append(snapshot)
        return snapshots, warnings

    currencies = (market_data or {}).get("currencies") or getattr(exchange, "currencies", {}) or {}
    worker_local = threading.local()

    def _worker(symbol: str) -> tuple[str, MarketSnapshot | None, list[str]]:
        worker_exchange = getattr(worker_local, "exchange", None)
        if worker_exchange is None:
            worker_exchange = _build_snapshot_exchange(
                config,
                markets=markets if isinstance(markets, dict) else {},
                currencies=currencies if isinstance(currencies, dict) else {},
            )
            worker_local.exchange = worker_exchange
        snapshot, symbol_warnings = _fetch_single_snapshot(
            worker_exchange,
            symbol,
            markets=markets if isinstance(markets, dict) else {},
            tickers=tickers if isinstance(tickers, dict) else {},
            timeframe=timeframe,
            limit=limit,
            higher_enabled=higher_enabled,
            higher_frames=higher_frames,
            higher_limit=higher_limit,
            derivatives_metrics_enabled=derivatives_metrics_enabled,
            fear_greed_value=fear_greed_value,
        )
        return symbol, snapshot, symbol_warnings

    completed_symbols: set[str] = set()
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        for symbol, snapshot, symbol_warnings in executor.map(_worker, active_symbols):
            completed_symbols.add(symbol)
            warnings.extend(symbol_warnings)
            if snapshot is not None:
                snapshots.append(snapshot)
    if len(snapshots) < len(active_symbols):
        snapshot_symbols = {snapshot.symbol for snapshot in snapshots}
        for symbol in active_symbols:
            if symbol in snapshot_symbols:
                continue
            snapshot, symbol_warnings = _fetch_single_snapshot(
                exchange,
                symbol,
                markets=markets if isinstance(markets, dict) else {},
                tickers=tickers if isinstance(tickers, dict) else {},
                timeframe=timeframe,
                limit=limit,
                higher_enabled=higher_enabled,
                higher_frames=higher_frames,
                higher_limit=higher_limit,
                derivatives_metrics_enabled=derivatives_metrics_enabled,
                fear_greed_value=fear_greed_value,
            )
            warnings.extend(symbol_warnings)
            if snapshot is not None:
                snapshots.append(snapshot)
    return snapshots, warnings


def amount_for_notional(exchange: Any, symbol: str, notional_usdt: float, price: float) -> float:
    market = exchange.market(symbol)
    contract_size = float(market.get("contractSize") or 1)
    if market.get("contract"):
        raw_amount = notional_usdt / (price * contract_size)
    else:
        raw_amount = notional_usdt / price
    precise = exchange.amount_to_precision(symbol, raw_amount)
    return float(precise)
