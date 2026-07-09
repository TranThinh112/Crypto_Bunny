from __future__ import annotations

import os
import threading
from typing import Any


_ATLAS_LOCK = threading.RLock()
_ATLAS_CLIENTS: dict[tuple[str, str], Any] = {}
_ATLAS_INDEXES_READY: set[tuple[str, str]] = set()

_ATLAS_COLLECTION_INDEX_SPECS: dict[str, list[list[tuple[str, int]]]] = {
    "decisions": [
        [("id", -1)],
        [("created_at", -1), ("id", -1)],
    ],
    "market_scan_observations": [
        [("created_at", -1), ("id", -1)],
        [("timeframe", 1), ("created_at", -1), ("id", -1)],
        [("symbol", 1), ("timeframe", 1), ("created_at", -1), ("id", -1)],
    ],
    "pending_orders": [
        [("status", 1), ("updated_at", -1), ("created_at", -1), ("id", -1)],
        [("exchange_order_id", 1)],
    ],
    "internal_pending_orders": [
        [("status", 1), ("updated_at", -1), ("created_at", -1), ("id", -1)],
        [("exchange_order_id", 1)],
    ],
    "trade_executions": [
        [("status", 1), ("created_at", -1), ("id", -1)],
        [("symbol", 1), ("side", 1), ("status", 1), ("created_at", -1), ("id", -1)],
    ],
    "trade_candidates": [
        [("is_used", 1), ("created_at", -1), ("id", -1)],
        [("is_used", 1), ("rule_score", -1), ("gpt_confidence", -1), ("risk_reward", -1), ("id", 1)],
    ],
}


def atlas_backend_enabled(config: dict[str, Any]) -> bool:
    backend = str(config.get("database", {}).get("backend", "atlas") or "atlas").strip().lower()
    return backend == "atlas"


def atlas_runtime_is_primary(config: dict[str, Any]) -> bool:
    role = str(config.get("runtime", {}).get("instance_role", "primary") or "primary").strip().lower()
    return role == "primary"


def atlas_runtime_is_read_only(config: dict[str, Any]) -> bool:
    return not atlas_runtime_is_primary(config)


def atlas_env_requirements(config: dict[str, Any]) -> tuple[str, str]:
    atlas = config.get("database", {}).get("atlas", {})
    uri_env = str(atlas.get("uri_env", "MONGODB_URI") or "MONGODB_URI")
    database_env = str(atlas.get("database_env", "MONGODB_DATABASE") or "MONGODB_DATABASE")
    return uri_env, database_env


def _atlas_identity(config: dict[str, Any]) -> tuple[str, str]:
    atlas = config.get("database", {}).get("atlas", {})
    uri_env, database_env = atlas_env_requirements(config)
    uri = os.getenv(uri_env, "").strip() or str(atlas.get("uri", "") or "").strip()
    database_name = os.getenv(database_env, "").strip() or str(atlas.get("database", "") or "").strip()
    if not uri:
        raise RuntimeError(f"Missing MongoDB Atlas connection string env: {uri_env}")
    if not database_name:
        raise RuntimeError(f"Missing MongoDB Atlas database name env: {database_env}")
    return uri, database_name


def _test_mode_enabled(config: dict[str, Any]) -> bool:
    return bool(os.getenv("PYTEST_CURRENT_TEST")) or bool(config.get("_atlas_test_mode"))


def _load_pymongo() -> Any:
    try:
        from pymongo import MongoClient
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Atlas backend requires pymongo. Install requirements.txt before running.") from exc
    return MongoClient


def _ensure_atlas_indexes(database: Any, cache_key: tuple[str, str]) -> None:
    if cache_key in _ATLAS_INDEXES_READY:
        return
    with _ATLAS_LOCK:
        if cache_key in _ATLAS_INDEXES_READY:
            return
        for collection_name, index_specs in _ATLAS_COLLECTION_INDEX_SPECS.items():
            collection = database[collection_name]
            for index_fields in index_specs:
                collection.create_index(index_fields)
        _ATLAS_INDEXES_READY.add(cache_key)


def atlas_database(config: dict[str, Any]) -> Any:
    if _test_mode_enabled(config):
        default_name = config.get("_atlas_test_database")
        if not default_name:
            seed = str(config.get("_config_dir") or config.get("_config_path") or id(config))
            default_name = f"crypto_bunny_test_{abs(hash(seed))}"
        database_name = str(
            default_name
            or config.get("database", {}).get("atlas", {}).get("database")
            or "crypto_bunny_test"
        )
        cache_key = ("mongomock", database_name)
        cached = _ATLAS_CLIENTS.get(cache_key)
        if cached is not None:
            return cached
        with _ATLAS_LOCK:
            cached = _ATLAS_CLIENTS.get(cache_key)
            if cached is not None:
                return cached
            try:
                import mongomock
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("Test Atlas fallback requires mongomock to be installed.") from exc
            client = mongomock.MongoClient()
            database = client[database_name]
            _ensure_atlas_indexes(database, cache_key)
            _ATLAS_CLIENTS[cache_key] = database
            return database

    uri, database_name = _atlas_identity(config)
    cache_key = (uri, database_name)
    cached = _ATLAS_CLIENTS.get(cache_key)
    if cached is not None:
        return cached
    with _ATLAS_LOCK:
        cached = _ATLAS_CLIENTS.get(cache_key)
        if cached is not None:
            return cached
        MongoClient = _load_pymongo()
        atlas = config.get("database", {}).get("atlas", {})
        client = MongoClient(
            uri,
            appname=str(atlas.get("app_name", "Crypto_Bunny")),
            serverSelectionTimeoutMS=int(atlas.get("server_selection_timeout_ms", 10000) or 10000),
            connectTimeoutMS=int(atlas.get("connect_timeout_ms", 10000) or 10000),
            socketTimeoutMS=int(atlas.get("socket_timeout_ms", 15000) or 15000),
            waitQueueTimeoutMS=int(atlas.get("wait_queue_timeout_ms", 10000) or 10000),
        )
        client.admin.command("ping")
        database = client[database_name]
        _ensure_atlas_indexes(database, cache_key)
        _ATLAS_CLIENTS[cache_key] = database
        return database
