import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.agents.codex import CodexAgentProvider
from harness.agents.app_server import AppServerError
from harness.agents.config import GeneralAgentConfig
from harness.agents.contracts import AgentRequest, AgentResult
from harness.agents.media import should_recover_media


def result(outcome="failed", text="图片生成工具不可用，缺少 API 凭证", artifacts=()):
    return dict(outcome=outcome, full_result=text, artifacts=list(artifacts),
                assumptions=[], unresolved=[])


class EligibilityTests(unittest.TestCase):
    def test_creation_and_capability_failure_required(self):
        for objective, text, expected in [
            ("生成一张图片", "图片生成工具不可用", True),
            ("制作一段动画", "ffmpeg 未安装", True),
            ("Create an MP4 video", "Video service is unreachable", True),
            ("generate a PNG poster", "Missing API credentials", True),
            ("帮我画一只猫", "图片生成工具不可用", True),
            ("draw a cat", "image_gen is unavailable", True),
            ("create two videos", "Video service is unreachable", True),
            ("分析这张图片", "视觉工具不可用", False),
            ("生成 CSV 文件", "工具不可用", False),
            ("生成一段视频", "缺少用户要求使用的原始素材", False),
        ]:
            with self.subTest(objective=objective, text=text):
                self.assertEqual(should_recover_media(
                    AgentRequest("s", "w", objective), AgentResult("failed", text)), expected)

    def test_success_and_existing_artifacts_not_replayed(self):
        req = AgentRequest("s", "w", "生成图片")
        self.assertFalse(should_recover_media(req, AgentResult("completed", "工具不可用")))
        self.assertFalse(should_recover_media(req, AgentResult("partial", "工具不可用", (object(),))))
        self.assertTrue(should_recover_media(req, AgentResult("partial", "工具不可用")))


