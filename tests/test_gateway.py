import importlib.util
import io
import json
import os
import pathlib
import sqlite3
import shutil
import sys
import tempfile
import types
import unittest
import uuid
from fastapi.testclient import TestClient
from contextlib import contextmanager, redirect_stdout
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
# The provider unit tests don't need a live Redis server or its Python client.
if "redis" not in sys.modules:
    redis_stub = types.ModuleType("redis")
    redis_stub.Redis = type("Redis", (), {"from_url": classmethod(lambda cls, *_args, **_kwargs: object())})
    sys.modules["redis"] = redis_stub
sys.path.insert(0, str(ROOT / "backend"))
from app import common as common_module  # noqa: E402
from app import main as api  # noqa: E402
from app import tool_calling  # noqa: E402

worker_spec = importlib.util.spec_from_file_location("gateway_worker", ROOT / "worker" / "main.py")
worker = importlib.util.module_from_spec(worker_spec)
worker_spec.loader.exec_module(worker)

from providers import agy as agy_provider  # noqa: E402
from providers import codex as codex_provider  # noqa: E402
from providers import mcp_n8n_bridge, mcp_sync  # noqa: E402
from providers import opencode as opencode_provider  # noqa: E402
from providers import build_registry, parse_enabled_providers  # noqa: E402


class MockTemporaryDirectory:
    def __init__(self, prefix):
        self.path = str(ROOT / "tests" / f"{prefix}mock")

    def __enter__(self):
        return self.path

    def __exit__(self, *_args):
        return False


def workspace_temporary_directory(prefix):
    return MockTemporaryDirectory(prefix)


