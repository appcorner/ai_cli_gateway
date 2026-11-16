from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

# Gateway-internal and cross-provider credential keys that must never leak into a CLI subprocess.
COMMON_BLOCKED_ENV = frozenset({
    "GATEWAY_API_KEY",
    "DATABASE_URL",
    "POSTGRES_PASSWORD",
    "REDIS_URL",
})


@dataclass(frozen=True)
class ProviderCapabilities:
    """Declares what features a CLI provider supports so API/UI can adapt dynamically."""

    modes: tuple[str, ...] = ("chat",)
    default_mode: str = "chat"
    supports_system_prompt: bool = True
    supports_sessions: bool = False
    supports_session_history: bool = False
    supports_session_delete: bool = False
    supports_usage_quota: bool = False
    supports_model_discovery: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "modes": list(self.modes),
            "default_mode": self.default_mode,
            "supports_system_prompt": self.supports_system_prompt,
            "supports_sessions": self.supports_sessions,
            "supports_session_history": self.supports_session_history,
            "supports_session_delete": self.supports_session_delete,
            "supports_usage_quota": self.supports_usage_quota,
            "supports_model_discovery": self.supports_model_discovery,
        }


@dataclass
class ExecutionRequest:
    prompt: str
    model: str | None = None
    mode: str = "chat"
    session_id: str | None = None
    store_session: bool = False
    system_prompt: str | None = None
    timeout_seconds: int = 120


@dataclass
class ExecutionResult:
    text: str
    session_id: str | None = None
    usage: dict[str, Any] | None = None


class BaseCLIProvider(ABC):
    """Unified contract for all AI CLI adapters in the gateway worker."""

    name: str
    display_name: str
    capabilities: ProviderCapabilities

    def __init__(self, redis_client: Any, timeout_seconds: int = 120):
        self.redis = redis_client
        self.timeout = timeout_seconds

    @staticmethod
    def sanitize_env(
        extra_blocked: frozenset[str] = frozenset(),
        overrides: dict[str, str] | None = None,
    ) -> dict[str, str]:
        blocked = COMMON_BLOCKED_ENV | extra_blocked
        env = {k: v for k, v in os.environ.items() if k not in blocked}
        if overrides:
            env.update(overrides)
        return env

    def startup_init(self) -> None:
        """Optional one-time initialization when the worker starts."""

    @abstractmethod
    def execute_turn(self, req: ExecutionRequest) -> ExecutionResult:
        """Execute a single prompt turn and return normalized output."""

    @abstractmethod
    def list_models(self) -> list[dict[str, Any]]:
        """Return [{"id": str, "name": str, "is_default": bool}, ...]."""

    def session_control(
        self,
        operation: str,
        session_id: str | None = None,
        mode: str = "agent",
    ) -> Any:
        """Handle control operations: list, history, delete, usage."""
        raise ValueError(f"Unsupported operation '{operation}' for provider '{self.name}'")
