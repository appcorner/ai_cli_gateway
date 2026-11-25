from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from typing import Any

from .base import (
    BaseCLIProvider,
    ExecutionRequest,
    ExecutionResult,
    ProviderCapabilities,
    agent_network_enabled,
    append_trace_event,
    trace_cli_streams,
    truncate_single_line,
    worker_trace_enabled,
    workspace_context,
)
from .mcp_sync import sync_agy_mcp_config
from .session_store import GatewaySessionStore

AGY_SESSION_MESSAGE_LIMIT = 80
AGY_ALLOWED_TOOL_PERMISSIONS = frozenset({
    "request-review",
    "proceed-in-sandbox",
    "always-proceed",
    "strict",
})
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


def agy_environment(mode: str | None = None) -> dict[str, str]:
    return BaseCLIProvider.sanitize_env(extra_blocked=AGY_NON_OAUTH_ENV, mode=mode)


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


def _extract_error_text(value: Any) -> str:
    """Format an error value into a bounded single-line string."""
    if isinstance(value, dict):
        text = value.get("message") or value.get("code") or value.get("name") or json.dumps(value, ensure_ascii=False)
    else:
        text = value
    return truncate_single_line(text, 200)


def _summarize_agy_event(event: dict[str, Any]) -> dict[str, Any]:
    """Extract non-sensitive metadata from an agy NDJSON stream event."""
    event_name = str(event.get("event") or event.get("type") or "unknown")
    if event_name == "result":
        res = event.get("result") if isinstance(event.get("result"), dict) else {}
        resp = res.get("response")
        response_len = len(resp.strip()) if isinstance(resp, str) else 0
        summary: dict[str, Any] = {
            "event": "result",
            "status": res.get("status"),
            "response_len": response_len,
            "has_structured_output": res.get("structured_output") is not None,
            "keys": sorted(str(k) for k in res.keys()),
        }
        for key in ("finished_reason", "num_turns", "duration_seconds"):
            if res.get(key) is not None:
                summary[key] = res[key]
        if isinstance(res.get("usage"), dict) and res.get("usage"):
            summary["usage"] = res["usage"]
        err = res.get("error") or res.get("error_message") or res.get("message")
        if err:
            summary["error"] = _extract_error_text(err)
        return summary

    summary = {"event": event_name, "keys": sorted(str(k) for k in event.keys())}
    containers = [event] + [
        event[k]
        for k in ("step", "data", "part", "payload", "tool", "delta")
        if isinstance(event.get(k), dict)
    ]
    for field in ("step_type", "type", "status", "state", "finished_reason", "reason", "model", "code"):
        for container in containers:
            val = container.get(field)
            if field == "type" and container is event and val == event_name:
                continue
            if isinstance(val, (str, int, float, bool)) and val != "":
                summary[field] = val
                break
    for container in containers:
        for key in ("tool_name", "tool", "name"):
            val = container.get(key)
            if isinstance(val, (str, int, float)) and val != "":
                summary["tool_name"] = val
                break
        if "tool_name" in summary:
            break
    for container in containers:
        for key in ("text", "text_delta", "delta", "content"):
            val = container.get(key)
            if isinstance(val, str) and val:
                summary["text_len"] = len(val)
                break
        if "text_len" in summary:
            break
    is_error_event = (
        event_name == "error"
        or summary.get("step_type") == "error"
        or summary.get("type") == "error"
    )
    for container in containers:
        err = container.get("error") or (container.get("message") if is_error_event else None)
        if err:
            summary["error"] = _extract_error_text(err)
            break
    return summary


def _compact_event_tag(ev: dict[str, Any]) -> str:
    """Render a summarized stream event as a compact tag for single-line diagnostics."""
    name = str(ev.get("event") or "unknown")
    if name == "result":
        parts = [str(ev.get("status") or "unknown")]
        if ev.get("finished_reason") is not None:
            parts.append(f"finished_reason={truncate_single_line(ev['finished_reason'], 60)}")
        parts.append(f"len={ev.get('response_len', 0)}")
        if ev.get("error"):
            parts.append(f"err={truncate_single_line(ev['error'], 60)}")
        return f"result({','.join(parts)})"

    parts = []
    if ev.get("step_type"):
        parts.append(truncate_single_line(ev["step_type"], 60))
    elif ev.get("type") and ev["type"] != name:
        parts.append(truncate_single_line(ev["type"], 60))
    for field in ("status", "state", "tool_name", "finished_reason", "reason", "code"):
        if ev.get(field) is not None:
            label = "name" if field == "tool_name" else field
            parts.append(f"{label}={truncate_single_line(ev[field], 60)}")
    if ev.get("text_len"):
        parts.append(f"text_len={ev['text_len']}")
    if ev.get("error"):
        parts.append(f"err={truncate_single_line(ev['error'], 60)}")
    return f"{name}({','.join(parts)})" if parts else name


