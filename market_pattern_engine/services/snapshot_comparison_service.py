from __future__ import annotations

from typing import Any


def compare_snapshots(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[dict[str, Any]]:
    if not previous:
        return []
    changes: list[dict[str, Any]] = []
    for path in (
        ("market_structure", "trend_regime"),
        ("market_structure", "structure_state"),
        ("confluence", "bias"),
    ):
        old = previous
        new = current
        for key in path:
            old = old.get(key, {}) if isinstance(old, dict) else None
            new = new.get(key, {}) if isinstance(new, dict) else None
        if old != new:
            changes.append({"field": ".".join(path), "old_value": old, "new_value": new})
    return changes
