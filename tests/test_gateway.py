import importlib.util
import os
import pathlib
import sqlite3
import shutil
import sys
import types
import unittest
import uuid
from fastapi.testclient import TestClient
from contextlib import contextmanager
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
# The provider unit tests don't need a live Redis server or its Python client.
if "redis" not in sys.modules:
    redis_stub = types.ModuleType("redis")
    redis_stub.Redis = type("Redis", (), {"from_url": classmethod(lambda cls, *_args, **_kwargs: object())})
    sys.modules["redis"] = redis_stub
sys.path.insert(0, str(ROOT / "backend"))
from app import main as api  # noqa: E402

worker_spec = importlib.util.spec_from_file_location("gateway_worker", ROOT / "worker" / "main.py")
worker = importlib.util.module_from_spec(worker_spec)
worker_spec.loader.exec_module(worker)


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
            with patch.object(worker, "agy_cli_home", return_value=str(cli_home)), \
                    patch.object(worker, "read_agy_session_index", return_value={session_id: {"id": session_id}}), \
                    patch.object(worker, "write_agy_session_index") as write_index:
                result = worker.delete_agy_session(session_id)
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
            with patch.object(worker, "agy_cli_home", return_value=str(cli_home)), \
                    patch.object(worker, "read_agy_session_index", return_value={session_id: {"id": session_id}}), \
                    patch.object(worker, "write_agy_session_index") as write_index:
                result = worker.delete_agy_session(session_id)
            self.assertTrue(result["deleted"])
            write_index.assert_called_once_with({})
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_agy_session_history_is_read_from_registry(self):
        with patch.object(worker, "read_agy_session_index", return_value={
                "conv-123": {"messages": [{"role": "user", "content": "hello"}]}}):
            history = worker.agy_session_control("history", "conv-123")
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
        with patch.object(worker.subprocess, "run", return_value=completed) as run, \
                patch.object(worker.tempfile, "TemporaryDirectory", workspace_temporary_directory):
            result = worker.run_agy_turn("question", "gemini-4-argon-high", "conv-123",
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
        with patch.object(worker.subprocess, "run", return_value=completed), \
                patch.object(worker.tempfile, "TemporaryDirectory", workspace_temporary_directory):
            with self.assertRaises(RuntimeError):
                worker.run_agy_turn("question", None)

    def test_model_catalog_parses_cli_slugs(self):
        completed = type("Completed", (), {
            "stdout": "gemini-4-argon-high    Gemini 4 Argon (High)\ngemini-3.8-flash-medium Gemini 3.8 Flash (Medium)\n",
            "returncode": 0,
        })()
        with patch.object(worker.subprocess, "run", return_value=completed):
            models = worker.read_models_from_agy()
        self.assertEqual([item["id"] for item in models], ["gemini-4-argon-high", "gemini-3.8-flash-medium"])


if __name__ == "__main__":
    unittest.main()
