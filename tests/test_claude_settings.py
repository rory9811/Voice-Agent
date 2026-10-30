import unittest
from pathlib import Path
from unittest.mock import patch

from harness.agents.config import GeneralAgentConfig
from harness.config import ModelCallConfig
from harness.settings import HarnessSetup, template


def claude_data(**changes):
    data = template()
    data["agent"] = "claude"
    data["claude"] = {"command": ["/opt/claude"], "model": "sonnet", "effort": "max"}
    for role in ("routing", "responses", "multimodal"):
        data[role]["provider"] = "claude"
    data.update(changes)
    return data


class ClaudeSettingsTests(unittest.TestCase):
    def test_claude_agent_uses_claude_section(self):
        data = claude_data()
        data["codex"]["execution_timeout_s"] = 123
        setup = HarnessSetup(data, Path("/tmp/config.json"))
        self.assertEqual(setup.general.agent, "claude")
        self.assertEqual(setup.general.command, ("/opt/claude",))
        self.assertEqual(setup.general.model, "sonnet")
        self.assertEqual(setup.general.effort, "max")
        self.assertEqual(setup.general.execution_timeout_s, 123)

    def test_role_provider_must_match_agent(self):
        data = claude_data()
        data["responses"]["provider"] = "codex"
        with self.assertRaises(ValueError):
            HarnessSetup(data, "/tmp/config.json")
        data = template()
        data["multimodal"]["provider"] = "claude"
        with self.assertRaises(ValueError):
            HarnessSetup(data, "/tmp/config.json")
        data = claude_data()
        data["routing"]["provider"] = "gemini"
        data["gemini"]["api_key"] = "k"
        self.assertEqual(HarnessSetup(data, "/tmp/config.json").routing.provider, "gemini")

    def test_effort_sets_follow_agent(self):
        data = claude_data()
        data["claude"]["effort"] = "minimal"
        with self.assertRaises(ValueError):
            HarnessSetup(data, "/tmp/config.json")
        data = template()
        data["codex"]["effort"] = "max"
        with self.assertRaises(ValueError):
            HarnessSetup(data, "/tmp/config.json")
        self.assertEqual(ModelCallConfig(provider="gemini", effort="max").effort, "max")

    def test_claude_agent_rejects_read_only(self):
        with self.assertRaises(ValueError):
            GeneralAgentConfig(agent="claude", command=("claude",), sandbox="read-only")

    def test_check_names_selected_executable(self):
        setup = HarnessSetup(claude_data(), "/tmp/config.json")
        with patch("shutil.which", return_value=None):
            problems = setup.check()["problems"]
        self.assertIn("找不到 Claude Code 程序", problems)

    def test_template_keeps_codex_default(self):
        self.assertEqual(HarnessSetup(template(), "/tmp/config.json").general.agent, "codex")
        self.assertNotIn("agent", template()["codex"])
        self.assertEqual(
            template()["claude"], {"command": ["claude"], "model": None, "effort": "low"}
        )


if __name__ == "__main__":
    unittest.main()
