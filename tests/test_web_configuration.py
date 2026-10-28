import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from demos.server.app import create_app
from demos.settings import save_setup, load_setup


class WebConfigurationTests(unittest.TestCase):
    def setUp(self):
        login = patch('demos.server.codex_login.CodexLoginCheck.check', return_value={'state':'logged_in','message':'Codex is logged in'})
        login.start()
        self.addCleanup(login.stop)

    def test_config_api_save_reload_key_redaction_and_default_preservation(self):
        with tempfile.TemporaryDirectory() as tmp, patch('shutil.which', return_value='/bin/codex'), patch.dict(os.environ, {'GEMINI_API_KEY':'','GOOGLE_API_KEY':''}):
            p=Path(tmp)/'harness.json';save_setup({'workspace':'./workspace'},p)
            app=create_app(settings_path=p,tokenizer_path='/unused')
            with TestClient(app) as c:
                old=c.get('/api/config').json()
                self.assertEqual(old['data']['routing']['mode'],'general')
                headers={'X-Venus-Config-Token':old['token']}
                data=old['data'];data['responses']['provider']='gemini'
                missing=c.post('/api/config',json={'data':data,'revision':old['revision']},headers=headers)
                self.assertEqual(missing.status_code,422)
                self.assertEqual(load_setup(p).responses.provider,'codex')
                data['gemini']['api_key']='testing-only-not-real'
                saved=c.post('/api/config',json={'data':data,'revision':old['revision']},headers=headers)
                self.assertEqual(saved.status_code,200,saved.text)
                self.assertNotIn('testing-only-not-real',saved.text)
                self.assertTrue(saved.json()['gemini_key_configured'])
                got=c.get('/api/config').json()
                self.assertEqual(got['data']['responses']['provider'],'gemini')
                self.assertEqual(got['data']['multimodal']['provider'],'codex')
                self.assertEqual(got['data']['routing']['mode'],'general')
                self.assertEqual(got['data']['codex'],old['data']['codex'])
                self.assertFalse(got['data']['feedback']['proactive_progress'])
                got['data']['responses']['provider']='codex'
                self.assertEqual(c.post('/api/config',json=got,headers=headers).status_code,200)
                self.assertEqual(load_setup(p).gemini.api_key,'testing-only-not-real')
                self.assertEqual(c.post('/api/config',json=got,headers=headers).status_code,409)
                self.assertEqual(c.post('/api/config',json=got).status_code,403)


if __name__=='__main__': unittest.main()
