from __future__ import annotations

import json
import sys

from market_pattern_engine.domain.models import MarketAnalysisRequest
from market_pattern_engine.infrastructure.config_loader import load_engine_config
from market_pattern_engine.services.analysis_service import AnalysisService


def main() -> int:
    payload = json.load(sys.stdin)
    request = MarketAnalysisRequest(**payload)
    result, snapshot_id, elapsed = AnalysisService(load_engine_config()).analyze(request)
    print(json.dumps({"snapshot_id": snapshot_id, "processing_time_ms": elapsed, "result": result.model_dump(mode="json")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
