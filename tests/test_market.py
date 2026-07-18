from __future__ import annotations

from unittest import TestCase

from crypto_trader.market import select_top_volume_symbols_from_tickers, snapshot_from_ohlcv


class MarketUniverseTest(TestCase):
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
        expected_vwap = sum((((row[2] + row[3] + row[4]) / 3.0) * row[5]) for row in rows) / sum(
            row[5] for row in rows
        )
        self.assertAlmostEqual(snapshot.vwap or 0.0, expected_vwap)

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

    def test_caps_top_volume_universe_at_30_symbols(self) -> None:
        markets = {
            f"COIN{i}/USDT:USDT": {
                "active": True,
                "base": f"COIN{i}",
                "quote": "USDT",
                "settle": "USDT",
                "type": "swap",
                "swap": True,
            }
            for i in range(35)
        }
        tickers = {symbol: {"quoteVolume": index + 1} for index, symbol in enumerate(markets)}

        symbols = select_top_volume_symbols_from_tickers(
            markets,
            tickers,
            limit=50,
            quote="USDT",
            account_type="swap",
        )

        self.assertEqual(len(symbols), 30)

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
