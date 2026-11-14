import json
import os
import queue
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone

from redis import Redis

redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
TIMEOUT = max(10, min(int(os.environ.get("JOB_TIMEOUT_SECONDS", "120")), 900))
CONCURRENCY = max(1, min(int(os.environ.get("MAX_CONCURRENT_JOBS", "1")), 4))
JOB_TTL_SECONDS = max(60, int(os.environ.get("JOB_TTL_SECONDS", "3600")))
CODEX_HOME = os.environ.get("CODEX_HOME", "/home/node/.codex")
CHAT_CODEX_HOME = os.path.join(CODEX_HOME, "chat-mode")
CHAT_HOME_LOCK = threading.Lock()
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


def codex_environment(mode="agent"):
    env = {k: v for k, v in os.environ.items()
           if k not in ("GATEWAY_API_KEY", "DATABASE_URL", "POSTGRES_PASSWORD")}
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


def chat_config_args():
    values = [
        'model_reasoning_effort="low"', 'model_verbosity="low"',
        'model_reasoning_summary="none"', 'web_search="disabled"',
        'project_doc_max_bytes=0', 'skills.include_instructions=false',
        'skills.bundled.enabled=false',
    ]
    return [part for value in values for part in ("-c", value)]


def execute(job_id):
    start = time.monotonic()
    key = f"codex:job:{job_id}"
    # Prompt leaves Redis as soon as the worker claims it; it is held only in memory
    # for this invocation and passed to Codex through stdin.
    with redis.pipeline(transaction=True) as pipe:
        pipe.hgetall(key)
        pipe.hset(key, "status", "running")
        pipe.hdel(key, "prompt", "system_prompt")
        job, _, _ = pipe.execute()
    if not job or job.get("status") != "queued" or not job.get("prompt"):
        # hgetall was taken before the status/prompt update in the transaction.
        if not job:
            return
        if job.get("status") != "queued" or not job.get("prompt"):
            return
    prompt = job["prompt"]
    model = job.get("model") or None
    session_id = job.get("session_id") or None
    store_session = job.get("store_session") == "1"
    system_prompt = job.get("system_prompt") or None
    mode = job.get("mode") or "chat"
    result = None
    error = None
    status = "failed"
    if session_id or store_session or system_prompt:
        try:
            persistent_session = bool(session_id or store_session)
            result, session_id = run_session_turn(prompt, model, session_id, system_prompt, store_session, mode)
            if not persistent_session:
                session_id = None
            status = "completed" if result else "failed"
            if not result:
                error = "Codex returned no answer"
        except Exception as exc:
            error = f"Codex session failed: {type(exc).__name__}"
    else:
        with tempfile.TemporaryDirectory(prefix="codex-job-") as workdir:
            outfile = os.path.join(workdir, "answer.txt")
            cmd = ["codex", "exec", "--json", "--ephemeral", "--sandbox", "read-only",
                   "--skip-git-repo-check", "--output-last-message", outfile]
            env = codex_environment(mode)
            if mode == "chat":
                cmd.extend(["--ignore-user-config", *chat_config_args()])
            if model:
                cmd.extend(["--model", model])
            cmd.append("-")
            try:
                p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                   cwd=workdir, timeout=TIMEOUT, check=False,
                                   env=env | {"CI": "1"})
                # Parse and discard CLI event output; it can contain prompt or response text.
                for line in p.stdout.splitlines():
                    try:
                        json.loads(line)
                    except json.JSONDecodeError:
                        pass
                if os.path.isfile(outfile):
                    with open(outfile, encoding="utf-8") as f:
                        result = f.read()[:100000]
                if p.returncode == 0 and result:
                    status = "completed"
                else:
                    error = "Codex failed; check worker logs and sign-in status"
            except subprocess.TimeoutExpired:
                error = f"Codex exceeded {TIMEOUT}s timeout"
            except Exception as exc:
                error = f"Worker error: {type(exc).__name__}"

    duration_ms = int((time.monotonic() - start) * 1000)
    if redis.exists(key):
        redis.hset(key, mapping={"status": status, "result": result or "", "error": error or "",
                                "session_id": session_id or "",
                                "duration_ms": duration_ms,
                                "finished_at": datetime.now(timezone.utc).isoformat()})
        redis.expire(key, JOB_TTL_SECONDS)
    if session_id:
        redis.delete("codex:sessions:list:agent", "codex:sessions:list:chat")
    print(f"job={job_id} status={status} duration_ms={duration_ms}", flush=True)


