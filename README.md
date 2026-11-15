# AI CLI Gateway

Private Docker-based HTTP gateway for **one user**: n8n -> FastAPI -> Redis -> worker -> Codex CLI or Google Antigravity CLI (`agy`). Includes a React management console. Redis is used as a volatile queue and short-lived job store; no database is used.

## Scope and warnings

- This is a **personal prototype**, not a multi-user SaaS or production proxy. ChatGPT subscription usage is subject to eligibility, service terms, rate limits and any applicable credits. Sign-in does not provide a general-purpose unlimited OpenAI API.
- These CLIs are coding agents, not ordinary text-only inference endpoints. Codex runs with a read-only sandbox; `agy` runs headless with its terminal sandbox enabled, in a disposable working directory. Do not pass secrets or untrusted documents.
- The OpenAI-like chat endpoint is deliberately a **subset**: text messages only, no streaming, tool calls, images, embeddings, or reliable token accounting. Requests are stateless unless `store_session` or an existing `session_id` is supplied.
- API key is shared by the local browser and n8n. Only use on a trusted machine. Never publish port 8088 externally or expose the Docker public network to untrusted containers.
- Prompts are held in Redis memory while queued and removed when a worker claims the job. Redis persistence is disabled and it has no data volume, so queued jobs and job history are lost if Redis restarts.
- When session saving is enabled, Codex stores the full conversation (system instructions, prompts and answers) in the worker's persistent `codex_auth` volume. Antigravity stores its native conversation in `agy_auth`; the Gateway keeps up to 80 user/assistant transcript messages per saved conversation in `gateway_sessions.json` in the same volume. Redis is not used for persistent session history. Delete a session from the Web Console to remove its provider history and Gateway transcript.
- Saved sessions have no automatic expiration; remove them from the Dashboard when they are no longer needed.
- Results, status, and non-sensitive job metadata are kept in Redis memory for `JOB_TTL_SECONDS` (default one hour), then expire.
- The Dashboard reads Codex account rate-limit windows through the Codex app-server. `agy` account usage is not shown by this gateway.

## Prerequisites

Docker Engine/Compose v2, internet access from worker to OpenAI and Google, and an existing n8n Docker installation. For Codex use an account entitled to Codex CLI sign-in. For `agy`, authenticate the CLI in its persistent worker home or configure Gemini API key / Google Cloud ADC authentication as described below.

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
# Optional: agy sign-in if using Antigravity account authentication
docker compose exec -it worker agy
# Check API:
curl http://127.0.0.1:8088/health
# Call from host (use the actual key from .env):
curl -sS http://127.0.0.1:8088/v1/chat/completions \
  -H "Authorization: Bearer YOUR_GATEWAY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"codex-default","messages":[{"role":"user","content":"Hello in Thai"}],"stream":false}'
