from __future__ import annotations

import os
from typing import Any

from .agy import AgyProvider
from .base import BaseCLIProvider
from .codex import CodexProvider

PROVIDER_CLASSES: dict[str, type[BaseCLIProvider]] = {
    "codex": CodexProvider,
    "agy": AgyProvider,
}


def parse_enabled_providers(raw: str | None = None) -> list[str]:
    value = raw if raw is not None else os.environ.get("ENABLED_PROVIDERS", "")
    if not value.strip():
        return list(PROVIDER_CLASSES.keys())
    names = [part.strip().lower() for part in value.split(",") if part.strip()]
    unknown = [name for name in names if name not in PROVIDER_CLASSES]
    if unknown:
        raise ValueError(f"Unknown providers in ENABLED_PROVIDERS: {', '.join(unknown)}")
    return list(dict.fromkeys(names))


def build_registry(
    redis_client: Any,
    timeout_seconds: int = 120,
    enabled_names: list[str] | None = None,
) -> dict[str, BaseCLIProvider]:
    names = enabled_names if enabled_names is not None else parse_enabled_providers()
    return {
        name: PROVIDER_CLASSES[name](redis_client=redis_client, timeout_seconds=timeout_seconds)
        for name in names
    }
