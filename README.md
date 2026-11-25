# AI CLI Gateway

![AI CLI Gateway architecture and features](images/AI%20CLI%20Gateway.png)

Private Docker-based HTTP gateway for **one user**: n8n -> FastAPI -> Redis -> worker -> Codex CLI or Google Antigravity CLI (`agy`). Includes a React management console. Redis is used as a volatile queue and short-lived job store; no database is used.

### Dashboard preview

![AI CLI Gateway dashboard](images/dashboard.png)

## Scope and warnings

- This is a **personal prototype**, not a multi-user SaaS or production proxy. ChatGPT subscription usage is subject to eligibility, service terms, rate limits and any applicable credits. Sign-in does not provide a general-purpose unlimited OpenAI API.
- These CLIs are coding agents, not ordinary text-only inference endpoints. Codex runs with a read-only sandbox; `agy` runs headless with its terminal sandbox enabled, in a disposable working directory. Do not pass secrets or untrusted documents.
- The OpenAI-like chat endpoint is deliberately a **subset**: text messages only, no streaming, tool calls, images, embeddings, or reliable token accounting. Requests are stateless unless `store_session` or an existing `session_id` is supplied.
- API key is shared by the local browser and n8n. Only use on a trusted machine. Never publish port 8088 externally or expose the Docker public network to untrusted containers.
- Prompts are held in Redis memory while queued and removed when a worker claims the job. Redis persistence is disabled and it has no data volume, so queued jobs and job history are lost if Redis restarts.
- When session saving is enabled, Codex stores the full conversation (system instructions, prompts and answers) in the worker's persistent `codex_auth` volume. Antigravity stores its native conversation in `agy_auth`; the Gateway keeps up to 80 user/assistant transcript messages per saved conversation in `gateway_sessions.json` in the same volume. Redis is not used for persistent session history. Delete a session from the Web Console to remove its provider history and Gateway transcript.
- Saved sessions have no automatic expiration; remove them from the Dashboard when they are no longer needed.
- Results, status, and non-sensitive job metadata are kept in Redis memory for `JOB_TTL_SECONDS` (default one hour), then expire.
- The Dashboard reads Codex rate-limit windows through the Codex app-server and Antigravity model quota groups through the CLI's `/usage` command. Antigravity quotas are shown by model group, with separate cards for each available time window.

## Prerequisites

Docker Engine/Compose v2, internet access from worker to OpenAI and Google, and an existing n8n Docker installation. For Codex use an account entitled to Codex CLI sign-in. For `agy`, sign in with a Google account; this gateway does not accept Gemini API keys or Google Cloud ADC as Antigravity authentication.

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
# Sign in to Antigravity with your Google account (see the steps below)
docker compose exec -it worker agy acp --login
# Sign in to OpenCode (select OpenCode Go, OpenCode Zen, or another provider)
docker compose exec -it worker opencode auth login
# Check API:
curl http://127.0.0.1:8088/health
# Call from host (use the actual key from .env):
curl -sS http://127.0.0.1:8088/v1/chat/completions \
  -H "Authorization: Bearer YOUR_GATEWAY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"openai-default","messages":[{"role":"user","content":"Hello in Thai"}],"stream":false}'
