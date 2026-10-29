import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from demos.server.claude_login import ClaudeLoginCheck
from demos.server.configuration import WebConfiguration
from demos.settings import save_setup


def status(stdout, returncode=0):
    return SimpleNamespace(returncode=returncode, stdout=stdout)


class ClaudeLoginTests(unittest.TestCase):
    def test_claude_login_states(self):
        outputs = [
            (json.dumps({"loggedIn": True, "authMethod": "claude.ai"}), "logged_in"),
            (json.dumps({"loggedIn": True, "authMethod": "api_key"}), "not_subscription"),
            (json.dumps({"loggedIn": False, "authMethod": "none"}), "not_logged_in"),
            ("not json", "error"),
        ]
        with patch("shutil.which", return_value="/bin/claude"):
            for stdout, expected in outputs:
                with patch("demos.server.claude_login.subprocess.run", return_value=status(stdout)) as run:
                    self.assertEqual(ClaudeLoginCheck().check("claude")["state"], expected)
                self.assertEqual(
                    run.call_args.args[0],
                    ["/bin/claude", "--setting-sources", "", "--strict-mcp-config",
                     "auth", "status", "--json"],
                )
                self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_claude_login_ignores_api_key_in_environment(self):
        stdout = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}), \
                patch("shutil.which", return_value="/bin/claude"), \
                patch("demos.server.claude_login.subprocess.run", return_value=status(stdout)) as run:
            ClaudeLoginCheck().check("claude")
        self.assertNotIn("ANTHROPIC_API_KEY", run.call_args.kwargs["env"])

    def test_configuration_uses_selected_agent_login(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch("shutil.which", return_value="/bin/claude"), \
                patch("demos.server.claude_login.ClaudeLoginCheck.check",
                      return_value={"state": "not_subscription", "message": "not a subscription"}), \
                patch("demos.server.codex_login.CodexLoginCheck.check") as codex:
            path = Path(tmp) / "harness.json"
            providers = {role: {"provider": "claude"} for role in ("routing", "responses", "multimodal")}
            save_setup({"workspace": "./tasks", "agent": "claude", **providers}, path)
            public = WebConfiguration(path).public()
        self.assertEqual(public["agent_login"]["state"], "not_subscription")
        self.assertFalse(public["configured"])
        codex.assert_not_called()


if __name__ == "__main__":
    unittest.main()
