# Market Structure & Pattern Engine

Production-oriented rule-based analysis module for OHLCV market structure, candlestick patterns, support/resistance zones, chart patterns, smart-money heuristics, re-check invalidation and AI feature export.

This module does not place orders, calculate position size, call OKX, or use an LLM to detect patterns.

## API

- `POST /api/v1/market-pattern/analyze`
- `POST /api/v1/market-pattern/analyze-batch`
- `POST /api/v1/market-pattern/recheck`
- `GET /api/v1/market-pattern/snapshot/{id}`
- `GET /api/v1/market-pattern/latest`
- `GET /api/v1/market-pattern/health`
- `GET /api/v1/market-pattern/metrics`

## Example Input

```json
{
  "symbol": "BTC/USDT:USDT",
  "timeframe": "4h",
  "exchange": "OKX",
  "mode": "SCAN_MODE",
  "correlation_id": "scan-001",
  "candles": [
    {"timestamp": "2026-07-10T00:00:00Z", "open": "100", "high": "105", "low": "99", "close": "104", "volume": "1000", "is_closed": true}
  ]
}
```

Use at least 20 candles; 50-120 candles are recommended by timeframe profile.

## Scanner Usage

```python
from market_pattern_engine.domain.models import MarketAnalysisRequest
from market_pattern_engine.infrastructure.config_loader import load_engine_config
from market_pattern_engine.services.analysis_service import AnalysisService

request = MarketAnalysisRequest(**payload)
result, snapshot_id, _ = AnalysisService(load_engine_config()).analyze(request)
features = result.feature_vector.model_dump()
```

## Final Re-check Usage

Set `mode` to `RECHECK_MODE` and pass `previous_snapshot_id`. The response contains `recheck.setup_status`, `should_keep_setup`, invalidation reasons and changed fields.

## Thresholds

Edit YAML under `market_pattern_engine/config/`. You can pass `MARKET_PATTERN_CONFIG=/path/override.yaml` to override thresholds without code changes.

## Look-Ahead Bias Guard

`SCAN_MODE` uses closed candles only. Provisional candles are only allowed in `RECHECK_MODE` and are marked through `data_quality.provisional` and pattern `PROVISIONAL` status.

## Versioning

Snapshots store `engine_version`, `config_version`, `provider_versions`, and detector names. MongoDB uses a unique index on `exchange + symbol + timeframe + candle_close_time + engine_version`.

## Current Limits

Native chart pattern detectors are heuristic and conservative. External repositories are adapter-backed and optional; if not installed they are marked unavailable through fallback behavior rather than crashing analysis.
