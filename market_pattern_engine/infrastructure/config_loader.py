from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from market_pattern_engine.domain.exceptions import ConfigurationError


CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigurationError(f"Missing config file: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_engine_config(path: str | None = None) -> dict[str, Any]:
    config = _load_yaml(CONFIG_DIR / "default.yaml")
    config = _deep_merge(config, _load_yaml(CONFIG_DIR / "pattern_thresholds.yaml"))
    config = _deep_merge(config, _load_yaml(CONFIG_DIR / "timeframe_profiles.yaml"))
    config = _deep_merge(config, _load_yaml(CONFIG_DIR / "symbol_overrides.yaml"))
    override_path = path or os.getenv("MARKET_PATTERN_CONFIG")
    if override_path:
        config = _deep_merge(config, _load_yaml(Path(override_path)))
    env_audit_days = os.getenv("MARKET_PATTERN_AUDIT_LOG_DAYS")
    if env_audit_days:
        config.setdefault("retention", {})["audit_log_days"] = int(env_audit_days)
    return config
