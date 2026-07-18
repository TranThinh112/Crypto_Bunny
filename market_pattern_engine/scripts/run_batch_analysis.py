from __future__ import annotations

import asyncio
import json
import sys

from market_pattern_engine.domain.models import MarketAnalysisRequest
from market_pattern_engine.infrastructure.config_loader import load_engine_config
from market_pattern_engine.services.analysis_service import AnalysisService
from market_pattern_engine.services.batch_analysis_service import BatchAnalysisService


async def main() -> int:
    payload = json.load(sys.stdin)
    requests = [MarketAnalysisRequest(**item) for item in payload.get("requests", [])]
    service = BatchAnalysisService(AnalysisService(load_engine_config()))
    print(json.dumps({"items": await service.analyze_batch(requests)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
