from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .base import BaseCLIProvider, ExecutionRequest, ExecutionResult, ProviderCapabilities

CODEX_HOME = os.environ.get("CODEX_HOME", "/home/node/.codex")
CHAT_CODEX_HOME = os.path.join(CODEX_HOME, "chat-mode")
CHAT_HOME_LOCK = threading.Lock()
CODEX_BLOCKED_ENV = frozenset({"GEMINI_API_KEY", "ANTHROPIC_API_KEY"})

CHAT_CONFIG = '''model_reasoning_effort = "low"
model_verbosity = "low"
model_reasoning_summary = "none"
sandbox_mode = "read-only"
approval_policy = "never"
web_search = "disabled"
project_doc_max_bytes = 0

[skills]
include_instructions = false

[skills.bundled]
enabled = false
'''


def chat_config_args() -> list[str]:
    values = [
        'model_reasoning_effort="low"',
        'model_verbosity="low"',
        'model_reasoning_summary="none"',
        'web_search="disabled"',
        'project_doc_max_bytes=0',
        'skills.include_instructions=false',
        'skills.bundled.enabled=false',
    ]
    return [part for value in values for part in ("-c", value)]


def codex_environment(mode: str = "agent") -> dict[str, str]:
    env = BaseCLIProvider.sanitize_env(extra_blocked=CODEX_BLOCKED_ENV)
    if mode == "chat":
        with CHAT_HOME_LOCK:
            os.makedirs(CHAT_CODEX_HOME, mode=0o700, exist_ok=True)
            config_path = os.path.join(CHAT_CODEX_HOME, "config.toml")
            temporary_config = config_path + ".tmp"
            with open(temporary_config, "w", encoding="utf-8") as config_file:
                config_file.write(CHAT_CONFIG)
            os.replace(temporary_config, config_path)
            auth_path = os.path.join(CHAT_CODEX_HOME, "auth.json")
            if not os.path.lexists(auth_path):
                try:
                    os.symlink(os.path.join(CODEX_HOME, "auth.json"), auth_path)
                except FileExistsError:
                    pass
        env["CODEX_HOME"] = CHAT_CODEX_HOME
        env["HOME"] = CHAT_CODEX_HOME
    return env


def rpc_response(
    reader: queue.Queue,
    process: subprocess.Popen,
    request_id: int,
    timeout: float = 45,
    notifications: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = reader.get(timeout=max(0.1, deadline - time.monotonic()))
        except queue.Empty:
            break
        if line is None:
            break
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "method" in message and "id" not in message:
            if notifications is not None:
                notifications.append(message)
            continue
        if message.get("id") == request_id:
            if "error" in message:
                raise RuntimeError("Codex app-server returned an RPC error")
            return message.get("result", {})
    raise TimeoutError("Codex app-server did not respond")


def send_rpc(
    process: subprocess.Popen,
    incoming: queue.Queue,
    request_id: int,
    method: str,
    params: dict[str, Any],
    notifications: list[dict[str, Any]] | None = None,
    timeout: float = 45,
) -> dict[str, Any]:
    process.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}) + "\n")
    process.stdin.flush()
    return rpc_response(incoming, process, request_id, timeout=timeout, notifications=notifications)


def initialized_app_server(mode: str = "agent") -> tuple[subprocess.Popen, queue.Queue]:
    command = ["codex", "app-server", "--listen", "stdio://"]
    if mode == "chat":
        command.extend(chat_config_args())
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=codex_environment(mode),
    )
    incoming: queue.Queue = queue.Queue()

    def read_stdout() -> None:
        for line in process.stdout:
            incoming.put(line)
        incoming.put(None)

    threading.Thread(target=read_stdout, daemon=True).start()
    process.stdin.write(json.dumps({
        "id": 1,
        "method": "initialize",
        "params": {
            "clientInfo": {"name": "codex-gateway", "title": "Codex Gateway", "version": "0.1.0"},
            "capabilities": {},
        },
    }) + "\n")
    process.stdin.flush()
    rpc_response(incoming, process, 1)
    process.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
    process.stdin.flush()
    return process, incoming


def _terminate_process(process: subprocess.Popen | None) -> None:
    if not process:
        return
    if process.stdin:
        process.stdin.close()
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()


