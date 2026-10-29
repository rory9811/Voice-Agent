import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_fake import fake_claude, pid_alive, read_log
from harness.agents import AgentRequest, ClaudeAgentProvider, GeneralAgentConfig
from harness.factory import build_harness
from harness.jobs.faults import classify_fault
from harness.llm.claude import ClaudeCliError
from harness.settings import HarnessSetup, template

INIT = {"type": "system", "subtype": "init", "apiKeySource": "none"}
DONE = {"outcome": "completed", "full_result": "done", "artifacts": [],
        "assumptions": [], "unresolved": []}


def result(value):
    return {"type": "result", "subtype": "success", "is_error": False,
            "structured_output": value, "result": json.dumps(value)}


def assistant(*blocks):
    return {"type": "assistant", "message": {"content": list(blocks)}}


def tool_result(tool_id, error=False):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_id, "is_error": error, "content": "ok"}]}}


WORK = [
    INIT,
    assistant({"type": "text", "text": "正在写文件"}),
    assistant({"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "echo x"}}),
    tool_result("t1"),
]


def argument(argv, name):
    return argv[argv.index(name) + 1]


class ClaudeAgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.events = []

    def provider(self, script):
        executable, log, env = fake_claude(self.tmp.name, script)
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        config = GeneralAgentConfig(agent="claude", command=(executable,),
                                    workspace=str(self.root), execution_timeout_s=30)
        provider = ClaudeAgentProvider(config)
        self.addAsyncCleanup(provider.aclose)
        return provider, log

    async def emit(self, event):
        self.events.append(event)

    def request(self, work_id="w1", parent=None):
        return AgentRequest("s", work_id, "写一个文件", {"language": "zh"}, parent_work_id=parent)

    async def test_agent_returns_result_with_artifact(self):
        provider, log = self.provider([
            *WORK, {"write": {"path": "out.txt", "text": "x"}},
            result({**DONE, "artifacts": ["out.txt"]})])
        outcome = await provider.run(self.request(), self.emit)
        self.assertEqual(outcome.outcome, "completed")
        self.assertTrue(outcome.artifacts[0].path.endswith("/artifacts/out.txt"))
        self.assertIn("/results/", outcome.artifacts[0].path)
        argv = read_log(log)[0]["argv"]
        self.assertIn("--session-id", argv)
        self.assertEqual(argument(argv, "--add-dir"), str(self.root.resolve()))
        self.assertEqual(argument(argv, "--permission-mode"), "acceptEdits")
        self.assertEqual(argument(argv, "--permission-prompts"), "none")
        self.assertIn("--json-schema", argv)
        sandbox = json.loads(argument(argv, "--settings"))["sandbox"]
        self.assertTrue(sandbox["enabled"])
        self.assertTrue(sandbox["failIfUnavailable"])
        # No per-command dangerouslyDisableSandbox escape.
        self.assertIs(sandbox["allowUnsandboxedCommands"], False)
        self.assertEqual(sandbox["network"]["allowedDomains"], ["*"])
        self.assertNotIn("--dangerously-skip-permissions", argv)
        context_files = list(self.root.glob("*/inputs/*-context.json"))
        self.assertEqual(len(context_files), 1)
        self.assertEqual(set(json.loads(context_files[0].read_text())), {"snapshot", "inputs"})

    async def test_continuation_resumes_same_session(self):
        provider, log = self.provider([*WORK, result(DONE)])
        await provider.run(self.request(), self.emit)
        await provider.run(self.request("w2", parent="w1"), self.emit)
        first, second = read_log(log)
        session = argument(first["argv"], "--session-id")
        self.assertEqual(argument(second["argv"], "--resume"), session)
        self.assertNotIn("--session-id", second["argv"])
        self.assertEqual(first["cwd"], second["cwd"])

    async def test_progress_reports_commentary_and_tools(self):
        provider, _ = self.provider([*WORK, {"sleep": 2}, result(DONE)])
        task = asyncio.create_task(provider.run(self.request(), self.emit))
        items = []
        for _ in range(100):
            await asyncio.sleep(0.05)
            try:
                items = (await provider.read_progress("s", "w1"))["items"]
            except RuntimeError:
                continue
            if any(item["type"] == "commandExecution" and item["status"] == "completed"
                   for item in items):
                break
        await task
        self.assertIn({"type": "publicCommentary", "text": "正在写文件"}, items)
        self.assertIn({"type": "commandExecution", "status": "completed"}, items)

    async def test_agent_error_result_fails_run(self):
        provider, _ = self.provider([
            INIT, {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}},
            {"type": "result", "subtype": "success", "is_error": True,
             "api_error_status": 429, "result": "Claude usage limit reached"}])
        with self.assertRaises(ClaudeCliError) as caught:
            await provider.run(self.request(), self.emit)
        self.assertEqual(classify_fault(caught.exception).code, "QUOTA_EXHAUSTED")
        with self.assertRaises(RuntimeError):
            await provider.run(self.request("w2", parent="w1"), self.emit)

    async def test_cancel_kills_process_group(self):
        provider, log = self.provider([INIT, {"spawn_sleep": 60}, {"sleep": 60}])
        task = asyncio.create_task(provider.run(self.request(), self.emit))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if log.exists() and any("child" in entry for entry in read_log(log)):
                break
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        entries = read_log(log)
        self.assertFalse(pid_alive(entries[0]["pid"]))
        self.assertFalse(pid_alive(next(e["child"] for e in entries if "child" in e)))
        cancelled = [e.details for e in self.events if e.kind == "cancelled"]
        self.assertEqual(len(cancelled), 1)
        self.assertIs(cancelled[0]["background_terminals_cleaned"], True)
        self.assertIs(cancelled[0]["process_stopped"], True)

    async def test_cancel_stops_detached_tool_groups(self):
        provider, log = self.provider([INIT, {"spawn_sleep": 60, "detached": True}, {"sleep": 60}])
        task = asyncio.create_task(provider.run(self.request(), self.emit))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if log.exists() and any("child" in entry for entry in read_log(log)):
                break
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        child = next(e["child"] for e in read_log(log) if "child" in e)
        self.assertFalse(pid_alive(child))
        cancelled = [e.details for e in self.events if e.kind == "cancelled"]
        self.assertIs(cancelled[0]["background_terminals_cleaned"], True)

    def test_build_harness_uses_claude_agent(self):
        data = template()
        data["agent"] = "claude"
        for role in ("routing", "responses", "multimodal"):
            data[role]["provider"] = "claude"
        setup = HarnessSetup(data, Path("/tmp/config.json"))
        with patch("harness.factory.task_backends", return_value=(object(), object())), \
                patch("harness.factory.VenusOmniAgentHarness") as constructor:
            build_harness(setup)
        self.assertIsInstance(constructor.call_args.kwargs["general_agent"], ClaudeAgentProvider)


if __name__ == "__main__":
    unittest.main()
