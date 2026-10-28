import asyncio
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from harness.core.models import BackendResponse, DelegateRequest, DelegateResult
from harness.core.frontend import assemble_frontend_message
from harness.core.speech import brief_progress, limit_spoken_sentences, spoken_observation
from harness.jobs.models import Work, WorkState
from harness.jobs.progress import ProgressReporter
from harness.jobs.feedback import WorkFeedbackController, FeedbackConfig
from harness.llm.delegate import CodexDirectAndPolish


BAD_PROGRESS = 'C++快速排序的编写任务仍在进行中，已观察到一次命令执行失败、另一次成功，但尚未确认代码已写好或通过验证。目前仍在等待执行结果，没有新的阶段进展。'


def request():
    return DelegateRequest('w', 's', '写一个快速排序', None, 0, 9999999999999, 'test')


class SpeechTests(unittest.TestCase):
    def test_chinese_three_sentences(self):
        self.assertEqual(limit_spoken_sentences('完成了。文件已保存！有一个限制？第四句。'),
                         '完成了。 文件已保存！ 有一个限制？')

    def test_english_filenames_and_decimals(self):
        self.assertEqual(limit_spoken_sentences('Saved quicksort.cpp. Used 3.14 seconds. Tests passed. More detail.'),
                         'Saved quicksort.cpp. Used 3.14 seconds. Tests passed.')

    def test_lines_and_bullets_count(self):
        self.assertEqual(limit_spoken_sentences('1. Done\n2. Saved\n3. Tested\n4. Extra'), 'Done Saved Tested')

    def test_strip_code_block(self):
        self.assertEqual(limit_spoken_sentences('已完成。\n```cpp\nx();\n```\n文件已保存。'), '已完成。 文件已保存。')

    def test_diagnostic_progress_replaced(self):
        self.assertEqual(brief_progress(BAD_PROGRESS), '我还在处理，有结果就告诉你。')

    def test_progress_one_short_sentence(self):
        self.assertEqual(brief_progress('正在检查排序结果。后面还有很多细节。'), '正在检查排序结果。')
        self.assertEqual(brief_progress('正在'+'处理'*80+'。'), '我还在处理，有结果就告诉你。')

    def test_frontend_no_labels_and_preserves_kind(self):
        result = DelegateResult('w', 's', 'pending', spoken_text='正在检查排序结果。',
                                feedback_id='f', metadata={'kind': 'progress'})
        message = assemble_frontend_message(result)
        self.assertEqual(message['text'], result.spoken_text)
        self.assertEqual(message['kind'], 'progress')
        self.assertEqual(message['feedback_id'], 'f')

    def test_frontend_caps_legacy_final_without_changing_raw(self):
        result = DelegateResult('w', 's', 'completed', spoken_text='一。二。三。四。', raw_text='完整原文')
        self.assertEqual(assemble_frontend_message(result)['text'], '一。 二。 三。')
        self.assertEqual(result.raw_text, '完整原文')

    def test_observation_omits_internal_command_status(self):
        observed = {'turn_id': 'private', 'items': [
            {'type': 'commandExecution', 'exit_code': 1},
            {'type': 'publicCommentary', 'text': '我正在检查代码。'}]}
        self.assertEqual(spoken_observation(observed), {'worker_updates': ['我正在检查代码。']})


class ReporterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.work = Work('w', '写一个快速排序', session_id='s', state=WorkState.RUNNING,
                         phase='agent', execution_outcome='running')
        self.observed = {'items': [{'type': 'commandExecution', 'exit_code': 1}]}
        self.direct = SimpleNamespace(oralize=AsyncMock(return_value=BackendResponse(BAD_PROGRESS, 'fake', 'fake', 0)))
        self.backend = SimpleNamespace(
            _general=SimpleNamespace(read_progress=AsyncMock(side_effect=lambda *args: self.observed)),
            _direct_backend=self.direct, journal=SimpleNamespace(save=Mock()),
            work_manager=SimpleNamespace(get=lambda key: None))
        self.backend.feedback = WorkFeedbackController({'w': self.work}, FeedbackConfig(poll_s=.01), self.backend.journal)
        self.reporter = ProgressReporter(self.backend)

    async def asyncTearDown(self):
        await self.reporter.close()

    async def test_repeated_observation_is_silent_without_polish(self):
        self.assertEqual(await self.reporter.text(request(), self.work, proactive=True), '我还在处理，有结果就告诉你。')
        self.assertIsNone(await self.reporter.text(request(), self.work, proactive=True))
        self.assertEqual(self.direct.oralize.await_count, 1)

    async def test_command_count_change_does_not_create_spoken_progress(self):
        await self.reporter.text(request(), self.work, proactive=True)
        self.observed['items'].append({'type': 'commandExecution', 'exit_code': 0})
        self.assertIsNone(await self.reporter.text(request(), self.work, proactive=True))

    async def test_explicit_query_still_answers_unchanged_state(self):
        await self.reporter.text(request(), self.work, proactive=True)
        got = await asyncio.wait_for(self.reporter.query_text(request(), self.work), .5)
        self.assertEqual(got, '我还在处理，有结果就告诉你。')

    async def test_new_useful_progress_is_reported(self):
        await self.reporter.text(request(), self.work, proactive=True)
        self.observed['items'].append({'type': 'publicCommentary', 'text': '正在检查排序结果。'})
        self.direct.oralize.return_value = BackendResponse('正在检查排序结果。', 'fake', 'fake', 0)
        self.assertEqual(await self.reporter.text(request(), self.work, proactive=True), '正在检查排序结果。')

    async def test_same_wording_not_repeated(self):
        await self.reporter.text(request(), self.work, proactive=True)
        self.observed['items'].append({'type': 'publicCommentary', 'text': '我还在处理。'})
        self.assertIsNone(await self.reporter.text(request(), self.work, proactive=True))

    async def test_read_failure_silent(self):
        self.backend._general.read_progress.side_effect = RuntimeError('read failed')
        self.assertIsNone(await self.reporter.text(request(), self.work, proactive=True))
        self.direct.oralize.assert_not_awaited()

    async def test_polish_failure_silent_only_for_timer(self):
        self.direct.oralize.side_effect = RuntimeError('polish unavailable')
        self.assertIsNone(await self.reporter.text(request(), self.work, proactive=True))
        self.assertEqual(await self.reporter.text(request(), self.work), '暂时查不到进度，请稍后再试。')

    async def test_completion_during_polish_invalidates_progress(self):
        async def complete(*args):
            self.work.state = WorkState.COMPLETED
            self.work.execution_outcome = 'completed'
            return BackendResponse('仍在处理。', 'fake', 'fake', 0)
        self.direct.oralize.side_effect = complete
        self.assertIsNone(await self.reporter.text(request(), self.work, proactive=True))

    async def test_blocking_fault_not_hidden_by_generic_fallback(self):
        self.work.fault = {'message': '没有写入文件的权限，暂时无法继续。'}
        self.assertEqual(await self.reporter.text(request(), self.work, proactive=True), self.work.fault['message'])

    async def test_real_polish_boundary_caps_backend_output(self):
        backend = SimpleNamespace(complete=AsyncMock(return_value=json.dumps({'speech': '一。二。三。四。五。'})))
        provider = CodexDirectAndPolish(backend)
        got = await provider.oralize(request(), BackendResponse('完整结果', 'test', 'test', 0))
        self.assertEqual(got.text, '一。 二。 三。')
        self.assertIn('绝对不能超过三句话', backend.complete.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