class ProviderApiTests(unittest.TestCase):
    def test_omitted_provider_keeps_codex_default(self):
        request = api.ChatRequest(messages=[{"role": "user", "content": "hello"}])
        self.assertIsNone(request.provider)
        self.assertEqual(request.model, "codex-default")

    def test_provider_is_inferred_from_qualified_model_id(self):
        self.assertEqual(api.resolve_provider("agy:gemini-4-argon-high"), "agy")
        with patch.object(api, "get_model_catalog", return_value=[{"id": "gemini-4-argon-high"}]) as catalog:
            self.assertEqual(api.normalized_model("agy:gemini-4-argon-high"), "gemini-4-argon-high")
        catalog.assert_called_once_with("agy")
        self.assertEqual(api.resolve_provider("codex:o4-mini"), "codex")

    def test_model_prefix_must_match_explicit_provider(self):
        with self.assertRaises(api.HTTPException):
            api.resolve_provider("agy:gemini-4-argon-high", "codex")

    def test_agy_default_alias_and_configured_model(self):
        with patch.object(api, "AGY_MODEL_DEFAULT", "gemini-3.8-flash-medium"):
            self.assertEqual(api.normalized_model("agy-default", "agy"), "gemini-3.8-flash-medium")
            self.assertEqual(api.normalized_model("codex-default", "agy"), "gemini-3.8-flash-medium")
        with patch.object(api, "get_model_catalog", return_value=[{"id": "gemini-4-argon-high"}]):
            self.assertEqual(api.normalized_model("gemini-4-argon-high", "agy"), "gemini-4-argon-high")

    def test_model_catalog_is_scoped_to_requested_provider(self):
        with patch.object(api, "get_model_catalog", return_value=[{"id": "gemini-4-argon-high", "name": "Gemini 4 Argon"}]) as catalog:
            response = api.models(provider="agy")
        self.assertEqual(response["data"][0]["id"], "agy-default")
        self.assertEqual(response["data"][1]["id"], "agy:gemini-4-argon-high")
        self.assertEqual(response["data"][1]["owned_by"], "agy")
        catalog.assert_called_once_with("agy")

    def test_model_catalog_without_provider_combines_both_providers(self):
        def catalog(provider):
            return [{"id": f"{provider}-sample", "name": f"{provider} sample"}]
        with patch.object(api, "get_model_catalog", side_effect=catalog) as get_catalog:
            response = api.models()
        self.assertEqual([item["id"] for item in response["data"]], [
            "codex-default", "codex:codex-sample", "agy-default", "agy:agy-sample",
        ])
        self.assertEqual(get_catalog.call_count, 2)

    def test_models_http_route_accepts_agy_provider(self):
        client = TestClient(api.app)
        with patch.object(api, "API_KEY", "test-key"), \
                patch.object(api, "get_model_catalog", return_value=[{"id": "gemini-4-argon-high", "name": "Gemini 4 Argon"}]):
            response = client.get("/v1/models?provider=agy", headers={"Authorization": "Bearer test-key"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["id"], "agy-default")
        self.assertEqual(response.json()["data"][1]["id"], "agy:gemini-4-argon-high")


class AgyWorkerTests(unittest.TestCase):
    def test_agy_delete_removes_native_session_and_registry_entry(self):
        session_id = "123e4567-e89b-42d3-a456-426614174000"
        home = ROOT / "tests" / f"agy-delete-fixture-{uuid.uuid4().hex}"
        try:
            cli_home = home / ".gemini" / "antigravity-cli"
            conversations = cli_home / "conversations"
            conversations.mkdir(parents=True)
            session_db = conversations / f"{session_id}.db"
            sqlite3.connect(session_db).close()
            summary_db = cli_home / "conversation_summaries.db"
            with sqlite3.connect(summary_db) as connection:
                connection.execute("CREATE TABLE conversation_summaries (conversation_id TEXT)")
                connection.execute("INSERT INTO conversation_summaries VALUES (?)", (session_id,))
            with patch.object(agy_provider, "agy_cli_home", return_value=str(cli_home)), \
                    patch.object(agy_provider._SESSION_STORE, "read_index", return_value={session_id: {"id": session_id}}), \
                    patch.object(agy_provider._SESSION_STORE, "write_index") as write_index:
                result = agy_provider.delete_agy_session(session_id)
            self.assertTrue(result["deleted"])
            self.assertFalse(session_db.exists())
            with sqlite3.connect(summary_db) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()[0], 0)
            write_index.assert_called_once_with({})
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_agy_delete_removes_registry_entry_when_db_missing(self):
        session_id = "123e4567-e89b-42d3-a456-426614174000"
        home = ROOT / "tests" / f"agy-delete-fixture-{uuid.uuid4().hex}"
        try:
            cli_home = home / ".gemini" / "antigravity-cli"
            cli_home.mkdir(parents=True)
            with patch.object(agy_provider, "agy_cli_home", return_value=str(cli_home)), \
                    patch.object(agy_provider._SESSION_STORE, "read_index", return_value={session_id: {"id": session_id}}), \
                    patch.object(agy_provider._SESSION_STORE, "write_index") as write_index:
                result = agy_provider.delete_agy_session(session_id)
            self.assertTrue(result["deleted"])
            write_index.assert_called_once_with({})
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_agy_session_history_is_read_from_registry(self):
        with patch.object(agy_provider._SESSION_STORE, "read_index", return_value={
                "conv-123": {"messages": [{"role": "user", "content": "hello"}]}}):
            history = agy_provider.agy_session_control("history", "conv-123")
        self.assertEqual(history["provider"], "agy")
        self.assertEqual(history["messages"][0]["content"], "hello")

    def test_sessions_route_combines_providers(self):
        with patch.object(api, "API_KEY", "test-key"), \
                patch.object(api, "session_control", side_effect=[
                    [{"id": "codex-1", "mode": "chat"}], [],
                    [{"id": "agy-1", "provider": "agy"}],
                ]):
            response = TestClient(api.app).get("/admin/sessions", headers={"Authorization": "Bearer test-key"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["provider"] for item in response.json()["sessions"]], ["codex", "agy"])

    def test_delete_session_route_passes_provider(self):
        with patch.object(api, "API_KEY", "test-key"), patch.object(api, "session_control") as control:
            response = TestClient(api.app).delete(
                "/admin/sessions/123e4567-e89b-42d3-a456-426614174000?provider=agy",
                headers={"Authorization": "Bearer test-key"},
            )
        self.assertEqual(response.status_code, 200)
        control.assert_called_once_with("delete", "123e4567-e89b-42d3-a456-426614174000", "agent", provider="agy")

    def test_headless_turn_uses_json_model_and_resume_id(self):
        completed = type("Completed", (), {
            "stdout": '{"event":"init","conversation_id":"conv-123"}\n{"event":"result","result":{"status":"SUCCESS","response":"  answer\\n","conversation_id":"conv-123","usage":{"input_tokens":12,"output_tokens":4,"thinking_tokens":2,"cache_read_tokens":3,"total_tokens":18}}}',
            "returncode": 0,
        })()
        with patch.object(agy_provider.subprocess, "run", return_value=completed) as run, \
                patch.object(agy_provider.tempfile, "TemporaryDirectory", workspace_temporary_directory):
            result = agy_provider.run_agy_turn("question", "gemini-4-argon-high", "conv-123",
                                               "Be concise", persist=True)
        self.assertEqual(result, ("answer", "conv-123", {
            "input_tokens": 12, "output_tokens": 4, "thinking_tokens": 2,
            "cache_read_tokens": 3, "total_tokens": 18,
        }))
        args, kwargs = run.call_args
        self.assertEqual(args[0][:5], ["agy", "--input-format", "stream-json", "--output-format", "stream-json"])
        self.assertNotIn("question", args[0])
        self.assertIn('"content": "System instructions:\\nBe concise\\n\\nUser request:\\nquestion"', kwargs["input"])
        self.assertIn("--model", args[0])
        self.assertIn("--conversation", args[0])
        self.assertTrue(args[0][-2:] == ["--conversation", "conv-123"])
        self.assertTrue(pathlib.Path(kwargs["cwd"]).name.startswith("agy-job-"))
        self.assertTrue(kwargs["timeout"] > 0)

    def test_headless_turn_rejects_cli_failure(self):
        completed = type("Completed", (), {"stdout": '{"event":"result","result":{"status":"ERROR","response":""}}', "returncode": 1})()
        with patch.object(agy_provider.subprocess, "run", return_value=completed), \
                patch.object(agy_provider.tempfile, "TemporaryDirectory", workspace_temporary_directory):
            with self.assertRaises(RuntimeError):
                agy_provider.run_agy_turn("question", None)

    def test_model_catalog_parses_cli_slugs(self):
        completed = type("Completed", (), {
            "stdout": "gemini-4-argon-high    Gemini 4 Argon (High)\ngemini-3.8-flash-medium Gemini 3.8 Flash (Medium)\n",
            "returncode": 0,
        })()
        with patch.object(agy_provider.subprocess, "run", return_value=completed):
            models = agy_provider.read_models_from_agy()
        self.assertEqual([item["id"] for item in models], ["gemini-4-argon-high", "gemini-3.8-flash-medium"])


class ProviderRegistryTests(unittest.TestCase):
    def test_parse_enabled_providers_supports_mode_a_and_mode_b(self):
        self.assertEqual(parse_enabled_providers(""), ["codex", "agy", "opencode"])
        self.assertEqual(parse_enabled_providers("codex, agy"), ["codex", "agy"])
        self.assertEqual(parse_enabled_providers("opencode"), ["opencode"])
        with self.assertRaises(ValueError):
            parse_enabled_providers("codex,unknown_cli")

    def test_build_registry_returns_only_enabled_providers(self):
        registry = build_registry(redis_client=object(), timeout_seconds=90, enabled_names=["agy"])
        self.assertEqual(list(registry.keys()), ["agy"])
        self.assertTrue(registry["agy"].capabilities.supports_sessions)

    def test_worker_queue_keys_support_mode_a_and_mode_b(self):
        with patch.object(worker, "REGISTRY", {"codex": object(), "agy": object()}):
            self.assertEqual(worker._job_queue_keys(), ["gateway:jobs:codex", "gateway:jobs:agy"])
        with patch.object(worker, "REGISTRY", {"opencode": object()}):
            self.assertEqual(worker._job_queue_keys(), ["gateway:jobs:opencode"])

    def test_dynamic_provider_routing_and_providers_endpoint(self):
        custom_registry = {
            "codex": {"name": "codex", "display_name": "Codex CLI", "capabilities": {"default_mode": "chat"}},
            "opencode": {"name": "opencode", "display_name": "OpenCode CLI", "capabilities": {"default_mode": "chat"}},
        }
        client = TestClient(api.app)
        with patch.object(api, "API_KEY", "test-key"), \
                patch.object(api, "get_registered_providers", return_value=custom_registry):
            resp = client.get("/v1/providers", headers={"Authorization": "Bearer test-key"})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual([item["id"] for item in resp.json()["data"]], ["codex", "opencode"])
            self.assertEqual(api.resolve_provider("opencode:claude-sonnet"), "opencode")
            self.assertEqual(api.resolve_provider("opencode-default"), "opencode")
            with self.assertRaises(api.HTTPException):
                api.validate_provider("unknown_cli", custom_registry)


