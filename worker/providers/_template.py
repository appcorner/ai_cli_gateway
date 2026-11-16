"""Template for adding a new CLI provider adapter (e.g. OpenCode, Claude Code, Gemini CLI).

Checklist to onboard a new CLI:
1. Copy this file to `worker/providers/<provider_name>.py` and implement `BaseCLIProvider`.
2. Register the class in `PROVIDER_CLASSES` inside `worker/providers/registry.py`.
3. Add `<provider_name>` to `ENABLED_PROVIDERS` in `.env` / `docker-compose.yml`
   - Mode A (Unified Worker): `ENABLED_PROVIDERS=codex,agy,<provider_name>`
   - Mode B (Dedicated Worker): run a separate service with `ENABLED_PROVIDERS=<provider_name>`
4. Install the CLI in `worker/Dockerfile` (Mode A) or a dedicated `worker-<provider>/Dockerfile` (Mode B).
5. Mount an auth/state volume in `docker-compose.yml` for the CLI's credentials/session storage.
"""

from __future__ import annotations

import subprocess
import tempfile
from typing import Any

from .base import BaseCLIProvider, ExecutionRequest, ExecutionResult, ProviderCapabilities


class TemplateCLIProvider(BaseCLIProvider):
    name = "template"
    display_name = "Template CLI"
    capabilities = ProviderCapabilities(
        modes=("chat",),
        default_mode="chat",
        supports_system_prompt=True,
        supports_sessions=False,
        supports_session_history=False,
        supports_session_delete=False,
        supports_usage_quota=False,
        supports_model_discovery=True,
    )

    # List any credential env vars from other vendors that must not leak into this CLI.
    BLOCKED_ENV = frozenset({"GEMINI_API_KEY", "OPENAI_API_KEY", "CODEX_HOME"})

    def startup_init(self) -> None:
        # Optional: verify binary exists or write safe non-interactive config.
        pass

    def execute_turn(self, req: ExecutionRequest) -> ExecutionResult:
        timeout = req.timeout_seconds or self.timeout
        env = self.sanitize_env(extra_blocked=self.BLOCKED_ENV)
        with tempfile.TemporaryDirectory(prefix=f"{self.name}-job-") as workdir:
            cmd = [self.name]
            if req.model:
                cmd.extend(["--model", req.model])
            process = subprocess.run(
                cmd,
                input=req.prompt,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=env,
            )
        if process.returncode != 0:
            raise RuntimeError(f"{self.name} exited with non-zero status")
        answer = (process.stdout or "").strip()
        if not answer:
            raise RuntimeError(f"{self.name} returned no answer")
        return ExecutionResult(text=answer)

    def list_models(self) -> list[dict[str, Any]]:
        return [{"id": "default", "name": f"{self.display_name} default", "is_default": True}]