```

If `--device-auth` isn't allowed by the account, consult official Codex CLI sign-in instructions for your installation; do not paste or publish session tokens. `codex login` is another supported interactive sign-in route when a browser callback is available.

Open **http://127.0.0.1:8088/**, unlock with the local Gateway API key, then select Codex or Antigravity CLI in the tester. Unsaved chat context stays in browser memory and is sent with later turns. **Save conversation** returns a provider session ID that can be continued from Sessions. The manager lists and manages sessions for both providers. Named System prompt templates are saved in this browser's `localStorage`; they are not uploaded until used. The UI key stays in browser `sessionStorage` (cleared after closing the tab). This UI is **not** a separate login/account system.

### Antigravity (`agy`) authentication

The worker image installs the official Antigravity CLI. Google OAuth credentials, CLI settings, and Antigravity conversations persist in the Docker volume `ai-cli-gateway_agy_auth`, mounted at `/home/node/.gemini`. The Gateway accepts Google account sign-in only; Gemini API-key and ADC credentials are not passed to agy.

#### First-time setup (PowerShell)

1. Rebuild and start the worker so the OAuth-only configuration is active:

   ```powershell
   docker compose up -d --build worker
   ```

2. Confirm the installed CLI supports ACP login:

   ```powershell
   docker compose exec worker agy acp --help
   ```

   ACP login is available in Antigravity CLI 1.5.0 and later. If `acp` or `--login` is not recognized, rebuild the worker image without cache to install the current CLI:

   ```powershell
   docker compose build --no-cache worker
   docker compose up -d worker
   ```

3. Start Google OAuth login in terminal mode:

   ```powershell
   docker compose exec -it worker agy acp --login
   ```

4. Follow the terminal instructions: open the displayed sign-in URL on the host, sign in with the intended Google account, then paste the returned code into the worker terminal. The login command exits when authentication completes.

5. Verify the saved credentials by opening **http://127.0.0.1:8088/**, selecting **Antigravity CLI (agy)** in Chat Tester, and sending a short prompt. The credentials remain in the `agy_auth` volume for later worker restarts.

The sign-in state survives `docker compose down` and worker rebuilds because the named `agy_auth` volume is retained. Do not remove that volume when resetting containers. Google documents `agy acp --login` as terminal-based Google sign-in for environments without a browser; the command prompts for the browser code and exits after login. See the [ACP guide](https://antigravity.google/docs/ide/extensions/acp/) and the [Antigravity CLI 1.5.0 release notes](https://antigravity.google/docs/changelog?tab=extensions) for details.

For an older `.env`, remove any `GEMINI_API_KEY`, `AGY_ADC_AUTH`, `GOOGLE_CLOUD_PROJECT`, or `GOOGLE_CLOUD_LOCATION` entries. Current Compose no longer forwards these values, and worker startup removes a stale `modelProvider: gemini` setting from the agy CLI profile.

The headless integration uses Antigravity's documented JSON stream input/output mode so prompts go over stdin rather than process arguments; model discovery runs `agy models`. The worker pins Antigravity tool permission to `request-review`; headless requests cannot accept interactive approvals, so approval-gated tool actions may be denied. Docker may also block the Linux user namespaces required by Antigravity's OS sandbox. Treat this as a chat gateway, not an unattended file-changing agent. Saved `agy` conversation IDs are provided by Antigravity's headless CLI and persist under its home volume.

### OpenCode (`opencode`) authentication

The worker image installs `opencode-ai` globally. Credentials, SQLite sessions, and Gateway transcript history (`gateway_sessions.json`) persist in the Docker volumes `ai-cli-gateway_opencode_auth` (`/home/node/.local/share/opencode`) and `ai-cli-gateway_opencode_config` (`/home/node/.config/opencode`).

#### First-time setup (PowerShell)

1. Rebuild and start the worker:

   ```powershell
   docker compose up -d --build worker
   ```

2. Run interactive provider authentication inside `worker`:

   ```powershell
   docker compose exec -it worker opencode auth login
   ```

   - Select **OpenCode Go** if you have an OpenCode Go subscription (models use the `opencode-go/...` prefix).
   - Select **OpenCode** (Zen) for pay-as-you-go / free-tier models (`opencode/...`), or choose another supported provider and paste your API key.

3. Verify the available models and test a turn:

   ```powershell
   docker compose exec worker opencode models --refresh
   docker compose exec worker opencode run --format json "Reply OK"
   ```

4. *(Optional)* Restrict the model dropdown to **OpenCode Go** only and set a default model in `.env`:

   ```env
   OPENCODE_MODEL_FILTER=go
   OPENCODE_DEFAULT_MODEL=deepseek-v4.1-flash
   ```

   Apply changes and clear the 5-minute Redis model catalog cache:

   ```powershell
   docker compose up -d --build worker api
   docker compose exec redis redis-cli DEL gateway:provider:opencode:models
   ```

In **Chat** mode (`gateway_options.mode: "chat"`), the worker runs `opencode run --pure` with `OPENCODE_CONFIG_CONTENT={"permission":{"*":"deny"}}` so external plugins, tools, and skills are disabled. In **Normal** mode (`"agent"`), OpenCode runs with its standard plugins, tools, and skills enabled.

### Debugging incoming request JSON and worker traces

Request-body logging and verbose worker CLI tracing are disabled by default. To inspect JSON sent by n8n, set `LOG_REQUEST_BODIES=true` in `.env`, then recreate the API container:

```powershell
docker compose up -d --force-recreate api
docker compose logs -f --tail 100 api
```

To inspect full CLI stdout event lines, stderr output, and per-stage trace markers (`cli_start`, `stdout_line`, `stderr`, `cli_exit`) when debugging worker failures, set `WORKER_TRACE=true` in `.env` and recreate the worker container:

```powershell
docker compose up -d --force-recreate worker
docker compose logs -f --tail 100 worker
```

Even with `WORKER_TRACE=false`, safe provider failure diagnostics (CLI return code, event summary tags, token usage, stderr tail), Python tracebacks in the worker log, and per-job diagnostic `events` via `GET /admin/jobs/{id}` are always recorded without including raw prompt or answer prose. Turn `LOG_REQUEST_BODIES=false` and `WORKER_TRACE=false` back off after debugging and recreate `api` / `worker`; remove old container logs if they contain sensitive prompts or outputs.

## n8n integration

1. Join n8n container to the gateway public network, and optionally mount the shared `ai-cli-gateway_cli_workspace` volume if n8n and the CLI worker share files (such as reference images or generated outputs):

```yaml
services:
  n8n:
    volumes:
      - cli_workspace:/work
    networks:
      - default
      - codex-gateway-public
