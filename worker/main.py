import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

from redis import Redis

# Ensure sibling `providers` package is importable even when main.py is loaded by file path.
_WORKER_DIR = os.path.dirname(os.path.abspath(__file__))
if _WORKER_DIR not in sys.path:
    sys.path.insert(0, _WORKER_DIR)

from providers import ExecutionRequest, build_registry  # noqa: E402

redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
TIMEOUT = max(10, min(int(os.environ.get("JOB_TIMEOUT_SECONDS", "120")), 900))
CONCURRENCY = max(1, min(int(os.environ.get("MAX_CONCURRENT_JOBS", "1")), 4))
JOB_TTL_SECONDS = max(60, int(os.environ.get("JOB_TTL_SECONDS", "3600")))
PROVIDERS_REGISTRY_KEY = "gateway:providers:registry"

REGISTRY = build_registry(redis_client=redis, timeout_seconds=TIMEOUT)


def _job_queue_keys() -> list[str]:
    return [f"gateway:jobs:{name}" for name in REGISTRY]


def register_providers() -> None:
    mapping = {
        name: json.dumps({
            "name": provider.name,
            "display_name": provider.display_name,
            "capabilities": provider.capabilities.to_dict(),
        })
        for name, provider in REGISTRY.items()
    }
    if mapping:
        redis.hset(PROVIDERS_REGISTRY_KEY, mapping=mapping)


def execute(job_id: str) -> None:
    start = time.monotonic()
    key = f"gateway:job:{job_id}"
    # Prompt leaves Redis as soon as the worker claims it; it is held only in memory
    # for this invocation and passed to the CLI through stdin.
    with redis.pipeline(transaction=True) as pipe:
        pipe.hgetall(key)
        pipe.hset(key, "status", "running")
        pipe.hdel(key, "prompt", "system_prompt")
        job, _, _ = pipe.execute()
    if not job or job.get("status") != "queued" or not job.get("prompt"):
        return

    provider_name = job.get("provider") or "codex"
    provider = REGISTRY.get(provider_name)
    session_id = job.get("session_id") or None
    store_session = job.get("store_session") == "1"
    mode = job.get("mode") or "chat"
    usage = None
    result = None
    error = None
    status = "failed"

    if provider is None:
        error = f"Provider '{provider_name}' is not enabled on this worker"
    else:
        req = ExecutionRequest(
            prompt=job["prompt"],
            model=job.get("model") or None,
            mode=mode,
            session_id=session_id,
            store_session=store_session,
            system_prompt=job.get("system_prompt") or None,
            timeout_seconds=TIMEOUT,
        )
        try:
            outcome = provider.execute_turn(req)
            result = outcome.text
            session_id = outcome.session_id
            usage = outcome.usage
            status = "completed" if result else "failed"
            if not result:
                error = f"{provider_name} returned no answer"
        except subprocess.TimeoutExpired:
            error = f"{provider_name} exceeded {TIMEOUT}s timeout"
        except Exception as exc:
            error = f"{provider_name} request failed: {type(exc).__name__}"

    duration_ms = int((time.monotonic() - start) * 1000)
    if redis.exists(key):
        redis.hset(
            key,
            mapping={
                "status": status,
                "result": result or "",
                "error": error or "",
                "session_id": session_id or "",
                "usage": json.dumps(usage) if usage else "",
                "duration_ms": duration_ms,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        redis.expire(key, JOB_TTL_SECONDS)
    if session_id:
        redis.delete(f"{provider_name}:sessions:list:agent", f"{provider_name}:sessions:list:chat")
    print(f"job={job_id} provider={provider_name} status={status} duration_ms={duration_ms}", flush=True)


def control_loop(provider_name: str) -> None:
    provider = REGISTRY[provider_name]
    request_queue = f"gateway:provider:{provider_name}:control:requests"
    while True:
        try:
            item = redis.brpop(request_queue, timeout=3)
            if not item:
                continue
            request = json.loads(item[1])
            request_id = request["request_id"]
            operation = request["operation"]
            response_key = f"gateway:provider:{provider_name}:control:response:{request_id}"
            try:
                if operation == "models":
                    payload = {"result": provider.list_models()}
                else:
                    mode = request.get("mode", "agent")
                    session_id = request.get("session_id")
                    payload = {
                        "result": provider.session_control(
                            operation, session_id=session_id, mode=mode
                        )
                    }
            except Exception as exc:
                payload = {"error": f"{provider_name} {operation} failed: {type(exc).__name__}"}
            redis.setex(response_key, 120, json.dumps(payload))
        except Exception as exc:
            print(f"control queue error ({provider_name}): {type(exc).__name__}", flush=True)
            time.sleep(2)


def loop() -> None:
    queue_keys = _job_queue_keys()
    while True:
        try:
            item = redis.brpop(queue_keys, timeout=3)
            if item:
                execute(item[1])
        except Exception as exc:
            print(f"queue error: {type(exc).__name__}", flush=True)
            time.sleep(3)


if __name__ == "__main__":
    enabled = ", ".join(REGISTRY.keys())
    print(f"AI CLI Gateway worker started, concurrency={CONCURRENCY}, providers=[{enabled}]", flush=True)
    for name, provider in REGISTRY.items():
        provider.startup_init()
        threading.Thread(target=control_loop, args=(name,), daemon=True).start()
    register_providers()
    for _ in range(CONCURRENCY - 1):
        threading.Thread(target=loop, daemon=True).start()
    loop()