class OpenCodeWorkerTests(unittest.TestCase):
    def test_model_catalog_parses_provider_slash_model(self):
        completed = type("Completed", (), {
            "stdout": "opencode-go/glm-5.3\nopencode/deepseek-v4.1-flash\nopenai/gpt-4o\ninvalid-line\n",
            "returncode": 0,
        })()
        with patch.object(opencode_provider.subprocess, "run", return_value=completed):
            all_models = opencode_provider.read_models_from_opencode(model_filter="all")
            go_models = opencode_provider.read_models_from_opencode(model_filter="go")
            zen_models = opencode_provider.read_models_from_opencode(model_filter="zen")
            multi_models = opencode_provider.read_models_from_opencode(model_filter="go,openai")
        self.assertEqual(
            [item["id"] for item in all_models],
            ["opencode-go/glm-5.3", "opencode/deepseek-v4.1-flash", "openai/gpt-4o"],
        )
        self.assertEqual([item["id"] for item in go_models], ["opencode-go/glm-5.3"])
        self.assertEqual([item["id"] for item in zen_models], ["opencode/deepseek-v4.1-flash"])
        self.assertEqual([item["id"] for item in multi_models], ["opencode-go/glm-5.3", "openai/gpt-4o"])
        self.assertEqual(
            opencode_provider.resolve_opencode_model("deepseek-v4.1-flash", "go"),
            "opencode-go/deepseek-v4.1-flash",
        )
        self.assertEqual(
            opencode_provider.resolve_opencode_model("opencode/deepseek-v4.1-flash", "go"),
            "opencode/deepseek-v4.1-flash",
        )

    def test_run_opencode_turn_parses_jsonl_and_passes_prompt(self):
        stdout = "\n".join([
            '{"type":"step_start","sessionID":"ses_abc123"}',
            '{"type":"text","sessionID":"ses_abc123","part":{"type":"text","text":"Hello "}}',
            '{"type":"text","sessionID":"ses_abc123","part":{"type":"text","text":"world"}}',
            '{"type":"step_finish","sessionID":"ses_abc123","part":{"type":"step-finish","tokens":{"input":10,"output":5,"reasoning":1,"cache":{"read":2}}}}',
        ])
        completed = type("Completed", (), {"stdout": stdout, "returncode": 0})()
        with patch.object(opencode_provider.subprocess, "run", return_value=completed) as run, \
                patch.object(opencode_provider.tempfile, "TemporaryDirectory", workspace_temporary_directory):
            answer, session_id, usage = opencode_provider.run_opencode_turn(
                "hello prompt", "anthropic/claude-sonnet-4-5", "ses_prev", "Be brief", persist=True
            )
        self.assertEqual(answer, "Hello world")
        self.assertEqual(session_id, "ses_abc123")
        self.assertEqual(usage, {"input_tokens": 10, "output_tokens": 5, "thinking_tokens": 1, "cache_read_tokens": 2})
        cmd = run.call_args[0][0]
        try_kwargs = run.call_args[1]
        self.assertEqual(cmd[:5], ["opencode", "run", "--format", "json", "--pure"])
        self.assertIn("OPENCODE_CONFIG_CONTENT", try_kwargs["env"])
        self.assertIn("--model", cmd)
        self.assertIn("--session", cmd)
        self.assertEqual(cmd[-2], "--")
        self.assertIn("System instructions:\nBe brief", cmd[-1])
        self.assertIn("User request:\nhello prompt", cmd[-1])

        with patch.object(opencode_provider.subprocess, "run", return_value=completed) as run_agent, \
                patch.object(opencode_provider.tempfile, "TemporaryDirectory", workspace_temporary_directory):
            opencode_provider.run_opencode_turn("hello prompt", None, mode="agent")
        agent_cmd = run_agent.call_args[0][0]
        self.assertNotIn("--pure", agent_cmd)
        self.assertNotIn("OPENCODE_CONFIG_CONTENT", run_agent.call_args[1]["env"])

    def test_delete_opencode_session_calls_cli_and_cleans_store(self):
        completed = type("Completed", (), {"returncode": 0})()
        with patch.object(opencode_provider.subprocess, "run", return_value=completed) as run, \
                patch.object(opencode_provider._SESSION_STORE, "has_session", return_value=True), \
                patch.object(opencode_provider._SESSION_STORE, "remove_session") as remove_session:
            result = opencode_provider.delete_opencode_session("ses_abc123")
        self.assertTrue(result["deleted"])
        run.assert_called_once()
        self.assertEqual(run.call_args[0][0], ["opencode", "session", "delete", "ses_abc123"])
        remove_session.assert_called_once_with("ses_abc123")