networks:
  codex-gateway-public:
    external: true
volumes:
  cli_workspace:
    external: true
    name: ai-cli-gateway_cli_workspace
```

2. In n8n use an **HTTP Request** node:
   - POST `http://api:8000/v1/chat/completions` (the `api` name is resolvable on the shared network)
   - Header `Authorization: Bearer YOUR_GATEWAY_API_KEY` (use n8n credentials rather than hardcoding secret)
   - JSON body `{"model":"openai-default","messages":[{"role":"user","content":"Hi"}],"stream":false}`
   - Request timeout greater than `CHAT_WAIT_SECONDS`, e.g. 130000 ms.
3. Optionally import `examples/n8n-workflow.json`. **Important:** this sample references `$env.GATEWAY_API_KEY`; your n8n installation must supply it and allow env access in expressions. Many n8n installations restrict `$env`. Replace the sample Authorization header with n8n Header Auth credentials when necessary. The sample assumes Docker network connectivity to hostname `api`.
4. **Shared CLI workspace & n8n tools (`agent` vs client-side `tools`)**:
   - **Shared workspace (`/work`)**: `docker-compose.yml` mounts the `ai-cli-gateway_cli_workspace` Docker volume at `/work` (`CLI_WORKSPACE_DIR=/work`). All CLIs (`agy`, `codex`, `opencode`) use `/work` as their working directory in both `chat` and `agent` modes.
   - **Worker-side n8n tools (`gateway_options.mode: "agent"`)**: Because `worker` is attached to `codex-gateway-public`, CLIs in `agent` mode can reach n8n over the Docker network in three ways (while `chat` mode remains sandboxed and strips `N8N_*` secrets):
     - **Remote MCP Server (`N8N_MCP_URL`)**: Point `N8N_MCP_URL` to your n8n MCP Server Trigger endpoint (e.g. `http://n8n:5678/mcp/<id>`), optionally with `N8N_MCP_BEARER_TOKEN` or `N8N_MCP_AUTH_HEADER`. On worker startup, `mcp_sync.py` writes the server entry into `agy` (`serverUrl`), `codex` (`config.toml`), and `opencode` (`opencode.json`).
     - **Built-in Webhook MCP Bridge (`N8N_WEBHOOK_TOOLS_FILE` / `N8N_WEBHOOK_TOOLS_JSON`)**: Copy `examples/n8n-tools.example.json` to `/work/n8n_tools.json`, set `N8N_WEBHOOK_BASE_URL=http://n8n:5678`, `N8N_WEBHOOK_TOOLS_FILE=/work/n8n_tools.json`, and optionally `N8N_WEBHOOK_TOKEN`. The worker registers `mcp_n8n_bridge.py` (`n8n_webhooks`) across all three CLIs automatically in `agent` mode.
     - **Direct `curl` / scripts**: With `AGENT_ALLOW_NETWORK=true`, `agy --dangerously-skip-permissions`, `codex --sandbox workspace-write` (with `network_access=true`), and `opencode` in `agent` mode can run scripts or `curl http://n8n:5678/webhook/...` inside `/work`.
   - **Client-side n8n AI Agent tools (`gateway_options.tool_execution`)**: When calling `POST /v1/chat/completions` with OpenAI `tools` (e.g. from an n8n AI Agent node), `gateway_options.tool_execution` (or `DEFAULT_TOOL_EXECUTION`) selects who executes tools:
     - `auto` *(default)*: uses client-side OpenAI `tool_calls` (`finish_reason: "tool_calls"`) when `mode: "chat"`, and lets the CLI execute worker/MCP tools when `mode: "agent"`.
     - `client`: always returns OpenAI `tool_calls` to the caller (even in `mode: "agent"`), and accepts subsequent `role: "assistant"` (`tool_calls`) + `role: "tool"` turns.
     - `worker` / `off`: never translates OpenAI `tools` for client-side execution.

