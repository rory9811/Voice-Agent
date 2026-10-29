import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from claude_fake import fake_claude, pid_alive, read_log
from harness.backends import task_backends
from harness.core.models import VideoFrame
from harness.jobs.faults import classify_fault
from harness.llm.backends import CodexPlannerBackend
from harness.llm.claude import ClaudeCliError, ClaudeDirect, ClaudeModelBackend, cli_env
from harness.settings import HarnessSetup, template

INIT = {"type": "system", "subtype": "init", "apiKeySource": "none", "session_id": "s"}


def result(value):
    return {"type": "result", "subtype": "success", "is_error": False,
            "structured_output": value, "result": json.dumps(value)}


def error_result(text, status=None):
    return {"type": "result", "subtype": "success", "is_error": True,
            "api_error_status": status, "result": text}


def rate_limit(**info):
    return {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", **info}}


class ClaudeBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def backend(self, script, **options):
        executable, log, env = fake_claude(self.tmp.name, script)
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        backend = ClaudeModelBackend(executable=executable, model=options.get("model"),
                                     effort="low", timeout_s=20)
        return backend, log

    def test_cli_env_drops_api_billing_sources(self):
        environ = {name: "x" for name in (
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_OAUTH_TOKEN", "HTTPS_PROXY", "PATH")}
        self.assertEqual(cli_env(environ), {"HTTPS_PROXY": "x", "PATH": "x"})

    async def test_complete_returns_structured_output(self):
        backend, log = self.backend([INIT, result({"speech": "好"})])
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}):
            raw = await backend.complete("Return speech.", {"q": 1})
        self.assertEqual(json.loads(raw), {"speech": "好"})
        call = read_log(log)[0]
        argv = call["argv"]
        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("--no-session-persistence", argv)
        self.assertEqual(argv[argv.index("--system-prompt") + 1], "Return speech.")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertIn("--strict-mcp-config", argv)
        # Every caller needs at least one field; an empty object must fail CLI validation.
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]),
                         {"type": "object", "minProperties": 1})
        self.assertNotIn("ANTHROPIC_API_KEY", call["env"])

    async def test_api_key_source_is_refused(self):
        backend, log = self.backend([{**INIT, "apiKeySource": "ANTHROPIC_API_KEY"}, {"sleep": 30}])
        with self.assertRaises(ClaudeCliError) as caught:
            await asyncio.wait_for(backend.complete("x", {}), 5)
        self.assertIn("subscription", str(caught.exception))
        self.assertEqual(classify_fault(caught.exception).code, "AUTH_FAILED")
        self.assertFalse(pid_alive(read_log(log)[0]["pid"]))

    async def test_overage_is_refused(self):
        backend, log = self.backend([INIT, rate_limit(isUsingOverage=True), {"sleep": 30}])
        with self.assertRaises(ClaudeCliError) as caught:
            await asyncio.wait_for(backend.complete("x", {}), 5)
        self.assertEqual(classify_fault(caught.exception).code, "QUOTA_EXHAUSTED")
        self.assertFalse(pid_alive(read_log(log)[0]["pid"]))

    async def test_error_result_carries_fault_codes(self):
        backend, _ = self.backend([
            INIT, rate_limit(status="rejected"),
            error_result("Claude usage limit reached", 429)])
        with self.assertRaises(ClaudeCliError) as caught:
            await backend.complete("x", {})
        self.assertIn("usage limit", str(caught.exception))
        self.assertEqual(classify_fault(caught.exception).code, "QUOTA_EXHAUSTED")
        backend, _ = self.backend([INIT, error_result("Invalid credentials", 401)])
        with self.assertRaises(ClaudeCliError) as caught:
            await backend.complete("x", {})
        self.assertEqual(classify_fault(caught.exception).code, "AUTH_FAILED")

    async def test_direct_sends_frames_as_images(self):
        backend, log = self.backend([INIT, result({"speech": "红色"})])
        request = SimpleNamespace(query="这是什么颜色", language="zh")
        context = SimpleNamespace(
            video=SimpleNamespace(frames=(VideoFrame(b"\xff\xd8jpeg", 1),)),
            snapshot=SimpleNamespace(audio_chunks=()))
        response = await ClaudeDirect(backend).execute(request, context)
        self.assertEqual(response.provider, "claude-code")
        self.assertEqual(response.text, "红色")
        message = json.loads(read_log(log)[0]["stdin"])
        images = [b for b in message["message"]["content"] if b["type"] == "image"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["source"]["media_type"], "image/jpeg")

    def test_claude_agent_builds_no_codex_backends(self):
        data = template()
        data["agent"] = "claude"
        data["routing"]["mode"] = "auto"
        for role in ("routing", "responses", "multimodal"):
            data[role]["provider"] = "claude"
        direct_and_polish, planner = task_backends(HarnessSetup(data, "/tmp/config.json"))
        self.assertIsInstance(direct_and_polish.direct, ClaudeDirect)
        backends = (direct_and_polish.direct.backend, direct_and_polish.polish.backend,
                    planner._backend, planner._router_backend)
        for backend in backends:
            self.assertIsInstance(backend, ClaudeModelBackend)
            self.assertNotIsInstance(backend, CodexPlannerBackend)


if __name__ == "__main__":
    unittest.main()