def rpc_response(reader, process, request_id, timeout=45, notifications=None):
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


def read_models_from_codex():
    process = subprocess.Popen(["codex", "app-server", "--listen", "stdio://"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, bufsize=1,
                               env={k: v for k, v in os.environ.items()
                                    if k not in ("GATEWAY_API_KEY", "DATABASE_URL", "POSTGRES_PASSWORD")})
    incoming = queue.Queue()

    def read_stdout():
        for line in process.stdout:
            incoming.put(line)
        incoming.put(None)

    threading.Thread(target=read_stdout, daemon=True).start()
    try:
        process.stdin.write(json.dumps({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "codex-gateway", "title": "Codex Gateway", "version": "0.1.0"},
            "capabilities": {}}}) + "\n")
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
                models.append({"id": model_id, "name": item.get("displayName") or model_id,
                               "is_default": bool(item.get("isDefault"))})
        if not models:
            raise RuntimeError("Codex returned an empty model catalog")
        return models
    finally:
        if process.stdin:
            process.stdin.close()
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()


def initialized_app_server(mode="agent"):
    command = ["codex", "app-server", "--listen", "stdio://"]
    if mode == "chat":
        command.extend(chat_config_args())
    process = subprocess.Popen(command,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, bufsize=1,
                               env=codex_environment(mode))
    incoming = queue.Queue()

    def read_stdout():
        for line in process.stdout:
            incoming.put(line)
        incoming.put(None)

    threading.Thread(target=read_stdout, daemon=True).start()
    process.stdin.write(json.dumps({"id": 1, "method": "initialize", "params": {
        "clientInfo": {"name": "codex-gateway", "title": "Codex Gateway", "version": "0.1.0"},
        "capabilities": {}}}) + "\n")
    process.stdin.flush()
    rpc_response(incoming, process, 1)
    process.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
    process.stdin.flush()
    return process, incoming


def send_rpc(process, incoming, request_id, method, params, notifications=None, timeout=45):
    process.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}) + "\n")
    process.stdin.flush()
    return rpc_response(incoming, process, request_id, timeout=timeout, notifications=notifications)


def run_session_turn(prompt, model, session_id=None, system_prompt=None, store_session=False, mode="agent"):
    lock_key = f"codex:session:lock:{session_id}" if session_id else None
    lock_value = str(uuid.uuid4())
    locked = False
    if lock_key:
        lock_deadline = time.monotonic() + TIMEOUT + 120
        while time.monotonic() < lock_deadline:
            if redis.set(lock_key, lock_value, nx=True, ex=TIMEOUT + 120):
                locked = True
                break
            time.sleep(0.2)
        if not locked:
            raise TimeoutError("Session is busy")
    process = None
    notifications = []
    try:
        process, incoming = initialized_app_server(mode)
        safety = {"cwd": "/work", "sandbox": "read-only", "approvalPolicy": "never"}
        if session_id:
            if system_prompt:
                safety["developerInstructions"] = system_prompt
            thread = send_rpc(process, incoming, 2, "thread/resume",
                              {"threadId": session_id, **safety}, notifications)
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
            if not redis.set(lock_key, lock_value, nx=True, ex=TIMEOUT + 120):
                raise RuntimeError("New session is unexpectedly locked")
            locked = True
        turn_params = {"threadId": session_id, "input": [{"type": "text", "text": prompt}]}
        if model:
            turn_params["model"] = model
        turn = send_rpc(process, incoming, 3, "turn/start", turn_params, notifications)
        turn_id = turn.get("turn", {}).get("id")
        if not turn_id:
            raise RuntimeError("Codex did not start a turn")
        answer = []
        deadline = time.monotonic() + TIMEOUT
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
        if process and process.stdin:
            process.stdin.close()
        if process:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        if locked:
            redis.eval("if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
                       1, lock_key, lock_value)


