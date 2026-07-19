from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument

from market_pattern_engine.domain.models import MarketAnalysisResult
from market_pattern_engine.domain.exceptions import MongoRepositoryError, SnapshotNotFoundError
from market_pattern_engine.infrastructure.mongodb import mongo_database
from .mongo_indexes import ensure_market_pattern_indexes


class AnalysisRepository:
    def __init__(self, db: Any | None = None, config: dict | None = None) -> None:
        self.config = config or {}
        self.db = db if db is not None else mongo_database()
        ensure_market_pattern_indexes(self.db, self.config)

    def save_snapshot(self, result: MarketAnalysisResult) -> str:
        now = datetime.now(timezone.utc)
        doc = result.model_dump(mode="json")
        created_at = doc.pop("created_at", result.created_at.isoformat())
        doc.update(
            {
                "analysis_mode": result.analysis_mode.value,
                "updated_at": now.isoformat(),
                "candle_close_time": result.candle_close_time.isoformat(),
            }
        )
        key = {
            "exchange": result.exchange,
            "symbol": result.symbol,
            "timeframe": result.timeframe,
            "candle_close_time": result.candle_close_time.isoformat(),
            "engine_version": result.engine_version,
        }
        try:
            saved = self.db["market_analysis_snapshots"].find_one_and_update(
                key,
                {"$set": doc, "$setOnInsert": {"created_at": created_at}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            snapshot_id = str(saved["_id"])
            self._replace_children(snapshot_id, result)
            return snapshot_id
        except Exception as exc:
            raise MongoRepositoryError(str(exc)) from exc

    def _replace_children(self, snapshot_id: str, result: MarketAnalysisResult) -> None:
        for collection in ("pattern_detections", "support_resistance_zones", "market_structure_events"):
            self.db[collection].delete_many({"snapshot_id": snapshot_id})
        if result.candlestick_patterns or result.chart_patterns or result.smart_money:
            self.db["pattern_detections"].insert_many(
                [
                    {"snapshot_id": snapshot_id, "kind": "candlestick", **item.model_dump(mode="json")}
                    for item in result.candlestick_patterns
                ]
                + [
                    {"snapshot_id": snapshot_id, "kind": "chart_pattern", **item.model_dump(mode="json")}
                    for item in result.chart_patterns
                ]
                + [
                    {"snapshot_id": snapshot_id, "kind": "smart_money", **item.model_dump(mode="json")}
                    for item in result.smart_money
                ]
            )
        zones = [
            {"snapshot_id": snapshot_id, **item.model_dump(mode="json")}
            for item in [*result.support_zones, *result.resistance_zones]
        ]
        if zones:
            self.db["support_resistance_zones"].insert_many(zones)
        self.db["market_structure_events"].insert_one({"snapshot_id": snapshot_id, **result.market_structure.model_dump(mode="json"), "created_at": datetime.now(timezone.utc).isoformat()})

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        try:
            oid = ObjectId(snapshot_id)
        except Exception as exc:
            raise SnapshotNotFoundError(snapshot_id) from exc
        row = self.db["market_analysis_snapshots"].find_one({"_id": oid})
        if not row:
            raise SnapshotNotFoundError(snapshot_id)
        row["_id"] = str(row["_id"])
        return row

    def latest(self, *, symbol: str | None = None, timeframe: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if symbol:
            query["symbol"] = symbol
        if timeframe:
            query["timeframe"] = timeframe
        rows = list(self.db["market_analysis_snapshots"].find(query).sort("created_at", -1).limit(max(1, min(limit, 200))))
        for row in rows:
            row["_id"] = str(row["_id"])
        return rows

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "collections": {
                name: self.db[name].estimated_document_count()
                for name in (
                    "market_analysis_snapshots",
                    "pattern_detections",
                    "support_resistance_zones",
                    "market_structure_events",
                    "analysis_configurations",
                    "analysis_audit_logs",
                    "detector_versions",
                )
            },
        }
