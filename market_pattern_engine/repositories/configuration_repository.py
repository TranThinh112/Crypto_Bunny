from __future__ import annotations

from typing import Any


class ConfigurationRepository:
    def __init__(self, db: Any) -> None:
        self.db = db

    def save_config(self, config_version: str, payload: dict[str, Any]) -> None:
        self.db["analysis_configurations"].update_one(
            {"config_version": config_version},
            {"$set": {"config_version": config_version, "payload": payload}},
            upsert=True,
        )
