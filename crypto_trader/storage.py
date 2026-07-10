from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .atlas_mirror import (
    atlas_database,
    atlas_runtime_is_primary,
)
from .config import project_path
from .models import Decision, TradeCandidate, to_jsonable


PENDING_OKX_COLLECTION = "pending_orders"
PENDING_INTERNAL_COLLECTION = "internal_pending_orders"
ACTIVE_PENDING_STATUSES = ("LC_OKX", "WAIT_SLOT", "OPEN")
OKX_PENDING_STATUSES = ("LC_OKX",)
INTERNAL_PENDING_STATUSES = ("WAIT_SLOT", "OPEN")
DEFAULT_MARKET_SCAN_MAX_JSON_BYTES = 8000
DEPRECATED_JOURNAL_STATE_KEYS = {
    "ai_internal_market_scan_latest",
}
DASHBOARD_SNAPSHOT_PREFIX = "dashboard_snapshot:"
DASHBOARD_SNAPSHOT_VERSION_KEY = "dashboard_snapshot:version"
JOURNAL_STATE_COMPRESSION_THRESHOLD_BYTES = 8_192
DEFAULT_JOURNAL_STATE_CACHE_TTL_SECONDS = 5.0
DEFAULT_MONGO_OPERATION_RETRY_ATTEMPTS = 3
DEFAULT_MONGO_OPERATION_RETRY_DELAY_SECONDS = 0.35
LOCAL_MARKET_SCAN_CACHE_FILENAME = "latest_market_scan_memory.json"

_JOURNAL_STATE_CACHE_LOCK = threading.Lock()
_JOURNAL_STATE_CACHE: dict[str, tuple[float, str]] = {}
LOGGER = logging.getLogger(__name__)


class _RetryingCollectionProxy:
    def __init__(self, config: dict[str, Any], collection: Any) -> None:
        self._config = config
        self._collection = collection

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._collection, name)
        if not callable(attr) or name == "find":
            return attr

        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            return _mongo_call_with_retry(self._config, lambda: attr(*args, **kwargs))

        return _wrapped


def _journal_state_cache_namespace(config: dict[str, Any]) -> str:
    return str(
        config.get("_config_path")
        or config.get("_config_dir")
        or config.get("database", {}).get("atlas", {}).get("database")
        or "default"
    )


def _journal_state_cache_key(config: dict[str, Any], key: str) -> str:
    return f"{_journal_state_cache_namespace(config)}::{key}"


def _journal_state_cache_ttl_seconds(config: dict[str, Any]) -> float:
    atlas = config.get("database", {}).get("atlas", {})
    raw = atlas.get("journal_state_cache_ttl_seconds", DEFAULT_JOURNAL_STATE_CACHE_TTL_SECONDS)
    try:
        return max(0.0, float(raw or 0.0))
    except (TypeError, ValueError):
        return DEFAULT_JOURNAL_STATE_CACHE_TTL_SECONDS


def _journal_state_cache_get(config: dict[str, Any], key: str) -> str | None:
    ttl = _journal_state_cache_ttl_seconds(config)
    if ttl <= 0:
        return None
    cache_key = _journal_state_cache_key(config, key)
    now = time.monotonic()
    with _JOURNAL_STATE_CACHE_LOCK:
        entry = _JOURNAL_STATE_CACHE.get(cache_key)
        if not entry:
            return None
        expires_at, value = entry
        if expires_at <= now:
            _JOURNAL_STATE_CACHE.pop(cache_key, None)
            return None
        return value


def _journal_state_cache_set(config: dict[str, Any], key: str, value: str) -> None:
    ttl = _journal_state_cache_ttl_seconds(config)
    if ttl <= 0:
        return
    cache_key = _journal_state_cache_key(config, key)
    with _JOURNAL_STATE_CACHE_LOCK:
        _JOURNAL_STATE_CACHE[cache_key] = (time.monotonic() + ttl, value)


def _journal_state_cache_invalidate(config: dict[str, Any], key: str) -> None:
    cache_key = _journal_state_cache_key(config, key)
    with _JOURNAL_STATE_CACHE_LOCK:
        _JOURNAL_STATE_CACHE.pop(cache_key, None)


def _journal_state_cache_invalidate_prefix(config: dict[str, Any], prefix: str) -> None:
    match = f"{_journal_state_cache_namespace(config)}::{str(prefix or '')}"
    with _JOURNAL_STATE_CACHE_LOCK:
        stale_keys = [cache_key for cache_key in _JOURNAL_STATE_CACHE if cache_key.startswith(match)]
        for cache_key in stale_keys:
            _JOURNAL_STATE_CACHE.pop(cache_key, None)


def _mongo_operation_retry_attempts(config: dict[str, Any]) -> int:
    atlas = config.get("database", {}).get("atlas", {})
    raw = atlas.get("operation_retry_attempts", DEFAULT_MONGO_OPERATION_RETRY_ATTEMPTS)
    try:
        return max(1, int(raw or 1))
    except (TypeError, ValueError):
        return DEFAULT_MONGO_OPERATION_RETRY_ATTEMPTS


def _mongo_operation_retry_delay_seconds(config: dict[str, Any]) -> float:
    atlas = config.get("database", {}).get("atlas", {})
    raw = atlas.get("operation_retry_delay_seconds", DEFAULT_MONGO_OPERATION_RETRY_DELAY_SECONDS)
    try:
        return max(0.0, float(raw or 0.0))
    except (TypeError, ValueError):
        return DEFAULT_MONGO_OPERATION_RETRY_DELAY_SECONDS


def _mongo_error_is_retryable(exc: Exception) -> bool:
    if exc.__class__.__name__ in {
        "AutoReconnect",
        "ConnectionFailure",
        "ExecutionTimeout",
        "NetworkTimeout",
        "ServerSelectionTimeoutError",
        "WaitQueueTimeoutError",
    }:
        return True
    message = str(exc).lower()
    return any(
        token in message
        for token in (
            "read operation timed out",
            "timed out",
            "wait queue timeout",
            "connection pool paused",
            "connection reset",
            "temporarily unavailable",
        )
    )


def is_retryable_storage_error(exc: Exception) -> bool:
    return _mongo_error_is_retryable(exc)


def _mongo_call_with_retry(config: dict[str, Any], operation: Any) -> Any:
    attempts = _mongo_operation_retry_attempts(config)
    delay = _mongo_operation_retry_delay_seconds(config)
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            if attempt >= attempts or not _mongo_error_is_retryable(exc):
                raise
            time.sleep(delay * attempt)


def _best_effort_retryable_storage_side_effect(config: dict[str, Any], operation: Any) -> Any:
    try:
        return operation()
    except Exception as exc:
        if not _mongo_error_is_retryable(exc):
            raise
        return None


def _mongo_collection(config: dict[str, Any], table: str) -> Any:
    return _RetryingCollectionProxy(config, atlas_database(config)[table])


def _mongo_meta_collection(config: dict[str, Any]) -> Any:
    return _RetryingCollectionProxy(config, atlas_database(config)["_meta_counters"])


def _ensure_mongo_write_allowed(config: dict[str, Any]) -> None:
    if not atlas_runtime_is_primary(config):
        raise RuntimeError("This runtime is read-only. Only Railway primary may write Atlas state.")


