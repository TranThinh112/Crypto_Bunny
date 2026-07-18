from __future__ import annotations

import time

from market_pattern_engine.infrastructure.metrics import metrics


def main() -> int:
    started = time.perf_counter()
    snapshot = metrics.snapshot()
    print({"benchmark_note": "Run analyze-batch with fixture data for provider timing.", "metrics": snapshot, "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
