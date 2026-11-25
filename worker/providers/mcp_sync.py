from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

from .base import agent_network_enabled

MANAGED_MCP_NAMES = ("n8n", "n8n_webhooks")
_START_MARKER = "# >>> ai-cli-gateway managed mcp start >>>"
_END_MARKER = "# <<< ai-cli-gateway managed mcp end <<<"
_MANAGED_TOML_BLOCK_RE = re.compile(
    rf"\n?{re.escape(_START_MARKER)}.*?{re.escape(_END_MARKER)}\n?",
    re.DOTALL,
)
_BRIDGE_ENV_KEYS = (
    "N8N_WEBHOOK_BASE_URL",
    "N8N_WEBHOOK_TOKEN",
    "N8N_WEBHOOK_AUTH_HEADER",
    "N8N_WEBHOOK_TOOLS_FILE",
    "N8N_WEBHOOK_TOOLS_JSON",
)


def _bridge_script_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_n8n_bridge.py")


def _build_remote_mcp_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    bearer = os.environ.get("N8N_MCP_BEARER_TOKEN", "").strip()
    if bearer:
        headers["Authorization"] = bearer if bearer.lower().startswith("bearer ") else f"Bearer {bearer}"
    custom = os.environ.get("N8N_MCP_AUTH_HEADER", "").strip()
    if custom and ":" in custom:
        name, value = custom.split(":", 1)
        if name.strip():
            headers[name.strip()] = value.strip()
    return headers


def _managed_mcp_spec() -> tuple[str, dict[str, str], bool]:
    mcp_url = os.environ.get("N8N_MCP_URL", "").strip()
    headers = _build_remote_mcp_headers() if mcp_url else {}
    webhook_enabled = bool(
        os.environ.get("N8N_WEBHOOK_TOOLS_FILE", "").strip()
        or os.environ.get("N8N_WEBHOOK_TOOLS_JSON", "").strip()
    )
    return mcp_url, headers, webhook_enabled


def _atomic_write_text(path: str, content: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp_path, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_json_dict(path: str) -> dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            parsed = json.load(f)
        return parsed if isinstance(parsed, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def sync_agy_mcp_config(home_dir: str | None = None) -> str | None:
    """Sync managed n8n MCP entries into Antigravity's `~/.gemini/config/mcp_config.json`."""
    mcp_url, headers, webhook_enabled = _managed_mcp_spec()
    root = home_dir or os.environ.get("HOME", "/home/node")
    path = os.path.join(root, ".gemini", "config", "mcp_config.json")
    has_managed = bool(mcp_url or webhook_enabled)
    if not has_managed and not os.path.isfile(path):
        return None

    data = _read_json_dict(path)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    for key in MANAGED_MCP_NAMES:
        servers.pop(key, None)

    if mcp_url:
        entry: dict[str, Any] = {"serverUrl": mcp_url}
        if headers:
            entry["headers"] = headers
        servers["n8n"] = entry
    if webhook_enabled:
        servers["n8n_webhooks"] = {
            "command": sys.executable,
            "args": [_bridge_script_path()],
        }

    data["mcpServers"] = servers
    _atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return path


def _build_codex_managed_toml(mcp_url: str, headers: dict[str, str], webhook_enabled: bool) -> str:
    if not mcp_url and not webhook_enabled:
        return ""
    lines = [_START_MARKER]
    if mcp_url:
        lines.append("[mcp_servers.n8n]")
        lines.append(f"url = {json.dumps(mcp_url)}")
        if headers:
            inline_pairs = ", ".join(f"{json.dumps(k)} = {json.dumps(v)}" for k, v in headers.items())
            lines.append(f"http_headers = {{ {inline_pairs} }}")
    if webhook_enabled:
        if mcp_url:
            lines.append("")
        lines.append("[mcp_servers.n8n_webhooks]")
        lines.append(f"command = {json.dumps(sys.executable)}")
        lines.append(f"args = [{json.dumps(_bridge_script_path())}]")
        lines.append("")
        lines.append("[mcp_servers.n8n_webhooks.env]")
        lines.append('GATEWAY_CLI_MODE = "agent"')
        for env_key in _BRIDGE_ENV_KEYS:
            val = os.environ.get(env_key, "").strip()
            if val:
                lines.append(f"{env_key} = {json.dumps(val)}")
    lines.append(_END_MARKER)
    return "\n".join(lines)


def sync_codex_mcp_config(codex_home: str | None = None) -> str | None:
    """Sync managed n8n MCP entries into Codex's `config.toml` while preserving user settings."""
    mcp_url, headers, webhook_enabled = _managed_mcp_spec()
    root = codex_home or os.environ.get("CODEX_HOME", "/home/node/.codex")
    path = os.path.join(root, "config.toml")
    has_managed = bool(mcp_url or webhook_enabled)
    if not has_managed and not os.path.isfile(path):
        return None

    existing = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            existing = f.read()

    stripped = _MANAGED_TOML_BLOCK_RE.sub("\n", existing).strip()
    managed_block = _build_codex_managed_toml(mcp_url, headers, webhook_enabled)
    if stripped and managed_block:
        updated = f"{stripped}\n\n{managed_block}\n"
    elif managed_block:
        updated = f"{managed_block}\n"
    elif stripped:
        updated = f"{stripped}\n"
    else:
        updated = ""

    if updated != existing or not os.path.isfile(path):
        _atomic_write_text(path, updated)
    return path


def sync_opencode_mcp_config(config_dir: str | None = None) -> str | None:
    """Sync managed n8n MCP entries into OpenCode's `opencode.json`."""
    mcp_url, headers, webhook_enabled = _managed_mcp_spec()
    if config_dir is None:
        xdg_config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.environ.get("HOME", "/home/node"), ".config"
        )
        config_dir = os.path.join(xdg_config, "opencode")
    path = os.path.join(config_dir, "opencode.json")
    has_managed = bool(mcp_url or webhook_enabled)
    if not has_managed and not os.path.isfile(path):
        return None

    data = _read_json_dict(path)
    mcp_section = data.get("mcp")
    if not isinstance(mcp_section, dict):
        mcp_section = {}
    for key in MANAGED_MCP_NAMES:
        mcp_section.pop(key, None)

    if mcp_url:
        entry: dict[str, Any] = {
            "type": "remote",
            "url": mcp_url,
            "enabled": True,
        }
        if headers:
            entry["headers"] = headers
        mcp_section["n8n"] = entry
    if webhook_enabled:
        mcp_section["n8n_webhooks"] = {
            "type": "local",
            "command": [sys.executable, _bridge_script_path()],
            "enabled": True,
        }

    data["mcp"] = mcp_section
    if agent_network_enabled() and "permission" not in data:
        data["permission"] = {"*": "allow"}
    _atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return path
