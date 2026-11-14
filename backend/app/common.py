import os
import json
import time
import uuid
from datetime import datetime, timezone

from redis import Redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
JOB_TTL_SECONDS = max(60, int(os.environ.get("JOB_TTL_SECONDS", "3600")))
redis_client = Redis.from_url(REDIS_URL, decode_responses=True)


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def enqueue(prompt: str, model: str | None, session_id: str | None = None,
            store_session: bool = False, system_prompt: str | None = None,
            mode: str = "chat"):
    job_id = str(uuid.uuid4())
    now = utcnow()
    job = {"id": job_id, "status": "queued", "prompt": prompt, "model": model or "",
           "session_id": session_id or "", "store_session": "1" if store_session else "0",
           "system_prompt": system_prompt or "",
           "mode": mode,
           "created_at": now, "finished_at": "", "result": "", "error": "", "duration_ms": ""}
    with redis_client.pipeline(transaction=True) as pipe:
        pipe.hset(f"codex:job:{job_id}", mapping=job)
        pipe.expire(f"codex:job:{job_id}", JOB_TTL_SECONDS)
        pipe.lpush("codex:jobs", job_id)
        pipe.execute()
    return job_id


def fetch(job_id: str):
    job = redis_client.hgetall(f"codex:job:{job_id}")
    if not job:
        return None
    return {"id": job["id"], "status": job["status"], "model": job.get("model") or None,
            "session_id": job.get("session_id") or None,
            "result": job.get("result") or None, "error": job.get("error") or None,
            "duration_ms": int(job["duration_ms"]) if job.get("duration_ms") else None,
            "created_at": job.get("created_at") or None, "finished_at": job.get("finished_at") or None}


def public_job(job):
    return job


def get_model_catalog(timeout_seconds: int = 100):
    cached = redis_client.get("codex:model-catalog")
    if cached:
        return json.loads(cached)
    request_id = str(uuid.uuid4())
    response_key = f"codex:model-catalog:response:{request_id}"
    redis_client.lpush("codex:model-catalog:requests", request_id)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = redis_client.get(response_key)
        if response:
            redis_client.delete(response_key)
            payload = json.loads(response)
            if payload.get("error"):
                raise RuntimeError(payload["error"])
            redis_client.setex("codex:model-catalog", 300, json.dumps(payload["models"]))
            return payload["models"]
        time.sleep(0.2)
    redis_client.delete(response_key)
    raise TimeoutError("Timed out waiting for Codex model catalog")


def session_control(operation: str, session_id: str | None = None, mode: str = "agent",
                    timeout_seconds: int = 100):
    request_id = str(uuid.uuid4())
    response_key = f"codex:session:response:{request_id}"
    redis_client.lpush("codex:session:requests", json.dumps({
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
    raise TimeoutError("Timed out waiting for session operation")