class CodexProvider(BaseCLIProvider):
    name = "codex"
    display_name = "Codex CLI"
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

    def execute_turn(self, req: ExecutionRequest) -> ExecutionResult:
        timeout = req.timeout_seconds or self.timeout
        if req.session_id or req.store_session or req.system_prompt:
            persistent_session = bool(req.session_id or req.store_session)
            text, session_id = self._run_session_turn(
                req.prompt,
                req.model,
                req.session_id,
                req.system_prompt,
                req.store_session,
                req.mode,
                timeout,
            )
            if not persistent_session:
                session_id = None
            if not text:
                raise RuntimeError("Codex returned no answer")
            return ExecutionResult(text=text, session_id=session_id)
        return self._run_ephemeral_exec(req.prompt, req.model, req.mode, timeout)

    def _run_ephemeral_exec(
        self, prompt: str, model: str | None, mode: str, timeout: int
    ) -> ExecutionResult:
        with tempfile.TemporaryDirectory(prefix="codex-job-") as workdir:
            outfile = os.path.join(workdir, "answer.txt")
            cmd = [
                "codex", "exec", "--json", "--ephemeral", "--sandbox", "read-only",
                "--skip-git-repo-check", "--output-last-message", outfile,
            ]
            env = codex_environment(mode)
            if mode == "chat":
                cmd.extend(["--ignore-user-config", *chat_config_args()])
            if model:
                cmd.extend(["--model", model])
            cmd.append("-")
            p = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                cwd=workdir,
                timeout=timeout,
                check=False,
                env=env | {"CI": "1"},
            )
            # Parse and discard CLI event output; it can contain prompt or response text.
            for line in p.stdout.splitlines():
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    pass
            result = ""
            if os.path.isfile(outfile):
                with open(outfile, encoding="utf-8") as f:
                    result = f.read()[:100000]
            if p.returncode == 0 and result:
                return ExecutionResult(text=result)
            raise RuntimeError("Codex failed; check worker logs and sign-in status")

    def _run_session_turn(
        self,
        prompt: str,
        model: str | None,
        session_id: str | None = None,
        system_prompt: str | None = None,
        store_session: bool = False,
        mode: str = "agent",
        timeout: int | None = None,
    ) -> tuple[str, str]:
        timeout = timeout or self.timeout
        lock_key = f"codex:session:lock:{session_id}" if session_id else None
        lock_value = str(uuid.uuid4())
        locked = False
        if lock_key:
            lock_deadline = time.monotonic() + timeout + 120
            while time.monotonic() < lock_deadline:
                if self.redis.set(lock_key, lock_value, nx=True, ex=timeout + 120):
                    locked = True
                    break
                time.sleep(0.2)
            if not locked:
                raise TimeoutError("Session is busy")
        process = None
        notifications: list[dict[str, Any]] = []
        try:
            process, incoming = initialized_app_server(mode)
            safety: dict[str, Any] = {"cwd": "/work", "sandbox": "read-only", "approvalPolicy": "never"}
            if session_id:
                if system_prompt:
                    safety["developerInstructions"] = system_prompt
                thread = send_rpc(
                    process, incoming, 2, "thread/resume",
                    {"threadId": session_id, **safety}, notifications,
                )
            else:
                params = dict(safety)
                params["ephemeral"] = not store_session
                if system_prompt:
                    params["developerInstructions"] = system_prompt
                if model:
                    params["model"] = model
                thread = send_rpc(process, incoming, 2, "thread/start", params, notifications)
                session_id = thread.get("thread", {}).get("id")
            if not session_id:
                raise RuntimeError("Codex did not return a session ID")
            if not locked:
                lock_key = f"codex:session:lock:{session_id}"
                if not self.redis.set(lock_key, lock_value, nx=True, ex=timeout + 120):
                    raise RuntimeError("New session is unexpectedly locked")
                locked = True
            turn_params: dict[str, Any] = {
                "threadId": session_id,
                "input": [{"type": "text", "text": prompt}],
            }
            if model:
                turn_params["model"] = model
            turn = send_rpc(process, incoming, 3, "turn/start", turn_params, notifications)
            turn_id = turn.get("turn", {}).get("id")
            if not turn_id:
                raise RuntimeError("Codex did not start a turn")
            answer: list[str] = []
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if notifications:
                    message = notifications.pop(0)
                else:
                    try:
                        line = incoming.get(timeout=max(0.1, deadline - time.monotonic()))
                    except queue.Empty:
                        break
                    if line is None:
                        break
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "id" in message:
                        continue
                method_name = message.get("method")
                params = message.get("params", {})
                if params.get("threadId") != session_id:
                    continue
                if method_name == "item/agentMessage/delta" and params.get("turnId") == turn_id:
                    answer.append(params.get("delta", ""))
                elif method_name == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                    if params.get("turn", {}).get("status") != "completed":
                        raise RuntimeError("Codex turn did not complete")
                    return "".join(answer).strip(), session_id
            raise TimeoutError("Codex session turn timed out")
        finally:
            _terminate_process(process)
            if locked and lock_key:
                self.redis.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
                    1,
                    lock_key,
                    lock_value,
                )

    def list_models(self) -> list[dict[str, Any]]:
        process = subprocess.Popen(
            ["codex", "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=BaseCLIProvider.sanitize_env(extra_blocked=CODEX_BLOCKED_ENV),
        )
        incoming: queue.Queue = queue.Queue()

        def read_stdout() -> None:
            for line in process.stdout:
                incoming.put(line)
            incoming.put(None)

        threading.Thread(target=read_stdout, daemon=True).start()
        try:
            process.stdin.write(json.dumps({
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "codex-gateway", "title": "Codex Gateway", "version": "0.1.0"},
                    "capabilities": {},
                },
            }) + "\n")
            process.stdin.flush()
            rpc_response(incoming, process, 1)
            process.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
            process.stdin.write(json.dumps({"id": 2, "method": "model/list", "params": {"limit": 100}}) + "\n")
            process.stdin.flush()
            response = rpc_response(incoming, process, 2)
            models = []
            for item in response.get("data", []):
                model_id = item.get("model") or item.get("id")
                if model_id:
                    models.append({
                        "id": model_id,
                        "name": item.get("displayName") or model_id,
                        "is_default": bool(item.get("isDefault")),
                    })
            if not models:
                raise RuntimeError("Codex returned an empty model catalog")
            return models
        finally:
            _terminate_process(process)

    def session_control(
        self,
        operation: str,
        session_id: str | None = None,
        mode: str = "agent",
    ) -> Any:
        if operation == "list":
            cache_key = f"codex:sessions:list:{mode}"
            cached = self.redis.get(cache_key)
            if cached:
                return json.loads(cached)
            result = self._perform_session_control("list", mode=mode)
            self.redis.setex(cache_key, 30, json.dumps(result))
            return result
        result = self._perform_session_control(operation, session_id=session_id, mode=mode)
        if operation != "usage":
            self.redis.delete("codex:sessions:list:agent", "codex:sessions:list:chat")
        return result

    def _perform_session_control(
        self, operation: str, session_id: str | None = None, mode: str = "agent"
    ) -> Any:
        process, incoming = initialized_app_server(mode)
        try:
            if operation == "usage":
                response = send_rpc(process, incoming, 2, "account/rateLimits/read", {})
                limits = response.get("rateLimits", response.get("rate_limits", {}))

                def normalize_window(window: dict[str, Any] | None) -> dict[str, Any] | None:
                    if not window:
                        return None
                    return {
                        "used_percent": window.get("usedPercent", window.get("used_percent")),
                        "window_duration_mins": window.get("windowDurationMins", window.get("window_duration_mins")),
                        "resets_at": window.get("resetsAt", window.get("resets_at")),
                    }

                return {
                    "plan_type": response.get("planType", response.get("plan_type")),
                    "primary": normalize_window(limits.get("primary")),
                    "secondary": normalize_window(limits.get("secondary")),
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }
            if operation == "list":
                sessions = []
                cursor = None
                for page in range(50):
                    request_id = page + 2
                    params: dict[str, Any] = {
                        "limit": 100,
                        "sortKey": "updated_at",
                        "sortDirection": "desc",
                        "archived": False,
                    }
                    if cursor:
                        params["cursor"] = cursor
                    response = send_rpc(process, incoming, request_id, "thread/list", params)
                    sessions.extend(
                        {
                            "id": item.get("id"),
                            "mode": mode,
                            "updated_at": item.get("updatedAt"),
                            "created_at": item.get("createdAt"),
                            "model": item.get("model"),
                            "status": item.get("status", {}).get("type")
                            if isinstance(item.get("status"), dict)
                            else None,
                        }
                        for item in response.get("data", [])
                        if item.get("id")
                    )
                    cursor = response.get("nextCursor")
                    if not cursor:
                        break
                return sessions
            if operation == "history" and session_id:
                response = send_rpc(
                    process, incoming, 2, "thread/read",
                    {"threadId": session_id, "includeTurns": True},
                )
                thread = response.get("thread", response)
                messages = []
                for turn in thread.get("turns", []):
                    for item in turn.get("items", []):
                        kind = item.get("type")
                        if kind == "userMessage":
                            content = item.get("content", [])
                            if isinstance(content, str):
                                text = content.strip()
                            else:
                                text = "\n".join(
                                    part.get("text", "")
                                    for part in content
                                    if part.get("type") == "text"
                                ).strip()
                            role = "user"
                        elif kind == "agentMessage":
                            text = item.get("text", "").strip()
                            role = "assistant"
                        else:
                            continue
                        if text:
                            messages.append({"role": role, "content": text})
                return {"id": session_id, "messages": messages}
            if operation == "delete" and session_id:
                lock_key = f"codex:session:lock:{session_id}"
                lock_value = f"delete-{uuid.uuid4()}"
                if not self.redis.set(lock_key, lock_value, nx=True, ex=self.timeout + 120):
                    raise RuntimeError("Session is busy; wait for its active request to finish")
                try:
                    return send_rpc(process, incoming, 2, "thread/delete", {"threadId": session_id})
                finally:
                    self.redis.eval(
                        "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
                        1,
                        lock_key,
                        lock_value,
                    )
            raise ValueError("Unsupported session operation")
        finally:
            _terminate_process(process)
