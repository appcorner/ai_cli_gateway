import importlib.util
import os
import pathlib
import sys
import types
import unittest
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
        self.assertEqual(request.provider, "codex")
        self.assertEqual(request.model, "codex-default")

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
        self.assertEqual(response["data"][1]["owned_by"], "agy")
        catalog.assert_called_once_with("agy")

    def test_models_http_route_accepts_agy_provider(self):
        client = TestClient(api.app)
        with patch.object(api, "API_KEY", "test-key"), \
                patch.object(api, "get_model_catalog", return_value=[{"id": "gemini-4-argon-high", "name": "Gemini 4 Argon"}]):
            response = client.get("/v1/models?provider=agy", headers={"Authorization": "Bearer test-key"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["id"], "agy-default")
        self.assertEqual(response.json()["data"][1]["id"], "gemini-4-argon-high")


class AgyWorkerTests(unittest.TestCase):
    def test_headless_turn_uses_json_model_and_resume_id(self):
        completed = type("Completed", (), {
            "stdout": '{"event":"init","conversation_id":"conv-123"}\n{"event":"result","result":{"status":"SUCCESS","response":"  answer\\n","conversation_id":"conv-123"}}',
            "returncode": 0,
        })()
        with patch.object(worker.subprocess, "run", return_value=completed) as run, \
                patch.object(worker.tempfile, "TemporaryDirectory", workspace_temporary_directory):
            result = worker.run_agy_turn("question", "gemini-4-argon-high", "conv-123",
                                         "Be concise", persist=True)
        self.assertEqual(result, ("answer", "conv-123"))
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
