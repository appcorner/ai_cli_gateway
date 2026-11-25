from __future__ import annotations

import os
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def worker_trace_enabled() -> bool:
    """Return True when verbose worker CLI tracing is enabled via WORKER_TRACE."""
    return os.environ.get("WORKER_TRACE", "false").lower() in ("1", "true", "yes", "on")


def truncate_single_line(text: Any, limit: int) -> str:
    """Collapse whitespace to single spaces and truncate to `limit` characters."""
    normalized = " ".join(str(text).split())
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "...[truncated]"


def append_trace_event(
    trace_events: list[dict[str, Any]] | None,
    stage: str,
    **fields: Any,
) -> None:
    """Record a timestamped diagnostic event when a trace buffer is attached."""
    if trace_events is None:
        return
    event: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
    }
    for key, value in fields.items():
        if value is not None:
            event[key] = value
    trace_events.append(event)


def trace_cli_streams(
    job_id: str | None,
    provider_name: str,
    stdout_text: str,
    stderr_text: str,
) -> None:
    """Print bounded CLI stdout/stderr lines when WORKER_TRACE is enabled."""
    if not worker_trace_enabled():
        return
    job_label = job_id or "-"
    for idx, line in enumerate(stdout_text.splitlines(), 1):
        if idx <= 100:
            print(
                f"trace job={job_label} provider={provider_name} stage=stdout_line idx={idx} "
                f"raw={truncate_single_line(line, 2000)}",
                flush=True,
            )
        elif idx == 101:
            print(
                f"trace job={job_label} provider={provider_name} stage=stdout_truncated "
                "note='more than 100 stdout lines'",
                flush=True,
            )
            break
    if stderr_text.strip():
        print(
            f"trace job={job_label} provider={provider_name} stage=stderr "
            f"text={truncate_single_line(stderr_text[-4000:], 4000)}",
            flush=True,
        )

# Gateway-internal and cross-provider credential keys that must never leak into a CLI subprocess.
COMMON_BLOCKED_ENV = frozenset({
    "GATEWAY_API_KEY",
    "DATABASE_URL",
    "POSTGRES_PASSWORD",
    "REDIS_URL",
})

# n8n integration credentials/URLs are stripped in chat mode and available only in agent mode.
CHAT_ONLY_BLOCKED_ENV = frozenset({
    "N8N_MCP_URL",
    "N8N_MCP_BEARER_TOKEN",
    "N8N_MCP_AUTH_HEADER",
    "N8N_WEBHOOK_BASE_URL",
    "N8N_WEBHOOK_TOKEN",
    "N8N_WEBHOOK_AUTH_HEADER",
    "N8N_WEBHOOK_TOOLS_FILE",
    "N8N_WEBHOOK_TOOLS_JSON",
})


def resolve_workspace_dir() -> str | None:
    """Return configured shared workspace path, ensuring it exists if set."""
    raw = os.environ.get("CLI_WORKSPACE_DIR", "").strip()
    if not raw:
        return None
    os.makedirs(raw, exist_ok=True)
    return raw


@contextmanager
def workspace_context(
    prefix: str,
    temp_dir_factory: Any = tempfile.TemporaryDirectory,
) -> Iterator[str]:
    """Yield shared CLI_WORKSPACE_DIR when configured, or a temporary directory."""
    shared = resolve_workspace_dir()
    if shared:
        yield shared
        return
    with temp_dir_factory(prefix=prefix) as tmpdir:
        yield tmpdir


def agent_network_enabled() -> bool:
    """Return True when CLI agent mode is allowed to reach Docker/network endpoints."""
    return os.environ.get("AGENT_ALLOW_NETWORK", "true").lower() in ("1", "true", "yes", "on")


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
    session_list_modes: tuple[str, ...] = ()

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
            "session_list_modes": list(self.session_list_modes),
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
    job_id: str | None = None
    trace_events: list[dict[str, Any]] | None = None


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
        mode: str | None = None,
    ) -> dict[str, str]:
        blocked = COMMON_BLOCKED_ENV | extra_blocked
        if mode == "chat":
            blocked = blocked | CHAT_ONLY_BLOCKED_ENV
        env = {k: v for k, v in os.environ.items() if k not in blocked}
        if mode:
            env["GATEWAY_CLI_MODE"] = mode
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
