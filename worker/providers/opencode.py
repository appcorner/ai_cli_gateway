from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from typing import Any

from .base import BaseCLIProvider, ExecutionRequest, ExecutionResult, ProviderCapabilities
from .session_store import GatewaySessionStore

OPENCODE_SESSION_MESSAGE_LIMIT = 80
OPENCODE_BLOCKED_ENV = frozenset({
    "CODEX_HOME",
    "AGY_ADC_AUTH",
})


def opencode_environment() -> dict[str, str]:
    return BaseCLIProvider.sanitize_env(extra_blocked=OPENCODE_BLOCKED_ENV)


def opencode_data_home() -> str:
    xdg_data = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.environ.get("HOME", "/home/node"), ".local", "share"
    )
    return os.path.join(xdg_data, "opencode")


def opencode_session_index_path() -> str:
    return os.path.join(opencode_data_home(), "gateway_sessions.json")


_SESSION_STORE = GatewaySessionStore(
    index_path_fn=opencode_session_index_path,
    message_limit=OPENCODE_SESSION_MESSAGE_LIMIT,
)


def read_models_from_opencode(timeout: int = 60) -> list[dict[str, Any]]:
    process = subprocess.run(
        ["opencode", "models"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=min(timeout, 60),
        check=False,
        env=opencode_environment(),
    )
    if process.returncode != 0:
        raise RuntimeError("opencode model discovery failed")
    models = []
    seen = set()
    for line in process.stdout.splitlines():
        token = line.strip().split(None, 1)[0] if line.strip() else ""
        if (
            token
            and re.fullmatch(r"[a-zA-Z0-9._-]+/[a-zA-Z0-9._:-]+", token)
            and token not in seen
        ):
            seen.add(token)
            models.append({"id": token, "name": token, "is_default": False})
    if not models:
        raise RuntimeError("opencode returned an empty model catalog")
    return models


def _extract_opencode_output(stdout: str) -> tuple[str, str | None, dict[str, Any], str | None]:
    texts: list[str] = []
    session_id: str | None = None
    usage: dict[str, Any] = {}
    error_type: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not session_id and isinstance(event.get("sessionID"), str):
            session_id = event["sessionID"]
        part = event.get("part") or {}
        event_type = event.get("type")
        if event_type == "text":
            chunk = part.get("text")
            if isinstance(chunk, str) and chunk:
                texts.append(chunk)
        elif event_type == "error":
            err = event.get("error")
            if isinstance(err, dict):
                name = str(err.get("name") or "error")
                data_msg = (err.get("data") or {}).get("message") if isinstance(err.get("data"), dict) else None
                msg = data_msg or err.get("message") or ""
                error_type = f"{name}: {msg}"[:180] if msg else name[:120]
            elif err:
                error_type = str(err)[:120]
        elif event_type == "step_finish":
            tokens = part.get("tokens") or {}
            cache = tokens.get("cache") or {}
            if tokens:
                usage = {
                    "input_tokens": tokens.get("input"),
                    "output_tokens": tokens.get("output"),
                    "thinking_tokens": tokens.get("reasoning"),
                    "cache_read_tokens": cache.get("read"),
                }
    return "".join(texts).strip(), session_id, usage, error_type


def run_opencode_turn(
    prompt: str,
    model: str | None,
    session_id: str | None = None,
    system_prompt: str | None = None,
    persist: bool = False,
    timeout: int = 120,
) -> tuple[str, str | None, dict[str, Any]]:
    formatted_prompt = prompt
    if system_prompt:
        formatted_prompt = f"System instructions:\n{system_prompt}\n\nUser request:\n{prompt}"

    with tempfile.TemporaryDirectory(prefix="opencode-job-") as workdir:
        command = ["opencode", "run", "--format", "json"]
        if model:
            command.extend(["--model", model])
        if session_id:
            command.extend(["--session", session_id])
        command.extend(["--", formatted_prompt])

        process = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout + 30,
            check=False,
            env=opencode_environment(),
        )

    answer, detected_session_id, usage, error_type = _extract_opencode_output(process.stdout or "")
    if process.returncode != 0 or not answer:
        print(
            f"opencode run failed: rc={process.returncode} has_answer={bool(answer)} error={error_type or 'none'}",
            flush=True,
        )
        raise RuntimeError("opencode did not complete successfully")
    returned_session_id = (detected_session_id or session_id) if persist else None
    return answer, returned_session_id, usage


def delete_opencode_session(session_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{4,128}", session_id):
        raise ValueError("Invalid OpenCode session ID")
    process = subprocess.run(
        ["opencode", "session", "delete", session_id],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=opencode_environment(),
    )
    has_index_entry = _SESSION_STORE.has_session(session_id)
    if process.returncode != 0 and not has_index_entry:
        raise RuntimeError("opencode session delete failed")
    _SESSION_STORE.remove_session(session_id)
    return {"id": session_id, "deleted": True}


class OpenCodeProvider(BaseCLIProvider):
    name = "opencode"
    display_name = "OpenCode CLI"
    capabilities = ProviderCapabilities(
        modes=("chat",),
        default_mode="chat",
        supports_system_prompt=True,
        supports_sessions=True,
        supports_session_history=True,
        supports_session_delete=True,
        supports_usage_quota=False,
        supports_model_discovery=True,
    )

    def execute_turn(self, req: ExecutionRequest) -> ExecutionResult:
        timeout = req.timeout_seconds or self.timeout
        persist = req.store_session or bool(req.session_id)
        answer, returned_session_id, usage = run_opencode_turn(
            req.prompt,
            req.model,
            req.session_id,
            req.system_prompt,
            persist=persist,
            timeout=timeout,
        )
        if returned_session_id and persist:
            _SESSION_STORE.record_turn(
                "opencode",
                returned_session_id,
                req.prompt,
                answer,
                req.model,
                req.mode,
            )
        return ExecutionResult(text=answer, session_id=returned_session_id, usage=usage)

    def list_models(self) -> list[dict[str, Any]]:
        return read_models_from_opencode(timeout=self.timeout)

    def session_control(
        self,
        operation: str,
        session_id: str | None = None,
        mode: str = "agent",
    ) -> Any:
        if operation == "list":
            return _SESSION_STORE.list_sessions()
        if operation == "history" and session_id:
            return _SESSION_STORE.get_history("opencode", session_id)
        if operation == "delete" and session_id:
            return delete_opencode_session(session_id)
        raise ValueError(f"Unsupported OpenCode session operation: {operation}")