def _format_agy_diagnostics(diag: dict[str, Any]) -> str:
    """Format safe execution diagnostics into a compact single-line summary."""
    parts = [
        f"rc={diag.get('rc')}",
        f"duration_ms={diag.get('duration_ms')}",
        f"result_count={diag.get('result_count', 0)}",
        f"status={diag.get('status') or 'none'}",
    ]
    if diag.get("finished_reason") is not None:
        parts.append(f"finished_reason={truncate_single_line(diag['finished_reason'], 60)}")
    if diag.get("num_turns") is not None:
        parts.append(f"num_turns={diag['num_turns']}")
    parts.append(f"response_len={diag.get('response_len', 0)}")
    if diag.get("has_structured_output"):
        parts.append("structured_output=true")
    if diag.get("earlier_nonempty_results", 0) > 0:
        parts.append(f"earlier_nonempty_results={diag['earlier_nonempty_results']}")
    if diag.get("result_error"):
        parts.append(f"result_error={truncate_single_line(diag['result_error'], 200)}")
    usage = diag.get("usage")
    if isinstance(usage, dict) and usage:
        usage_map = (
            ("in", "input_tokens"),
            ("out", "output_tokens"),
            ("think", "thinking_tokens"),
            ("total", "total_tokens"),
        )
        usage_parts = [f"{label}:{usage[key]}" for label, key in usage_map if usage.get(key) is not None]
        if usage_parts:
            parts.append(f"usage={','.join(usage_parts)}")
    envelope_keys = diag.get("envelope_keys") or []
    if envelope_keys:
        parts.append(f"envelope_keys=[{','.join(str(k) for k in envelope_keys[:20])}]")
    event_tags = [_compact_event_tag(ev) for ev in (diag.get("events") or [])]
    if len(event_tags) > 20:
        shown_tags = event_tags[:10] + [f"...(+{len(event_tags) - 20})"] + event_tags[-10:]
    else:
        shown_tags = event_tags
    rendered_events = ",".join(shown_tags)
    if len(rendered_events) > 550:
        rendered_events = rendered_events[:550] + "...[truncated]"
    parts.append(f"events=[{rendered_events}]")
    parts.append(f"non_json={diag.get('non_json_lines', 0)}")
    stderr_tail = diag.get("stderr_tail") or ""
    parts.append(f"stderr={truncate_single_line(stderr_tail, 400) if stderr_tail else 'none'}")
    return " ".join(parts)


