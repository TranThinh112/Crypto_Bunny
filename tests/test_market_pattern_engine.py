from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import mongomock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_pattern_engine.api.router import router
from market_pattern_engine.domain.enums import AnalysisMode
from market_pattern_engine.domain.models import Candle, MarketAnalysisRequest
from market_pattern_engine.infrastructure.config_loader import load_engine_config
from market_pattern_engine.repositories.analysis_repository import AnalysisRepository
from market_pattern_engine.services.analysis_service import AnalysisService
from crypto_trader.market_pattern import analyze_market_pattern_snapshots, attach_market_pattern_features_to_candidates
from crypto_trader.models import MarketSnapshot, TradeCandidate


def _candles(count: int = 80) -> list[dict[str, object]]:
    start = datetime(2026, 7, 10, tzinfo=timezone.utc)
    rows: list[dict[str, object]] = []
    price = Decimal("100")
    for index in range(count):
        if index == count - 2:
            open_price = Decimal("106")
            close_price = Decimal("102")
        elif index == count - 1:
            open_price = Decimal("101")
            close_price = Decimal("108")
        else:
            wave = Decimal(index % 9) / Decimal("10")
            open_price = price + wave
            close_price = open_price + (Decimal("0.7") if index % 3 else Decimal("-0.3"))
            price += Decimal("0.15")
        high = max(open_price, close_price) + Decimal("1.2")
        low = min(open_price, close_price) - Decimal("1.1")
        rows.append(
            {
                "timestamp": (start + timedelta(hours=4 * index)).isoformat(),
                "open": str(open_price),
                "high": str(high),
                "low": str(low),
                "close": str(close_price),
                "volume": str(1000 + index * 10),
                "is_closed": True,
            }
        )
    return rows


def _request_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": "BTC/USDT:USDT",
        "timeframe": "4h",
        "exchange": "OKX",
        "candles": _candles(),
        "mode": "SCAN_MODE",
        "correlation_id": "test-market-pattern",
    }
    payload.update(overrides)
    return payload


def test_request_validation_rejects_duplicate_timestamps() -> None:
    candles = _candles(20)
    candles[3]["timestamp"] = candles[2]["timestamp"]

    with pytest.raises(ValueError, match="Duplicate candle timestamp"):
        MarketAnalysisRequest(**_request_payload(candles=candles))


def test_analysis_service_detects_patterns_and_exports_features() -> None:
    db = mongomock.MongoClient()["market_pattern_test"]
    repository = AnalysisRepository(db=db, config=load_engine_config())
    service = AnalysisService(load_engine_config(), repository)

    result, snapshot_id, elapsed = service.analyze(MarketAnalysisRequest(**_request_payload()))

    assert snapshot_id
    assert elapsed >= 0
    assert result.analysis_mode == AnalysisMode.SCAN_MODE
    assert result.feature_vector.schema_version == "1.0"
    assert result.data_quality.score > 0
    assert any(item.pattern_type == "bullish_engulfing" for item in result.candlestick_patterns)
    assert db["market_analysis_snapshots"].count_documents({}) == 1
    assert db["pattern_detections"].count_documents({"snapshot_id": snapshot_id}) >= 1
    assert db["market_structure_events"].count_documents({"snapshot_id": snapshot_id}) == 1


def test_repository_save_snapshot_is_idempotent_per_closed_candle() -> None:
    db = mongomock.MongoClient()["market_pattern_idempotent_test"]
    repository = AnalysisRepository(db=db, config=load_engine_config())
    service = AnalysisService(load_engine_config(), repository)
    request = MarketAnalysisRequest(**_request_payload(correlation_id="same-candle"))

    first = service.analyze(request)[1]
    second = service.analyze(request)[1]

    assert first == second
    assert db["market_analysis_snapshots"].count_documents({}) == 1
    assert db["market_structure_events"].count_documents({"snapshot_id": first}) == 1


def test_market_pattern_api_analyze_uses_app_state_database() -> None:
    app = FastAPI()
    app.state.market_pattern_db = mongomock.MongoClient()["market_pattern_api_test"]
    app.include_router(router)
    client = TestClient(app)

    response = client.post("/api/v1/market-pattern/analyze", json=_request_payload(correlation_id="api-test"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["snapshot_id"]
    assert payload["result"]["symbol"] == "BTC/USDT:USDT"
    assert app.state.market_pattern_db["market_analysis_snapshots"].count_documents({}) == 1


def test_crypto_trader_helper_analyzes_snapshot_and_attaches_candidate_features() -> None:
    db = mongomock.MongoClient()["market_pattern_crypto_trader_helper"]
    repository = AnalysisRepository(db=db, config=load_engine_config())
    ohlcv = []
    start = datetime(2026, 7, 10, tzinfo=timezone.utc)
    for index, candle in enumerate(_candles()):
        timestamp = int((start + timedelta(hours=4 * index)).timestamp() * 1000)
        ohlcv.append(
            [
                timestamp,
                float(candle["open"]),
                float(candle["high"]),
                float(candle["low"]),
                float(candle["close"]),
                float(candle["volume"]),
            ]
        )
    snapshot = MarketSnapshot(
        symbol="BTC/USDT:USDT",
        timestamp=start,
        last=float(ohlcv[-1][4]),
        bid=None,
        ask=None,
        spread_pct=None,
        ema_fast=105.0,
        ema_slow=104.0,
        rsi=55.0,
        atr=2.0,
        atr_pct=1.8,
        volume_ratio=1.2,
        support=99.0,
        resistance=110.0,
        ohlcv_timeframe="4h",
        ohlcv=ohlcv,
    )
    candidate = TradeCandidate(
        symbol="BTC/USDT:USDT",
        base="BTC",
        side="long",
        confidence=80.0,
        entry=108.0,
        stop_loss=102.0,
        take_profit=120.0,
        risk_reward=2.0,
        order_usdt=50.0,
        quantity=None,
        spread_pct=None,
        news_score=0.0,
        news_count=0,
    )

    result = analyze_market_pattern_snapshots(
        {"market_pattern_engine": {"enabled": True, "max_snapshots_per_scan": 1}},
        [snapshot],
        correlation_id="helper-test",
        source="unit-test",
        repository=repository,
    )
    attach_market_pattern_features_to_candidates([candidate], result["by_symbol"])

    assert result["analyzed"] == 1
    assert db["market_analysis_snapshots"].count_documents({}) == 1
    assert snapshot.market_pattern_analysis["snapshot_id"]
    assert snapshot.market_pattern_analysis["candlestick_patterns"]
    assert candidate.indicator_summary["market_pattern"]["snapshot_id"] == snapshot.market_pattern_analysis["snapshot_id"]
    assert candidate.indicator_summary["market_pattern"]["candlestick_patterns"]
    assert candidate.decision_metadata["market_pattern_snapshot_id"] == snapshot.market_pattern_analysis["snapshot_id"]
