# Personal Codex Gateway MVP

Private Docker-based HTTP gateway for **one user**: n8n -> FastAPI -> Redis -> worker -> Codex CLI (ChatGPT sign-in). Includes a React management console. Redis is used as a volatile queue and short-lived job store; no database is used.

## Scope and warnings

- This is a **personal prototype**, not a multi-user SaaS or production proxy. ChatGPT subscription usage is subject to eligibility, service terms, rate limits and any applicable credits. Sign-in does not provide a general-purpose unlimited OpenAI API.
- Codex is a coding agent, not an ordinary text-only inference endpoint. The worker invokes `codex exec` in a disposable working directory with read-only sandbox; do not pass secrets, untrusted documents or enable arbitrary tools.
- The OpenAI-like chat endpoint is deliberately a **subset**: text messages only, no streaming, tool calls, images, embeddings, or reliable token accounting. Requests are stateless unless `store_session` or an existing `session_id` is supplied.
- API key is shared by the local browser and n8n. Only use on a trusted machine. Never publish port 8088 externally or expose the Docker public network to untrusted containers.
- Prompts are held in Redis memory while queued, removed when a worker claims the job, and passed to Codex via stdin. Redis persistence is disabled and it has no data volume, so queued jobs and job history are lost if Redis restarts.
- When session saving is enabled, Codex stores the full conversation (system instructions, prompts and answers) as local session history files in the worker's persistent `codex_auth` volume. Session history is not copied into Redis or another database. Delete a session from the Web Console to remove its Codex history files.
- Saved sessions have no automatic expiration; remove them from the Dashboard when they are no longer needed.
- Results, status, and non-sensitive job metadata are kept in Redis memory for `JOB_TTL_SECONDS` (default one hour), then expire. No PostgreSQL service or persistent job database is used.
- The system has **no provider-side subscription quota API** and cannot determine remaining subscription allowance; rate-limit errors surface as failed jobs.

## Prerequisites

Docker Engine/Compose v2, internet access from worker to OpenAI, a ChatGPT account entitled to Codex CLI sign-in, and an existing n8n Docker installation.

## Quick start

```bash
cp .env.example .env
# Edit .env and set a long random GATEWAY_API_KEY.
docker compose build
docker compose up -d
# Interactive sign-in only on your own trusted host:
docker compose exec worker codex login --device-auth
# Verify login:
docker compose exec worker codex login status
# Check API:
curl http://127.0.0.1:8088/health
# Call from host (use the actual key from .env):
curl -sS http://127.0.0.1:8088/v1/chat/completions \
  -H "Authorization: Bearer YOUR_GATEWAY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"codex-default","messages":[{"role":"user","content":"Hello in Thai"}],"stream":false}'
```

If `--device-auth` isn't allowed by the account, consult official Codex CLI sign-in instructions for your installation; do not paste or publish session tokens. `codex login` is another supported interactive sign-in route when a browser callback is available.

Open **http://127.0.0.1:8088/**, unlock with the local Gateway API key, then test a prompt. Select **Save this conversation as a Codex session** to save the conversation and continue it on later requests. Manage or delete saved sessions in the Sessions panel. The UI key stays in browser `sessionStorage` (cleared after closing the tab). This UI is **not** a separate login/account system.

### Upgrading from the PostgreSQL version

If the previous Compose version was started, run `docker compose down --remove-orphans` before starting this version. This removes the old PostgreSQL container but keeps named volumes. If you also want to erase its old job history, inspect `docker volume ls` and remove only the project's `*_postgres_data` volume after confirming the name. Do not use `docker compose down -v` unless you also intend to delete the Codex sign-in volume.

## n8n integration

1. Join n8n container to the gateway public network, or attach its Compose service to it:

```yaml
services:
  n8n:
    networks:
      - default
      - codex-gateway-public
networks:
  codex-gateway-public:
    external: true
```

2. In n8n use an **HTTP Request** node:
   - POST `http://api:8000/v1/chat/completions` (the `api` name is resolvable on the shared network)
   - Header `Authorization: Bearer YOUR_GATEWAY_API_KEY` (use n8n credentials rather than hardcoding secret)
   - JSON body `{"model":"codex-default","messages":[{"role":"user","content":"Hi"}],"stream":false}`
   - Request timeout greater than `CHAT_WAIT_SECONDS`, e.g. 130000 ms.
3. Optionally import `examples/n8n-workflow.json`. **Important:** this sample references `$env.GATEWAY_API_KEY`; your n8n installation must supply it and allow env access in expressions. Many n8n installations restrict `$env`. Replace the sample Authorization header with n8n Header Auth credentials when necessary. The sample assumes Docker network connectivity to hostname `api`.
4. If n8n uses its **AI Agent / OpenAI Chat Model** node, compatibility is not guaranteed; start with HTTP Request first.

