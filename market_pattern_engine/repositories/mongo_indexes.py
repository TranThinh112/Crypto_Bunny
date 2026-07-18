from __future__ import annotations

from typing import Any

from pymongo import ASCENDING, DESCENDING


def ensure_market_pattern_indexes(db: Any, config: dict | None = None) -> None:
    snapshots = db["market_analysis_snapshots"]
    snapshots.create_index(
        [
            ("exchange", ASCENDING),
            ("symbol", ASCENDING),
            ("timeframe", ASCENDING),
            ("candle_close_time", ASCENDING),
            ("engine_version", ASCENDING),
        ],
        unique=True,
        name="uniq_market_pattern_snapshot",
    )
    for spec, name in (
        ([("symbol", ASCENDING)], "symbol"),
        ([("timeframe", ASCENDING)], "timeframe"),
        ([("candle_close_time", DESCENDING)], "candle_close_time"),
        ([("analysis_mode", ASCENDING)], "analysis_mode"),
        ([("confluence.bias", ASCENDING)], "confluence_bias"),
        ([("confluence.confluence_score", DESCENDING)], "confluence_score"),
        ([("market_structure.trend_regime", ASCENDING)], "trend_regime"),
        ([("created_at", DESCENDING)], "created_at"),
    ):
        snapshots.create_index(spec, name=name)
    db["pattern_detections"].create_index([("snapshot_id", ASCENDING), ("pattern_type", ASCENDING)])
    db["support_resistance_zones"].create_index([("snapshot_id", ASCENDING), ("type", ASCENDING), ("strength_score", DESCENDING)])
    db["market_structure_events"].create_index([("snapshot_id", ASCENDING), ("created_at", DESCENDING)])
    db["analysis_configurations"].create_index([("config_version", ASCENDING)], unique=True)
    db["detector_versions"].create_index([("detector_name", ASCENDING), ("detector_version", ASCENDING)], unique=True)
    audit_days = ((config or {}).get("retention") or {}).get("audit_log_days", 45)
    db["analysis_audit_logs"].create_index([("created_at", ASCENDING)], expireAfterSeconds=int(audit_days) * 86400)