def perform_session_control(operation, session_id=None, mode="agent"):
    process, incoming = initialized_app_server(mode)
    try:
        if operation == "usage":
            response = send_rpc(process, incoming, 2, "account/rateLimits/read", {})
            limits = response.get("rateLimits", response.get("rate_limits", {}))
            def normalize_window(window):
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
                params = {"limit": 100, "sortKey": "updated_at", "sortDirection": "desc", "archived": False}
                if cursor:
                    params["cursor"] = cursor
                response = send_rpc(process, incoming, request_id, "thread/list", params)
                sessions.extend({"id": item.get("id"), "mode": mode, "updated_at": item.get("updatedAt"),
                                 "created_at": item.get("createdAt"), "model": item.get("model"),
                                 "status": item.get("status", {}).get("type") if isinstance(item.get("status"), dict) else None}
                                for item in response.get("data", []) if item.get("id"))
                cursor = response.get("nextCursor")
                if not cursor:
                    break
            return sessions
        if operation == "history" and session_id:
            response = send_rpc(process, incoming, 2, "thread/read",
                                {"threadId": session_id, "includeTurns": True})
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
                            text = "\n".join(part.get("text", "") for part in content
                                            if part.get("type") == "text").strip()
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
            if not redis.set(lock_key, lock_value, nx=True, ex=TIMEOUT + 120):
                raise RuntimeError("Session is busy; wait for its active request to finish")
            try:
                return send_rpc(process, incoming, 2, "thread/delete", {"threadId": session_id})
            finally:
                redis.eval("if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
                           1, lock_key, lock_value)
        raise ValueError("Unsupported session operation")
    finally:
        if process.stdin:
            process.stdin.close()
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()


def model_catalog_loop():
    while True:
        try:
            item = redis.brpop("codex:model-catalog:requests", timeout=3)
            if not item:
                continue
            request_id = item[1]
            response_key = f"codex:model-catalog:response:{request_id}"
            try:
                payload = {"models": read_models_from_codex()}
            except Exception as exc:
                payload = {"error": f"Codex model discovery failed: {type(exc).__name__}"}
            redis.setex(response_key, 120, json.dumps(payload))
        except Exception as exc:
            print(f"model catalog queue error: {type(exc).__name__}", flush=True)
            time.sleep(2)


def session_control_loop():
    while True:
        try:
            item = redis.brpop("codex:session:requests", timeout=3)
            if not item:
                continue
            request = json.loads(item[1])
            request_id = request["request_id"]
            response_key = f"codex:session:response:{request_id}"
            try:
                mode = request.get("mode", "agent")
                if request["operation"] == "list":
                    cache_key = f"codex:sessions:list:{mode}"
                    cached = redis.get(cache_key)
                    result = json.loads(cached) if cached else perform_session_control("list", mode=mode)
                    if not cached:
                        redis.setex(cache_key, 30, json.dumps(result))
                    payload = {"result": result}
                else:
                    result = perform_session_control(request["operation"], request.get("session_id"), mode)
                    redis.delete("codex:sessions:list:agent", "codex:sessions:list:chat")
                    payload = {"result": result}
            except Exception as exc:
                payload = {"error": f"Session operation failed: {type(exc).__name__}"}
            redis.setex(response_key, 120, json.dumps(payload))
        except Exception as exc:
            print(f"session control queue error: {type(exc).__name__}", flush=True)
            time.sleep(2)


def loop():
    while True:
        try:
            item = redis.brpop("codex:jobs", timeout=3)
            if item:
                execute(item[1])
        except Exception as exc:
            print(f"queue error: {type(exc).__name__}", flush=True)
            time.sleep(3)


if __name__ == "__main__":
    print(f"Codex worker started, concurrency={CONCURRENCY}", flush=True)
    threading.Thread(target=model_catalog_loop, daemon=True).start()
    threading.Thread(target=session_control_loop, daemon=True).start()
    for _ in range(CONCURRENCY - 1):
        threading.Thread(target=loop, daemon=True).start()
    loop()