class _InMemoryRedis:
    def __init__(self):
        self.hashes = {}

    def pipeline(self, transaction=True):
        redis = self

        class _Pipe:
            def __init__(self):
                self._ops = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def hgetall(self, key):
                self._ops.append(lambda: dict(redis.hashes.get(key, {})))

            def hset(self, key, name=None, value=None, mapping=None):
                def _op():
                    redis.hset(key, name=name, value=value, mapping=mapping)
                    return 1
                self._ops.append(_op)

            def hdel(self, key, *fields):
                def _op():
                    target = redis.hashes.get(key, {})
                    removed = 0
                    for field in fields:
                        if field in target:
                            del target[field]
                            removed += 1
                    return removed
                self._ops.append(_op)

            def execute(self):
                return [op() for op in self._ops]

        return _Pipe()

    def exists(self, key):
        return int(key in self.hashes)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, name=None, value=None, mapping=None):
        bucket = self.hashes.setdefault(key, {})
        if name is not None:
            bucket[name] = str(value)
        if mapping:
            for k, v in mapping.items():
                bucket[k] = str(v)

    def expire(self, _key, _ttl):
        return True

    def delete(self, *keys):
        for key in keys:
            self.hashes.pop(key, None)


class WorkerTraceTests(unittest.TestCase):
    def test_agy_empty_answer_includes_diagnostics_and_skips_session_record(self):
        stdout = "\n".join([
            '{"event":"init","session_id":"conv-empty"}',
            '{"event":"step_update","step_type":"tool_call","tool_name":"read_file","state":"waiting"}',
            json.dumps({
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "finished_reason": "interrupted",
                    "response": "   ",
                    "conversation_id": "conv-empty",
                    "usage": {"input_tokens": 120, "output_tokens": 0, "thinking_tokens": 45, "total_tokens": 165},
                },
            }),
        ])
        completed = type("Completed", (), {"stdout": stdout, "stderr": "Permission required\n", "returncode": 0})()
        provider = agy_provider.AgyProvider(redis_client=None, timeout_seconds=120)
        trace_events = []
        req = worker.ExecutionRequest(
            prompt="secret user prompt",
            model="gemini-4-hydrogen-medium",
            mode="chat",
            store_session=True,
            job_id="job-empty-1",
            trace_events=trace_events,
        )
        with patch.object(agy_provider.subprocess, "run", return_value=completed), \
                patch.object(agy_provider.tempfile, "TemporaryDirectory", workspace_temporary_directory), \
                patch.object(agy_provider._SESSION_STORE, "record_turn") as record_turn:
            with self.assertRaises(RuntimeError) as ctx:
                provider.execute_turn(req)

        message = str(ctx.exception)
        self.assertIn("agy returned no answer", message)
        self.assertIn("rc=0", message)
        self.assertIn("status=SUCCESS", message)
        self.assertIn("finished_reason=interrupted", message)
        self.assertIn("step_update", message)
        self.assertIn("stderr=Permission required", message)
        record_turn.assert_not_called()
        self.assertEqual([ev["stage"] for ev in trace_events], ["cli_start", "cli_exit"])
        self.assertNotIn("secret user prompt", json.dumps(trace_events))

    def test_agy_missing_result_and_error_status_include_diagnostics_without_stderr_attr(self):
        missing_result = type("Completed", (), {
            "stdout": '{"event":"init","session_id":"s1"}\n',
            "returncode": 1,
        })()
        with patch.object(agy_provider.subprocess, "run", return_value=missing_result), \
                patch.object(agy_provider.tempfile, "TemporaryDirectory", workspace_temporary_directory):
            with self.assertRaises(RuntimeError) as missing_ctx:
                agy_provider.run_agy_turn("question", None)
        missing_msg = str(missing_ctx.exception)
        self.assertIn("agy did not return a result event", missing_msg)
        self.assertIn("rc=1", missing_msg)
        self.assertIn("events=[init]", missing_msg)
        self.assertIn("stderr=none", missing_msg)

        error_result = type("Completed", (), {
            "stdout": '{"event":"result","result":{"status":"ERROR","response":"","error":"quota exceeded"}}\n',
            "returncode": 1,
        })()
        with patch.object(agy_provider.subprocess, "run", return_value=error_result), \
                patch.object(agy_provider.tempfile, "TemporaryDirectory", workspace_temporary_directory):
            with self.assertRaises(RuntimeError) as error_ctx:
                agy_provider.run_agy_turn("question", None)
        error_msg = str(error_ctx.exception)
        self.assertIn("agy did not complete successfully", error_msg)
        self.assertIn("rc=1", error_msg)
        self.assertIn("status=ERROR", error_msg)
        self.assertIn("err=quota exceeded", error_msg)
        self.assertIn("stderr=none", error_msg)

    def test_worker_trace_prints_stdout_lines_and_stderr(self):
        stdout = "\n".join([
            '{"event":"init","conversation_id":"conv-t1"}',
            '{"event":"result","result":{"status":"SUCCESS","response":"ok","conversation_id":"conv-t1"}}',
        ])
        completed = type("Completed", (), {"stdout": stdout, "stderr": "debug stderr line\n", "returncode": 0})()
        buf = io.StringIO()
        with patch.dict(os.environ, {"WORKER_TRACE": "true"}), \
                patch.object(agy_provider.subprocess, "run", return_value=completed), \
                patch.object(agy_provider.tempfile, "TemporaryDirectory", workspace_temporary_directory), \
                redirect_stdout(buf):
            answer, _conv_id, _usage = agy_provider.run_agy_turn(
                "question", "gemini-4-argon-high", job_id="job-trace-1"
            )
        self.assertEqual(answer, "ok")
        logged = buf.getvalue()
        self.assertIn("trace job=job-trace-1 provider=agy stage=cli_start", logged)
        self.assertIn("stage=stdout_line idx=1", logged)
        self.assertIn("stage=stdout_line idx=2", logged)
        self.assertIn("stage=stderr text=debug stderr line", logged)
        self.assertIn("trace job=job-trace-1 provider=agy stage=cli_exit", logged)

    def test_worker_execute_logs_traceback_persists_events_and_admin_job_exposes_them(self):
        fake_redis = _InMemoryRedis()
        fake_redis.hset("gateway:job:job-err-1", mapping={
            "id": "job-err-1",
            "status": "queued",
            "prompt": "secret user prompt",
            "provider": "agy",
            "model": "gemini-4-hydrogen-medium",
            "mode": "chat",
            "store_session": "0",
            "created_at": "2026-11-19T10:40:00+00:00",
        })
        completed = type("Completed", (), {
            "stdout": (
                '{"event":"step_update","step_type":"tool_call","tool_name":"read_file"}\n'
                '{"event":"result","result":{"status":"SUCCESS","finished_reason":"interrupted","response":""}}\n'
            ),
            "stderr": "Waiting on approval\n",
            "returncode": 0,
        })()
        buf = io.StringIO()
        with patch.object(worker, "redis", fake_redis), \
                patch.object(agy_provider.subprocess, "run", return_value=completed), \
                patch.object(agy_provider.tempfile, "TemporaryDirectory", workspace_temporary_directory), \
                redirect_stdout(buf):
            worker.execute("job-err-1")

        logged = buf.getvalue()
        self.assertIn("job=job-err-1 provider=agy model=gemini-4-hydrogen-medium mode=chat error_type=RuntimeError detail=agy returned no answer", logged)
        self.assertIn("finished_reason=interrupted", logged)
        self.assertIn("job=job-err-1 provider=agy traceback:", logged)
        self.assertIn("RuntimeError: agy returned no answer", logged)

        stored = fake_redis.hgetall("gateway:job:job-err-1")
        self.assertEqual(stored["status"], "failed")
        events = json.loads(stored["events"])
        self.assertEqual([ev["stage"] for ev in events], ["claimed", "cli_start", "cli_exit", "error"])
        self.assertNotIn("secret user prompt", stored["events"])

        with patch.object(common_module, "redis_client", fake_redis), \
                patch.object(api, "API_KEY", "test-key"):
            fetched = common_module.fetch("job-err-1")
            self.assertEqual([ev["stage"] for ev in fetched["events"]], ["claimed", "cli_start", "cli_exit", "error"])
            self.assertNotIn("events", common_module.public_job(fetched))

            client = TestClient(api.app)
            resp = client.get("/admin/jobs/job-err-1", headers={"Authorization": "Bearer test-key"})
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual([ev["stage"] for ev in body["events"]], ["claimed", "cli_start", "cli_exit", "error"])
            self.assertEqual(body["status"], "failed")

            missing = client.get("/admin/jobs/missing-job", headers={"Authorization": "Bearer test-key"})
            self.assertEqual(missing.status_code, 404)


