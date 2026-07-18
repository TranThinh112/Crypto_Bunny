from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock
from typing import Any


@dataclass
class MetricsRegistry:
    counters: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    timings_ms: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    _lock: Lock = field(default_factory=Lock)

    def inc(self, name: str, value: int = 1) -> None:
        with self._lock:
            self.counters[name] += value

    def observe_ms(self, name: str, value: float) -> None:
        with self._lock:
            self.timings_ms[name].append(float(value))
            self.timings_ms[name] = self.timings_ms[name][-500:]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            timings = {
                key: {
                    "count": len(values),
                    "avg_ms": round(sum(values) / len(values), 3) if values else 0.0,
                    "max_ms": round(max(values), 3) if values else 0.0,
                }
                for key, values in self.timings_ms.items()
            }
            return {"counters": dict(self.counters), "timings": timings}


metrics = MetricsRegistry()
