from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import project_path


def ledger_path(config: dict[str, Any]) -> Path:
    return project_path(config, config.get("ledger_path", "data/trades.jsonl"))


def read_events(config: dict[str, Any]) -> list[dict[str, Any]]:
    path = ledger_path(config)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def append_event(config: dict[str, Any], event: dict[str, Any]) -> None:
    path = ledger_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"created_at": datetime.now(timezone.utc).isoformat(), **event}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
