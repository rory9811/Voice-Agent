"""Automatic speech requires opt-in; explicit queries and results do not."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from demos.settings import UserSetup, load_setup, save_setup, template
from harness.core.models import BackendResponse, DelegateRequest, DelegateResult
from harness.jobs.feedback import FeedbackConfig, WorkFeedbackController
from harness.jobs.models import Work, WorkState
from harness.jobs.progress import ProgressReporter


def request():
    return DelegateRequest('w', 's', '写一个快速排序', None, 0, 9999999999999, 'test')


class ConfigurationTests(unittest.TestCase):
    def test_default_and_legacy_config_are_off(self):
        self.assertFalse(FeedbackConfig().proactive_progress)
        self.assertFalse(template()['feedback']['proactive_progress'])
        self.assertFalse(UserSetup({'feedback': {'progress_interval_s': 30}}, '/tmp/test-venus.json').feedback.proactive_progress)

    def test_boolean_required(self):
        for value in ['false', 'true', 0, 1, None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                UserSetup({'feedback': {'proactive_progress': value}}, '/tmp/test-venus.json')

    def test_opt_in_and_opt_out_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'harness.json'
            for enabled in [True, False]:
                save_setup({'feedback': {'proactive_progress': enabled}}, path)
                self.assertIs(load_setup(path).feedback.proactive_progress, enabled)


class FeedbackConsentTests(unittest.IsolatedAsyncioTestCase):
    def controller(self, enabled=False, capability='general'):
        work = Work('w', '写一个快速排序', session_id='s', state=WorkState.RUNNING,
                    phase='agent', execution_outcome='running', capability=capability)
        controller = WorkFeedbackController({'w': work}, FeedbackConfig(
            proactive_progress=enabled, progress_interval_s=.01, first_notice_s=.01,
            poll_s=.001), SimpleNamespace(save=Mock()))
        controller.reporter = SimpleNamespace(active_queries={}, retry_at={}, query_generation={},
                                             text=AsyncMock(return_value='我正在检查快速排序代码的边界情况。'))
        return controller, work

    async def test_disabled_skips_polling_and_publishing_all_capabilities(self):
        for capability in ['general', 'skill']:
            controller, work = self.controller(capability=capability)
            publish = AsyncMock()
            await asyncio.wait_for(controller.watch(request(), publish), .2)
            controller.reporter.text.assert_not_awaited()
            publish.assert_not_awaited()
            self.assertEqual(work.state, WorkState.RUNNING)

    async def test_enabled_publishes_progress(self):
        for capability in ['general', 'skill']:
            controller, work = self.controller(True, capability)
            async def finish(result):
                work.state = WorkState.COMPLETED
                self.assertEqual(result.status, 'pending')
            publish = AsyncMock(side_effect=finish)
            await asyncio.wait_for(controller.watch(request(), publish), .5)
            publish.assert_awaited_once()
            controller.reporter.text.assert_awaited_once_with(request(), work, proactive=True)

    async def test_disabled_keeps_final_results(self):
        for state, outcome in [(WorkState.COMPLETED, 'completed'), (WorkState.FAILED, 'failed')]:
            controller, work = self.controller()
            work.state, work.execution_outcome = state, outcome
            result = controller.terminal(DelegateResult('w', 's', outcome, spoken_text='任务结果。'))
            self.assertTrue(result.metadata['terminal'])
            self.assertEqual(result.spoken_text, '任务结果。')
            self.assertEqual(work.delivery_status, 'pending')

    async def test_disabled_keeps_explicit_query(self):
        controller, work = self.controller()
        backend = SimpleNamespace(feedback=controller, journal=controller.journal,
            _general=SimpleNamespace(read_progress=AsyncMock(return_value={'items': [
                {'type': 'publicCommentary', 'text': '正在检查快速排序的边界情况。'}]})),
            _direct_backend=SimpleNamespace(oralize=AsyncMock(return_value=BackendResponse(
                '我正在检查快速排序代码的边界情况。', 'test', 'test', 0))),
            work_manager=SimpleNamespace(get=lambda key: None))
        reporter = ProgressReporter(backend)
        try:
            self.assertEqual(await reporter.query_text(request(), work), '我正在检查快速排序代码的边界情况。')
            backend._general.read_progress.assert_awaited_once()
        finally:
            await reporter.close()


if __name__ == '__main__':
    unittest.main()