class SharedWorkspaceAndAgentTests(unittest.TestCase):
    def test_agy_shared_workspace_and_mode_permissions_and_env(self):
        completed = type("Completed", (), {
            "stdout": '{"event":"result","result":{"status":"SUCCESS","response":"ok","conversation_id":"conv-1"}}',
            "stderr": "",
            "returncode": 0,
        })()
        with tempfile.TemporaryDirectory() as shared_dir, patch.dict(
            os.environ,
            {
                "CLI_WORKSPACE_DIR": shared_dir,
                "AGENT_ALLOW_NETWORK": "true",
                "N8N_WEBHOOK_TOKEN": "secret-hook-token",
                "GATEWAY_API_KEY": "secret-gw-key",
            },
        ):
            with patch.object(agy_provider.subprocess, "run", return_value=completed) as run_chat:
                agy_provider.run_agy_turn("hi", "gemini-4-argon-high", mode="chat")
            chat_cmd = run_chat.call_args[0][0]
            chat_kwargs = run_chat.call_args[1]
            self.assertEqual(chat_kwargs["cwd"], shared_dir)
            self.assertIn("--sandbox", chat_cmd)
            self.assertIn("--disable-slash-commands", chat_cmd)
            self.assertNotIn("--dangerously-skip-permissions", chat_cmd)
            self.assertNotIn("N8N_WEBHOOK_TOKEN", chat_kwargs["env"])
            self.assertNotIn("GATEWAY_API_KEY", chat_kwargs["env"])
            self.assertEqual(chat_kwargs["env"]["GATEWAY_CLI_MODE"], "chat")

            with patch.object(agy_provider.subprocess, "run", return_value=completed) as run_agent:
                agy_provider.run_agy_turn("hi", "gemini-4-argon-high", mode="agent")
            agent_cmd = run_agent.call_args[0][0]
            agent_kwargs = run_agent.call_args[1]
            self.assertEqual(agent_kwargs["cwd"], shared_dir)
            self.assertIn("--dangerously-skip-permissions", agent_cmd)
            self.assertNotIn("--sandbox", agent_cmd)
            self.assertNotIn("--disable-slash-commands", agent_cmd)
            self.assertEqual(agent_kwargs["env"]["N8N_WEBHOOK_TOKEN"], "secret-hook-token")
            self.assertNotIn("GATEWAY_API_KEY", agent_kwargs["env"])
            self.assertEqual(agent_kwargs["env"]["GATEWAY_CLI_MODE"], "agent")

    def test_codex_shared_workspace_ephemeral_and_session_and_sandbox_modes(self):
        provider = codex_provider.CodexProvider(redis_client=None, timeout_seconds=60)
        with tempfile.TemporaryDirectory() as shared_dir, \
                tempfile.TemporaryDirectory() as codex_home, \
                patch.object(codex_provider, "CODEX_HOME", codex_home), \
                patch.object(codex_provider, "CHAT_CODEX_HOME", os.path.join(codex_home, "chat-mode")), \
                patch.dict(
                    os.environ,
                    {
                        "CLI_WORKSPACE_DIR": shared_dir,
                        "AGENT_ALLOW_NETWORK": "true",
                        "N8N_WEBHOOK_TOKEN": "secret-hook-token",
                    },
                ):
            def fake_run(cmd, **_kwargs):
                out_idx = cmd.index("--output-last-message") + 1
                with open(cmd[out_idx], "w", encoding="utf-8") as f:
                    f.write("codex-ok")
                return type("Completed", (), {"stdout": "", "stderr": "", "returncode": 0})()

            with patch.object(codex_provider.subprocess, "run", side_effect=fake_run) as run_chat:
                res_chat = provider._run_ephemeral_exec("hello", None, "chat", 30)
            self.assertEqual(res_chat.text, "codex-ok")
            chat_cmd = run_chat.call_args[0][0]
            chat_kwargs = run_chat.call_args[1]
            self.assertEqual(chat_kwargs["cwd"], shared_dir)
            self.assertEqual(chat_cmd[chat_cmd.index("--sandbox") + 1], "read-only")
            self.assertNotIn("N8N_WEBHOOK_TOKEN", chat_kwargs["env"])
            outfile_chat = chat_cmd[chat_cmd.index("--output-last-message") + 1]
            self.assertNotEqual(os.path.dirname(outfile_chat), shared_dir)

            with patch.object(codex_provider.subprocess, "run", side_effect=fake_run) as run_agent:
                res_agent = provider._run_ephemeral_exec("hello", None, "agent", 30)
            self.assertEqual(res_agent.text, "codex-ok")
            agent_cmd = run_agent.call_args[0][0]
            agent_kwargs = run_agent.call_args[1]
            self.assertEqual(agent_kwargs["cwd"], shared_dir)
            self.assertEqual(agent_cmd[agent_cmd.index("--sandbox") + 1], "workspace-write")
            self.assertIn("sandbox_workspace_write.network_access=true", agent_cmd)
            self.assertEqual(agent_kwargs["env"]["N8N_WEBHOOK_TOKEN"], "secret-hook-token")

            # Verify _run_session_turn passes shared cwd and workspace-write in agent mode
            rpc_calls = []

            def fake_send_rpc(_proc, _inc, _rid, method, params, _notif=None):
                rpc_calls.append((method, params))
                if method == "thread/start":
                    return {"thread": {"id": "th-1"}}
                if method == "turn/start":
                    return {"turn": {"id": "tu-1"}}
                return {}

            incoming = types.SimpleNamespace(
                get=lambda timeout=None: json.dumps({
                    "method": "turn/completed",
                    "params": {"threadId": "th-1", "turn": {"id": "tu-1", "status": "completed"}},
                })
            )
            fake_redis = types.SimpleNamespace(
                set=lambda *_a, **_kw: True,
                delete=lambda *_a: None,
                eval=lambda *_a: 1,
            )
            provider.redis = fake_redis
            with patch.object(codex_provider, "initialized_app_server", return_value=(object(), incoming)), \
                    patch.object(codex_provider, "send_rpc", side_effect=fake_send_rpc), \
                    patch.object(codex_provider, "_terminate_process"):
                _text, sid = provider._run_session_turn("hi", None, mode="agent", store_session=False)
            self.assertEqual(sid, "th-1")
            self.assertEqual(rpc_calls[0][1]["cwd"], shared_dir)
            self.assertEqual(rpc_calls[0][1]["sandbox"], "workspace-write")

    def test_opencode_shared_workspace_and_mode_env(self):
        stdout = "\n".join([
            '{"type":"text","sessionID":"ses_1","part":{"type":"text","text":"ok"}}',
        ])
        completed = type("Completed", (), {"stdout": stdout, "stderr": "", "returncode": 0})()
        with tempfile.TemporaryDirectory() as shared_dir, patch.dict(
            os.environ,
            {
                "CLI_WORKSPACE_DIR": shared_dir,
                "N8N_WEBHOOK_TOKEN": "secret-hook-token",
            },
        ):
            with patch.object(opencode_provider.subprocess, "run", return_value=completed) as run_chat:
                opencode_provider.run_opencode_turn("hi", None, mode="chat")
            chat_kwargs = run_chat.call_args[1]
            self.assertEqual(chat_kwargs["cwd"], shared_dir)
            self.assertIn("OPENCODE_CONFIG_CONTENT", chat_kwargs["env"])
            self.assertNotIn("N8N_WEBHOOK_TOKEN", chat_kwargs["env"])

            with patch.object(opencode_provider.subprocess, "run", return_value=completed) as run_agent:
                opencode_provider.run_opencode_turn("hi", None, mode="agent")
            agent_kwargs = run_agent.call_args[1]
            self.assertEqual(agent_kwargs["cwd"], shared_dir)
            self.assertNotIn("OPENCODE_CONFIG_CONTENT", agent_kwargs["env"])
            self.assertEqual(agent_kwargs["env"]["N8N_WEBHOOK_TOKEN"], "secret-hook-token")
            self.assertEqual(agent_kwargs["env"]["GATEWAY_CLI_MODE"], "agent")


