from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol


class Cache(Protocol):
    def get(self, key: str) -> Any | None:
        ...

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        ...


@dataclass
class InMemoryCache:
    values: dict[str, tuple[float | None, Any]] = field(default_factory=dict)

    def get(self, key: str) -> Any | None:
        row = self.values.get(key)
        if not row:
            return None
        expires_at, value = row
        if expires_at is not None and expires_at <= time.time():
            self.values.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        expires_at = None if ttl_seconds is None else time.time() + max(1, ttl_seconds)
        self.values[key] = (expires_at, value)
