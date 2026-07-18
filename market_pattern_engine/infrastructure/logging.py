from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any


LOGGER = logging.getLogger("market_pattern_engine")


def log_json(event: str, **fields: Any) -> None:
    payload = {
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **fields,
    }
    LOGGER.info(json.dumps(payload, ensure_ascii=False, default=str))
