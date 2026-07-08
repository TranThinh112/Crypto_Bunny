from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

from .candles import detect_candlestick_patterns
from .indicators import atr, ema, rsi, volume_ratio
from .models import MarketSnapshot


NON_CRYPTO_BASES = {
    "AAPL",
    "AMD",
    "AMZN",
    "BRENT",
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
    "XAG",
    "XAU",
    "XAUT",
}
NON_CRYPTO_KEYWORDS = {
    "STOCK",
    "EQUITY",
    "SHARE",
    "COMMODITY",
    "METAL",
    "FOREX",
}


def create_exchange(config: dict[str, Any], authenticated: bool = False) -> Any:
    import ccxt

    load_dotenv()
    exchange_name = config["exchange"].get("name", "okx")
    exchange_class = getattr(ccxt, exchange_name)
    params: dict[str, Any] = {
        "enableRateLimit": True,
        "timeout": int(config["exchange"].get("timeout_ms", 10000) or 10000),
        "options": {
            "defaultType": config["exchange"].get("account_type", "swap"),
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


def _ticker_quote_volume(ticker: dict[str, Any]) -> float:
    quote_volume = _ticker_number(
        ticker,
        "quoteVolume",
        "quote_volume",
        "volCcy24h",
        "turnover",
        "turnover24h",
    )
    if quote_volume is not None:
        return max(0.0, quote_volume)
    base_volume = _ticker_number(ticker, "baseVolume", "base_volume", "vol24h")
    last = _ticker_number(ticker, "last", "close")
    if base_volume is not None and last is not None:
        return max(0.0, base_volume * last)
    return 0.0


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
) -> list[str]:
    max_symbols = max(1, min(50, int(limit or 50)))
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
    return [symbol for _, symbol in ranked[:max_symbols]]


def fetch_top_volume_symbols(config: dict[str, Any]) -> tuple[list[str], list[str]]:
    strategy_config = config.get("strategy", {})
    universe = strategy_config.get("universe", {})
    limit = max(1, min(50, int(universe.get("max_symbols", 50) or 50)))
    quote = str(universe.get("quote", config.get("exchange", {}).get("default_settle", "USDT")) or "USDT")
    account_type = str(config.get("exchange", {}).get("account_type", "swap") or "swap")
    asset_class = str(universe.get("asset_class", "crypto") or "crypto")
    excluded_bases = universe.get("exclude_bases") or list(NON_CRYPTO_BASES)
    excluded_keywords = universe.get("exclude_keywords") or list(NON_CRYPTO_KEYWORDS)
    try:
        exchange = create_exchange(config, authenticated=False)
        markets = exchange.load_markets() or getattr(exchange, "markets", {}) or {}
        tickers = exchange.fetch_tickers()
        if not isinstance(markets, dict) or not isinstance(tickers, dict):
            return [], ["Top-volume universe fetch returned invalid market data"]
        symbols = select_top_volume_symbols_from_tickers(
            markets,
            tickers,
            limit=limit,
            quote=quote,
            account_type=account_type,
            asset_class=asset_class,
            excluded_bases=excluded_bases,
            excluded_keywords=excluded_keywords,
        )
        if not symbols:
            return [], ["Top-volume universe returned no eligible symbols"]
        return symbols, []
    except Exception as exc:
        return [], [f"Top-volume universe fetch failed: {exc}"]


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
    current_atr = atr(ohlcv, 14)
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
        "ema_gap_pct": ((ema_fast - ema_slow) / ema_slow) * 100 if ema_slow else 0.0,
        "price_vs_ema_slow_pct": ((last - ema_slow) / ema_slow) * 100 if ema_slow else 0.0,
        "rsi": rsi(closes, 14),
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
    return MarketSnapshot(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        last=last,
        bid=ticker.get("bid"),
        ask=ticker.get("ask"),
        spread_pct=_spread_pct(ticker, last),
        ema_fast=ema(closes, 20),
        ema_slow=ema(closes, 50),
        rsi=rsi(closes, 14),
        atr=current_atr,
        atr_pct=(current_atr / last) * 100 if last else 0.0,
        volume_ratio=volume_ratio(ohlcv, 20),
        support=min(recent_lows),
        resistance=max(recent_highs),
        higher_timeframes=higher_timeframes or {},
        candlestick_patterns={timeframe: detect_candlestick_patterns(ohlcv)},
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


def fetch_market_snapshots(
    config: dict[str, Any],
    symbols: list[str] | None = None,
) -> tuple[list[MarketSnapshot], list[str]]:
    exchange = create_exchange(config, authenticated=False)
    timeframe = config["strategy"].get("timeframe", "15m")
    limit = int(config["strategy"].get("ohlcv_limit", 180))
    higher_enabled, higher_frames, higher_limit = _confirmation_timeframes(config)
    snapshots: list[MarketSnapshot] = []
    warnings: list[str] = []

    exchange.load_markets()
    for symbol in symbols or config["strategy"]["symbols"]:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
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
            snapshots.append(snapshot_from_ohlcv(symbol, ohlcv, ticker, higher_timeframes, timeframe=timeframe))
        except Exception as exc:
            warnings.append(f"{symbol}: market fetch failed: {exc}")
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