class McpSyncAndBridgeTests(unittest.TestCase):
    def test_sync_configs_across_all_three_clis_and_preserve_user_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agy_home = os.path.join(tmpdir, "agy_home")
            agy_cfg_path = os.path.join(agy_home, ".gemini", "config", "mcp_config.json")
            os.makedirs(os.path.dirname(agy_cfg_path), exist_ok=True)
            with open(agy_cfg_path, "w", encoding="utf-8") as f:
                json.dump({"mcpServers": {"user_server": {"command": "echo"}}}, f)

            codex_home = os.path.join(tmpdir, "codex_home")
            os.makedirs(codex_home, exist_ok=True)
            codex_cfg_path = os.path.join(codex_home, "config.toml")
            with open(codex_cfg_path, "w", encoding="utf-8") as f:
                f.write('model = "gpt-5"\n\n[mcp_servers.custom]\ncommand = "echo"\n')

            opencode_cfg_dir = os.path.join(tmpdir, "opencode_cfg")
            os.makedirs(opencode_cfg_dir, exist_ok=True)
            opencode_cfg_path = os.path.join(opencode_cfg_dir, "opencode.json")
            with open(opencode_cfg_path, "w", encoding="utf-8") as f:
                json.dump({"mcp": {"trail": {"type": "local", "command": ["trail", "mcp"]}}}, f)

            with patch.dict(
                os.environ,
                {
                    "N8N_MCP_URL": "http://n8n:5678/mcp/abc",
                    "N8N_MCP_BEARER_TOKEN": "tok-123",
                    "N8N_WEBHOOK_TOOLS_JSON": '[{"name":"mem","url":"/webhook/mem"}]',
                    "N8N_WEBHOOK_BASE_URL": "http://n8n:5678",
                    "AGENT_ALLOW_NETWORK": "true",
                },
            ):
                mcp_sync.sync_agy_mcp_config(agy_home)
                mcp_sync.sync_codex_mcp_config(codex_home)
                mcp_sync.sync_opencode_mcp_config(opencode_cfg_dir)

            with open(agy_cfg_path, encoding="utf-8") as f:
                agy_data = json.load(f)
            self.assertIn("user_server", agy_data["mcpServers"])
            self.assertEqual(agy_data["mcpServers"]["n8n"]["serverUrl"], "http://n8n:5678/mcp/abc")
            self.assertNotIn("url", agy_data["mcpServers"]["n8n"])
            self.assertEqual(agy_data["mcpServers"]["n8n"]["headers"]["Authorization"], "Bearer tok-123")
            self.assertIn("n8n_webhooks", agy_data["mcpServers"])

            with open(codex_cfg_path, encoding="utf-8") as f:
                codex_text = f.read()
            self.assertIn('model = "gpt-5"', codex_text)
            self.assertIn("[mcp_servers.custom]", codex_text)
            self.assertIn("[mcp_servers.n8n]", codex_text)
            self.assertIn('url = "http://n8n:5678/mcp/abc"', codex_text)
            self.assertIn("[mcp_servers.n8n_webhooks]", codex_text)

            with open(opencode_cfg_path, encoding="utf-8") as f:
                opencode_data = json.load(f)
            self.assertIn("trail", opencode_data["mcp"])
            self.assertEqual(opencode_data["mcp"]["n8n"]["type"], "remote")
            self.assertEqual(opencode_data["mcp"]["n8n"]["url"], "http://n8n:5678/mcp/abc")
            self.assertEqual(opencode_data["mcp"]["n8n_webhooks"]["type"], "local")
            self.assertEqual(opencode_data["permission"], {"*": "allow"})

            # Clearing env vars removes managed entries while preserving user entries
            with patch.dict(
                os.environ,
                {"N8N_MCP_URL": "", "N8N_WEBHOOK_TOOLS_JSON": "", "N8N_WEBHOOK_TOOLS_FILE": ""},
            ):
                mcp_sync.sync_agy_mcp_config(agy_home)
                mcp_sync.sync_codex_mcp_config(codex_home)
                mcp_sync.sync_opencode_mcp_config(opencode_cfg_dir)

            with open(agy_cfg_path, encoding="utf-8") as f:
                agy_after = json.load(f)
            self.assertIn("user_server", agy_after["mcpServers"])
            self.assertNotIn("n8n", agy_after["mcpServers"])
            self.assertNotIn("n8n_webhooks", agy_after["mcpServers"])

            with open(codex_cfg_path, encoding="utf-8") as f:
                codex_after = f.read()
            self.assertIn("[mcp_servers.custom]", codex_after)
            self.assertNotIn("[mcp_servers.n8n]", codex_after)

            with open(opencode_cfg_path, encoding="utf-8") as f:
                opencode_after = json.load(f)
            self.assertIn("trail", opencode_after["mcp"])
            self.assertNotIn("n8n", opencode_after["mcp"])

    def test_mcp_n8n_bridge_rpc_lifecycle_and_mode_guard(self):
        tools_payload = json.dumps({
            "tools": [
                {
                    "name": "search_long_term_memory",
                    "description": "Search memory",
                    "method": "POST",
                    "url": "/webhook/memory",
                    "inputSchema": {"type": "object", "properties": {"input": {"type": "string"}}},
                }
            ]
        })
        captured_requests = []

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b'{"matches":["item-1"]}'

        def fake_opener(req, timeout=30):
            captured_requests.append((req, timeout))
            return _FakeResp()

        with patch.dict(
            os.environ,
            {
                "N8N_WEBHOOK_TOOLS_JSON": tools_payload,
                "N8N_WEBHOOK_BASE_URL": "http://n8n:5678",
                "N8N_WEBHOOK_TOKEN": "hook-secret",
                "GATEWAY_CLI_MODE": "agent",
            },
        ):
            init_resp = mcp_n8n_bridge.handle_rpc_message({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            })
            self.assertEqual(init_resp["result"]["serverInfo"]["name"], "gateway-n8n-webhooks")

            notif = mcp_n8n_bridge.handle_rpc_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.assertIsNone(notif)

            list_resp = mcp_n8n_bridge.handle_rpc_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            self.assertEqual([t["name"] for t in list_resp["result"]["tools"]], ["search_long_term_memory"])

            call_resp = mcp_n8n_bridge.handle_rpc_message(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "search_long_term_memory", "arguments": {"input": "mind"}},
                },
                opener=fake_opener,
            )
            self.assertFalse(call_resp["result"]["isError"])
            self.assertEqual(call_resp["result"]["content"][0]["text"], '{"matches":["item-1"]}')
            sent_req, _timeout = captured_requests[0]
            self.assertEqual(sent_req.full_url, "http://n8n:5678/webhook/memory")
            self.assertEqual(sent_req.get_header("Authorization"), "Bearer hook-secret")
            self.assertEqual(json.loads(sent_req.data.decode("utf-8")), {"input": "mind"})

        # In chat mode, bridge blocks tools/list and tools/call defensively
        with patch.dict(os.environ, {"N8N_WEBHOOK_TOOLS_JSON": tools_payload, "GATEWAY_CLI_MODE": "chat"}):
            chat_list = mcp_n8n_bridge.handle_rpc_message({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
            self.assertEqual(chat_list["result"]["tools"], [])
            chat_call = mcp_n8n_bridge.handle_rpc_message({
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "search_long_term_memory", "arguments": {"input": "mind"}},
            })
            self.assertTrue(chat_call["result"]["isError"])


class ClientToolCallingTests(unittest.TestCase):
    def test_resolve_client_tool_mode_matrix(self):
        tools = [{"name": "search_long_term_memory", "description": "", "parameters": {}}]
        self.assertTrue(tool_calling.resolve_client_tool_mode("chat", tools, "auto", None, "auto"))
        self.assertFalse(tool_calling.resolve_client_tool_mode("agent", tools, "auto", None, "auto"))
        self.assertTrue(tool_calling.resolve_client_tool_mode("agent", tools, "auto", "client", "auto"))
        self.assertFalse(tool_calling.resolve_client_tool_mode("chat", tools, "auto", "worker", "auto"))
        self.assertFalse(tool_calling.resolve_client_tool_mode("chat", tools, "auto", "off", "auto"))
        self.assertFalse(tool_calling.resolve_client_tool_mode("chat", tools, "none", "client", "auto"))
        self.assertFalse(tool_calling.resolve_client_tool_mode("chat", [], "auto", "client", "auto"))

    def test_chat_completions_client_tool_call_roundtrip_and_non_tool_json_safety(self):
        client = TestClient(api.app)
        sample_tools = [{
            "type": "function",
            "function": {
                "name": "search_long_term_memory",
                "description": "Search saved memory",
                "parameters": {"type": "object", "properties": {"input": {"type": "string"}}},
            },
        }]
        enqueued_calls = []
        fetch_results = []

        def fake_enqueue(prompt, model, session_id=None, store_session=False, system_prompt=None, mode="chat", provider="codex"):
            enqueued_calls.append({"prompt": prompt, "system_prompt": system_prompt, "mode": mode})
            return f"job-{len(enqueued_calls)}"

        def fake_fetch(_job_id):
            return fetch_results.pop(0)

        with patch.object(api, "API_KEY", "test-key"), \
                patch.object(api, "resolve_model_request", return_value=("agy", "gemini-4-hydrogen-medium")), \
                patch.object(api, "enqueue", side_effect=fake_enqueue), \
                patch.object(api, "fetch", side_effect=fake_fetch):
            # Turn 1: model requests client tool call via <gateway_tool_calls>
            fetch_results.append({
                "id": "job-1",
                "status": "completed",
                "result": '<gateway_tool_calls>{"tool_calls":[{"name":"search_long_term_memory","arguments":{"input":"mind_master"}}]}</gateway_tool_calls>',
                "usage": {"total_tokens": 50},
            })
            resp1 = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-key"},
                json={
                    "model": "agy:gemini-4-hydrogen-medium",
                    "tools": sample_tools,
                    "gateway_options": {"mode": "agent", "tool_execution": "client"},
                    "messages": [{"role": "user", "content": "จำเรื่อง mind_master ได้ไหม"}],
                },
            )
            self.assertEqual(resp1.status_code, 200)
            body1 = resp1.json()
            self.assertIn("[Client-Executed Tools]", enqueued_calls[0]["system_prompt"])
            self.assertEqual(body1["choices"][0]["finish_reason"], "tool_calls")
            self.assertIsNone(body1["choices"][0]["message"]["content"])
            tool_calls = body1["choices"][0]["message"]["tool_calls"]
            self.assertEqual(len(tool_calls), 1)
            self.assertEqual(tool_calls[0]["function"]["name"], "search_long_term_memory")
            self.assertEqual(json.loads(tool_calls[0]["function"]["arguments"]), {"input": "mind_master"})

            # Turn 2: client returns assistant tool_calls + role=tool result
            fetch_results.append({
                "id": "job-2",
                "status": "completed",
                "result": "จำได้แล้วค่ะ อยู่ตรงนี้ค่ะ",
            })
            resp2 = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-key"},
                json={
                    "model": "agy:gemini-4-hydrogen-medium",
                    "tools": sample_tools,
                    "gateway_options": {"mode": "chat"},
                    "messages": [
                        {"role": "user", "content": "จำเรื่อง mind_master ได้ไหม"},
                        {"role": "assistant", "content": None, "tool_calls": tool_calls},
                        {"role": "tool", "tool_call_id": tool_calls[0]["id"], "name": "search_long_term_memory", "content": "found: mind_master.jpg"},
                    ],
                },
            )
            self.assertEqual(resp2.status_code, 200)
            body2 = resp2.json()
            self.assertEqual(body2["choices"][0]["finish_reason"], "stop")
            self.assertEqual(body2["choices"][0]["message"]["content"], "จำได้แล้วค่ะ อยู่ตรงนี้ค่ะ")
            self.assertIn('<turn role="tool"', enqueued_calls[1]["prompt"])
            self.assertIn("found: mind_master.jpg", enqueued_calls[1]["prompt"])

            # Non-tool JSON (like n8n proactive heartbeat JSON) must stay normal content
            proactive_json = '{\n  "reasoning": "wait",\n  "should_speak": false,\n  "message_content": ""\n}'
            fetch_results.append({"id": "job-3", "status": "completed", "result": proactive_json})
            resp3 = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-key"},
                json={
                    "model": "agy:gemini-4-hydrogen-medium",
                    "tools": sample_tools,
                    "messages": [{"role": "user", "content": "heartbeat"}],
                },
            )
            self.assertEqual(resp3.status_code, 200)
            self.assertEqual(resp3.json()["choices"][0]["finish_reason"], "stop")
            self.assertEqual(resp3.json()["choices"][0]["message"]["content"], proactive_json)

            # Undeclared tool name must NOT produce tool_calls
            fetch_results.append({
                "id": "job-4",
                "status": "completed",
                "result": '<gateway_tool_calls>{"tool_calls":[{"name":"unknown_tool","arguments":{}}]}</gateway_tool_calls>',
            })
            resp4 = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-key"},
                json={
                    "model": "agy:gemini-4-hydrogen-medium",
                    "tools": sample_tools,
                    "messages": [{"role": "user", "content": "test"}],
                },
            )
            self.assertEqual(resp4.status_code, 200)
            self.assertEqual(resp4.json()["choices"][0]["finish_reason"], "stop")


if __name__ == "__main__":
    unittest.main()
