from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_BLOCKED_ENV = frozenset({
    "GATEWAY_API_KEY",
    "DATABASE_URL",
    "POSTGRES_PASSWORD",
    "REDIS_URL",
})
_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _parse_tools_payload(raw_text: str) -> list[dict[str, Any]]:
    data = json.loads(raw_text)
    if isinstance(data, dict):
        items = data.get("tools", [])
    elif isinstance(data, list):
        items = data
    else:
        return []
    if not isinstance(items, list):
        return []

    parsed: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        url = item.get("url")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(url, str) or not url.strip():
            continue
        method = str(item.get("method") or "POST").strip().upper()
        if method not in _ALLOWED_METHODS:
            method = "POST"
        raw_headers = item.get("headers")
        headers = (
            {str(k): str(v) for k, v in raw_headers.items()}
            if isinstance(raw_headers, dict)
            else {}
        )
        schema = item.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        try:
            timeout = max(1.0, min(float(item.get("timeout", 30)), 120.0))
        except (TypeError, ValueError):
            timeout = 30.0
        parsed.append({
            "name": name.strip(),
            "url": url.strip(),
            "description": str(item.get("description") or ""),
            "method": method,
            "headers": headers,
            "inputSchema": schema,
            "timeout": timeout,
        })
    return parsed


def load_webhook_tools() -> list[dict[str, Any]]:
    """Load tool definitions from N8N_WEBHOOK_TOOLS_FILE and/or N8N_WEBHOOK_TOOLS_JSON."""
    by_name: dict[str, dict[str, Any]] = {}
    tools_file = os.environ.get("N8N_WEBHOOK_TOOLS_FILE", "").strip()
    if tools_file and os.path.isfile(tools_file):
        try:
            with open(tools_file, encoding="utf-8") as f:
                for tool in _parse_tools_payload(f.read()):
                    by_name[tool["name"]] = tool
        except (OSError, json.JSONDecodeError) as exc:
            print(f"mcp_n8n_bridge: failed to read {tools_file}: {exc}", file=sys.stderr, flush=True)

    tools_json = os.environ.get("N8N_WEBHOOK_TOOLS_JSON", "").strip()
    if tools_json:
        try:
            for tool in _parse_tools_payload(tools_json):
                by_name[tool["name"]] = tool
        except json.JSONDecodeError as exc:
            print(f"mcp_n8n_bridge: invalid N8N_WEBHOOK_TOOLS_JSON: {exc}", file=sys.stderr, flush=True)

    return list(by_name.values())


def _resolve_url(raw_url: str) -> str:
    url = raw_url.strip()
    if url.startswith("/"):
        base = os.environ.get("N8N_WEBHOOK_BASE_URL", "").strip().rstrip("/")
        if not base:
            raise ValueError("N8N_WEBHOOK_BASE_URL must be set for relative webhook URLs")
        url = f"{base}{url}"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Unsupported webhook URL: {url}")
    return url


def _interpolate_env(value: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        if var_name in _BLOCKED_ENV:
            return ""
        return os.environ.get(var_name, "")

    return _ENV_REF_PATTERN.sub(_replace, value)


def _build_headers(tool_headers: dict[str, str] | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if tool_headers:
        for key, value in tool_headers.items():
            headers[key] = _interpolate_env(value)

    token = os.environ.get("N8N_WEBHOOK_TOKEN", "").strip()
    if token:
        auth_header = os.environ.get("N8N_WEBHOOK_AUTH_HEADER", "Authorization").strip() or "Authorization"
        if not any(k.lower() == auth_header.lower() for k in headers):
            if auth_header.lower() == "authorization" and not token.lower().startswith("bearer "):
                headers[auth_header] = f"Bearer {token}"
            else:
                headers[auth_header] = token
    return headers


def invoke_webhook_tool(
    tool: dict[str, Any],
    arguments: dict[str, Any],
    opener: Any = urllib.request.urlopen,
) -> tuple[str, bool]:
    """Execute one HTTP webhook call and return `(body_text, is_error)`."""
    try:
        url = _resolve_url(tool["url"])
        headers = _build_headers(tool.get("headers"))
        method = tool.get("method", "POST")
        args = arguments if isinstance(arguments, dict) else {}

        if method == "GET":
            query_items = {
                k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
                for k, v in args.items()
                if v is not None
            }
            if query_items:
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}{urllib.parse.urlencode(query_items)}"
            data = None
        else:
            data = json.dumps(args, ensure_ascii=False).encode("utf-8")
            if not any(k.lower() == "content-type" for k in headers):
                headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with opener(request, timeout=tool.get("timeout", 30.0)) as response:
            raw_body = response.read(100_000)
        return raw_body.decode("utf-8", errors="replace"), False
    except urllib.error.HTTPError as exc:
        err_body = exc.read(20_000).decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
        return f"HTTP {exc.code}: {err_body}", True
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}", True


def handle_rpc_message(
    msg: dict[str, Any],
    *,
    opener: Any = urllib.request.urlopen,
) -> dict[str, Any] | None:
    """Process one JSON-RPC 2.0 MCP message and return a response or None for notifications."""
    if "id" not in msg:
        return None
    req_id = msg["id"]
    method = msg.get("method")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

    if method == "initialize":
        protocol = params.get("protocolVersion") or "2024-11-05"
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": protocol,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "gateway-n8n-webhooks", "version": "1.0.0"},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    is_chat_mode = os.environ.get("GATEWAY_CLI_MODE", "agent").lower() == "chat"

    if method == "tools/list":
        if is_chat_mode:
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": []}}
        tools = [
            {
                "name": tool["name"],
                "description": tool["description"],
                "inputSchema": tool["inputSchema"],
            }
            for tool in load_webhook_tools()
        ]
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}

    if method == "tools/call":
        if is_chat_mode:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": "n8n webhook tools are disabled in chat mode"}],
                    "isError": True,
                },
            }
        tool_name = params.get("name")
        tools = {tool["name"]: tool for tool in load_webhook_tools()}
        tool = tools.get(tool_name)
        if tool is None:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Unknown webhook tool: {tool_name}"}],
                    "isError": True,
                },
            }
        raw_args = params.get("arguments")
        if raw_args is None:
            arguments: dict[str, Any] = {}
        elif isinstance(raw_args, dict):
            arguments = raw_args
        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": "Tool arguments must be a JSON object"}],
                    "isError": True,
                },
            }
        text, is_error = invoke_webhook_tool(tool, arguments, opener=opener)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": is_error,
            },
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> None:
    """Run the newline-delimited JSON-RPC MCP stdio loop."""
    for line in sys.stdin:
        raw = line.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
            sys.stdout.write(json.dumps(err_resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue
        if not isinstance(msg, dict):
            continue
        resp = handle_rpc_message(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