## API

- `GET /health` — dependency status, not authenticated (only localhost-published)
- `GET /v1/models` — provider default aliases and model catalogs (cached in Redis for five minutes). Model IDs are provider-qualified as `openai:<model-id>` or `agy:<model-id>` so clients can select a model without a separate provider field. Optional `?provider=openai|agy` filters the list. Legacy `codex:` model IDs remain accepted.
- `POST /v1/chat/completions` — synchronous, returns OpenAI-like subset
- `POST /v1/jobs` — enqueue asynchronous task
- `GET /v1/jobs/{id}` — job status/result
- `GET /admin/overview` — job counts/last 20 jobs
- `GET /admin/jobs/{id}` — diagnostic event summaries
- `GET /admin/sessions`, `GET /admin/sessions/{session_id}`, and `DELETE /admin/sessions/{session_id}` — list and continue Codex or Antigravity sessions; Codex sessions can also be deleted

Every route except `/health` requires the shared bearer key.
Model discovery runs through the authenticated CLIs. The list is a catalog, not a guarantee that the account can use every entry; the selected model is verified by the next request. Selecting an ID with the `agy:` prefix automatically routes a chat or job request to Antigravity; `openai:` routes to the Codex CLI through the OpenAI-compatible gateway. The `openai-default` and `agy-default` aliases select their respective providers. If `provider` is supplied, it must match the model ID prefix. An unqualified model ID with no provider continues to route to the Codex CLI.
`gateway_options.mode` selects `chat` (default) or `agent` for Codex, and `chat` (default) or `agent` (shown as Normal in the tester) for agy. Codex Chat mode uses a clean `CODEX_HOME`, disables user/project extensions, bundled Skills and web search, sets reasoning/verbosity low, and limits stateless request history. Agy Chat mode disables headless slash-command handling; core instructions and global/plugin rules can still add context. Agy Normal mode enables normal slash-command and skill behavior. The Chat Tester shows the provider-specific mode selector.

To save a conversation, set `store_session: true` on its first `/v1/chat/completions` request. The response includes `session_id`; pass that ID on subsequent calls to continue the same Codex thread. Also pass the same `gateway_options.mode` on subsequent calls. If session history is not wanted, omit both fields and each request remains ephemeral.
Set `system_prompt` to provide Codex developer instructions separately from the user message. The Chat Tester exposes this as a dedicated field. If omitted, normal stateless requests continue to use `codex exec`.

```json
{"model":"agy-default","store_session":true,"messages":[{"role":"user","content":"Help plan this work"}]}
```

Use the returned `session_id` on the next request:

```json
{"model":"agy-default","session_id":"CONVERSATION_ID_FROM_PREVIOUS_RESPONSE","messages":[{"role":"user","content":"Explain the first step"}]}
```

### Asynchronous example

