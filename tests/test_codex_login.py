import asyncio
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from demos.settings import save_setup
from demos.server.app import create_app
from demos.server.configuration import WebConfiguration
from demos.server.codex_login import CodexLoginCheck


class CodexLoginTests(unittest.TestCase):
    def test_cache_force_and_binary_change(self):
        checker = CodexLoginCheck()
        with patch('shutil.which', side_effect=lambda x: '/bin/'+x), patch('demos.server.codex_login.subprocess.run', return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(checker.check('codex')['state'], 'logged_in')
            run.return_value.returncode = 1
            self.assertEqual(checker.check('codex')['state'], 'logged_in')
            self.assertEqual(run.call_count, 1)
            self.assertEqual(checker.check('codex', force=True)['state'], 'not_logged_in')
            checker.check('another-codex')
            self.assertEqual(run.call_count, 3)
            self.assertEqual(run.call_args.args[0], ['/bin/another-codex', 'login', 'status'])
            self.assertEqual(run.call_args.kwargs['timeout'], 5)
            self.assertEqual(run.call_args.kwargs['stdout'], subprocess.DEVNULL)
            self.assertEqual(run.call_args.kwargs['stderr'], subprocess.DEVNULL)

    def test_missing_timeout_error_and_nonzero_fail_closed(self):
        with patch('shutil.which', return_value=None), patch('demos.server.codex_login.subprocess.run') as run:
            self.assertEqual(CodexLoginCheck().check('missing')['state'], 'missing')
            run.assert_not_called()
        with patch('shutil.which', return_value='/bin/codex'):
            for failure, expected in [(subprocess.TimeoutExpired('cmd', 5), 'timeout'), (OSError('SECRET'), 'error')]:
                with patch('demos.server.codex_login.subprocess.run', side_effect=failure):
                    result = CodexLoginCheck().check('codex')
                    self.assertEqual(result['state'], expected)
                    self.assertNotIn('SECRET', json.dumps(result))
            with patch('demos.server.codex_login.subprocess.run', return_value=SimpleNamespace(returncode=2)):
                self.assertEqual(CodexLoginCheck().check('codex')['state'], 'error')

    def test_gate_rejects_logout_even_after_cached_success_and_gemini_selection(self):
        with tempfile.TemporaryDirectory() as tmp, patch('shutil.which', return_value='/bin/codex'), patch('demos.server.codex_login.subprocess.run', return_value=SimpleNamespace(returncode=0)) as run:
            path = Path(tmp)/'settings.json'
            save_setup({'workspace':'./tasks', 'responses':{'provider':'gemini'}, 'multimodal':{'provider':'gemini'}, 'routing':{'mode':'auto', 'provider':'gemini'}, 'gemini':{'api_key':'SECRET'}}, path)
            app = create_app(settings_path=path, tokenizer_path='/unused')
            with TestClient(app) as client:
                self.assertTrue(client.get('/api/status').json()['configured'])
                run.return_value.returncode = 1
                self.assertTrue(client.get('/api/status').json()['configured'])
                with patch('demos.server.app.WebAgentSession') as session:
                    with client.websocket_connect('/ws') as socket:
                        self.assertEqual(socket.receive_json()['code'], 'configuration')
                    session.assert_not_called()
                self.assertFalse(client.get('/api/status?fresh=true').json()['configured'])
                public = client.get('/api/config').json()
                self.assertEqual(public['codex_login']['state'], 'not_logged_in')
                self.assertNotIn('SECRET', json.dumps(public))
                run.return_value.returncode = 0
                self.assertTrue(client.get('/api/config').json()['configured'])
                self.assertTrue(client.get('/api/status?fresh=true').json()['configured'])

    def test_configuration_can_be_saved_while_login_is_pending(self):
        with tempfile.TemporaryDirectory() as tmp, patch('shutil.which', return_value='/bin/codex'), patch('demos.server.codex_login.subprocess.run', return_value=SimpleNamespace(returncode=1)):
            path = Path(tmp)/'settings.json'
            with TestClient(create_app(settings_path=path, tokenizer_path='/unused')) as client:
                public = client.get('/api/config').json()
                public['data']['workspace'] = './tasks'
                result = client.post('/api/config', json=public, headers={'X-Venus-Config-Token':public['token']})
                self.assertEqual(result.status_code, 200)
                self.assertFalse(result.json()['configured'])
                self.assertEqual(result.json()['codex_login']['state'], 'not_logged_in')

    def test_changed_configuration_during_probe_does_not_admit_session(self):
        with tempfile.TemporaryDirectory() as tmp, patch('shutil.which', return_value='/bin/codex'):
            path = Path(tmp)/'settings.json'
            save_setup({'workspace':'./tasks'}, path)
            def change_config(*args, **kwargs):
                save_setup({'workspace':'./different-tasks'}, path)
                return SimpleNamespace(returncode=0)
            with patch('demos.server.codex_login.subprocess.run', side_effect=change_config), patch('demos.server.app.WebAgentSession') as session:
                with TestClient(create_app(settings_path=path, tokenizer_path='/unused')) as client:
                    with client.websocket_connect('/ws') as socket:
                        self.assertEqual(socket.receive_json()['code'], 'configuration')
                session.assert_not_called()

    def test_logged_in_session_passes_gate(self):
        class FakeSession:
            def __init__(self, websocket, *args, **kwargs):
                self.websocket = websocket
                self.session_id = 'fake-session'
            async def run(self):
                await self.websocket.accept()
                await self.websocket.send_json({'type':'ready'})
                await self.websocket.close()
        with tempfile.TemporaryDirectory() as tmp, patch('shutil.which', return_value='/bin/codex'), patch('demos.server.codex_login.subprocess.run', return_value=SimpleNamespace(returncode=0)), patch('demos.server.app.WebAgentSession', FakeSession):
            path = Path(tmp)/'settings.json'
            save_setup({'workspace':'./tasks'}, path)
            with TestClient(create_app(settings_path=path, tokenizer_path='/unused')) as client:
                with client.websocket_connect('/ws') as socket:
                    self.assertEqual(socket.receive_json()['type'], 'ready')


class NonBlockingLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_login_probe_does_not_block_other_requests(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*args, **kwargs):
            entered.set()
            release.wait(3)
            return SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as tmp, patch('shutil.which', return_value='/bin/codex'), patch('demos.server.codex_login.subprocess.run', side_effect=slow):
            path = Path(tmp)/'settings.json'
            save_setup({'workspace':'./tasks'}, path)
            app = create_app(settings_path=path, tokenizer_path='/unused')
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                pending = asyncio.create_task(client.get('/api/status?fresh=true'))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    response = await asyncio.wait_for(client.get('/health'), timeout=.5)
                    self.assertEqual(response.status_code, 200)
                finally:
                    release.set()
                    await pending

if __name__ == '__main__':
    unittest.main()
