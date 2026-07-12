from __future__ import annotations

import logging
import os
import threading
from typing import Any


_ATLAS_LOCK = threading.RLock()
_ATLAS_CLIENTS: dict[tuple[str, str], Any] = {}
_ATLAS_INDEXES_READY: set[tuple[str, str, str]] = set()
LOGGER = logging.getLogger(__name__)

AI_DATABASE_COLLECTIONS = {
    "decisions",
    "ai_trade_decisions",
    "prompt_metrics",
    "prompt_versions",
    "ai_model_versions",
    "ai_experiments",
    "replay_history",
}

_ATLAS_COLLECTION_INDEX_SPECS: dict[str, list[Any]] = {
    "decisions": [
        [("id", -1)],
        [("created_at", -1), ("id", -1)],
    ],
    "prompt_metrics": [
        [("prompt_version", 1)],
        [("updated_at", -1)],
    ],
    "prompt_versions": [
        [("version", 1)],
        [("created_at", -1), ("id", -1)],
    ],
    "ai_model_versions": [
        [("model_name", 1)],
        [("updated_at", -1)],
    ],
    "ai_experiments": [
        [("name", 1)],
        [("enabled", 1), ("created_at", -1), ("id", -1)],
    ],
    "market_scan_observations": [
        [("created_at", -1), ("id", -1)],
        [("timeframe", 1), ("created_at", -1), ("id", -1)],
        [("symbol", 1), ("timeframe", 1), ("created_at", -1), ("id", -1)],
        {"fields": [("expires_at", 1)], "kwargs": {"expireAfterSeconds": 0}},
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
        {"fields": [("expires_at", 1)], "kwargs": {"expireAfterSeconds": 0}},
    ],
    "ai_trade_decisions": [
        [("created_at", -1), ("id", -1)],
        [("decision", 1), ("trade_status", 1), ("created_at", -1), ("id", -1)],
        {"fields": [("expires_at", 1)], "kwargs": {"expireAfterSeconds": 0}},
    ],
    "market_guard_observations": [
        [("observed_at", -1), ("id", -1)],
        {"fields": [("expires_at", 1)], "kwargs": {"expireAfterSeconds": 0}},
    ],
    "market_regime_history": [
        [("created_at", -1), ("id", -1)],
        {"fields": [("expires_at", 1)], "kwargs": {"expireAfterSeconds": 0}},
    ],
    "replay_history": [
        [("replay_at", -1), ("id", -1)],
        {"fields": [("expires_at", 1)], "kwargs": {"expireAfterSeconds": 0}},
    ],
    "paper_trades": [
        [("status", 1), ("updated_at", -1), ("created_at", -1), ("id", -1)],
        {"fields": [("expires_at", 1)], "kwargs": {"expireAfterSeconds": 0}},
    ],
    "capital_snapshots": [
        [("created_at", -1), ("id", -1)],
    ],
    "capital_reserve_states": [
        [("created_at", -1), ("id", -1)],
        [("mode", 1), ("created_at", -1)],
    ],
    "position_size_calculations": [
        [("created_at", -1), ("id", -1)],
        [("symbol", 1), ("created_at", -1)],
    ],
    "trading_config_versions": [
        [("is_active", 1), ("created_at", -1), ("id", -1)],
        [("version", 1)],
    ],
    "configuration_impact_reports": [
        [("created_at", -1), ("id", -1)],
        [("risk_level", 1), ("created_at", -1)],
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


def atlas_ai_database_env(config: dict[str, Any]) -> str:
    atlas = config.get("database", {}).get("atlas", {})
    return str(atlas.get("ai_database_env", "MONGODB_AI_DATABASE") or "MONGODB_AI_DATABASE")


def _atlas_identity(config: dict[str, Any], *, database_name: str | None = None) -> tuple[str, str]:
    atlas = config.get("database", {}).get("atlas", {})
    uri_env, database_env = atlas_env_requirements(config)
    uri = os.getenv(uri_env, "").strip() or str(atlas.get("uri", "") or "").strip()
    resolved_database_name = (
        str(database_name or "").strip()
        or os.getenv(database_env, "").strip()
        or str(atlas.get("database", "") or "").strip()
    )
    if not uri:
        raise RuntimeError(f"Missing MongoDB Atlas connection string env: {uri_env}")
    if not resolved_database_name:
        raise RuntimeError(f"Missing MongoDB Atlas database name env: {database_env}")
    return uri, resolved_database_name


def atlas_database_name(config: dict[str, Any], *, role: str = "runtime") -> str:
    atlas = config.get("database", {}).get("atlas", {})
    if _test_mode_enabled(config):
        base = config.get("_atlas_test_database")
        if not base:
            seed = str(config.get("_config_dir") or config.get("_config_path") or id(config))
            base = f"crypto_bunny_test_{abs(hash(seed))}"
        if role == "ai":
            return str(config.get("_atlas_test_ai_database") or f"{base}_ai")
        return str(base)
    if role == "ai":
        env_name = atlas_ai_database_env(config)
        return (
            str(atlas.get("ai_database", "") or "").strip()
            or os.getenv(env_name, "").strip()
            or str(atlas.get("database", "") or "").strip()
        )
    _, database_env = atlas_env_requirements(config)
    return str(atlas.get("database", "") or "").strip() or os.getenv(database_env, "").strip()


def atlas_collection_database_name(config: dict[str, Any], collection_name: str) -> str:
    role = "ai" if str(collection_name or "") in AI_DATABASE_COLLECTIONS else "runtime"
    return atlas_database_name(config, role=role)


def _atlas_index_collections(config: dict[str, Any], database_name: str) -> set[str]:
    runtime_database = atlas_database_name(config, role="runtime")
    ai_database = atlas_database_name(config, role="ai")
    all_collections = set(_ATLAS_COLLECTION_INDEX_SPECS)
    if runtime_database == ai_database:
        return all_collections
    if database_name == ai_database:
        return set(AI_DATABASE_COLLECTIONS)
    return all_collections - set(AI_DATABASE_COLLECTIONS)


def _test_mode_enabled(config: dict[str, Any]) -> bool:
    return bool(os.getenv("PYTEST_CURRENT_TEST")) or bool(config.get("_atlas_test_mode"))


def _load_pymongo() -> Any:
    try:
        from pymongo import MongoClient
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Atlas backend requires pymongo. Install requirements.txt before running.") from exc
    return MongoClient


def _atlas_write_blocked(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    message = str(exc).lower()
    return code == 8000 or "space quota" in message or "writes are blocked" in message


def _ensure_atlas_indexes(database: Any, cache_key: tuple[str, str], collection_names: set[str]) -> None:
    pending_collections = {
        collection_name
        for collection_name in collection_names
        if (cache_key[0], cache_key[1], collection_name) not in _ATLAS_INDEXES_READY
    }
    if not pending_collections:
        return
    with _ATLAS_LOCK:
        pending_collections = {
            collection_name
            for collection_name in pending_collections
            if (cache_key[0], cache_key[1], collection_name) not in _ATLAS_INDEXES_READY
        }
        if not pending_collections:
            return
        try:
            for collection_name in sorted(pending_collections):
                index_specs = _ATLAS_COLLECTION_INDEX_SPECS.get(collection_name, [])
                collection = database[collection_name]
                for index_spec in index_specs:
                    if isinstance(index_spec, dict):
                        index_fields = index_spec.get("fields") or []
                        index_kwargs = dict(index_spec.get("kwargs") or {})
                    else:
                        index_fields = index_spec
                        index_kwargs = {}
                    collection.create_index(index_fields, **index_kwargs)
        except Exception as exc:
            if not _atlas_write_blocked(exc):
                raise
            LOGGER.warning(
                "Skipping Atlas index ensure because cluster writes are blocked: %s",
                exc,
            )
        for collection_name in pending_collections:
            _ATLAS_INDEXES_READY.add((cache_key[0], cache_key[1], collection_name))


def atlas_database(config: dict[str, Any], database_name: str | None = None) -> Any:
    if _test_mode_enabled(config):
        default_name = database_name or config.get("_atlas_test_database")
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
            _ensure_atlas_indexes(database, cache_key, _atlas_index_collections(config, database_name))
            _ATLAS_CLIENTS[cache_key] = database
            return database

    database_name = database_name or atlas_database_name(config, role="runtime")
    uri, database_name = _atlas_identity(config, database_name=database_name)
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
        _ensure_atlas_indexes(database, cache_key, _atlas_index_collections(config, database_name))
        _ATLAS_CLIENTS[cache_key] = database
        return database


def atlas_database_for_collection(config: dict[str, Any], collection_name: str) -> Any:
    return atlas_database(config, atlas_collection_database_name(config, collection_name))
