from __future__ import annotations

from datetime import datetime, timezone
from unittest import TestCase

from types import SimpleNamespace

from crypto_trader.market import (
    apply_news_scores_to_snapshots,
    create_exchange,
    select_top_volume_symbols_from_tickers,
    snapshot_from_ohlcv,
)


class MarketUniverseTest(TestCase):
    def test_create_exchange_limits_okx_market_loading_to_account_type(self) -> None:
        exchange = create_exchange(
            {
                "mode": "live",
                "exchange": {
                    "name": "okx",
                    "account_type": "swap",
                    "timeout_ms": 1000,
                },
            }
        )

        self.assertEqual(exchange.options["defaultType"], "swap")
        self.assertEqual(exchange.options["fetchMarkets"]["types"], ["swap"])

    def test_snapshot_from_ohlcv_adds_true_ema200_and_vwap(self) -> None:
        rows = []
        for index in range(200):
            open_price = 100.0 + index
            high = open_price + 2.0
            low = open_price - 1.0
            close = open_price + 1.0
            volume = 10.0 + index
            rows.append([index, open_price, high, low, close, volume])

        snapshot = snapshot_from_ohlcv("BTC/USDT:USDT", rows, {"last": rows[-1][4]})

        self.assertIsNotNone(snapshot.ema200)
        self.assertIsNotNone(snapshot.vwap)
        self.assertIsNotNone(snapshot.adx)
        expected_vwap = sum((((row[2] + row[3] + row[4]) / 3.0) * row[5]) for row in rows) / sum(
            row[5] for row in rows
        )
        self.assertAlmostEqual(snapshot.vwap or 0.0, expected_vwap)

    def test_snapshot_from_ohlcv_attaches_market_regime_metrics(self) -> None:
        rows = []
        for index in range(220):
            open_price = 100.0 + index * 0.1
            rows.append([index, open_price, open_price + 2.0, open_price - 1.0, open_price + 1.0, 10.0 + index])

        snapshot = snapshot_from_ohlcv(
            "BTC/USDT:USDT",
            rows,
            {"last": rows[-1][4]},
            market_metrics={
                "funding_rate": 0.00012,
                "open_interest": 123456.0,
                "fear_greed": 71,
                "news_score": 2.5,
            },
        )

        self.assertEqual(snapshot.funding_rate, 0.00012)
        self.assertEqual(snapshot.open_interest, 123456.0)
        self.assertEqual(snapshot.fear_greed, 71.0)
        self.assertEqual(snapshot.news_score, 2.5)

    def test_apply_news_scores_to_snapshots_uses_symbol_base(self) -> None:
        rows = [[index, 100.0, 102.0, 99.0, 101.0, 10.0] for index in range(120)]
        snapshot = snapshot_from_ohlcv("BTC/USDT:USDT", rows, {"last": 101.0})

        apply_news_scores_to_snapshots(
            [snapshot],
            SimpleNamespace(by_symbol_score={"BTC": -1.75}),
        )

        self.assertEqual(snapshot.news_score, -1.75)

    def test_snapshot_from_ohlcv_leaves_ema200_empty_until_enough_rows(self) -> None:
        rows = [[index, 100.0, 102.0, 99.0, 101.0, 10.0] for index in range(120)]

        snapshot = snapshot_from_ohlcv("BTC/USDT:USDT", rows, {"last": 101.0})

        self.assertIsNone(snapshot.ema200)
        self.assertIsNotNone(snapshot.vwap)

    def test_selects_top_usdt_swap_symbols_by_24h_volume(self) -> None:
        markets = {
            "BTC/USDT:USDT": {"active": True, "quote": "USDT", "settle": "USDT", "type": "swap", "swap": True},
            "ETH/USDT:USDT": {"active": True, "quote": "USDT", "settle": "USDT", "type": "swap", "swap": True},
            "SOL/USDT:USDT": {"active": True, "quote": "USDT", "settle": "USDT", "type": "swap", "swap": True},
            "DOGE/USDT": {"active": True, "quote": "USDT", "type": "spot"},
            "XRP/USDC:USDC": {"active": True, "quote": "USDC", "settle": "USDC", "type": "swap", "swap": True},
            "ADA/USDT:USDT": {"active": False, "quote": "USDT", "settle": "USDT", "type": "swap", "swap": True},
        }
        tickers = {
            "BTC/USDT:USDT": {"quoteVolume": 1000},
            "ETH/USDT:USDT": {"info": {"volCcyQuote24h": "2500"}},
            "SOL/USDT:USDT": {"baseVolume": 20, "last": 40},
            "DOGE/USDT": {"quoteVolume": 9999},
            "XRP/USDC:USDC": {"quoteVolume": 9999},
            "ADA/USDT:USDT": {"quoteVolume": 9999},
        }

        symbols = select_top_volume_symbols_from_tickers(
            markets,
            tickers,
            limit=2,
            quote="USDT",
            account_type="swap",
        )

        self.assertEqual(symbols, ["ETH/USDT:USDT", "BTC/USDT:USDT"])

    def test_prefers_quote_volume_over_base_coin_volume(self) -> None:
        markets = {
            "BTC/USDT:USDT": {"active": True, "base": "BTC", "quote": "USDT", "settle": "USDT", "type": "swap", "swap": True},
            "SHIB/USDT:USDT": {"active": True, "base": "SHIB", "quote": "USDT", "settle": "USDT", "type": "swap", "swap": True},
        }
        tickers = {
            "BTC/USDT:USDT": {"info": {"volCcyQuote24h": "2000000000", "volCcy24h": "20000"}},
            "SHIB/USDT:USDT": {"info": {"volCcyQuote24h": "200000000", "volCcy24h": "100000000000000"}},
        }

        symbols = select_top_volume_symbols_from_tickers(
            markets,
            tickers,
            limit=2,
            quote="USDT",
            account_type="swap",
        )

        self.assertEqual(symbols, ["BTC/USDT:USDT", "SHIB/USDT:USDT"])

    def test_caps_top_volume_universe_at_40_symbols(self) -> None:
        markets = {
            f"COIN{i}/USDT:USDT": {
                "active": True,
                "base": f"COIN{i}",
                "quote": "USDT",
                "settle": "USDT",
                "type": "swap",
                "swap": True,
            }
            for i in range(45)
        }
        tickers = {symbol: {"quoteVolume": index + 1} for index, symbol in enumerate(markets)}

        symbols = select_top_volume_symbols_from_tickers(
            markets,
            tickers,
            limit=50,
            quote="USDT",
            account_type="swap",
        )

        self.assertEqual(len(symbols), 40)

    def test_crypto_universe_excludes_tokenized_equities_and_commodities(self) -> None:
        markets = {
            "BTC/USDT:USDT": {
                "active": True,
                "base": "BTC",
                "quote": "USDT",
                "settle": "USDT",
                "type": "swap",
                "swap": True,
                "info": {"instCategory": "1"},
            },
            "TSLA/USDT:USDT": {
                "active": True,
                "base": "TSLA",
                "quote": "USDT",
                "settle": "USDT",
                "type": "swap",
                "swap": True,
            },
            "PAXG/USDT:USDT": {
                "active": True,
                "base": "PAXG",
                "quote": "USDT",
                "settle": "USDT",
                "type": "swap",
                "swap": True,
            },
            "CL/USDT:USDT": {
                "active": True,
                "base": "CL",
                "quote": "USDT",
                "settle": "USDT",
                "type": "swap",
                "swap": True,
                "info": {"instCategory": "4"},
            },
            "HOOD/USDT:USDT": {
                "active": True,
                "base": "HOOD",
                "quote": "USDT",
                "settle": "USDT",
                "type": "swap",
                "swap": True,
                "info": {"instCategory": "3"},
            },
        }
        tickers = {
            "BTC/USDT:USDT": {"quoteVolume": 1000},
            "TSLA/USDT:USDT": {"quoteVolume": 9000},
            "PAXG/USDT:USDT": {"quoteVolume": 8000},
            "CL/USDT:USDT": {"quoteVolume": 7000},
            "HOOD/USDT:USDT": {"quoteVolume": 6000},
        }

        symbols = select_top_volume_symbols_from_tickers(
            markets,
            tickers,
            limit=5,
            quote="USDT",
            account_type="swap",
            asset_class="crypto",
        )

        self.assertEqual(symbols, ["BTC/USDT:USDT"])

    def test_priority_symbols_are_kept_before_top_volume_fill(self) -> None:
        priority = ["BTC/USDT:USDT", "SOL/USDT:USDT", "ETH/USDT:USDT", "BNB/USDT:USDT", "XRP/USDT:USDT"]
        markets = {
            symbol: {
                "active": True,
                "base": symbol.split("/")[0],
                "quote": "USDT",
                "settle": "USDT",
                "type": "swap",
                "swap": True,
            }
            for symbol in priority
        }
        for index in range(10):
            markets[f"COIN{index}/USDT:USDT"] = {
                "active": True,
                "base": f"COIN{index}",
                "quote": "USDT",
                "settle": "USDT",
                "type": "swap",
                "swap": True,
            }
        tickers = {symbol: {"quoteVolume": 1} for symbol in priority}
        tickers.update({f"COIN{index}/USDT:USDT": {"quoteVolume": 1000 - index} for index in range(10)})

        symbols = select_top_volume_symbols_from_tickers(
            markets,
            tickers,
            limit=8,
            quote="USDT",
            account_type="swap",
            asset_class="crypto",
            priority_symbols=priority,
            now=datetime(2026, 7, 19, tzinfo=timezone.utc),
        )

        self.assertEqual(symbols[:5], priority)
        self.assertEqual(len(symbols), 8)
        self.assertEqual(symbols[5:], ["COIN0/USDT:USDT", "COIN1/USDT:USDT", "COIN2/USDT:USDT"])

    def test_weekday_priority_can_include_xau_without_crypto_universe_leak(self) -> None:
        markets = {
            "BTC/USDT:USDT": {
                "active": True,
                "base": "BTC",
                "quote": "USDT",
                "settle": "USDT",
                "type": "swap",
                "swap": True,
                "info": {"instCategory": "1"},
            },
            "XAU/USDT:USDT": {
                "active": True,
                "base": "XAU",
                "quote": "USDT",
                "settle": "USDT",
                "type": "swap",
                "swap": True,
                "info": {"instCategory": "4"},
            },
            "TSLA/USDT:USDT": {
                "active": True,
                "base": "TSLA",
                "quote": "USDT",
                "settle": "USDT",
                "type": "swap",
                "swap": True,
                "info": {"instCategory": "3"},
            },
        }
        tickers = {
            "BTC/USDT:USDT": {"quoteVolume": 100},
            "XAU/USDT:USDT": {"quoteVolume": 9000},
            "TSLA/USDT:USDT": {"quoteVolume": 8000},
        }

        symbols = select_top_volume_symbols_from_tickers(
            markets,
            tickers,
            limit=5,
            quote="USDT",
            account_type="swap",
            asset_class="crypto",
            weekday_priority_symbols=["XAU/USDT:USDT"],
            now=datetime(2026, 7, 20, tzinfo=timezone.utc),
            timezone_name="Asia/Ho_Chi_Minh",
        )

        self.assertIn("XAU/USDT:USDT", symbols)
        self.assertNotIn("TSLA/USDT:USDT", symbols)

    def test_weekday_priority_skips_xau_on_weekends(self) -> None:
        markets = {
            "BTC/USDT:USDT": {
                "active": True,
                "base": "BTC",
                "quote": "USDT",
                "settle": "USDT",
                "type": "swap",
                "swap": True,
                "info": {"instCategory": "1"},
            },
            "XAU/USDT:USDT": {
                "active": True,
                "base": "XAU",
                "quote": "USDT",
                "settle": "USDT",
                "type": "swap",
                "swap": True,
                "info": {"instCategory": "4"},
            },
        }
        tickers = {
            "BTC/USDT:USDT": {"quoteVolume": 100},
            "XAU/USDT:USDT": {"quoteVolume": 9000},
        }

        symbols = select_top_volume_symbols_from_tickers(
            markets,
            tickers,
            limit=5,
            quote="USDT",
            account_type="swap",
            asset_class="crypto",
            weekday_priority_symbols=["XAU/USDT:USDT"],
            now=datetime(2026, 7, 19, tzinfo=timezone.utc),
            timezone_name="Asia/Ho_Chi_Minh",
        )

        self.assertEqual(symbols, ["BTC/USDT:USDT"])