```

If `--device-auth` isn't allowed by the account, consult official Codex CLI sign-in instructions for your installation; do not paste or publish session tokens. `codex login` is another supported interactive sign-in route when a browser callback is available.

Open **http://127.0.0.1:8088/**, unlock with the local Gateway API key, then select Codex or Antigravity CLI in the tester. Unsaved chat context stays in browser memory and is sent with later turns. **Save conversation** returns a provider session ID that can be continued from Sessions. The manager lists and manages sessions for both providers. Named System prompt templates are saved in this browser's `localStorage`; they are not uploaded until used. The UI key stays in browser `sessionStorage` (cleared after closing the tab). This UI is **not** a separate login/account system.

### Antigravity (`agy`) authentication

The worker image installs the official Antigravity CLI. Its data is stored in the separate `agy_auth` volume at `/home/node/.gemini`.

- Account sign-in: run `docker compose exec -it worker agy` and follow the CLI's sign-in instructions. Some personal-account OAuth flows require an interactive browser/keyring available on the host; if the container cannot complete that flow, use Gemini API key or ADC auth.
- Gemini API key: set `GEMINI_API_KEY` in `.env`. The worker configures `modelProvider: gemini` in the Antigravity settings. Keep `.env` private.
- Google Cloud ADC: provision ADC credentials at `/home/node/.config/gcloud/application_default_credentials.json` in the worker (the `agy_gcloud_auth` named volume persists this directory), then set `AGY_ADC_AUTH=true`, `GOOGLE_CLOUD_PROJECT`, and `GOOGLE_CLOUD_LOCATION` in `.env`. The gateway does not mount host credentials automatically.

The headless integration uses Antigravity's documented JSON stream input/output mode so prompts go over stdin rather than process arguments; model discovery runs `agy models`. The worker pins Antigravity tool permission to `request-review`; headless requests cannot accept interactive approvals, so approval-gated tool actions may be denied. Docker may also block the Linux user namespaces required by Antigravity's OS sandbox. Treat this as a chat gateway, not an unattended file-changing agent. Saved `agy` conversation IDs are provided by Antigravity's headless CLI and persist under its home volume.

### Debugging incoming request JSON

Request-body logging is disabled by default. To inspect JSON sent by n8n, set `LOG_REQUEST_BODIES=true` in `.env`, then recreate the API container:

```powershell
docker compose up -d --force-recreate api
docker compose logs -f --tail 100 api
```

This logs bodies for `POST /v1/chat/completions` and `POST /v1/jobs` to the API container log, including prompts and system prompts. Authorization headers are not logged. Turn it off again after debugging by setting `LOG_REQUEST_BODIES=false` and recreating `api`; remove old container logs if they contain sensitive prompts.

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
- `GET /v1/models?provider=codex|agy` — provider default alias plus that CLI's model catalog (cached in Redis for five minutes); omitted provider defaults to Codex
- `POST /v1/chat/completions` — synchronous, returns OpenAI-like subset
- `POST /v1/jobs` — enqueue asynchronous task
- `GET /v1/jobs/{id}` — job status/result
- `GET /admin/overview` — job counts/last 20 jobs
- `GET /admin/jobs/{id}` — diagnostic event summaries
- `GET /admin/sessions`, `GET /admin/sessions/{session_id}`, and `DELETE /admin/sessions/{session_id}` — list and continue Codex or Antigravity sessions; Codex sessions can also be deleted

Every route except `/health` requires the shared bearer key.
Model discovery runs through the selected authenticated CLI. The list is a catalog, not a guarantee that the account can use every entry; the selected model is verified by the next request. Add `"provider":"agy"` to a chat or job request to use Antigravity. Existing clients that omit `provider` continue to use Codex.
`gateway_options.mode` selects `chat` (default) or `agent` for Codex, and `chat` (default) or `agent` (shown as Normal in the tester) for agy. Codex Chat mode uses a clean `CODEX_HOME`, disables user/project extensions, bundled Skills and web search, sets reasoning/verbosity low, and limits stateless request history. Agy Chat mode disables headless slash-command handling; core instructions and global/plugin rules can still add context. Agy Normal mode enables normal slash-command and skill behavior. The Chat Tester shows the provider-specific mode selector.

To save a conversation, set `store_session: true` on its first `/v1/chat/completions` request. The response includes `session_id`; pass that ID on subsequent calls to continue the same Codex thread. Also pass the same `gateway_options.mode` on subsequent calls. If session history is not wanted, omit both fields and each request remains ephemeral.
Set `system_prompt` to provide Codex developer instructions separately from the user message. The Chat Tester exposes this as a dedicated field. If omitted, normal stateless requests continue to use `codex exec`.

```json
{"provider":"agy","model":"agy-default","store_session":true,"messages":[{"role":"user","content":"Help plan this work"}]}
```

Use the returned `session_id` on the next request:

```json
{"provider":"agy","model":"agy-default","session_id":"CONVERSATION_ID_FROM_PREVIOUS_RESPONSE","messages":[{"role":"user","content":"Explain the first step"}]}
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
| DEFAULT_MODEL | Optional exact Codex-supported model name; blank lets CLI select default | blank |
| AGY_DEFAULT_MODEL | Optional exact `agy` model slug; blank lets CLI select default | blank |
| GEMINI_API_KEY | Optional key for Antigravity's Gemini API-key auth mode | blank |
| AGY_ADC_AUTH | Enable Antigravity Application Default Credentials auth | false |
| GOOGLE_CLOUD_PROJECT | Google Cloud project for ADC auth | blank |
| GOOGLE_CLOUD_LOCATION | Google Cloud location for ADC auth | blank |
| MAX_CONCURRENT_JOBS | Worker threads, capped at 4 | 1 |
| JOB_TIMEOUT_SECONDS | Per CLI invocation, capped at 900 | 120 |
| CHAT_WAIT_SECONDS | HTTP chat polling timeout, capped at 300 | 90 |
| JOB_TTL_SECONDS | How long status and results remain in Redis memory | 3600 |

### Data & security

- Docker named volumes: `codex_auth` contains Codex sign-in and session history; `agy_auth` contains Antigravity CLI settings and account data; `agy_gcloud_auth` contains ADC credentials. Protect all of them. Redis has no persistent volume and persistence is disabled.
- Agent session history files live under `/home/node/.codex/sessions`; Chat session files live under `/home/node/.codex/chat-mode/sessions` in the same worker volume. Chat mode shares only the `auth.json` sign-in file with Agent mode. Delete sessions through the Web Console so Codex can remove the history files and metadata together.
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
docker compose exec worker agy --version
docker compose exec worker agy models
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
                              /           \\
             Codex CLI (exec/app-server)   Antigravity CLI (headless)
                       |                            |
             ChatGPT sign-in              Google/ADC/API-key auth
```