class FakeServer:
    responses = []
    instances = []

    def __init__(self, command, root, receive, timeout):
        self.receive = receive
        self.failure = None
        self.process = SimpleNamespace(returncode=None)
        self.calls = []
        self.turns = []
        self.__class__.instances.append(self)

    async def start(self):
        pass

    async def close(self):
        self.process.returncode = 0

    async def send(self, message):
        pass

    async def request(self, method, params, **kwargs):
        self.calls.append((method, params))
        if method == "thread/start":
            self.workspace = Path(params["cwd"])
            return {"thread": {"id": "thread-1"}}
        if method == "turn/interrupt":
            await self.receive({"method": "turn/completed", "params": {
                "threadId": "thread-1", "turn": {"id": params["turnId"], "status": "interrupted"}}})
            return {}
        if method == "thread/backgroundTerminals/clean":
            return {}
        if method == "thread/backgroundTerminals/list":
            return {"data": []}
        if method != "turn/start":
            raise AssertionError(method)
        index = len(self.turns)
        turn_id = f"turn-{index + 1}"
        self.turns.append(params)
        # Late terminal notification from first attempt must not finish recovery.
        if index:
            await self.receive({"method": "turn/completed", "params": {
                "threadId": "thread-1", "turn": {"id": "turn-1", "status": "failed"}}})
        await self.receive({"method": "turn/started", "params": {
            "threadId": "thread-1", "turn": {"id": turn_id}}})
        response = self.responses[index]
        if response == "transport-failed":
            await self.receive({"method": "transport/failed", "params": {"error": "lost"}})
        elif response != "hang":
            for artifact in response["artifacts"]:
                (self.workspace / artifact).write_text("verified test artifact")
            await self.receive({"method": "turn/completed", "params": {
                "threadId": "thread-1", "turn": {"id": turn_id, "status": "completed", "items": [
                    {"type": "agentMessage", "phase": "final_answer", "text": json.dumps(response)}]}}})
        return {"turn": {"id": turn_id}}


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.events = []
        FakeServer.instances = []
        self.mock = patch("harness.agents.codex.AppServer", FakeServer)
        self.mock.start()
        self.provider = CodexAgentProvider(GeneralAgentConfig(workspace=self.temp.name, execution_timeout_s=2))

    async def asyncTearDown(self):
        await self.provider.aclose()
        self.mock.stop()
        self.temp.cleanup()

    async def emit(self, event):
        self.events.append(event)

    async def run_case(self, responses, objective="生成一段 MP4 动画", work_id="w", parent=None):
        FakeServer.responses = responses
        return await self.provider.run(AgentRequest("s", work_id, objective, parent_work_id=parent), self.emit)

    async def test_recovery_keeps_thread_and_returns_verified_artifact(self):
        got = await self.run_case([result(), result("completed", "已渲染", ["scene.svg"])])
        client = FakeServer.instances[0]
        self.assertEqual(got.outcome, "completed")
        self.assertEqual(got.turn_id, "turn-2")
        self.assertTrue(Path(got.artifacts[0].path).is_file())
        self.assertEqual(len(client.turns), 2)
        self.assertFalse(client.turns[1]["sandboxPolicy"]["networkAccess"])
        self.assertEqual(client.turns[0]["threadId"], client.turns[1]["threadId"])
        self.assertNotEqual(client.turns[0]["clientUserMessageId"], client.turns[1]["clientUserMessageId"])
        self.assertEqual([e.kind for e in self.events].count("completed"), 1)
        self.assertEqual([e.kind for e in self.events].count("retrying"), 1)
        saved = list(Path(self.temp.name).glob("results/*/before-media-recovery.json"))
        self.assertEqual(json.loads(saved[0].read_text())["outcome"], "failed")

    async def test_recovery_stops_after_one_failed_retry(self):
        got = await self.run_case([result(), result()])
        self.assertEqual(got.outcome, "failed")
        self.assertEqual(len(FakeServer.instances[0].turns), 2)
        self.assertFalse(self.provider._history[("s", "w")].valid)

    async def test_partial_without_artifact_can_recover(self):
        got = await self.run_case([result("partial"), result("completed", "已渲染", ["scene.svg"])])
        self.assertEqual(got.outcome, "completed")

    async def test_partial_artifacts_are_preserved(self):
        got = await self.run_case([result("partial", artifacts=["scene.svg"])])
        self.assertEqual(got.outcome, "partial")
        self.assertEqual(len(FakeServer.instances[0].turns), 1)

    async def test_success_not_retried(self):
        await self.run_case([result("completed", "done")])
        self.assertEqual(len(FakeServer.instances[0].turns), 1)

    async def test_non_media_failure_not_retried(self):
        await self.run_case([result()], objective="生成 CSV 文件")
        self.assertEqual(len(FakeServer.instances[0].turns), 1)

    async def test_read_only_does_not_attempt_rendering(self):
        self.provider.config = replace(self.provider.config, sandbox="read-only")
        await self.run_case([result()])
        self.assertEqual(len(FakeServer.instances[0].turns), 1)

    async def test_transport_failure_not_replayed(self):
        with self.assertRaises(AppServerError):
            await self.run_case(["transport-failed"])
        self.assertEqual(len(FakeServer.instances[0].turns), 1)

    async def test_recovery_obeys_total_timeout_and_interrupts(self):
        self.provider.config = replace(self.provider.config, execution_timeout_s=0.05)
        with self.assertRaises(TimeoutError):
            await self.run_case([result(), "hang"])
        calls = FakeServer.instances[0].calls
        self.assertTrue(any(m == "turn/interrupt" and p["turnId"] == "turn-2" for m, p in calls))
        self.assertNotIn("completed", [e.kind for e in self.events])

    async def test_cancel_interrupts_recovery(self):
        task = asyncio.create_task(self.run_case([result(), "hang"]))
        while not any(e.kind == "started" and e.details["turn_id"] == "turn-2" for e in self.events):
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn("cancelled", [e.kind for e in self.events])
        self.assertNotIn("completed", [e.kind for e in self.events])

    async def test_recovered_lineage_can_continue(self):
        await self.run_case([result(), result("completed", "done")])
        got = await self.run_case([result(), result(), result("completed", "continued")],
                                  objective="继续调整", work_id="w2", parent="w")
        self.assertEqual(got.full_result, "continued")
        self.assertEqual(len(FakeServer.instances), 1)

    async def test_progress_rejects_result_from_previous_attempt(self):
        run = SimpleNamespace(client=None, identity=("s", "w"), cancelled=False,
                              thread_id="thread-1", turn_id="turn-1")
        async def request(method, params):
            run.turn_id = "turn-2"
            return {"thread": {"id": "thread-1"}}
        run.client = SimpleNamespace(request=request)
        self.provider._active[run.identity] = run
        with self.assertRaisesRegex(AppServerError, "run changed"):
            await self.provider.read_progress("s", "w")
        self.provider._active.clear()


if __name__ == "__main__":
    unittest.main()