def run_agy_turn(
    prompt: str,
    model: str | None,
    session_id: str | None = None,
    system_prompt: str | None = None,
    persist: bool = False,
    mode: str = "chat",
    timeout: int = 120,
    *,
    job_id: str | None = None,
    trace_events: list[dict[str, Any]] | None = None,
    diagnostics_out: dict[str, Any] | None = None,
) -> tuple[str, str | None, dict[str, Any]]:
    formatted_prompt = f"User request:\n{prompt}"
    if system_prompt:
        formatted_prompt = f"System instructions:\n{system_prompt}\n\n{formatted_prompt}"
    trace_enabled = worker_trace_enabled()
    cli_start = time.monotonic()
    with workspace_context("agy-job-", tempfile.TemporaryDirectory) as workdir:
        command = [
            "agy", "--input-format", "stream-json", "--output-format", "stream-json",
            "--print-timeout", f"{timeout}s",
        ]
        if mode == "chat":
            command.extend(["--sandbox", "--disable-slash-commands"])
        elif agent_network_enabled():
            command.append("--dangerously-skip-permissions")
        else:
            command.append("--sandbox")
        if model:
            command.extend(["--model", model])
        if session_id:
            command.extend(["--conversation", session_id])
        append_trace_event(
            trace_events,
            "cli_start",
            provider="agy",
            mode=mode,
            model=model or "default",
            persist=persist,
            resume=bool(session_id),
            workspace=workdir,
            command=command,
            prompt_chars=len(prompt),
            system_prompt_chars=len(system_prompt or ""),
        )
        if trace_enabled:
            print(
                f"trace job={job_id or '-'} provider=agy stage=cli_start mode={mode} "
                f"model={model or 'default'} persist={persist} resume={bool(session_id)} "
                f"workspace={workdir} prompt_chars={len(prompt)} "
                f"system_chars={len(system_prompt or '')} cmd={json.dumps(command)}",
                flush=True,
            )
        input_event = json.dumps({"event": "user", "message": {"content": formatted_prompt}}) + "\n"
        process = subprocess.run(
            command,
            input=input_event,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout + 30,
            check=False,
            env=agy_environment(mode),
        )
    duration_ms = int((time.monotonic() - cli_start) * 1000)
    stdout_text = getattr(process, "stdout", "") or ""
    stderr_text = getattr(process, "stderr", "") or ""
    trace_cli_streams(job_id, "agy", stdout_text, stderr_text)

    envelope = None
    result_summary: dict[str, Any] = {}
    stdout_lines = 0
    non_json_lines = 0
    result_count = 0
    earlier_nonempty_result_count = 0
    event_summaries: list[dict[str, Any]] = []

    for line in stdout_text.splitlines():
        stdout_lines += 1
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            non_json_lines += 1
            continue
        if not isinstance(event, dict):
            non_json_lines += 1
            continue
        summary = _summarize_agy_event(event)
        event_summaries.append(summary)
        if event.get("event") == "result":
            result_count += 1
            if (
                envelope is not None
                and isinstance(envelope.get("response"), str)
                and envelope.get("response", "").strip()
            ):
                earlier_nonempty_result_count += 1
            envelope = event.get("result") or {}
            result_summary = summary

    stderr_tail = truncate_single_line(stderr_text[-1000:], 400) if stderr_text.strip() else ""
    diagnostics = {
        "rc": process.returncode,
        "duration_ms": duration_ms,
        "stdout_lines": stdout_lines,
        "non_json_lines": non_json_lines,
        "result_count": result_count,
        "earlier_nonempty_results": earlier_nonempty_result_count,
        "status": result_summary.get("status") if envelope is not None else None,
        "finished_reason": result_summary.get("finished_reason") if envelope is not None else None,
        "num_turns": result_summary.get("num_turns") if envelope is not None else None,
        "response_len": result_summary.get("response_len", 0) if envelope is not None else 0,
        "has_structured_output": result_summary.get("has_structured_output", False) if envelope is not None else False,
        "result_error": result_summary.get("error") if envelope is not None else None,
        "usage": result_summary.get("usage") if envelope is not None else None,
        "envelope_keys": result_summary.get("keys", []) if envelope is not None else [],
        "events": event_summaries,
        "stderr_tail": stderr_tail,
    }
    if diagnostics_out is not None:
        diagnostics_out.update(diagnostics)

    append_trace_event(
        trace_events,
        "cli_exit",
        provider="agy",
        rc=process.returncode,
        duration_ms=duration_ms,
        stdout_lines=stdout_lines,
        non_json_lines=non_json_lines,
        result_count=result_count,
        earlier_nonempty_results=earlier_nonempty_result_count,
        status=diagnostics["status"],
        finished_reason=diagnostics["finished_reason"],
        num_turns=diagnostics["num_turns"],
        response_len=diagnostics["response_len"],
        has_structured_output=diagnostics["has_structured_output"],
        result_error=diagnostics["result_error"],
        usage=diagnostics["usage"],
        envelope_keys=diagnostics["envelope_keys"],
        events=event_summaries[:30],
        events_truncated=max(0, len(event_summaries) - 30) or None,
        stderr_tail=stderr_tail or None,
    )
    if trace_enabled:
        print(
            f"trace job={job_id or '-'} provider=agy stage=cli_exit {_format_agy_diagnostics(diagnostics)}",
            flush=True,
        )

    if envelope is None:
        raise RuntimeError(f"agy did not return a result event ({_format_agy_diagnostics(diagnostics)})")
    if process.returncode != 0 or envelope.get("status") != "SUCCESS":
        raise RuntimeError(f"agy did not complete successfully ({_format_agy_diagnostics(diagnostics)})")
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
        tool_permission = os.environ.get("AGY_TOOL_PERMISSION", "proceed-in-sandbox").strip()
        if tool_permission not in AGY_ALLOWED_TOOL_PERMISSIONS:
            tool_permission = "proceed-in-sandbox"
        settings["toolPermission"] = tool_permission
        # The Gateway only supports Antigravity's Google account OAuth sign-in.
        # Remove a stale Gemini API-key provider setting from older deployments.
        settings.pop("modelProvider", None)
        temporary_settings_path = settings_path + ".tmp"
        with open(temporary_settings_path, "w", encoding="utf-8") as settings_file:
            json.dump(settings, settings_file)
        os.replace(temporary_settings_path, settings_path)
        os.chmod(settings_path, 0o600)
        sync_agy_mcp_config()

    def execute_turn(self, req: ExecutionRequest) -> ExecutionResult:
        timeout = req.timeout_seconds or self.timeout
        persist = req.store_session or bool(req.session_id)
        diagnostics: dict[str, Any] = {}
        answer, conversation_id, usage = run_agy_turn(
            req.prompt,
            req.model,
            req.session_id,
            req.system_prompt,
            persist=persist,
            mode=req.mode,
            timeout=timeout,
            job_id=req.job_id,
            trace_events=req.trace_events,
            diagnostics_out=diagnostics,
        )
        if not answer:
            detail = _format_agy_diagnostics(diagnostics) if diagnostics else ""
            message = f"agy returned no answer ({detail})" if detail else "agy returned no answer"
            raise RuntimeError(message)
        if conversation_id and persist:
            _SESSION_STORE.record_turn(
                "agy", conversation_id, req.prompt, answer, req.model, req.mode
            )
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