```bash
curl -sS http://127.0.0.1:8088/v1/jobs \
 -H 'Authorization: Bearer YOUR_GATEWAY_API_KEY' -H 'Content-Type: application/json' \
 -d '{"prompt":"Summarize REST API briefly in Thai","model":"openai-default"}'
# Then GET /v1/jobs/<id> with the same Authorization header.
```

## Configuration

| Environment | Purpose | Default |
|---|---|---|
| GATEWAY_API_KEY | Required API/UI bearer secret | none |
| DEFAULT_MODEL | Optional exact Codex-supported model name; blank lets CLI select default | blank |
| AGY_DEFAULT_MODEL | Optional exact `agy` model slug; blank lets CLI select default | blank |
| OPENCODE_DEFAULT_MODEL | Optional OpenCode model (`<provider>/<model>`, or model name when `OPENCODE_MODEL_FILTER` has one provider) | blank |
| OPENCODE_MODEL_FILTER | Filter OpenCode models: `all`, `go` (`opencode-go/`), `zen` (`opencode/`), or comma-separated prefixes | blank (`all`) |
| ENABLED_PROVIDERS | Comma-separated active providers on this worker (supports Mode A / Mode B) | `codex,agy,opencode` |
| MAX_CONCURRENT_JOBS | Worker threads, capped at 4 | 1 |
| JOB_TIMEOUT_SECONDS | Per CLI invocation, capped at 900 | 120 |
| CHAT_WAIT_SECONDS | HTTP chat polling timeout, capped at 300 | 90 |
| JOB_TTL_SECONDS | How long status and results remain in Redis memory | 3600 |
| WORKER_TRACE | Print full CLI stdout event lines, stderr, and stage markers to worker logs | `false` |
| CLI_WORKSPACE_DIR | Shared working directory for all CLIs (`agy`, `codex`, `opencode`) in both `chat` and `agent` modes | `/work` |
| AGENT_ALLOW_NETWORK | Allow `agent` mode CLIs to access Docker/network endpoints (e.g. n8n webhooks) | `true` |
| AGY_TOOL_PERMISSION | Default `toolPermission` written to `agy` `settings.json` on worker startup | `proceed-in-sandbox` |
| CODEX_AGENT_SANDBOX | Codex `--sandbox` profile in `agent` mode (`read-only`, `workspace-write`, `danger-full-access`) | `workspace-write` |
| N8N_MCP_URL | Optional remote n8n MCP server URL synced to all CLIs on startup | blank |
| N8N_WEBHOOK_BASE_URL | Base URL for relative entries in `N8N_WEBHOOK_TOOLS_FILE` / `N8N_WEBHOOK_TOOLS_JSON` | blank |
| N8N_WEBHOOK_TOOLS_FILE | Path to JSON file mapping n8n webhooks for the built-in `mcp_n8n_bridge.py` stdio MCP server | blank |
| DEFAULT_TOOL_EXECUTION | Default strategy for OpenAI `tools` in `/v1/chat/completions`: `auto`, `client`, `worker`, `off` | `auto` |

### Data & security

- Docker named volumes: `codex_auth` contains Codex sign-in and session history; `agy_auth` contains Antigravity Google OAuth account data, settings and conversation history. Protect both. Redis has no persistent volume and persistence is disabled. Existing `agy_gcloud_auth` volumes from older deployments are no longer mounted or used.
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
docker compose exec worker opencode --version
docker compose exec worker opencode models
```

If the worker reports authentication, model access or usage-limit errors, verify the actual sign-in/plan. If the Codex CLI flags differ in a future version, check `codex exec --help` and adapt `worker/providers/codex.py`.

## Architecture

```text
n8n Docker / Web UI --HTTP--> web nginx :8088 (host) / api:8000 (docker network)
                                     |
                                  FastAPI (dynamic provider registry)
                                  |     |
                          Redis memory bus (gateway:jobs:{provider})
                                     |
                          worker (worker/providers/)
                   /                 |                 \
  Codex CLI (exec/app-server)  Antigravity CLI    OpenCode CLI (run --format json)
            |                        |                       |
     ChatGPT sign-in        Google account OAuth     OpenCode Go / Zen / API keys
```

For full details on the extensible `BaseCLIProvider` adapter architecture, unified Redis protocol, and Mode A / Mode B worker scaling, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