def _mongo_next_id(config: dict[str, Any], table: str) -> int:
    from pymongo import ReturnDocument

    now = datetime.now(timezone.utc).isoformat()
    row = _mongo_meta_collection(config).find_one_and_update(
        {"_id": f"{table}:id"},
        {"$inc": {"value": 1}, "$set": {"updated_at": now}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int((row or {}).get("value") or 1)


def _next_pending_order_id(config: dict[str, Any]) -> int:
    from pymongo import ReturnDocument

    now = datetime.now(timezone.utc).isoformat()
    meta = _mongo_meta_collection(config)
    key = "pending_records:id"
    existing = meta.find_one({"_id": key})
    if existing is None:
        max_id = 0
        for table in (PENDING_OKX_COLLECTION, PENDING_INTERNAL_COLLECTION):
            row = _mongo_collection(config, table).find_one({}, {"_id": 0, "id": 1}, sort=[("id", -1)])
            if row and row.get("id") is not None:
                max_id = max(max_id, int(row["id"]))
        meta.update_one(
            {"_id": key},
            {"$setOnInsert": {"value": max_id, "updated_at": now}},
            upsert=True,
        )
    row = meta.find_one_and_update(
        {"_id": key},
        {"$inc": {"value": 1}, "$set": {"updated_at": now}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int((row or {}).get("value") or 1)


def _mongo_upsert_by_pk(config: dict[str, Any], table: str, pk_field: str, payload: dict[str, Any]) -> dict[str, Any]:
    document = dict(payload)
    document["_id"] = document[pk_field]
    _mongo_collection(config, table).replace_one({pk_field: document[pk_field]}, document, upsert=True)
    return document


def _mongo_find_many(
    config: dict[str, Any],
    table: str,
    *,
    query: dict[str, Any] | None = None,
    sort: list[tuple[str, int]] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    def _operation() -> list[dict[str, Any]]:
        cursor = _mongo_collection(config, table).find(query or {}, {"_id": 0})
        if sort:
            cursor = cursor.sort(sort)
        if limit is not None:
            cursor = cursor.limit(max(0, int(limit)))
        return [dict(row) for row in cursor]

    return _mongo_call_with_retry(config, _operation)


def _mongo_find_one(
    config: dict[str, Any],
    table: str,
    *,
    query: dict[str, Any] | None = None,
    sort: list[tuple[str, int]] | None = None,
) -> dict[str, Any] | None:
    rows = _mongo_find_many(config, table, query=query, sort=sort, limit=1)
    return rows[0] if rows else None


def _storage_retention(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("storage_retention", {})


def _market_scan_memory_cache_path(config: dict[str, Any]) -> Path:
    memory_config = config.get("market_scan_memory", {})
    configured_path = memory_config.get("cache_path")
    if configured_path:
        return project_path(config, configured_path)
    report_path = project_path(config, config.get("report_path", "reports/latest_decision.json"))
    return report_path.parent / LOCAL_MARKET_SCAN_CACHE_FILENAME


def _json_object_or_empty(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _load_local_market_scan_cache_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    path = _market_scan_memory_cache_path(config)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "created_at": item.get("created_at"),
                "source": item.get("source"),
                "symbol": item.get("symbol"),
                "side": item.get("side"),
                "timeframe": item.get("timeframe"),
                "confidence": item.get("confidence"),
                "win_probability_pct": item.get("win_probability_pct"),
                "risk_reward": item.get("risk_reward"),
                "score": item.get("score"),
                "indicator": _json_object_or_empty(item.get("indicator")),
                "payload": _json_object_or_empty(item.get("payload")),
            }
        )
    return result


def _store_local_market_scan_cache_rows(config: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    path = _market_scan_memory_cache_path(config)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "rows": rows,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return


def _prune_local_market_scan_cache_rows(config: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    memory_config = config.get("market_scan_memory", {})
    keep_hours = int(memory_config.get("keep_hours", 72) or 72)
    max_rows = int(memory_config.get("max_rows_per_symbol_timeframe", 200) or 200)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, keep_hours))).isoformat()
    ordered_rows = sorted(
        rows,
        key=lambda item: (
            str(item.get("created_at") or ""),
            str(item.get("symbol") or ""),
            str(item.get("timeframe") or ""),
            str(item.get("source") or ""),
        ),
        reverse=True,
    )
    counters: dict[tuple[str, str], int] = {}
    pruned: list[dict[str, Any]] = []
    for item in ordered_rows:
        created_at = str(item.get("created_at") or "")
        if created_at and created_at < cutoff:
            continue
        symbol = str(item.get("symbol") or "")
        timeframe = str(item.get("timeframe") or "")
        if not symbol or not timeframe:
            continue
        key = (symbol, timeframe)
        if counters.get(key, 0) >= max(1, max_rows):
            continue
        pruned.append(item)
        counters[key] = counters.get(key, 0) + 1
    return pruned


def _update_local_market_scan_cache(config: dict[str, Any], rows: list[tuple[Any, ...]]) -> None:
    cache_rows = [
        {
            "created_at": row[0],
            "source": row[1],
            "symbol": row[2],
            "side": row[3],
            "timeframe": row[4],
            "confidence": row[5],
            "win_probability_pct": row[6],
            "risk_reward": row[7],
            "score": row[8],
            "indicator": _json_object_or_empty(row[9]),
            "payload": _json_object_or_empty(row[10]),
        }
        for row in rows
    ]
    merged_rows = _prune_local_market_scan_cache_rows(
        config,
        cache_rows + _load_local_market_scan_cache_rows(config),
    )
    _store_local_market_scan_cache_rows(config, merged_rows)


def _group_recent_market_scan_rows(
    rows: list[dict[str, Any]],
    *,
    per_symbol_timeframe_limit: int,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    counters: dict[tuple[str, str], int] = {}
    for item in rows:
        symbol = str(item.get("symbol") or "")
        timeframe = str(item.get("timeframe") or "")
        if not symbol or not timeframe:
            continue
        key = (symbol, timeframe)
        if counters.get(key, 0) >= max(1, int(per_symbol_timeframe_limit)):
            continue
        indicator = _json_object_or_empty(item.get("indicator"))
        payload = _json_object_or_empty(item.get("payload"))
        grouped.setdefault(symbol, {}).setdefault(timeframe, []).append(
            {
                "created_at": item.get("created_at"),
                "source": item.get("source"),
                "side": item.get("side"),
                "confidence": item.get("confidence"),
                "win_probability_pct": item.get("win_probability_pct"),
                "risk_reward": item.get("risk_reward"),
                "score": item.get("score"),
                "indicator": indicator,
                "payload": payload,
            }
        )
        counters[key] = counters.get(key, 0) + 1
    return grouped


def _iso_cutoff_days(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(0.1, float(days)))).isoformat()


def _delete_by_ids(config: dict[str, Any], table: str, ids: list[int]) -> int:
    if not ids:
        return 0
    return int(_mongo_collection(config, table).delete_many({"id": {"$in": ids}}).deleted_count or 0)


def _prune_by_created_at(
    config: dict[str, Any],
    table: str,
    *,
    keep_days: float,
    preserve_query: dict[str, Any] | None = None,
) -> dict[str, int]:
    cutoff = _iso_cutoff_days(keep_days)
    stale_query: dict[str, Any] = {"created_at": {"$lt": cutoff}}
    if preserve_query:
        stale_query = {"$and": [stale_query, {"$nor": [preserve_query]}]}
    _ensure_mongo_write_allowed(config)
    deleted = int(_mongo_collection(config, table).delete_many(stale_query).deleted_count or 0)
    return {"deleted_old": deleted}


def _pending_collection_for_status(status: str, exchange_order_id: str | None = None) -> str:
    normalized_status = str(status or "").upper()
    if normalized_status in OKX_PENDING_STATUSES or str(exchange_order_id or "").strip():
        return PENDING_OKX_COLLECTION
    return PENDING_INTERNAL_COLLECTION


def _find_pending_record(config: dict[str, Any], order_id: int) -> tuple[str, dict[str, Any] | None]:
    for table in (PENDING_OKX_COLLECTION, PENDING_INTERNAL_COLLECTION):
        row = _mongo_find_one(config, table, query={"id": int(order_id)})
        if row is not None:
            return table, row
    return PENDING_OKX_COLLECTION, None


def _merge_pending_rows(*groups: list[dict[str, Any]], limit: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group in groups:
        rows.extend(group)
    rows.sort(key=lambda row: (str(row.get("updated_at") or row.get("created_at") or ""), int(row.get("id") or 0)), reverse=True)
    if limit is not None:
        return rows[: max(0, int(limit))]
    return rows


def migrate_legacy_pending_orders(config: dict[str, Any]) -> dict[str, int]:
    legacy_rows = _mongo_find_many(
        config,
        PENDING_OKX_COLLECTION,
        query={"status": {"$in": list(INTERNAL_PENDING_STATUSES)}},
        sort=[("id", 1)],
    )
    if not legacy_rows:
        return {"moved": 0}
    _ensure_mongo_write_allowed(config)
    moved = 0
    for row in legacy_rows:
        _mongo_upsert_by_pk(config, PENDING_INTERNAL_COLLECTION, "id", row)
        _mongo_collection(config, PENDING_OKX_COLLECTION).delete_one({"id": int(row["id"])})
        moved += 1
    return {"moved": moved}


def list_journal_state_prefix(config: dict[str, Any], prefix: str, *, limit: int = 100) -> list[dict[str, Any]]:
    safe_limit = max(1, int(limit))
    return _mongo_find_many(
        config,
        "journal_state",
        query={"key": {"$regex": f"^{re.escape(prefix)}"}},
        sort=[("key", -1)],
        limit=safe_limit,
    )
def ensure_ai_model_version(
    config: dict[str, Any],
    *,
    model_name: str,
    model_version: str,
    prompt_version: str,
    prompt_hash: str,
    created_at: str | None = None,
) -> None:
    filters = {
        "model_name": model_name,
        "model_version": model_version,
        "prompt_version": prompt_version,
        "prompt_hash": prompt_hash,
    }
    if _mongo_find_one(config, "ai_model_versions", query=filters):
        return
    _ensure_mongo_write_allowed(config)
    existing = _mongo_find_one(config, "ai_model_versions", query={"model_name": model_name})
    now = str(created_at or datetime.now(timezone.utc).isoformat())
    row = dict(filters)
    row["id"] = int((existing or {}).get("id") or _mongo_next_id(config, "ai_model_versions"))
    row["created_at"] = str((existing or {}).get("created_at") or now)
    row["updated_at"] = now
    _mongo_upsert_by_pk(config, "ai_model_versions", "id", row)
    return
def get_prompt_version(config: dict[str, Any], version: str) -> dict[str, Any] | None:
    return _mongo_find_one(config, "prompt_versions", query={"version": version})
def save_prompt_version(config: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    existing = get_prompt_version(config, str(row["version"]))
    payload = {
        "version": str(row["version"]),
        "hash": row.get("hash"),
        "description": row.get("description"),
        "created_at": row.get("created_at") or (existing or {}).get("created_at") or datetime.now(timezone.utc).isoformat(),
        "is_active": int(row.get("is_active", 0) or 0),
        "files_json": row.get("files_json") or "{}",
        "prompt_hash": row.get("prompt_hash") or row.get("hash"),
    }
    _ensure_mongo_write_allowed(config)
    payload["id"] = int((existing or {}).get("id") or _mongo_next_id(config, "prompt_versions"))
    _mongo_upsert_by_pk(config, "prompt_versions", "version", payload)
    return payload
def list_prompt_versions(config: dict[str, Any]) -> list[dict[str, Any]]:
    return _mongo_find_many(config, "prompt_versions", sort=[("created_at", -1), ("id", -1)])


def prune_prompt_versions(config: dict[str, Any], *, keep_days: float | None = None) -> dict[str, int]:
    retention = _storage_retention(config)
    keep_days = float(keep_days or retention.get("prompt_versions_keep_days", 365) or 365)
    return _prune_by_created_at(config, "prompt_versions", keep_days=keep_days, preserve_query={"is_active": 1})
def get_prompt_metric(config: dict[str, Any], prompt_version: str) -> dict[str, Any] | None:
    return _mongo_find_one(config, "prompt_metrics", query={"prompt_version": prompt_version})
def save_prompt_metric_snapshot(config: dict[str, Any], row: dict[str, Any]) -> dict[str, Any] | None:
    prompt_version = str(row.get("prompt_version") or "")
    if not prompt_version:
        return None
    payload = {
        "prompt_version": prompt_version,
        "prompt_hash": str(row.get("prompt_hash") or ""),
        "total_requests": int(row.get("total_requests") or 0),
        "average_prompt_tokens": float(row.get("average_prompt_tokens") or 0),
        "average_completion_tokens": float(row.get("average_completion_tokens") or 0),
        "average_latency": float(row.get("average_latency") or 0),
        "estimated_cached_tokens": float(row.get("estimated_cached_tokens") or 0),
        "estimated_dynamic_tokens": float(row.get("estimated_dynamic_tokens") or 0),
        "cache_hit_percent": float(row.get("cache_hit_percent") or 0),
        "updated_at": str(row.get("updated_at") or datetime.now(timezone.utc).isoformat()),
    }
    for extra_key in ("source", "notes"):
        if row.get(extra_key) not in (None, ""):
            payload[extra_key] = row.get(extra_key)
    _ensure_mongo_write_allowed(config)
    _mongo_upsert_by_pk(config, "prompt_metrics", "prompt_version", payload)
    return payload
def merge_prompt_metric(config: dict[str, Any], metric: dict[str, Any]) -> None:
    prompt_version = str(metric.get("prompt_version") or "")
    if not prompt_version:
        return
    prompt_tokens = float(metric.get("prompt_tokens") or 0)
    completion_tokens = float(metric.get("completion_tokens") or 0)
    latency_ms = float(metric.get("latency_ms") or 0)
    cached_tokens = float(metric.get("estimated_cached_tokens") or 0)
    dynamic_tokens = float(metric.get("estimated_dynamic_tokens") or 0)
    cache_hit_percent = float(metric.get("cache_hit_percent") or 0)
    existing = get_prompt_metric(config, prompt_version)
    if existing is None:
        payload = {
            "prompt_version": prompt_version,
            "prompt_hash": str(metric.get("prompt_hash") or ""),
            "total_requests": 1,
            "average_prompt_tokens": prompt_tokens,
            "average_completion_tokens": completion_tokens,
            "average_latency": latency_ms,
            "estimated_cached_tokens": cached_tokens,
            "estimated_dynamic_tokens": dynamic_tokens,
            "cache_hit_percent": cache_hit_percent,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    else:
        total = int(existing.get("total_requests") or 0)
        new_total = total + 1
        payload = {
            "prompt_version": prompt_version,
            "prompt_hash": str(metric.get("prompt_hash") or existing.get("prompt_hash") or ""),
            "total_requests": new_total,
            "average_prompt_tokens": ((float(existing.get("average_prompt_tokens") or 0) * total) + prompt_tokens) / new_total,
            "average_completion_tokens": ((float(existing.get("average_completion_tokens") or 0) * total) + completion_tokens) / new_total,
            "average_latency": ((float(existing.get("average_latency") or 0) * total) + latency_ms) / new_total,
            "estimated_cached_tokens": ((float(existing.get("estimated_cached_tokens") or 0) * total) + cached_tokens) / new_total,
            "estimated_dynamic_tokens": ((float(existing.get("estimated_dynamic_tokens") or 0) * total) + dynamic_tokens) / new_total,
            "cache_hit_percent": ((float(existing.get("cache_hit_percent") or 0) * total) + cache_hit_percent) / new_total,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    _ensure_mongo_write_allowed(config)
    _mongo_upsert_by_pk(config, "prompt_metrics", "prompt_version", payload)
    return
def save_ai_experiment(config: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    name = str(row["name"])
    _ensure_mongo_write_allowed(config)
    existing = _mongo_find_one(config, "ai_experiments", query={"name": name})
    payload = {
        "id": int((existing or {}).get("id") or _mongo_next_id(config, "ai_experiments")),
        "name": name,
        "description": str(row.get("description") or ""),
        "prompt_version": str(row.get("prompt_version") or ""),
        "traffic_percent": float(row.get("traffic_percent") or 0),
        "enabled": int(row.get("enabled", 0) or 0),
        "created_at": str((existing or {}).get("created_at") or row.get("created_at") or datetime.now(timezone.utc).isoformat()),
    }
    _mongo_upsert_by_pk(config, "ai_experiments", "name", payload)
    return payload


def prune_ai_experiments(config: dict[str, Any], *, keep_days: float | None = None) -> dict[str, int]:
    retention = _storage_retention(config)
    keep_days = float(keep_days or retention.get("ai_experiments_keep_days", 365) or 365)
    return _prune_by_created_at(config, "ai_experiments", keep_days=keep_days, preserve_query={"enabled": 1})
def list_ai_experiment_rows(config: dict[str, Any], *, enabled_only: bool = False) -> list[dict[str, Any]]:
    query = {"enabled": 1} if enabled_only else None
    return _mongo_find_many(config, "ai_experiments", query=query, sort=[("created_at", -1), ("id", -1)])
def get_strategy_version(config: dict[str, Any], version: str) -> dict[str, Any] | None:
    return _mongo_find_one(config, "strategy_versions", query={"version": version})
def ensure_strategy_version(config: dict[str, Any], row: dict[str, Any]) -> None:
    if get_strategy_version(config, str(row["version"])) is not None:
        return
    save_strategy_version(config, row, deactivate_others=False)


def save_strategy_version(config: dict[str, Any], row: dict[str, Any], *, deactivate_others: bool = False) -> dict[str, Any]:
    version = str(row["version"])
    existing = get_strategy_version(config, version)
    payload = {
        "version": version,
        "name": str(row.get("name") or version.upper()),
        "description": str(row.get("description") or ""),
        "created_at": str((existing or {}).get("created_at") or row.get("created_at") or datetime.now(timezone.utc).isoformat()),
        "is_active": int(row.get("is_active", 0) or 0),
        "traffic_percent": float(row.get("traffic_percent") or 0),
        "indicators_json": row.get("indicators_json") or "{}",
        "rules_json": row.get("rules_json") or "{}",
        "risk_config_json": row.get("risk_config_json") or "{}",
        "payload_json": row.get("payload_json") or "{}",
    }
    _ensure_mongo_write_allowed(config)
    collection = _mongo_collection(config, "strategy_versions")
    if deactivate_others and payload["is_active"]:
        collection.update_many({}, {"$set": {"is_active": 0}})
    payload["id"] = int((existing or {}).get("id") or _mongo_next_id(config, "strategy_versions"))
    _mongo_upsert_by_pk(config, "strategy_versions", "version", payload)
    return payload
def activate_strategy_version_record(config: dict[str, Any], version: str) -> dict[str, Any] | None:
    row = get_strategy_version(config, version)
    if row is None:
        return None
    _ensure_mongo_write_allowed(config)
    collection = _mongo_collection(config, "strategy_versions")
    collection.update_many({}, {"$set": {"is_active": 0}})
    collection.update_one({"version": version}, {"$set": {"is_active": 1}})
    return _mongo_find_one(config, "strategy_versions", query={"version": version})
def list_strategy_versions(
    config: dict[str, Any],
    *,
    active_only: bool | None = None,
    order: str = "created_desc",
) -> list[dict[str, Any]]:
    if order == "id_asc":
        sort = [("id", 1)]
    elif order == "created_asc":
        sort = [("created_at", 1), ("id", 1)]
    else:
        sort = [("created_at", -1), ("id", -1)]
    query: dict[str, Any] | None = None
    if active_only is True:
        query = {"is_active": 1}
    elif active_only is False:
        query = None
    return _mongo_find_many(config, "strategy_versions", query=query, sort=sort)


def prune_strategy_versions(config: dict[str, Any], *, keep_days: float | None = None) -> dict[str, int]:
    retention = _storage_retention(config)
    keep_days = float(keep_days or retention.get("strategy_versions_keep_days", 365) or 365)
    return _prune_by_created_at(config, "strategy_versions", keep_days=keep_days, preserve_query={"is_active": 1})
def insert_market_regime_history(config: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "created_at": row.get("created_at") or datetime.now(timezone.utc).isoformat(),
        "regime": row.get("regime") or "UNKNOWN",
        "confidence": row.get("confidence") or 0,
        "indicators_json": row.get("indicators_json") or "{}",
        "reason": row.get("reason") or "",
    }
    _ensure_mongo_write_allowed(config)
    payload["id"] = _mongo_next_id(config, "market_regime_history")
    _mongo_upsert_by_pk(config, "market_regime_history", "id", payload)
    return payload
def latest_market_regime_history(config: dict[str, Any]) -> dict[str, Any] | None:
    return _mongo_find_one(config, "market_regime_history", sort=[("created_at", -1), ("id", -1)])
def list_market_regime_rows(config: dict[str, Any], *, limit: int = 100) -> list[dict[str, Any]]:
    safe_limit = max(1, int(limit))
    return _mongo_find_many(
        config,
        "market_regime_history",
        sort=[("created_at", -1), ("id", -1)],
        limit=safe_limit,
    )
def insert_trade_candidate_rows(config: dict[str, Any], rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    _ensure_mongo_write_allowed(config)
    collection = _mongo_collection(config, "trade_candidates")
    documents: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        payload["id"] = _mongo_next_id(config, "trade_candidates")
        payload["is_used"] = int(payload.get("is_used", 0) or 0)
        documents.append(payload)
    if documents:
        collection.insert_many([{**doc, "_id": doc["id"]} for doc in documents], ordered=True)
    return len(documents)


def prune_trade_candidates(config: dict[str, Any], *, keep_days: float | None = None) -> dict[str, int]:
    retention = _storage_retention(config)
    keep_days = float(keep_days or retention.get("trade_candidates_keep_days", 7) or 7)
    return _prune_by_created_at(config, "trade_candidates", keep_days=keep_days)
def list_trade_candidate_rows(
    config: dict[str, Any],
    *,
    min_created_at: str | None = None,
    unused_only: bool = False,
    min_rule_score: float | None = None,
    limit: int | None = None,
    order: str = "recent",
) -> list[dict[str, Any]]:
    query: dict[str, Any] = {}
    if min_created_at:
        query["created_at"] = {"$gte": min_created_at}
    if unused_only:
        query["is_used"] = 0
    if min_rule_score is not None:
        query["rule_score"] = {"$gte": float(min_rule_score)}
    if order == "refill":
        sort = [("rule_score", -1), ("gpt_confidence", -1), ("risk_reward", -1), ("id", 1)]
    else:
        sort = [("created_at", -1), ("id", -1)]
    return _mongo_find_many(config, "trade_candidates", query=query or None, sort=sort, limit=limit)
def mark_trade_candidate_used(config: dict[str, Any], candidate_id: int, *, used_at: str | None = None) -> None:
    now = str(used_at or datetime.now(timezone.utc).isoformat())
    _ensure_mongo_write_allowed(config)
    _mongo_collection(config, "trade_candidates").update_one(
        {"id": int(candidate_id)},
        {"$set": {"is_used": 1, "used_at": now}},
    )
    return
def claim_trade_candidate(config: dict[str, Any], candidate_id: int, *, used_at: str | None = None) -> bool:
    now = str(used_at or datetime.now(timezone.utc).isoformat())
    _ensure_mongo_write_allowed(config)
    result = _mongo_collection(config, "trade_candidates").update_one(
        {"id": int(candidate_id), "is_used": 0},
        {"$set": {"is_used": 1, "used_at": now}},
    )
    return int(result.modified_count or 0) == 1
def insert_ai_trade_decision_row(config: dict[str, Any], row: dict[str, Any]) -> int:
    _ensure_mongo_write_allowed(config)
    payload = dict(row)
    payload["id"] = _mongo_next_id(config, "ai_trade_decisions")
    _mongo_upsert_by_pk(config, "ai_trade_decisions", "id", payload)
    return int(payload["id"])


def prune_ai_trade_decisions(config: dict[str, Any], *, keep_days: float | None = None) -> dict[str, int]:
    retention = _storage_retention(config)
    keep_days = float(keep_days or retention.get("ai_trade_decisions_keep_days", 365) or 365)
    return _prune_by_created_at(config, "ai_trade_decisions", keep_days=keep_days)
def list_ai_trade_decision_rows(config: dict[str, Any], *, limit: int = 50) -> list[dict[str, Any]]:
    safe_limit = max(1, int(limit))
    return _mongo_find_many(config, "ai_trade_decisions", sort=[("created_at", -1), ("id", -1)], limit=safe_limit)


def list_ai_trade_decision_stat_rows(config: dict[str, Any], *, limit: int = 50) -> list[dict[str, Any]]:
    safe_limit = max(1, int(limit))

    def _operation() -> list[dict[str, Any]]:
        cursor = (
            _mongo_collection(config, "ai_trade_decisions")
            .find(
                {},
                {
                    "_id": 0,
                    "decision": 1,
                    "trade_status": 1,
                    "confidence": 1,
                    "pnl": 1,
                },
            )
            .sort([("created_at", -1), ("id", -1)])
            .limit(safe_limit)
        )
        return [dict(row) for row in cursor]

    return _mongo_call_with_retry(config, _operation)


def mark_ai_trade_decisions_closed(
    config: dict[str, Any],
    *,
    symbol: str,
    side: str,
    trade_status: str,
    pnl: float | None,
    closed_at: str | None,
) -> None:
    updates = {
        "trade_status": trade_status,
        "pnl": pnl,
        "closed_at": closed_at,
    }
    _ensure_mongo_write_allowed(config)
    _mongo_collection(config, "ai_trade_decisions").update_many(
        {"symbol": symbol, "side": side, "trade_status": None},
        {"$set": updates},
    )
    return
def insert_trade_execution_row(config: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    _ensure_mongo_write_allowed(config)
    payload = dict(row)
    payload["id"] = _mongo_next_id(config, "trade_executions")
    _mongo_upsert_by_pk(config, "trade_executions", "id", payload)
    return payload


def prune_trade_executions(config: dict[str, Any], *, keep_days: float | None = None) -> dict[str, int]:
    retention = _storage_retention(config)
    keep_days = float(keep_days or retention.get("trade_executions_keep_days", 365) or 365)
    cutoff = _iso_cutoff_days(keep_days)
    _ensure_mongo_write_allowed(config)
    deleted = int(
        _mongo_collection(config, "trade_executions").delete_many(
            {
                "$and": [
                    {"status": {"$nin": ["OPEN", "LC_PENDING"]}},
                    {
                        "$or": [
                            {"closed_at": {"$lt": cutoff}},
                            {
                                "$and": [
                                    {"closed_at": {"$in": [None, ""]}},
                                    {"created_at": {"$lt": cutoff}},
                                ]
                            },
                        ]
                    },
                ]
            }
        ).deleted_count
        or 0
    )
    return {"deleted_old": deleted}
def get_trade_execution(config: dict[str, Any], trade_execution_id: int) -> dict[str, Any] | None:
    return _mongo_find_one(config, "trade_executions", query={"id": int(trade_execution_id)})
def list_trade_execution_rows(
    config: dict[str, Any],
    *,
    statuses: list[str] | tuple[str, ...] | None = None,
    strategy_version: str | None = None,
    ids: list[int] | None = None,
    limit: int | None = None,
    order: str = "created_desc",
) -> list[dict[str, Any]]:
    query: dict[str, Any] = {}
    if statuses:
        query["status"] = {"$in": [str(status) for status in statuses]}
    if strategy_version:
        query["strategy_version"] = strategy_version
    if ids:
        query["id"] = {"$in": [int(value) for value in ids]}
    if order == "created_asc":
        sort = [("created_at", 1), ("id", 1)]
    elif order == "closed_desc":
        sort = [("closed_at", -1), ("updated_at", -1), ("created_at", -1), ("id", -1)]
    else:
        sort = [("created_at", -1), ("id", -1)]
    return _mongo_find_many(config, "trade_executions", query=query or None, sort=sort, limit=limit)
def update_trade_execution(config: dict[str, Any], trade_execution_id: int, updates: dict[str, Any]) -> dict[str, Any] | None:
    if not updates:
        return get_trade_execution(config, trade_execution_id)
    _ensure_mongo_write_allowed(config)
    _mongo_collection(config, "trade_executions").update_one({"id": int(trade_execution_id)}, {"$set": dict(updates)})
    return get_trade_execution(config, trade_execution_id)
def close_latest_trade_execution_by_status(
    config: dict[str, Any],
    *,
    symbol: str,
    side: str,
    source_status: str,
    target_status: str,
    reason: str | None = None,
    closed_at: str | None = None,
) -> dict[str, Any] | None:
    when = str(closed_at or datetime.now(timezone.utc).isoformat())
    row = _mongo_find_one(
        config,
        "trade_executions",
        query={"symbol": symbol, "side": side, "status": source_status},
        sort=[("created_at", -1), ("id", -1)],
    )
    if row is None:
        return None
    return update_trade_execution(
        config,
        int(row["id"]),
        {
            "status": target_status,
            "reject_reason": reason,
            "closed_at": when,
            "updated_at": when,
        },
    )
def list_trade_execution_ids(config: dict[str, Any], *, limit: int) -> list[int]:
    rows = list_trade_execution_rows(config, limit=limit, order="created_desc")
    return [int(row["id"]) for row in rows if row.get("id") is not None]


def get_trading_system_state_row(config: dict[str, Any]) -> dict[str, Any] | None:
    return _mongo_find_one(config, "trading_system_state", query={"id": 1})
def upsert_trading_system_state_row(config: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["id"] = 1
    _ensure_mongo_write_allowed(config)
    _mongo_upsert_by_pk(config, "trading_system_state", "id", payload)
    return payload
def upsert_trading_health_state_row(config: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["id"] = 1
    _ensure_mongo_write_allowed(config)
    _mongo_upsert_by_pk(config, "trading_health_state", "id", payload)
    return payload
def insert_replay_history_row(config: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    _ensure_mongo_write_allowed(config)
    payload = dict(row)
    payload["id"] = _mongo_next_id(config, "replay_history")
    _mongo_upsert_by_pk(config, "replay_history", "id", payload)
    return payload


def prune_replay_history(config: dict[str, Any], *, keep_days: float | None = None) -> dict[str, int]:
    retention = _storage_retention(config)
    keep_days = float(keep_days or retention.get("replay_history_keep_days", 365) or 365)
    cutoff = _iso_cutoff_days(keep_days)
    _ensure_mongo_write_allowed(config)
    deleted = int(
        _mongo_collection(config, "replay_history").delete_many(
            {
                "$or": [
                    {"replay_at": {"$lt": cutoff}},
                    {
                        "$and": [
                            {"replay_at": {"$in": [None, ""]}},
                            {"created_at": {"$lt": cutoff}},
                        ]
                    },
                ]
            }
        ).deleted_count
        or 0
    )
    return {"deleted_old": deleted}
def list_replay_history_rows(
    config: dict[str, Any],
    *,
    limit: int | None = None,
    include_trade_execution: bool = False,
) -> list[dict[str, Any]]:
    safe_limit = max(1, int(limit)) if limit is not None else None
    rows = _mongo_find_many(config, "replay_history", sort=[("replay_at", -1), ("id", -1)], limit=safe_limit)
    if not include_trade_execution or not rows:
        return rows
    execution_ids = [int(row["trade_execution_id"]) for row in rows if row.get("trade_execution_id") is not None]
    executions = {
        int(item["id"]): item
        for item in list_trade_execution_rows(config, ids=execution_ids, limit=None, order="created_desc")
    }
    merged: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        execution = executions.get(int(row["trade_execution_id"])) if row.get("trade_execution_id") is not None else None
        item["trade_status"] = execution.get("status") if execution else None
        item["trade_pnl"] = execution.get("pnl") if execution else None
        item["risk_reward"] = execution.get("risk_reward") if execution else None
        item["gpt_confidence"] = execution.get("gpt_confidence") if execution else None
        item["trade_created_at"] = execution.get("created_at") if execution else None
        item["trade_closed_at"] = execution.get("closed_at") if execution else None
        item["symbol"] = execution.get("symbol") if execution else None
        item["side"] = execution.get("side") if execution else None
        merged.append(item)
    return merged


def _is_deprecated_journal_state_key(key: str) -> bool:
    return str(key) in DEPRECATED_JOURNAL_STATE_KEYS


def _encode_journal_state_payload(value: str) -> dict[str, Any]:
    payload = str(value or "")
    raw = payload.encode("utf-8")
    if len(raw) < JOURNAL_STATE_COMPRESSION_THRESHOLD_BYTES:
        return {"value": payload}
    compressed = zlib.compress(raw, level=6)
    encoded = base64.b64encode(compressed).decode("ascii")
    if len(encoded) >= len(payload):
        return {"value": payload}
    return {
        "value": None,
        "value_compressed": encoded,
        "value_encoding": "zlib+base64",
    }


def _decode_journal_state_payload(row: dict[str, Any] | None) -> str | None:
    if not row:
        return None
    compressed = row.get("value_compressed")
    encoding = str(row.get("value_encoding") or "")
    if compressed and encoding == "zlib+base64":
        try:
            raw = zlib.decompress(base64.b64decode(str(compressed)))
            return raw.decode("utf-8")
        except Exception:
            return None
    value = row.get("value")
    return None if value is None else str(value)


def delete_journal_state(config: dict[str, Any], key: str) -> None:
    _ensure_mongo_write_allowed(config)
    _mongo_collection(config, "journal_state").delete_one({"_id": key})
    _journal_state_cache_invalidate(config, key)
    return
def delete_journal_state_prefix(config: dict[str, Any], prefix: str) -> None:
    _ensure_mongo_write_allowed(config)
    _mongo_collection(config, "journal_state").delete_many({"key": {"$regex": f"^{prefix}"}})
    _journal_state_cache_invalidate_prefix(config, prefix)
    return
def clear_dashboard_snapshot_cache(config: dict[str, Any]) -> None:
    _ensure_mongo_write_allowed(config)
    set_journal_state(config, DASHBOARD_SNAPSHOT_VERSION_KEY, datetime.now(timezone.utc).isoformat())


def dashboard_snapshot_cache_version(config: dict[str, Any]) -> str:
    return str(get_journal_state(config, DASHBOARD_SNAPSHOT_VERSION_KEY) or "0")


def purge_deprecated_journal_state(config: dict[str, Any]) -> list[str]:
    removed: list[str] = []
    _ensure_mongo_write_allowed(config)
    for key in sorted(DEPRECATED_JOURNAL_STATE_KEYS):
        result = _best_effort_retryable_storage_side_effect(
            config,
            lambda key=key: _mongo_collection(config, "journal_state").delete_one({"_id": key}),
        )
        if result is None:
            LOGGER.warning("Skipping deprecated journal_state purge for key '%s' due to transient Mongo issue.", key)
            continue
        if int(result.deleted_count or 0) > 0:
            removed.append(key)
    return removed
def get_journal_state(config: dict[str, Any], key: str) -> str | None:
    if _is_deprecated_journal_state_key(key):
        delete_journal_state(config, key)
        return None
    cached = _journal_state_cache_get(config, key)
    if cached is not None:
        return cached
    row = _mongo_collection(config, "journal_state").find_one(
        {"_id": key},
        {"_id": 0, "value": 1, "value_compressed": 1, "value_encoding": 1},
    )
    payload = _decode_journal_state_payload(row)
    if payload is None:
        return None
    _journal_state_cache_set(config, key, payload)
    if row and row.get("value") is not None and atlas_runtime_is_primary(config):
        encoded = _encode_journal_state_payload(payload)
        if encoded.get("value_compressed"):
            set_journal_state(config, key, payload)
    return payload
def set_journal_state(config: dict[str, Any], key: str, value: str) -> None:
    if _is_deprecated_journal_state_key(key):
        delete_journal_state(config, key)
        return
    now = datetime.now(timezone.utc).isoformat()
    _ensure_mongo_write_allowed(config)
    payload = _encode_journal_state_payload(value)
    _mongo_collection(config, "journal_state").replace_one(
        {"_id": key},
        {
            "_id": key,
            "key": key,
            **payload,
            "updated_at": now,
        },
        upsert=True,
    )
    _journal_state_cache_set(config, key, str(value or ""))
    return
def next_global_counter(config: dict[str, Any], name: str) -> int:
    key = f"counter:{name}"
    _ensure_mongo_write_allowed(config)
    from pymongo import ReturnDocument

    now = datetime.now(timezone.utc).isoformat()
    row = _mongo_collection(config, "journal_state").find_one_and_update(
        {"key": key},
        [
            {
                "$set": {
                    "key": key,
                    "counter_value": {"$add": [{"$ifNull": ["$counter_value", 0]}, 1]},
                    "updated_at": now,
                }
            },
            {"$set": {"value": {"$toString": "$counter_value"}}},
        ],
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int((row or {}).get("counter_value") or 1)
def next_daily_counter(config: dict[str, Any], name: str, date_key: str) -> int:
    key = f"counter:{name}:{date_key}"
    _ensure_mongo_write_allowed(config)
    from pymongo import ReturnDocument

    now = datetime.now(timezone.utc).isoformat()
    row = _mongo_collection(config, "journal_state").find_one_and_update(
        {"key": key},
        [
            {
                "$set": {
                    "key": key,
                    "counter_value": {"$add": [{"$ifNull": ["$counter_value", 0]}, 1]},
                    "updated_at": now,
                }
            },
            {"$set": {"value": {"$toString": "$counter_value"}}},
        ],
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int((row or {}).get("counter_value") or 1)
def _round_float(value: Any, digits: int = 6) -> Any:
    if isinstance(value, bool):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return round(number, digits)


def _trim_list(items: Any, limit: int) -> list[Any]:
    return list(items[:limit]) if isinstance(items, list) else []


def _compact_market_indicator(indicator: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(indicator, dict):
        return {}
    keep_keys = {
        "timeframe",
        "last",
        "trend",
        "rsi",
        "atr_pct",
        "volume_ratio",
        "spread_pct",
        "ema_gap_pct",
        "price_vs_ema_fast_pct",
        "price_vs_ema_slow_pct",
        "support_distance_pct",
        "resistance_distance_pct",
        "range_position",
        "signal_summary",
        "direction",
        "trend_context",
        "strongest_pattern",
        "bullish_score",
        "bearish_score",
    }
    compact: dict[str, Any] = {}
    for key in keep_keys:
        value = indicator.get(key)
        if value not in (None, "", [], {}):
            compact[key] = _round_float(value)
    patterns = indicator.get("candlestick_patterns")
    if isinstance(patterns, dict):
        compact["candlestick_patterns"] = {
            "direction": patterns.get("direction"),
            "trend_context": patterns.get("trend_context"),
            "strongest_pattern": patterns.get("strongest_pattern"),
            "signal_summary": patterns.get("signal_summary"),
            "patterns": _trim_list(patterns.get("patterns"), 3),
            "bullish_score": _round_float(patterns.get("bullish_score"), 3),
            "bearish_score": _round_float(patterns.get("bearish_score"), 3),
        }
        compact["candlestick_patterns"] = {
            key: value for key, value in compact["candlestick_patterns"].items()
            if value not in (None, "", [], {})
        }
    higher_timeframes = indicator.get("higher_timeframes")
    if isinstance(higher_timeframes, dict):
        compact["higher_timeframes"] = {
            str(frame): _compact_market_indicator(data)
            for frame, data in higher_timeframes.items()
            if isinstance(data, dict) and str(frame).lower() in {"5m", "15m", "1h", "4h"}
        }
        if not compact["higher_timeframes"]:
            compact.pop("higher_timeframes", None)
    return {key: value for key, value in compact.items() if value not in (None, "", [], {})}


def _compact_market_scan_payload(candidate: TradeCandidate, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": candidate.symbol,
        "base": candidate.base,
        "side": candidate.side,
        "confidence": _round_float(candidate.confidence, 3),
        "win_probability_pct": _round_float(candidate.win_probability_pct, 3),
        "risk_reward": _round_float(candidate.risk_reward, 3),
        "entry": _round_float(candidate.entry),
        "stop_loss": _round_float(candidate.stop_loss),
        "take_profit": _round_float(candidate.take_profit),
        "order_usdt": _round_float(candidate.order_usdt, 4),
        "quantity": _round_float(candidate.quantity, 8),
        "spread_pct": _round_float(candidate.spread_pct, 5),
        "news_score": _round_float(candidate.news_score, 4),
        "news_count": candidate.news_count,
        "target_mode": candidate.target_mode,
        "take_profit_pct": _round_float(candidate.take_profit_pct, 3),
        "stop_loss_pct": _round_float(candidate.stop_loss_pct, 3),
        "price_take_profit_pct": _round_float(candidate.price_take_profit_pct, 4),
        "price_stop_loss_pct": _round_float(candidate.price_stop_loss_pct, 4),
        "setup_quality": candidate.setup_quality,
        "market_regime": candidate.market_regime,
        "regime_confidence": _round_float(candidate.regime_confidence, 3),
        "scan_source": candidate.scan_source,
        "indicator_summary": _compact_market_indicator(candidate.indicator_summary),
        "reasons": _trim_list(payload.get("reasons"), 4),
        "warnings": _trim_list(payload.get("warnings"), 3),
    }


def _compact_frame_payload(candidate: TradeCandidate, frame_name: str, frame_indicator: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": candidate.symbol,
        "side": candidate.side,
        "timeframe": str(frame_name),
        "confidence": _round_float(candidate.confidence, 3),
        "win_probability_pct": _round_float(candidate.win_probability_pct, 3),
        "risk_reward": _round_float(candidate.risk_reward, 3),
        "frame_summary": _compact_market_indicator(frame_indicator),
    }


def _json_limited(payload: dict[str, Any], max_bytes: int) -> str:
    text = json.dumps(
        {key: value for key, value in payload.items() if value not in (None, "", [], {})},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    encoded = text.encode("utf-8")
    if len(encoded) <= max(256, max_bytes):
        return text
    trimmed = dict(payload)
    for key in ("reasons", "warnings"):
        if isinstance(trimmed.get(key), list):
            trimmed[key] = trimmed[key][:1]
    for key in ("indicator_summary", "frame_summary"):
        if isinstance(trimmed.get(key), dict):
            compact = dict(trimmed[key])
            compact.pop("higher_timeframes", None)
            compact.pop("candlestick_patterns", None)
            trimmed[key] = compact
    text = json.dumps(
        {key: value for key, value in trimmed.items() if value not in (None, "", [], {})},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(text.encode("utf-8")) <= max(256, max_bytes):
        return text
    return json.dumps(
        {
            "symbol": payload.get("symbol"),
            "side": payload.get("side"),
            "timeframe": payload.get("timeframe"),
            "confidence": payload.get("confidence"),
            "win_probability_pct": payload.get("win_probability_pct"),
            "risk_reward": payload.get("risk_reward"),
            "entry": payload.get("entry"),
            "stop_loss": payload.get("stop_loss"),
            "take_profit": payload.get("take_profit"),
            "truncated": True,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def save_decision(config: dict[str, Any], decision: Decision) -> int:
    payload = to_jsonable(decision)
    selected = decision.selected
    row_id = _insert_decision_payload(config, decision, payload, selected)
    _best_effort_retryable_storage_side_effect(config, lambda: prune_decision_history(config))
    return row_id


def _insert_decision_payload(
    config: dict[str, Any],
    decision: Decision,
    payload: dict[str, Any],
    selected: TradeCandidate | None,
) -> int:
    _ensure_mongo_write_allowed(config)
    row_id = _mongo_next_id(config, "decisions")
    _mongo_upsert_by_pk(
        config,
        "decisions",
        "id",
        {
            "id": row_id,
            "created_at": payload["created_at"],
            "action": decision.action,
            "selected_symbol": selected.symbol if selected else None,
            "selected_side": selected.side if selected else None,
            "selected_win_probability_pct": selected.win_probability_pct if selected else None,
            "payload_json": json.dumps(payload, ensure_ascii=False),
        },
    )
    return row_id
def prune_decision_history(
    config: dict[str, Any],
    *,
    keep_hours: int | None = None,
    max_rows: int | None = None,
) -> dict[str, int]:
    history_config = config.get("decision_history", {})
    keep_hours = int(keep_hours or history_config.get("keep_hours", 24) or 24)
    max_rows = int(max_rows or history_config.get("max_rows", 120) or 120)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, keep_hours))).isoformat()
    deleted_old = 0
    deleted_over_limit = 0
    _ensure_mongo_write_allowed(config)
    rows = _mongo_find_many(config, "decisions", sort=[("created_at", -1), ("id", -1)])
    latest_id = rows[0]["id"] if rows else None
    stale_ids = [row["id"] for row in rows if row.get("created_at", "") < cutoff and row.get("id") != latest_id]
    if stale_ids:
        deleted_old = int(_mongo_collection(config, "decisions").delete_many({"id": {"$in": stale_ids}}).deleted_count or 0)
    rows = _mongo_find_many(config, "decisions", sort=[("created_at", -1), ("id", -1)])
    overflow_ids = [row["id"] for row in rows[max(1, max_rows) :]]
    if overflow_ids:
        deleted_over_limit = int(
            _mongo_collection(config, "decisions").delete_many({"id": {"$in": overflow_ids}}).deleted_count or 0
        )
    return {"deleted_old": deleted_old, "deleted_over_limit": deleted_over_limit}
def save_market_scan_observations(
    config: dict[str, Any],
    candidates: list[TradeCandidate],
    *,
    source: str,
    limit: int = 100,
) -> int:
    if not candidates:
        return 0
    memory_config = config.get("market_scan_memory", {})
    max_json_bytes = int(memory_config.get("max_json_bytes", DEFAULT_MARKET_SCAN_MAX_JSON_BYTES) or DEFAULT_MARKET_SCAN_MAX_JSON_BYTES)
    now = datetime.now(timezone.utc).isoformat()
    rows: list[tuple[Any, ...]] = []
    for candidate in candidates[: max(1, int(limit or 100))]:
        payload = to_jsonable(candidate)
        indicator = _compact_market_indicator(to_jsonable(candidate.indicator_summary or {}))
        compact_payload = _compact_market_scan_payload(candidate, payload if isinstance(payload, dict) else {})
        score = float(candidate.win_probability_pct or candidate.confidence or 0)
        timeframe = str(indicator.get("timeframe") or config.get("strategy", {}).get("timeframe") or "")
        rows.append(
            (
                now,
                source,
                candidate.symbol,
                candidate.side,
                timeframe,
                candidate.confidence,
                candidate.win_probability_pct,
                candidate.risk_reward,
                score,
                _json_limited(indicator, max_json_bytes),
                _json_limited(compact_payload, max_json_bytes),
            )
        )
        higher_timeframes = payload.get("higher_timeframes") if isinstance(payload, dict) else {}
        if isinstance(higher_timeframes, dict):
            for frame_name, frame_payload in higher_timeframes.items():
                if not isinstance(frame_payload, dict):
                    continue
                frame_indicator = _compact_market_indicator(dict(frame_payload))
                frame_indicator.setdefault("timeframe", str(frame_name))
                frame_payload_json = _compact_frame_payload(candidate, str(frame_name), frame_indicator)
                frame_score = float(
                    frame_indicator.get("bullish_score")
                    or frame_indicator.get("bearish_score")
                    or (frame_indicator.get("candlestick_patterns") or {}).get("bullish_score")
                    or (frame_indicator.get("candlestick_patterns") or {}).get("bearish_score")
                    or candidate.confidence
                    or 0
                )
                rows.append(
                    (
                        now,
                        source,
                        candidate.symbol,
                        candidate.side,
                        str(frame_name),
                        candidate.confidence,
                        candidate.win_probability_pct,
                        candidate.risk_reward,
                        frame_score,
                        _json_limited(frame_indicator, max_json_bytes),
                        _json_limited(frame_payload_json, max_json_bytes),
                    )
                )
    _update_local_market_scan_cache(config, rows)
    _insert_market_scan_rows(config, rows)
    _best_effort_retryable_storage_side_effect(config, lambda: prune_market_scan_observations(config))
    _best_effort_retryable_storage_side_effect(config, lambda: run_storage_maintenance(config, vacuum=False))
    return len(rows)


def _insert_market_scan_rows(config: dict[str, Any], rows: list[tuple[Any, ...]]) -> None:
    _ensure_mongo_write_allowed(config)
    for row in rows:
        row_id = _mongo_next_id(config, "market_scan_observations")
        _mongo_upsert_by_pk(
            config,
            "market_scan_observations",
            "id",
            {
                "id": row_id,
                "created_at": row[0],
                "source": row[1],
                "symbol": row[2],
                "side": row[3],
                "timeframe": row[4],
                "confidence": row[5],
                "win_probability_pct": row[6],
                "risk_reward": row[7],
                "score": row[8],
                "indicator_json": row[9],
                "payload_json": row[10],
            },
        )
    return
def prune_market_scan_observations(
    config: dict[str, Any],
    *,
    keep_hours: int | None = None,
    max_rows_per_symbol_timeframe: int | None = None,
) -> dict[str, int]:
    memory_config = config.get("market_scan_memory", {})
    keep_hours = int(keep_hours or memory_config.get("keep_hours", 72) or 72)
    max_rows = int(
        max_rows_per_symbol_timeframe
        or memory_config.get("max_rows_per_symbol_timeframe", 200)
        or 200
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, keep_hours))).isoformat()
    deleted_old = 0
    deleted_over_limit = 0
    _ensure_mongo_write_allowed(config)
    deleted_old = int(
        _mongo_collection(config, "market_scan_observations").delete_many({"created_at": {"$lt": cutoff}}).deleted_count
        or 0
    )
    rows = _mongo_find_many(
        config,
        "market_scan_observations",
        sort=[("symbol", 1), ("timeframe", 1), ("created_at", -1), ("id", -1)],
    )
    counters: dict[tuple[str, str], int] = {}
    overflow_ids: list[int] = []
    for row in rows:
        key = (str(row.get("symbol") or ""), str(row.get("timeframe") or ""))
        counters[key] = counters.get(key, 0) + 1
        if counters[key] > max(1, max_rows):
            overflow_ids.append(int(row["id"]))
    if overflow_ids:
        deleted_over_limit = int(
            _mongo_collection(config, "market_scan_observations").delete_many({"id": {"$in": overflow_ids}}).deleted_count
            or 0
        )
    return {
        "deleted_old": deleted_old,
        "deleted_over_limit": deleted_over_limit,
    }
def compact_market_scan_observations(config: dict[str, Any], *, batch_limit: int = 5000) -> dict[str, int]:
    memory_config = config.get("market_scan_memory", {})
    max_json_bytes = int(memory_config.get("max_json_bytes", DEFAULT_MARKET_SCAN_MAX_JSON_BYTES) or DEFAULT_MARKET_SCAN_MAX_JSON_BYTES)
    checked = 0
    compacted = 0
    _ensure_mongo_write_allowed(config)
    rows = _mongo_find_many(config, "market_scan_observations", sort=[("id", -1)], limit=max(1, int(batch_limit or 5000)))
    for row in rows:
        checked += 1
        try:
            indicator = json.loads(str(row.get("indicator_json") or "{}"))
        except json.JSONDecodeError:
            indicator = {}
        try:
            payload = json.loads(str(row.get("payload_json") or "{}"))
        except json.JSONDecodeError:
            payload = {}
        compact_indicator_json = _json_limited(_compact_market_indicator(indicator), max_json_bytes)
        compact_payload_json = _json_limited(payload if isinstance(payload, dict) else {}, max_json_bytes)
        if compact_indicator_json != str(row.get("indicator_json") or "") or compact_payload_json != str(
            row.get("payload_json") or ""
        ):
            _mongo_collection(config, "market_scan_observations").update_one(
                {"id": row["id"]},
                {"$set": {"indicator_json": compact_indicator_json, "payload_json": compact_payload_json}},
            )
            compacted += 1
    return {"checked": checked, "compacted": compacted}
def storage_stats(config: dict[str, Any]) -> dict[str, Any]:
    tables = [
        "decisions",
        "paper_trades",
        "pending_orders",
        "internal_pending_orders",
        "journal_state",
        "trade_memory",
        "market_guard_observations",
        "market_scan_observations",
        "prompt_metrics",
        "prompt_versions",
        "strategy_versions",
        "ai_model_versions",
        "ai_experiments",
        "ai_trade_decisions",
        "trade_executions",
        "trading_system_state",
        "trading_health_state",
        "trade_candidates",
        "market_regime_history",
        "replay_history",
    ]

    def _collection_row_count(table: str) -> int:
        collection = _mongo_collection(config, table)
        try:
            return int(collection.estimated_document_count())
        except Exception:
            return int(collection.count_documents({}))

    def _collection_payload_bytes(table: str) -> int:
        collection = _mongo_collection(config, table)
        try:
            rows = list(
                collection.aggregate(
                    [
                        {
                            "$project": {
                                "payload_size": {
                                    "$strLenBytes": {
                                        "$ifNull": ["$payload_json", ""],
                                    }
                                }
                            }
                        },
                        {
                            "$group": {
                                "_id": None,
                                "total_bytes": {"$sum": "$payload_size"},
                            }
                        },
                    ]
                )
            )
            return int((rows[0] if rows else {}).get("total_bytes") or 0)
        except Exception:
            total = 0
            for row in collection.find({}, {"payload_json": 1}):
                total += len(str(row.get("payload_json") or "").encode("utf-8"))
            return total

    row_counts: dict[str, int] = {}
    payload_bytes: dict[str, int] = {}
    for table in tables:
        row_counts[table] = _collection_row_count(table)
        payload_bytes[table] = _collection_payload_bytes(table)
    try:
        timeframe_rows = _mongo_collection(config, "market_scan_observations").aggregate(
            [
                {
                    "$group": {
                        "_id": "$timeframe",
                        "rows": {"$sum": 1},
                        "symbols": {"$addToSet": "$symbol"},
                        "latest_at": {"$max": "$created_at"},
                    }
                },
                {"$sort": {"_id": 1}},
            ]
        )
    except Exception:
        timeframe_rows = []
    market_scan_by_timeframe = [
        {
            "timeframe": row.get("_id"),
            "rows": int(row.get("rows") or 0),
            "symbols": len(row.get("symbols") or []),
            "latest_at": row.get("latest_at"),
        }
        for row in timeframe_rows
    ]
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "db_path": f"atlas:{atlas_database(config).name}",
        "backend": "atlas",
        "files": {},
        "disk": {},
        "row_counts": row_counts,
        "payload_bytes": payload_bytes,
        "market_scan_by_timeframe": market_scan_by_timeframe,
        "retention": {
            "market_scan_memory": config.get("market_scan_memory", {}),
            "decision_history": config.get("decision_history", {}),
            "market_guard": {"memory_keep_hours": config.get("market_guard", {}).get("memory_keep_hours", 6)},
            "pending_orders": config.get("pending_orders", {}),
            "storage_retention": _storage_retention(config),
        },
    }
def run_storage_maintenance(
    config: dict[str, Any],
    *,
    vacuum: bool = False,
    emergency: bool = False,
) -> dict[str, Any]:
    memory_config = config.get("market_scan_memory", {})
    decision_config = config.get("decision_history", {})
    if emergency:
        prune_result = prune_market_scan_observations(
            config,
            keep_hours=int(memory_config.get("emergency_keep_hours", 24) or 24),
            max_rows_per_symbol_timeframe=int(memory_config.get("emergency_max_rows_per_symbol_timeframe", 50) or 50),
        )
        decision_prune_result = prune_decision_history(
            config,
            keep_hours=int(decision_config.get("emergency_keep_hours", 6) or 6),
            max_rows=int(decision_config.get("emergency_max_rows", 30) or 30),
        )
    else:
        prune_result = prune_market_scan_observations(config)
        decision_prune_result = prune_decision_history(config)
    extra_prune: dict[str, Any] = {}
    compact_result = {"checked": 0, "compacted": 0}
    checkpoint: list[Any] = []
    optimized = False
    vacuumed = False
    errors: list[str] = []
    _ensure_mongo_write_allowed(config)
    try:
        extra_prune = {
            "pending_orders": prune_pending_orders(config),
            "internal_pending_orders": prune_internal_pending_orders(config),
            "trade_candidates": prune_trade_candidates(config),
            "ai_trade_decisions": prune_ai_trade_decisions(config),
            "trade_executions": prune_trade_executions(config),
            "prompt_versions": prune_prompt_versions(config),
            "strategy_versions": prune_strategy_versions(config),
            "ai_experiments": prune_ai_experiments(config),
            "replay_history": prune_replay_history(config),
            "paper_trades": prune_paper_trades(config),
        }
    except Exception as exc:
        errors.append(f"extra_prune: {exc}")
    try:
        compact_result = compact_market_scan_observations(
            config,
            batch_limit=int(memory_config.get("compact_batch_limit", 5000) or 5000),
        )
    except Exception as exc:
        errors.append(f"compact_market_scan: {exc}")
    return {
        "ok": not errors,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "prune": prune_result,
        "decision_prune": decision_prune_result,
        "extra_prune": extra_prune,
        "compact": compact_result,
        "emergency": emergency,
        "checkpoint": checkpoint,
        "optimized": True,
        "vacuumed": False,
        "errors": errors,
        "stats": storage_stats(config),
    }
def recent_market_scan_memory(
    config: dict[str, Any],
    *,
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    lookback_hours: int = 12,
    per_symbol_timeframe_limit: int = 3,
    total_limit: int = 1000,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    query: dict[str, Any] = {
        "created_at": {
            "$gte": (datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))).isoformat()
        }
    }
    symbol_list = [str(item) for item in (symbols or []) if str(item)]
    timeframe_list = [str(item) for item in (timeframes or []) if str(item)]
    if symbol_list:
        query["symbol"] = {"$in": symbol_list}
    if timeframe_list:
        query["timeframe"] = {"$in": timeframe_list}
    try:
        rows = _mongo_find_many(
            config,
            "market_scan_observations",
            query=query,
            sort=[("created_at", -1), ("id", -1)],
            limit=max(10, int(total_limit)),
        )
        normalized_rows = [
            {
                "created_at": item.get("created_at"),
                "source": item.get("source"),
                "side": item.get("side"),
                "symbol": item.get("symbol"),
                "timeframe": item.get("timeframe"),
                "confidence": item.get("confidence"),
                "win_probability_pct": item.get("win_probability_pct"),
                "risk_reward": item.get("risk_reward"),
                "score": item.get("score"),
                "indicator": _json_object_or_empty(item.get("indicator_json")),
                "payload": _json_object_or_empty(item.get("payload_json")),
            }
            for item in rows
        ]
    except Exception as exc:
        if not _mongo_error_is_retryable(exc):
            raise
        normalized_rows = _load_local_market_scan_cache_rows(config)
        cutoff = query["created_at"]["$gte"]
        normalized_rows = [
            item
            for item in normalized_rows
            if str(item.get("created_at") or "") >= cutoff
            and (not symbol_list or str(item.get("symbol") or "") in symbol_list)
            and (not timeframe_list or str(item.get("timeframe") or "") in timeframe_list)
        ]
    return _group_recent_market_scan_rows(
        normalized_rows,
        per_symbol_timeframe_limit=per_symbol_timeframe_limit,
    )
def latest_decision_payload(config: dict[str, Any]) -> dict[str, Any] | None:
    try:
        row = _mongo_find_one(config, "decisions", sort=[("id", -1)])
        if not row:
            return None
        return json.loads(str(row["payload_json"]))
    except Exception as exc:
        if not _mongo_error_is_retryable(exc):
            raise
        report_path = project_path(config, config.get("report_path", "reports/latest_decision.json"))
        if not report_path.exists():
            return None
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None
def list_paper_trades(config: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
    return _mongo_find_many(config, "paper_trades", sort=[("id", -1)], limit=limit)
def active_paper_trades(config: dict[str, Any]) -> list[dict[str, Any]]:
    return _mongo_find_many(config, "paper_trades", query={"status": "OPEN"}, sort=[("id", 1)])
def list_pending_orders(config: dict[str, Any], status: str = "OPEN", limit: int = 100) -> list[dict[str, Any]]:
    migrate_legacy_pending_orders(config)
    normalized_status = str(status or "OPEN").upper()
    safe_limit = max(1, int(limit))
    if normalized_status in {"OPEN", "ACTIVE"}:
        internal_rows = _mongo_find_many(
            config,
            PENDING_INTERNAL_COLLECTION,
            query={"status": {"$in": list(INTERNAL_PENDING_STATUSES)}},
            sort=[("updated_at", -1), ("created_at", -1), ("id", -1)],
            limit=safe_limit,
        )
        okx_rows = _mongo_find_many(
            config,
            PENDING_OKX_COLLECTION,
            query={"status": {"$in": list(OKX_PENDING_STATUSES)}},
            sort=[("updated_at", -1), ("created_at", -1), ("id", -1)],
            limit=safe_limit,
        )
        return _merge_pending_rows(internal_rows, okx_rows, limit=safe_limit)
    if normalized_status in INTERNAL_PENDING_STATUSES:
        return _mongo_find_many(
            config,
            PENDING_INTERNAL_COLLECTION,
            query={"status": normalized_status},
            sort=[("updated_at", -1), ("created_at", -1), ("id", -1)],
            limit=safe_limit,
        )
    okx_rows = _mongo_find_many(
        config,
        PENDING_OKX_COLLECTION,
        query={"status": normalized_status},
        sort=[("updated_at", -1), ("created_at", -1), ("id", -1)],
        limit=safe_limit,
    )
    if okx_rows:
        return okx_rows
    return _mongo_find_many(
        config,
        PENDING_INTERNAL_COLLECTION,
        query={"status": normalized_status},
        sort=[("updated_at", -1), ("created_at", -1), ("id", -1)],
        limit=safe_limit,
    )
def open_pending_symbols(config: dict[str, Any]) -> set[str]:
    return {str(order["symbol"]) for order in list_pending_orders(config, status="OPEN")}


def _pending_expiry(now: datetime, *, max_age_days: float = 3, max_age_hours: float | None = None) -> datetime:
    if max_age_hours is not None:
        return now + timedelta(hours=max(0.1, float(max_age_hours)))
    return now + timedelta(days=max(0.1, float(max_age_days)))


def prune_pending_orders(config: dict[str, Any], *, keep_days: float | None = None) -> dict[str, int]:
    migrate_legacy_pending_orders(config)
    retention = _storage_retention(config)
    keep_days = float(keep_days or retention.get("pending_orders_keep_days", 5) or 5)
    cutoff = _iso_cutoff_days(keep_days)
    _ensure_mongo_write_allowed(config)
    deleted = int(_mongo_collection(config, PENDING_OKX_COLLECTION).delete_many({"created_at": {"$lt": cutoff}}).deleted_count or 0)
    return {"deleted_old": deleted}


def prune_internal_pending_orders(config: dict[str, Any], *, keep_days: float | None = None) -> dict[str, int]:
    migrate_legacy_pending_orders(config)
    retention = _storage_retention(config)
    keep_days = float(
        keep_days
        or retention.get("internal_pending_orders_keep_days", retention.get("pending_orders_keep_days", 5))
        or 5
    )
    cutoff = _iso_cutoff_days(keep_days)
    _ensure_mongo_write_allowed(config)
    deleted = int(_mongo_collection(config, PENDING_INTERNAL_COLLECTION).delete_many({"created_at": {"$lt": cutoff}}).deleted_count or 0)
    return {"deleted_old": deleted}


def save_pending_order(
    config: dict[str, Any],
    candidate: TradeCandidate,
    exchange_order_id: str | None,
    *,
    status: str | None = None,
    max_age_days: float = 3,
    max_age_hours: float | None = None,
    journal_id: int | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    migrate_legacy_pending_orders(config)
    payload = to_jsonable(candidate)
    normalized_status = str(status or ("LC_OKX" if exchange_order_id else "OPEN")).upper()
    _ensure_mongo_write_allowed(config)
    target_table = _pending_collection_for_status(normalized_status, exchange_order_id)
    order_id = _next_pending_order_id(config)
    row = {
        "id": order_id,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "expires_at": _pending_expiry(now, max_age_days=max_age_days, max_age_hours=max_age_hours).isoformat(),
        "status": normalized_status,
        "symbol": candidate.symbol,
        "base": candidate.base,
        "side": candidate.side,
        "exchange_order_id": exchange_order_id,
        "entry": candidate.entry,
        "stop_loss": candidate.stop_loss,
        "take_profit": candidate.take_profit,
        "quantity": candidate.quantity,
        "order_usdt": candidate.order_usdt,
        "confidence": candidate.confidence,
        "win_probability_pct": candidate.win_probability_pct,
        "risk_reward": candidate.risk_reward,
        "payload_json": json.dumps(payload, ensure_ascii=False),
        "journal_id": journal_id,
        "close_reason": None,
    }
    _mongo_upsert_by_pk(config, target_table, "id", row)
    _best_effort_retryable_storage_side_effect(config, lambda: prune_pending_orders(config))
    _best_effort_retryable_storage_side_effect(config, lambda: prune_internal_pending_orders(config))
    return row
def refresh_pending_order(
    config: dict[str, Any],
    order_id: int,
    candidate: TradeCandidate,
    *,
    status: str | None = None,
    max_age_days: float = 3,
    max_age_hours: float | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    payload = to_jsonable(candidate)
    migrate_legacy_pending_orders(config)
    _ensure_mongo_write_allowed(config)
    current_table, existing = _find_pending_record(config, order_id)
    if existing is None:
        return
    updates = {
        "updated_at": now.isoformat(),
        "expires_at": _pending_expiry(now, max_age_days=max_age_days, max_age_hours=max_age_hours).isoformat(),
        "entry": candidate.entry,
        "stop_loss": candidate.stop_loss,
        "take_profit": candidate.take_profit,
        "quantity": candidate.quantity,
        "order_usdt": candidate.order_usdt,
        "confidence": candidate.confidence,
        "win_probability_pct": candidate.win_probability_pct,
        "risk_reward": candidate.risk_reward,
        "payload_json": json.dumps(payload, ensure_ascii=False),
    }
    normalized_status = str(status or existing.get("status") or "").upper()
    if normalized_status:
        updates["status"] = normalized_status
    target_table = _pending_collection_for_status(
        normalized_status or str(existing.get("status") or ""),
        str(existing.get("exchange_order_id") or ""),
    )
    if current_table == target_table:
        _mongo_collection(config, current_table).update_one({"id": order_id}, {"$set": updates})
        return
    merged = dict(existing)
    merged.update(updates)
    _mongo_upsert_by_pk(config, target_table, "id", merged)
    _mongo_collection(config, current_table).delete_one({"id": order_id})
    return
def set_pending_order_exchange_order(
    config: dict[str, Any],
    order_id: int,
    candidate: TradeCandidate,
    exchange_order_id: str,
    *,
    max_age_days: float = 1.5,
) -> None:
    now = datetime.now(timezone.utc)
    payload = to_jsonable(candidate)
    migrate_legacy_pending_orders(config)
    _ensure_mongo_write_allowed(config)
    current_table, existing = _find_pending_record(config, order_id)
    if existing is None:
        return
    merged = dict(existing)
    merged.update(
        {
            "updated_at": now.isoformat(),
            "expires_at": _pending_expiry(now, max_age_days=max_age_days).isoformat(),
            "status": "LC_OKX",
            "exchange_order_id": exchange_order_id,
            "entry": candidate.entry,
            "stop_loss": candidate.stop_loss,
            "take_profit": candidate.take_profit,
            "quantity": candidate.quantity,
            "order_usdt": candidate.order_usdt,
            "confidence": candidate.confidence,
            "win_probability_pct": candidate.win_probability_pct,
            "risk_reward": candidate.risk_reward,
            "payload_json": json.dumps(payload, ensure_ascii=False),
        }
    )
    _mongo_upsert_by_pk(config, PENDING_OKX_COLLECTION, "id", merged)
    if current_table != PENDING_OKX_COLLECTION:
        _mongo_collection(config, current_table).delete_one({"id": order_id})
    return
def close_pending_order(config: dict[str, Any], order_id: int, status: str, reason: str) -> None:
    normalized_status = str(status or "").upper()
    now = datetime.now(timezone.utc).isoformat()
    migrate_legacy_pending_orders(config)
    _ensure_mongo_write_allowed(config)
    current_table, existing = _find_pending_record(config, order_id)
    if existing is None:
        return
    if normalized_status in {"CANCELED", "FILLED", "CLOSED"}:
        _mongo_collection(config, current_table).delete_one({"id": order_id})
        return
    target_table = _pending_collection_for_status(normalized_status, str(existing.get("exchange_order_id") or ""))
    updates = {"status": normalized_status, "updated_at": now, "close_reason": reason}
    if current_table == target_table:
        _mongo_collection(config, current_table).update_one({"id": order_id}, {"$set": updates})
        return
    merged = dict(existing)
    merged.update(updates)
    _mongo_upsert_by_pk(config, target_table, "id", merged)
    _mongo_collection(config, current_table).delete_one({"id": order_id})
    return
def count_pending_orders(config: dict[str, Any], status: str = "OPEN") -> int:
    migrate_legacy_pending_orders(config)
    normalized_status = str(status or "OPEN").upper()
    if normalized_status in {"OPEN", "ACTIVE"}:
        internal_count = int(
            _mongo_collection(config, PENDING_INTERNAL_COLLECTION).count_documents(
                {"status": {"$in": list(INTERNAL_PENDING_STATUSES)}}
            )
        )
        okx_count = int(
            _mongo_collection(config, PENDING_OKX_COLLECTION).count_documents(
                {"status": {"$in": list(OKX_PENDING_STATUSES)}}
            )
        )
        return internal_count + okx_count
    if normalized_status in INTERNAL_PENDING_STATUSES:
        return int(_mongo_collection(config, PENDING_INTERNAL_COLLECTION).count_documents({"status": normalized_status}))
    okx_count = int(_mongo_collection(config, PENDING_OKX_COLLECTION).count_documents({"status": normalized_status}))
    if okx_count:
        return okx_count
    return int(_mongo_collection(config, PENDING_INTERNAL_COLLECTION).count_documents({"status": normalized_status}))
def save_market_guard_observation(config: dict[str, Any], observation: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    created_at = str(observation.get("created_at") or now)
    observed_at = str(observation.get("observed_at") or created_at)
    symbol = str(observation.get("symbol") or "")
    if not symbol:
        return
    reasons = observation.get("reasons") or []
    _ensure_mongo_write_allowed(config)
    existing = _mongo_find_one(
        config,
        "market_guard_observations",
        query={"symbol": symbol, "observed_at": observed_at},
        sort=[("id", -1)],
    )
    row_id = int(existing["id"]) if existing else _mongo_next_id(config, "market_guard_observations")
    _mongo_upsert_by_pk(
        config,
        "market_guard_observations",
        "id",
        {
            "id": row_id,
            "created_at": created_at,
            "observed_at": observed_at,
            "symbol": symbol,
            "last": observation.get("last"),
            "move_pct": observation.get("move_pct"),
            "candle_range_pct": observation.get("candle_range_pct"),
            "wick_pct": observation.get("wick_pct"),
            "wick_body_ratio": observation.get("wick_body_ratio"),
            "volume_ratio": observation.get("volume_ratio"),
            "severity": str(observation.get("severity") or "normal"),
            "alert_reasons_json": json.dumps(reasons, ensure_ascii=False),
        },
    )
    return
def list_market_guard_observations(
    config: dict[str, Any],
    *,
    symbol: str | None = None,
    limit: int = 200,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    query: dict[str, Any] = {}
    if symbol:
        query["symbol"] = symbol
    if since:
        query["observed_at"] = {"$gte": since.isoformat()}
    rows = _mongo_find_many(config, "market_guard_observations", query=query, sort=[("observed_at", -1), ("id", -1)], limit=limit)
    result: list[dict[str, Any]] = []
    for item in rows:
        try:
            item["reasons"] = json.loads(str(item.get("alert_reasons_json") or "[]"))
        except json.JSONDecodeError:
            item["reasons"] = []
        result.append(item)
    return result
def prune_market_guard_observations(config: dict[str, Any], *, keep_hours: int = 6) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, keep_hours))).isoformat()
    _ensure_mongo_write_allowed(config)
    _mongo_collection(config, "market_guard_observations").delete_many({"observed_at": {"$lt": cutoff}})
    return
def save_trade_memory(config: dict[str, Any], record: dict[str, Any], *, limit: int = 100) -> bool:
    key = str(record.get("key") or "")
    symbol = str(record.get("symbol") or "")
    if not key or not symbol:
        return False

    now = datetime.now(timezone.utc).isoformat()
    pnl_usdt = record.get("pnl_usdt")
    outcome = "win" if float(pnl_usdt or 0) > 0 else "loss" if float(pnl_usdt or 0) < 0 else "flat"
    _ensure_mongo_write_allowed(config)
    existing = _mongo_find_one(config, "trade_memory", query={"key": key})
    if existing:
        return False
    _mongo_upsert_by_pk(
        config,
        "trade_memory",
        "key",
        {
            "key": key,
            "created_at": now,
            "updated_at": now,
            "symbol": symbol,
            "side": record.get("side"),
            "opened_at": record.get("opened_at"),
            "closed_at": record.get("closed_at"),
            "pnl_usdt": pnl_usdt,
            "pnl_pct": record.get("pnl_pct"),
            "outcome": outcome,
            "source": str(record.get("source") or "okx"),
            "payload_json": json.dumps(record.get("payload") or record, ensure_ascii=False),
        },
    )
    rows = _mongo_find_many(config, "trade_memory", sort=[("closed_at", -1), ("updated_at", -1)], limit=None)
    overflow_keys = [row["key"] for row in rows[max(1, limit) :]]
    if overflow_keys:
        _mongo_collection(config, "trade_memory").delete_many({"key": {"$in": overflow_keys}})
    return True
def list_trade_memory(config: dict[str, Any], limit: int = 100) -> list[dict[str, Any]]:
    return _mongo_find_many(config, "trade_memory", sort=[("closed_at", -1), ("updated_at", -1)], limit=limit)
def open_paper_trade(config: dict[str, Any], candidate: TradeCandidate) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    leverage = float(config.get("exchange", {}).get("leverage", 1) or 1)
    payload = to_jsonable(candidate)
    _ensure_mongo_write_allowed(config)
    trade_id = _mongo_next_id(config, "paper_trades")
    row = {
        "id": trade_id,
        "created_at": now,
        "updated_at": now,
        "status": "OPEN",
        "symbol": candidate.symbol,
        "base": candidate.base,
        "side": candidate.side,
        "entry": candidate.entry,
        "stop_loss": candidate.stop_loss,
        "take_profit": candidate.take_profit,
        "quantity": candidate.quantity,
        "order_usdt": candidate.order_usdt,
        "confidence": candidate.confidence,
        "win_probability_pct": candidate.win_probability_pct,
        "risk_reward": candidate.risk_reward,
        "leverage": leverage,
        "close_price": None,
        "close_reason": None,
        "pnl_pct": None,
        "payload_json": json.dumps(payload, ensure_ascii=False),
    }
    _mongo_upsert_by_pk(config, "paper_trades", "id", row)
    return row
def close_paper_trade(config: dict[str, Any], trade_id: int, close_price: float, reason: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    active = [trade for trade in active_paper_trades(config) if int(trade["id"]) == trade_id]
    if not active:
        raise ValueError(f"Paper trade {trade_id} is not open")
    trade = active[0]
    entry = float(trade["entry"])
    leverage = float(trade["leverage"] or 1)
    if trade["side"] == "long":
        pnl_pct = ((close_price - entry) / entry) * 100 * leverage
    else:
        pnl_pct = ((entry - close_price) / entry) * 100 * leverage
    _ensure_mongo_write_allowed(config)
    _mongo_collection(config, "paper_trades").update_one(
        {"id": trade_id},
        {
            "$set": {
                "status": "CLOSED",
                "updated_at": now,
                "close_price": close_price,
                "close_reason": reason,
                "pnl_pct": round(pnl_pct, 4),
            }
        },
    )
    updated = _mongo_find_one(config, "paper_trades", query={"id": trade_id})
    return updated or {}


def prune_paper_trades(config: dict[str, Any], *, keep_days: float | None = None) -> dict[str, int]:
    retention = _storage_retention(config)
    keep_days = float(keep_days or retention.get("paper_trades_keep_days", 365) or 365)
    cutoff = _iso_cutoff_days(keep_days)
    _ensure_mongo_write_allowed(config)
    deleted = int(
        _mongo_collection(config, "paper_trades").delete_many(
            {
                "$and": [
                    {"status": {"$ne": "OPEN"}},
                    {
                        "$or": [
                            {"updated_at": {"$lt": cutoff}},
                            {"created_at": {"$lt": cutoff}},
                        ]
                    },
                ]
            }
        ).deleted_count
        or 0
    )
    return {"deleted_old": deleted}
