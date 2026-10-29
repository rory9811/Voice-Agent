import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from demos.server.configuration import WebConfiguration
from demos.server.resources import real_resources
from demos.settings import save_setup, template


class WebSettingsTests(unittest.TestCase):
    def test_web_saves_speaking_length_in_harness_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'harness.json'
            data = template(); data['workspace'] = 'tasks'
            data['codex']['command'] = [sys.executable, 'app-server']
            data['gemini']['api_key'] = 'saved-key'
            save_setup(data, path)
            config = WebConfiguration(path)
            config.logins["codex"].check = MagicMock(return_value={'state': 'logged_in', 'message': ''})
            public = config.public()
            self.assertEqual(public['data']['duplex']['length_penalty'], 0.8)
            self.assertEqual(public['data']['gemini']['api_key'], '')
            public['data']['duplex']['length_penalty'] = 1.2
            public['data']['feedback']['progress_interval_s'] = 45
            self.assertTrue(config.save(public)['ok'])
            saved = json.loads(path.read_text())
            self.assertEqual(saved['duplex']['length_penalty'], 1.2)
            self.assertEqual(saved['feedback']['progress_interval_s'], 45)
            self.assertEqual(saved['gemini']['api_key'], 'saved-key')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotEqual(config.revision(), public['revision'])
            with self.assertRaises(RuntimeError):
                config.save(public)


    def test_workspace_must_stay_in_the_data_folder(self):
        # In the container only the data folder is the user's disk; anything else vanishes on rebuild.
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as elsewhere:
            path = Path(tmp)/'harness.json'
            data = template(); data['codex']['command'] = [sys.executable, 'app-server']
            data['workspace'] = elsewhere
            save_setup(data, path)
            config = WebConfiguration(path)
            config.logins["codex"].check = MagicMock(return_value={'state': 'logged_in', 'message': ''})
            self.assertFalse(config.status())
            public = config.public()
            result = config.save(public)
            self.assertFalse(result['ok'])
            self.assertTrue(any('workspace' in problem for problem in result['problems']))
            public = config.public()
            public['data']['workspace'] = 'workspace'
            self.assertTrue(config.save(public)['ok'])
            self.assertTrue(config.status())


class WebResourcesTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_uses_saved_length_and_configured_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'harness.json'
            data = template(); data['duplex']['length_penalty'] = 1.3
            save_setup(data, path)
            with patch('demos.server.resources.build_harness', return_value=MagicMock()):
                resources = await real_resources(
                    mode='omni', settings_path=path, server_url='http://10.0.0.1:8031',
                    tokenizer=None, model_type='omni', model_timeout_s=42,
                )
            self.assertEqual(resources.serving._length_penalty, 1.3)
            self.assertEqual(resources.serving._client.timeout.read, 42)
            await resources.serving.aclose()


class WebCommandTests(unittest.TestCase):
    def test_model_timeout_reaches_the_app(self):
        from demos.server import cli
        argv = ['cli', '--model-server', 'http://10.0.0.1:8031', '--config', '/data/harness.json', '--model-timeout', '42']
        with patch.object(sys, 'argv', argv), patch('demos.server.cli.fetch_model') as fetch, \
                patch('demos.server.cli.create_app') as create, patch('demos.server.cli.uvicorn.run'):
            fetch.return_value = MagicMock(model_type='omni', tokenizer='tok')
            cli.main()
        self.assertEqual(create.call_args.kwargs['model_timeout_s'], 42)
        self.assertNotIn('demo_path', create.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
