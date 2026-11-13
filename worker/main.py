import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone

from redis import Redis

redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
TIMEOUT = max(10, min(int(os.environ.get("JOB_TIMEOUT_SECONDS", "120")), 900))
CONCURRENCY = max(1, min(int(os.environ.get("MAX_CONCURRENT_JOBS", "1")), 4))
JOB_TTL_SECONDS = max(60, int(os.environ.get("JOB_TTL_SECONDS", "3600")))


def execute(job_id):
    start = time.monotonic()
    key = f"codex:job:{job_id}"
    # Prompt leaves Redis as soon as the worker claims it; it is held only in memory
    # for this invocation and passed to Codex through stdin.
    with redis.pipeline(transaction=True) as pipe:
        pipe.hgetall(key)
        pipe.hset(key, "status", "running")
        pipe.hdel(key, "prompt")
        job, _, _ = pipe.execute()
    if not job or job.get("status") != "queued" or not job.get("prompt"):
        # hgetall was taken before the status/prompt update in the transaction.
        if not job:
            return
        if job.get("status") != "queued" or not job.get("prompt"):
            return
    prompt = job["prompt"]
    model = job.get("model") or None
    result = None
    error = None
    status = "failed"
    with tempfile.TemporaryDirectory(prefix="codex-job-") as workdir:
        outfile = os.path.join(workdir, "answer.txt")
        cmd = ["codex", "exec", "--json", "--ephemeral", "--sandbox", "read-only",
               "--skip-git-repo-check", "--output-last-message", outfile]
        if model:
            cmd.extend(["--model", model])
        cmd.append("-")
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                               cwd=workdir, timeout=TIMEOUT, check=False,
                               env={k: v for k, v in os.environ.items()
                                    if k not in ("GATEWAY_API_KEY", "POSTGRES_PASSWORD")}
                               | {"CI": "1"})
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
                                "duration_ms": duration_ms,
                                "finished_at": datetime.now(timezone.utc).isoformat()})
        redis.expire(key, JOB_TTL_SECONDS)
    print(f"job={job_id} status={status} duration_ms={duration_ms}", flush=True)


def rpc_response(reader, process, request_id, timeout=45):
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
    for _ in range(CONCURRENCY - 1):
        threading.Thread(target=loop, daemon=True).start()
    loop()
