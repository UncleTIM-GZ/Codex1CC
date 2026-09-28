"""Extract only provider authentication and model settings for restricted CLI runs."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .common import BridgeError
from .store import register_secret

ALLOWED = {
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME", "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
}


def provider_environment() -> dict[str, str]:
    result = {key: value for key, value in os.environ.items() if key in ALLOWED}
    directory = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    settings = directory / "settings.json"
    if settings.exists():
        if (settings.is_symlink() or not settings.is_file() or
                settings.stat().st_uid != os.getuid() or settings.stat().st_size > 2 * 1024 * 1024):
            raise BridgeError("INVALID_CONFIG", "Claude settings file is invalid")
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BridgeError("INVALID_CONFIG", "Claude settings file cannot be read") from exc
        values = data.get("env", {})
        if isinstance(values, dict):
            for key, value in values.items():
                if key in ALLOWED and isinstance(value, str):
                    result.setdefault(key, value)
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if result.get(key):
            register_secret(result[key])
    return result