## API

- `GET /health` — dependency status, not authenticated (only localhost-published)
- `GET /v1/models` — `codex-default` alias plus the active Codex app-server model catalog (cached in Redis for five minutes)
- `POST /v1/chat/completions` — synchronous, returns OpenAI-like subset
- `POST /v1/jobs` — enqueue asynchronous task
- `GET /v1/jobs/{id}` — job status/result
- `GET /admin/overview` — job counts/last 20 jobs
- `GET /admin/jobs/{id}` — diagnostic event summaries
- `GET /admin/sessions` and `DELETE /admin/sessions/{session_id}` — list or permanently delete saved Codex sessions

Every route except `/health` requires the shared bearer key.
Model discovery runs through the worker's authenticated Codex CLI session. The list is a catalog, not a guarantee that the signed-in account can use every entry; the selected model is verified by the next Codex request.
To save a conversation, set `store_session: true` on its first `/v1/chat/completions` request. The response includes `session_id`; pass that ID on subsequent calls to continue the same Codex thread. If session history is not wanted, omit both fields and each request remains ephemeral.
Set `system_prompt` to provide Codex developer instructions separately from the user message. The Chat Tester exposes this as a dedicated field. If omitted, normal stateless requests continue to use `codex exec`.

```json
{"model":"codex-default","store_session":true,"messages":[{"role":"user","content":"ช่วยวางแผนงาน"}]}
```

Use the returned `session_id` on the next request:

```json
{"model":"codex-default","session_id":"SESSION_ID_FROM_PREVIOUS_RESPONSE","messages":[{"role":"user","content":"ขอรายละเอียดขั้นตอนแรก"}]}
```

### Asynchronous example

```bash
curl -sS http://127.0.0.1:8088/v1/jobs \
 -H 'Authorization: Bearer YOUR_GATEWAY_API_KEY' -H 'Content-Type: application/json' \
 -d '{"prompt":"Summarize REST API briefly in Thai","model":"codex-default"}'
# Then GET /v1/jobs/<id> with the same Authorization header.
```

## Configuration

| Environment | Purpose | Default |
|---|---|---|
| GATEWAY_API_KEY | Required API/UI bearer secret | none |
| DEFAULT_MODEL | Optional **exact Codex-supported** model name; blank lets CLI select default | blank |
| MAX_CONCURRENT_JOBS | Worker threads, capped at 4 | 1 |
| JOB_TIMEOUT_SECONDS | Per CLI invocation, capped at 900 | 120 |
| CHAT_WAIT_SECONDS | HTTP chat polling timeout, capped at 300 | 90 |
| JOB_TTL_SECONDS | How long status and results remain in Redis memory | 3600 |

### Data & security

- Docker named volume: `codex_auth`. It contains Codex sign-in credentials and saved session history. Never publish it; backup/restore with care. Redis has no persistent volume and persistence is disabled.
- Session history files live under `/home/node/.codex/sessions` in the worker volume. Delete sessions through the Web Console so Codex can remove the history files and metadata together.
- Redis temporarily contains queued prompt and system instruction text in memory until a worker claims the job. Job results and status are memory-only and expire automatically. Prompts are not included in API job history. The worker discards Codex event output and does not save stderr text, which may contain sensitive content. Opt-in saved sessions do persist full conversation history and instructions on disk.
- Job cancellation, startup crash recovery, multi-user isolation, rate limiting, reverse-proxy TLS, CSRF session architecture and encrypted-at-rest storage are **not implemented**.
- Do not put gateway secrets in the n8n exported workflow JSON or commit `.env`.
- If a worker crashes during a job, that job may remain `running`; inspect with the admin UI and restart/recreate for MVP testing. For production, implement leases/dead-letter queues.

## Diagnostics

```bash
docker compose ps
docker compose logs -f api worker
docker compose exec worker codex --version
docker compose exec worker codex login status
```

If the worker reports authentication, model access or usage-limit errors, verify the actual sign-in/plan. If the Codex CLI flags differ in a future version, check `codex exec --help` and adapt `worker/main.py`.

## Architecture

```text
n8n Docker --HTTP POST--> web nginx :8088 (host) / api:8000 (docker network)
                                 |
                              FastAPI
                              |      |
                                   Redis memory queue
                                     |
                                  worker
                              /           \
                codex exec (stateless)   codex app-server (saved threads)
                              \           /
                           ChatGPT sign-in session
```
