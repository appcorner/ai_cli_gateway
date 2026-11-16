from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Any

from .base import BaseCLIProvider, ExecutionRequest, ExecutionResult, ProviderCapabilities
from .session_store import GatewaySessionStore

AGY_SESSION_MESSAGE_LIMIT = 80
AGY_NON_OAUTH_ENV = frozenset({
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "AGY_ADC_AUTH",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_CLOUD_QUOTA_PROJECT",
    "CODEX_HOME",
})


def agy_environment() -> dict[str, str]:
    return BaseCLIProvider.sanitize_env(extra_blocked=AGY_NON_OAUTH_ENV)


def agy_cli_home() -> str:
    return os.path.join(os.environ.get("HOME", "/home/node"), ".gemini", "antigravity-cli")


def agy_session_index_path() -> str:
    return os.path.join(agy_cli_home(), "gateway_sessions.json")


_SESSION_STORE = GatewaySessionStore(
    index_path_fn=agy_session_index_path,
    message_limit=AGY_SESSION_MESSAGE_LIMIT,
)


def read_models_from_agy(timeout: int = 60) -> list[dict[str, Any]]:
    process = subprocess.run(
        ["agy", "models"],
        capture_output=True,
        text=True,
        timeout=min(timeout, 60),
        check=False,
        env=agy_environment(),
    )
    if process.returncode != 0:
        raise RuntimeError("agy model discovery failed")
    models = []
    seen = set()
    for line in process.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if not parts:
            continue
        model_id = parts[0]
        if (
            re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", model_id)
            and model_id.lower() not in ("model", "models")
            and model_id not in seen
        ):
            seen.add(model_id)
            models.append({
                "id": model_id,
                "name": parts[1].strip() if len(parts) > 1 else model_id,
                "is_default": False,
            })
    if not models:
        raise RuntimeError("agy returned an empty model catalog")
    return models


def run_agy_turn(
    prompt: str,
    model: str | None,
    session_id: str | None = None,
    system_prompt: str | None = None,
    persist: bool = False,
    mode: str = "chat",
    timeout: int = 120,
) -> tuple[str, str | None, dict[str, Any]]:
    formatted_prompt = f"User request:\n{prompt}"
    if system_prompt:
        formatted_prompt = f"System instructions:\n{system_prompt}\n\n{formatted_prompt}"
    with tempfile.TemporaryDirectory(prefix="agy-job-") as workdir:
        command = [
            "agy", "--input-format", "stream-json", "--output-format", "stream-json",
            "--print-timeout", f"{timeout}s", "--sandbox",
        ]
        if mode == "chat":
            command.append("--disable-slash-commands")
        if model:
            command.extend(["--model", model])
        if session_id:
            command.extend(["--conversation", session_id])
        input_event = json.dumps({"event": "user", "message": {"content": formatted_prompt}}) + "\n"
        process = subprocess.run(
            command,
            input=input_event,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout + 30,
            check=False,
            env=agy_environment(),
        )
    envelope = None
    for line in process.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "result":
            envelope = event.get("result") or {}
    if envelope is None:
        raise RuntimeError("agy did not return a result event")
    if process.returncode != 0 or envelope.get("status") != "SUCCESS":
        raise RuntimeError("agy did not complete successfully")
    answer = (envelope.get("response") or "").strip()
    conversation_id = envelope.get("conversation_id") if persist else None
    return answer, conversation_id, envelope.get("usage") or {}


def delete_agy_session(session_id: str) -> dict[str, Any]:
    # Antigravity does not expose a headless delete command. Remove this exact
    # conversation from its per-session SQLite store and picker index.
    if not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        session_id,
    ):
        raise ValueError("Invalid Antigravity conversation ID")
    root = agy_cli_home()
    session_db = os.path.join(root, "conversations", session_id + ".db")
    summary_db = os.path.join(root, "conversation_summaries.db")
    has_index_entry = _SESSION_STORE.has_session(session_id)
    if not os.path.isfile(session_db) and not has_index_entry:
        raise FileNotFoundError("Antigravity conversation was not found")
    if os.path.isfile(summary_db):
        with sqlite3.connect(summary_db, timeout=10) as connection:
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            for (table_name,) in tables:
                columns = connection.execute(
                    f'PRAGMA table_info("{table_name.replace(chr(34), chr(34) * 2)}")'
                ).fetchall()
                id_columns = [
                    column[1]
                    for column in columns
                    if column[1].lower() in ("conversation_id", "conversationid", "id")
                ]
                for column in id_columns:
                    safe_table = table_name.replace('"', '""')
                    safe_column = column.replace('"', '""')
                    connection.execute(f'DELETE FROM "{safe_table}" WHERE "{safe_column}" = ?', (session_id,))
    for suffix in ("", "-wal", "-shm"):
        path = session_db + suffix
        if os.path.isfile(path):
            os.remove(path)
    for folder in ("brain", "annotations", "presence"):
        path = os.path.join(root, folder, session_id)
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.isfile(path):
            os.remove(path)
        for extension in (".pb", ".pbtxt", ".json"):
            metadata_path = os.path.join(root, folder, session_id + extension)
            if os.path.isfile(metadata_path):
                os.remove(metadata_path)
    cache_path = os.path.join(root, "cache", "last_conversations.json")
    try:
        with open(cache_path, encoding="utf-8") as cache_file:
            cache = json.load(cache_file)
        if isinstance(cache, dict):
            cache = {
                workspace: conversation
                for workspace, conversation in cache.items()
                if conversation != session_id
            }
            temporary_path = cache_path + ".tmp"
            with open(temporary_path, "w", encoding="utf-8") as cache_file:
                json.dump(cache, cache_file)
            os.replace(temporary_path, cache_path)
    except (OSError, json.JSONDecodeError):
        pass
    _SESSION_STORE.remove_session(session_id)
    return {"id": session_id, "deleted": True}


