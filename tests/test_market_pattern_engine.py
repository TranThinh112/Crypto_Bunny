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
