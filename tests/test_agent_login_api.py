import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from demos.server.app import create_app
from harness.settings import save_setup

LOGGED_IN = {"state": "logged_in", "message": "ok"}
NOT_LOGGED_IN = {"state": "not_logged_in", "message": "log in"}


class AgentLoginApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "harness.json"
        for target, value in (
            ("demos.server.codex_login.CodexLoginCheck.check", NOT_LOGGED_IN),
            ("demos.server.claude_login.ClaudeLoginCheck.check", LOGGED_IN),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(create_app(settings_path=self.path, model_type="omni", tokenizer=None, model_server_url="http://unused"), base_url="http://localhost")

    def test_checks_an_agent_that_is_not_saved_yet(self):
        save_setup({"workspace": "./tasks", "agent": "codex"}, self.path)
        self.assertEqual(self.client.get("/api/agent-login?agent=claude").json()["state"], "logged_in")
        self.assertEqual(self.client.get("/api/agent-login?agent=codex").json()["state"], "not_logged_in")

    def test_works_before_the_first_save(self):
        self.assertFalse(self.path.exists())
        self.assertEqual(self.client.get("/api/agent-login?agent=claude").status_code, 200)
        self.assertFalse(self.path.exists())

    def test_unknown_agent_is_rejected(self):
        self.assertEqual(self.client.get("/api/agent-login?agent=gemini").status_code, 400)

    def test_checking_does_not_change_the_saved_agent(self):
        save_setup({"workspace": "./tasks", "agent": "codex"}, self.path)
        before = self.path.read_bytes()
        self.client.get("/api/agent-login?agent=claude")
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
