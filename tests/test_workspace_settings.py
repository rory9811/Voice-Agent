import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from demos.server.app import create_app
from demos.server.configuration import WebConfiguration
from demos.settings import save_setup, load_setup
from demos.launcher.cli import setup_backend


class WorkspaceSettingsTests(unittest.TestCase):
    def setUp(self):
        login = patch('demos.server.codex_login.CodexLoginCheck.check', return_value={'state':'logged_in','message':'Codex is logged in'})
        login.start()
        self.addCleanup(login.stop)

    def test_explicit_workspace_is_editable_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp, patch('shutil.which', return_value='/bin/codex'):
            path = Path(tmp)/'harness.json'
            config = WebConfiguration(path)
            public = config.public()
            self.assertEqual(public['data']['workspace'], '')
            public['data']['workspace'] = './chosen/tasks'
            self.assertTrue(config.save(public)['ok'])
            self.assertEqual(load_setup(path).workspace, (path.parent/'chosen/tasks').resolve())
            public = config.public()
            self.assertEqual(public['data']['workspace'], './chosen/tasks')
            public['data']['workspace'] = './another'
            self.assertTrue(config.save(public)['ok'])
            self.assertTrue((path.parent/'chosen/tasks').is_dir())
            self.assertEqual(config.public()['data']['workspace'], './another')

    def test_blank_workspace_cannot_be_saved_or_used(self):
        with tempfile.TemporaryDirectory() as tmp, patch('shutil.which', return_value='/bin/codex'):
            path = Path(tmp)/'runtime/harness.json'
            config = WebConfiguration(path)
            for blank in ['', '  ', None]:
                public = config.public()
                public['data']['workspace'] = blank
                if blank is None:
                    with self.assertRaises(ValueError):
                        config.save(public)
                else:
                    self.assertFalse(config.save(public)['ok'])
                self.assertFalse(config.status())
                self.assertFalse(path.exists())
            self.assertFalse((path.parent/'workspace').exists())
            save_setup({'workspace': ''}, path)
            self.assertFalse(config.status())

    def test_launcher_starts_for_setup_without_default_workspace(self):
        with tempfile.TemporaryDirectory() as tmp, patch('shutil.which', return_value='/bin/codex'), patch('subprocess.run', return_value=SimpleNamespace(returncode=0)), patch.dict(os.environ, {'GENERAL_CODEX_BINARY': '/bin/codex'}):
            config = SimpleNamespace(runtime=Path(tmp)/'runtime', settings=Path(tmp)/'runtime/harness.json')
            setup_backend(config, False)
            self.assertEqual(load_setup(config.settings).data['workspace'], '')
            self.assertFalse(WebConfiguration(config.settings).status())
            self.assertFalse((config.runtime/'workspace').exists())

    def test_incomplete_setup_rejects_session_then_explicit_path_makes_ready(self):
        with tempfile.TemporaryDirectory() as tmp, patch('shutil.which', return_value='/bin/codex'), patch.dict(os.environ, {'GEMINI_API_KEY': '', 'GOOGLE_API_KEY': ''}):
            path = Path(tmp)/'harness.json'
            app = create_app(settings_path=path, tokenizer_path='/unused')
            with TestClient(app) as client:
                self.assertFalse(client.get('/api/status').json()['configured'])
                with client.websocket_connect('/ws') as socket:
                    self.assertEqual(socket.receive_json()['code'], 'configuration')
                public = client.get('/api/config').json()
                headers = {'X-Venus-Config-Token': public['token']}
                self.assertEqual(client.post('/api/config', json=public, headers=headers).status_code, 422)
                public['data']['workspace'] = './my-tasks'
                result = client.post('/api/config', json=public, headers=headers)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertTrue(client.get('/api/status').json()['configured'])
                public = result.json()
                public['data']['responses']['provider'] = 'gemini'
                result = client.post('/api/config', json=public, headers=headers)
                self.assertEqual(result.status_code, 422)
                self.assertEqual(load_setup(path).responses.provider, 'codex')


if __name__ == '__main__':
    unittest.main()