def agy_session_control(operation: str, session_id: str | None = None) -> Any:
    if operation == "list":
        return _SESSION_STORE.list_sessions()
    if operation == "usage":
        # /usage is handled locally by the CLI, so this does not submit a model prompt.
        with tempfile.TemporaryDirectory(prefix="agy-usage-") as workdir:
            process = subprocess.run(
                ["agy", "-p", "/usage", "--output-format", "json"],
                capture_output=True,
                text=True,
                cwd=workdir,
                timeout=35,
                check=False,
                env=agy_environment(),
            )
        if process.returncode != 0 or not (process.stdout or "").strip():
            raise RuntimeError("agy usage command failed")
        result = json.loads(process.stdout)
        data = (result.get("command") or {}).get("data") or {}
        groups = []
        for group in data.get("groups") or []:
            buckets = []
            for bucket in group.get("buckets") or []:
                remaining = bucket.get("remaining_fraction")
                try:
                    remaining = min(1.0, max(0.0, float(remaining)))
                except (TypeError, ValueError):
                    remaining = None
                buckets.append({
                    "id": bucket.get("id"),
                    "name": bucket.get("name") or bucket.get("window") or "Quota",
                    "window": bucket.get("window"),
                    "remaining_fraction": remaining,
                    "reset_time": bucket.get("reset_time"),
                })
            if buckets:
                groups.append({
                    "name": group.get("name") or "Model group",
                    "description": group.get("description"),
                    "buckets": buckets,
                })
        if not groups:
            raise RuntimeError("agy usage response did not include model quota groups")
        return {"groups": groups, "fetched_at": datetime.now(timezone.utc).isoformat()}
    if operation == "history" and session_id:
        return _SESSION_STORE.get_history("agy", session_id)
    if operation == "delete" and session_id:
        return delete_agy_session(session_id)
    raise ValueError("Unsupported Antigravity session operation")


class AgyProvider(BaseCLIProvider):
    name = "agy"
    display_name = "Antigravity CLI (agy)"
    capabilities = ProviderCapabilities(
        modes=("chat", "agent"),
        default_mode="chat",
        supports_system_prompt=True,
        supports_sessions=True,
        supports_session_history=True,
        supports_session_delete=True,
        supports_usage_quota=True,
        supports_model_discovery=True,
    )

    def startup_init(self) -> None:
        settings_dir = agy_cli_home()
        os.makedirs(settings_dir, mode=0o700, exist_ok=True)
        settings_path = os.path.join(settings_dir, "settings.json")
        settings: dict[str, Any] = {}
        if os.path.isfile(settings_path):
            try:
                with open(settings_path, encoding="utf-8") as settings_file:
                    settings = json.load(settings_file)
            except (OSError, json.JSONDecodeError):
                pass
        settings["toolPermission"] = "request-review"
        # The Gateway only supports Antigravity's Google account OAuth sign-in.
        # Remove a stale Gemini API-key provider setting from older deployments.
        settings.pop("modelProvider", None)
        temporary_settings_path = settings_path + ".tmp"
        with open(temporary_settings_path, "w", encoding="utf-8") as settings_file:
            json.dump(settings, settings_file)
        os.replace(temporary_settings_path, settings_path)
        os.chmod(settings_path, 0o600)

    def execute_turn(self, req: ExecutionRequest) -> ExecutionResult:
        timeout = req.timeout_seconds or self.timeout
        persist = req.store_session or bool(req.session_id)
        answer, conversation_id, usage = run_agy_turn(
            req.prompt,
            req.model,
            req.session_id,
            req.system_prompt,
            persist=persist,
            mode=req.mode,
            timeout=timeout,
        )
        if conversation_id and persist:
            _SESSION_STORE.record_turn(
                "agy", conversation_id, req.prompt, answer, req.model, req.mode
            )
        if not answer:
            raise RuntimeError("agy returned no answer")
        return ExecutionResult(text=answer, session_id=conversation_id, usage=usage)

    def list_models(self) -> list[dict[str, Any]]:
        return read_models_from_agy(timeout=self.timeout)

    def session_control(
        self,
        operation: str,
        session_id: str | None = None,
        mode: str = "agent",
    ) -> Any:
        return agy_session_control(operation, session_id=session_id)
