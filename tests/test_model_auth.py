import io
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from demos.model.server import _ServerConfig, _parse_args, create_app
from demos.model.settings import require_token
from test_model_server import omni_adapter


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


class TokenRuleTests(unittest.TestCase):
    def test_network_host_requires_token(self):
        for host, token in [("0.0.0.0", None), ("10.194.99.8", "")]:
            with self.subTest(host=host), self.assertRaises(ValueError):
                require_token(host, token)
        for host, token in [("127.0.0.1", None), ("localhost", None), ("10.194.99.8", "t")]:
            require_token(host, token)

    def test_server_refuses_network_host_without_token(self):
        with patch.dict(os.environ, {"VENUS_MODEL_TOKEN": ""}), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                _parse_args(["--host", "0.0.0.0"])
        with patch.dict(os.environ, {"VENUS_MODEL_TOKEN": "t"}):
            self.assertEqual(_parse_args(["--host", "0.0.0.0"]).token, "t")


class ServerAuthTests(unittest.TestCase):
    @contextmanager
    def client(self, token):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "tokenizer.json").write_text("{}", encoding="utf-8")
            stub = SimpleNamespace(_model_revision="r", _session=None, health=AsyncMock(return_value={}))
            with patch("demos.model.server._build_adapter", AsyncMock(return_value=stub)):
                with TestClient(create_app(_ServerConfig(model_path=tmp, token=token))) as client:
                    yield client

    def test_requests_need_the_token(self):
        with self.client("secret") as client:
            self.assertEqual(client.get("/tokenizer").status_code, 401)
            self.assertEqual(client.get("/tokenizer", headers=bearer("wrong")).status_code, 401)
            self.assertEqual(client.get("/tokenizer", headers=bearer("secret")).status_code, 200)

    def test_health_is_public_and_reports_token_check(self):
        with self.client("secret") as client:
            self.assertTrue(client.get("/healthz").json()["ready"])
            self.assertFalse(client.get("/healthz").json()["token_ok"])
            self.assertTrue(client.get("/healthz", headers=bearer("secret")).json()["token_ok"])
        with self.client(None) as client:
            self.assertTrue(client.get("/healthz").json()["token_ok"])


class HealthPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_does_not_reveal_the_session(self):
        # A session id plus incarnation is enough to pull or close someone's conversation.
        self.assertNotIn("session", await omni_adapter().health())


if __name__ == "__main__":
    unittest.main()
