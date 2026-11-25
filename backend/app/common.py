import json
import os
import time
import uuid
from datetime import datetime, timezone

from redis import Redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
JOB_TTL_SECONDS = max(60, int(os.environ.get("JOB_TTL_SECONDS", "3600")))
redis_client = Redis.from_url(REDIS_URL, decode_responses=True)

PROVIDERS_REGISTRY_KEY = "gateway:providers:registry"

# Fallback descriptors used before a worker registers itself (or in unit tests).
DEFAULT_PROVIDER_DESCRIPTORS = {
    "codex": {
        "name": "codex",
        "display_name": "Codex CLI",
        "capabilities": {
            "modes": ["chat", "agent"],
            "default_mode": "chat",
            "supports_system_prompt": True,
            "supports_sessions": True,
            "supports_session_history": True,
            "supports_session_delete": True,
            "supports_usage_quota": True,
            "supports_model_discovery": True,
            "session_list_modes": ["agent", "chat"],
        },
    },
    "agy": {
        "name": "agy",
        "display_name": "Antigravity CLI (agy)",
        "capabilities": {
            "modes": ["chat", "agent"],
            "default_mode": "chat",
            "supports_system_prompt": True,
            "supports_sessions": True,
            "supports_session_history": True,
            "supports_session_delete": True,
            "supports_usage_quota": True,
            "supports_model_discovery": True,
            "session_list_modes": [],
        },
    },
}


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def get_registered_providers() -> dict[str, dict]:
    """Read active provider descriptors from Redis, falling back to configured defaults."""
    try:
        raw = redis_client.hgetall(PROVIDERS_REGISTRY_KEY)
    except Exception:
        raw = {}
    if raw:
        providers = {}
        for name, payload in raw.items():
            try:
                providers[name] = json.loads(payload)
            except (TypeError, json.JSONDecodeError):
                continue
        if providers:
            return providers
    return {k: dict(v) for k, v in DEFAULT_PROVIDER_DESCRIPTORS.items()}


def enqueue(prompt: str, model: str | None, session_id: str | None = None,
            store_session: bool = False, system_prompt: str | None = None,
            mode: str = "chat", provider: str = "codex"):
    job_id = str(uuid.uuid4())
    now = utcnow()
    job = {"id": job_id, "status": "queued", "prompt": prompt, "model": model or "",
           "session_id": session_id or "", "store_session": "1" if store_session else "0",
           "system_prompt": system_prompt or "",
           "mode": mode,
           "provider": provider,
           "created_at": now, "finished_at": "", "result": "", "error": "", "duration_ms": ""}
    with redis_client.pipeline(transaction=True) as pipe:
        pipe.hset(f"gateway:job:{job_id}", mapping=job)
        pipe.expire(f"gateway:job:{job_id}", JOB_TTL_SECONDS)
        pipe.lpush(f"gateway:jobs:{provider}", job_id)
        pipe.execute()
    return job_id


def fetch(job_id: str):
    job = redis_client.hgetall(f"gateway:job:{job_id}")
    if not job:
        return None
    try:
        usage = json.loads(job.get("usage") or job.get("agy_usage") or "") or None
    except json.JSONDecodeError:
        usage = None
    try:
        parsed_events = json.loads(job.get("events") or "") if job.get("events") else []
    except (TypeError, json.JSONDecodeError):
        parsed_events = []
    events = parsed_events if isinstance(parsed_events, list) else []
    return {"id": job["id"], "status": job["status"], "model": job.get("model") or None,
            "provider": job.get("provider") or "codex",
            "session_id": job.get("session_id") or None,
            "usage": usage,
            "events": events,
            "result": job.get("result") or None, "error": job.get("error") or None,
            "duration_ms": int(job["duration_ms"]) if job.get("duration_ms") else None,
            "created_at": job.get("created_at") or None, "finished_at": job.get("finished_at") or None}


def public_job(job):
    result = dict(job)
    result.pop("events", None)
    if result.get("provider") == "codex":
        result["provider"] = "openai"
    return result


def control_request(provider: str, operation: str, session_id: str | None = None,
                    mode: str = "agent", timeout_seconds: int = 100):
    request_id = str(uuid.uuid4())
    response_key = f"gateway:provider:{provider}:control:response:{request_id}"
    redis_client.lpush(f"gateway:provider:{provider}:control:requests", json.dumps({
        "request_id": request_id, "operation": operation, "session_id": session_id, "mode": mode
    }))
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = redis_client.get(response_key)
        if response:
            redis_client.delete(response_key)
            payload = json.loads(response)
            if payload.get("error"):
                raise RuntimeError(payload["error"])
            return payload.get("result")
        time.sleep(0.2)
    redis_client.delete(response_key)
    raise TimeoutError(f"Timed out waiting for {provider} {operation}")


def get_model_catalog(provider: str = "codex", timeout_seconds: int = 100):
    cache_key = f"gateway:provider:{provider}:models"
    cached = redis_client.get(cache_key)
    if cached:
        return json.loads(cached)
    models = control_request(provider, "models", timeout_seconds=timeout_seconds)
    redis_client.setex(cache_key, 300, json.dumps(models))
    return models


def session_control(operation: str, session_id: str | None = None, mode: str = "agent",
                    timeout_seconds: int = 100, provider: str = "codex"):
    return control_request(provider, operation, session_id=session_id, mode=mode,
                           timeout_seconds=timeout_seconds)
