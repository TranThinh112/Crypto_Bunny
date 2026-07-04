from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .config import load_config, project_path


REQUIRED_OKX_ENV = ("OKX_API_KEY", "OKX_SECRET", "OKX_PASSPHRASE")
REQUIRED_TELEGRAM_ENV = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def _present(name: str) -> bool:
    return bool(os.getenv(name, "").strip())


def _parent_status(path: Path) -> tuple[bool, str]:
    parent = path.parent
    if parent.exists():
        return True, f"{parent} exists"
    return False, f"{parent} does not exist yet"


def check_deploy(config_path: str, require_secrets: bool = True) -> tuple[bool, list[str], list[str]]:
    load_dotenv()
    config = load_config(config_path)
    errors: list[str] = []
    warnings: list[str] = []

    if require_secrets:
        for name in REQUIRED_OKX_ENV:
            if not _present(name):
                errors.append(f"Missing required OKX variable: {name}")

        telegram_config = config.get("notifications", {}).get("telegram", {})
        if telegram_config.get("enabled", True):
            for name in REQUIRED_TELEGRAM_ENV:
                if not _present(name):
                    errors.append(f"Missing required Telegram variable: {name}")

    if config.get("mode") == "live" and not config.get("execution", {}).get("enable_live", False):
        errors.append("mode is live but execution.enable_live is false")

    ai_config = config.get("ai", {})
    if ai_config.get("enabled", True):
        for role in ("internal", "okx"):
            role_config = ai_config.get(role, {}) if isinstance(ai_config.get(role), dict) else {}
            if role_config.get("provider") != "openai":
                continue
            key_env = str(role_config.get("api_key_env", ai_config.get("api_key_env", "OPENAI_API_KEY")))
            if _present(key_env):
                continue
            message = f"Missing OpenAI variable for {role} AI: {key_env}"
            if role == "okx" and role_config.get("require_external_approval", False):
                errors.append(message)
            else:
                warnings.append(message + " (local policy fallback will be used)")

    for key in ("report_path", "ledger_path", "state_db_path"):
        value = config.get(key)
        if not value:
            warnings.append(f"Config path {key} is not set")
            continue
        ok, message = _parent_status(project_path(config, str(value)))
        if not ok:
            warnings.append(f"{key}: {message}. On Railway, mount a volume at /data.")

    if "PORT" not in os.environ:
        warnings.append("PORT is not set. This is normal locally; Railway injects PORT during deploy.")

    return not errors, errors, warnings


def format_deploy_check(ok: bool, errors: list[str], warnings: list[str]) -> str:
    lines = ["Deploy check: OK" if ok else "Deploy check: needs attention"]
    if errors:
        lines.append("Errors:")
        lines.extend(f"- {item}" for item in errors)
    if warnings:
        lines.append("Warnings:")
        lines.extend(f"- {item}" for item in warnings)
    return "\n".join(lines)
