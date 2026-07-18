from __future__ import annotations

import os
from typing import Any

from pymongo import MongoClient


def mongo_client(uri: str | None = None) -> Any:
    resolved = uri or os.getenv("MONGODB_URI", "")
    if not resolved:
        raise RuntimeError("Missing MONGODB_URI")
    return MongoClient(resolved, serverSelectionTimeoutMS=10000, connectTimeoutMS=10000, socketTimeoutMS=15000)


def mongo_database(database: str | None = None, uri: str | None = None) -> Any:
    client = mongo_client(uri)
    name = database or os.getenv("MONGODB_DATABASE", "Bunny_Runtime_Live")
    client.admin.command("ping")
    return client[name]
