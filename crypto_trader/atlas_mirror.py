from __future__ import annotations

import os
import threading
from typing import Any


_ATLAS_LOCK = threading.Lock()
_ATLAS_CLIENTS: dict[tuple[str, str], Any] = {}


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
        )
        client.admin.command("ping")
        database = client[database_name]
        _ATLAS_CLIENTS[cache_key] = database
        return database
