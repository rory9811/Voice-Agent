"""Official Gemini contracts and independent web provider configuration."""
import asyncio
import base64
import itertools
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from demos.settings import UserSetup, load_setup, save_setup
from demos.server.configuration import WebConfiguration
from demos.server.backends import task_backends
from harness.config import GeminiConfig, ModelCallConfig, Settings
from harness.core.models import (BackendResponse, BinaryAsset, ContextSnapshot,
    DelegateRequest, PreparedContext, VideoFrame, VideoInput, VideoSegment)
from harness.llm.backends import CodexPlannerBackend
from harness.llm.delegate import CodexDirectAndPolish
from harness.llm.gemini import GeminiBackend, GeminiDirect

SECRET = 'test-key-do-not-return'


def response(speech='已完成。', **kwargs):
    return httpx.Response(200, json={'candidates': [{'finishReason': 'STOP', 'content': {
        'parts': [{'text': json.dumps({'speech': speech}, ensure_ascii=False)}]}, **kwargs}]})


def request():
    return DelegateRequest('w', 's', '描述音视频内容', None, 0, 9999999999999, 'test')


def context(audio=None, video=None):
    return PreparedContext(ContextSnapshot('s','snap',1000,1,(),(),(),' ',0),audio,video,'')


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        login = patch('demos.server.codex_login.CodexLoginCheck.check', return_value={'state':'logged_in','message':'Codex is logged in'})
        login.start()
        self.addCleanup(login.stop)
        self.env = patch.dict(os.environ, {'GEMINI_API_KEY':'', 'GOOGLE_API_KEY':''})
        self.env.start()
        self.addCleanup(self.env.stop)

    def setup(self, data):
        return UserSetup({'workspace': './workspace', **data}, '/tmp/venus-gemini-test.json')

    def test_defaults_keep_general_codex_and_opt_out(self):
        setup = self.setup({})
        self.assertEqual(setup.routing.mode, 'general')
        self.assertTrue(all(call.provider=='codex' for call in (setup.routing,setup.responses,setup.multimodal)))
        self.assertFalse(setup.feedback.proactive_progress)
        delegate, planner = task_backends(setup)
        self.assertIsInstance(delegate.direct, CodexDirectAndPolish)
        self.assertIsInstance(delegate.polish.backend, CodexPlannerBackend)
        self.assertEqual(planner._routing_mode, 'general')

    def test_legacy_multimodal_inherits_response_overrides(self):
        setup=self.setup({'responses':{'model':'custom-codex','effort':'high','timeout_s':87}})
        self.assertEqual(setup.responses,setup.multimodal)
        delegate,_=task_backends(setup)
        self.assertEqual(delegate.direct.backend._model,'custom-codex')
        self.assertEqual(delegate.direct.backend._effort,'high')

    def test_all_eight_provider_combinations_are_independent(self):
        for route,polish,media in itertools.product(['codex','gemini'], repeat=3):
            with self.subTest(route=route,polish=polish,media=media):
                setup=self.setup({'gemini':{'api_key':SECRET},
                    'routing':{'mode':'auto','provider':route},
                    'responses':{'provider':polish},'multimodal':{'provider':media}})
                delegate,planner=task_backends(setup)
                self.assertEqual(isinstance(planner._router_backend,GeminiBackend),route=='gemini')
                self.assertEqual(isinstance(delegate.polish.backend,GeminiBackend),polish=='gemini')
                self.assertEqual(isinstance(delegate.direct,GeminiDirect),media=='gemini')
                self.assertIsInstance(planner._backend,CodexPlannerBackend)

    def test_explicit_models_and_timeouts_are_per_role(self):
        setup=self.setup({'gemini':{'api_key':SECRET},'routing':{'mode':'auto','provider':'gemini','model':'gemini-pro-latest','timeout_s':11},
            'responses':{'provider':'gemini','model':'gemini-flash-latest','timeout_s':22},
            'multimodal':{'provider':'gemini','model':'gemini-flash-lite-latest','timeout_s':33}})
        d,p=task_backends(setup)
        self.assertEqual((p._router_backend.model_name,p._router_backend.timeout_s),('gemini-pro-latest',11))
        self.assertEqual((d.polish.backend.model_name,d.polish.backend.timeout_s),('gemini-flash-latest',22))
        self.assertEqual((d.direct.backend.model_name,d.direct.backend.timeout_s),('gemini-flash-lite-latest',33))

    def test_missing_key_only_blocks_active_gemini_roles(self):
        with patch('shutil.which',return_value='/bin/codex'):
            setup=self.setup({'routing':{'provider':'gemini','mode':'general'}})
            self.assertTrue(setup.check()['ok'])
            task_backends(setup)
            for role in ['responses','multimodal','routing']:
                data={role:{'provider':'gemini'}}
                if role=='routing': data[role]['mode']='auto'
                self.assertFalse(self.setup(data).check()['ok'])

    def test_environment_key_and_no_proxy_configuration(self):
        with patch.dict(os.environ,{'GEMINI_API_KEY':SECRET}):
            self.assertEqual(GeminiConfig().resolved_key(),SECRET)
        for data in [{'gemini':{'base_url':'http://relay'}}, {'responses':{'provider':'company'}},
                     {'responses':{'provider':'gemini','model':'https://relay/model'}}]:
            with self.assertRaises(ValueError):self.setup(data)

    def test_write_only_key_roundtrip_and_legacy_client_save(self):
        with tempfile.TemporaryDirectory() as tmp, patch('shutil.which',return_value='/bin/codex'):
            path=Path(tmp)/'config.json'
            save_setup({'workspace':'./workspace','gemini':{'api_key':SECRET}},path)
            configuration=WebConfiguration(path)
            public=configuration.public()
            self.assertNotIn(SECRET,json.dumps(public))
            self.assertTrue(public['gemini_key_configured'])
            public['data']['responses']['provider']='gemini'
            self.assertTrue(configuration.save(public)['ok'])
            self.assertEqual(load_setup(path).gemini.api_key,SECRET)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            public=configuration.public();del public['data']['gemini']
            configuration.save(public)
            self.assertEqual(load_setup(path).gemini.api_key,SECRET)
            self.assertNotIn(SECRET,repr(load_setup(path).gemini))

    def test_key_replacement_and_conflicting_save(self):
        with tempfile.TemporaryDirectory() as tmp, patch('shutil.which',return_value='/bin/codex'):
            path=Path(tmp)/'config.json';save_setup({'workspace':'./workspace'},path);config=WebConfiguration(path)
            public=config.public();public['data']['gemini']['api_key']=SECRET
            config.save(public)
            with self.assertRaises(RuntimeError):config.save(public)
            public=config.public();public['data']['gemini']['api_key']='replacement-key'
            config.save(public);self.assertEqual(load_setup(path).gemini.api_key,'replacement-key')


class GeminiTests(unittest.IsolatedAsyncioTestCase):
    def backend(self, handler, timeout=1):
        return GeminiBackend(api_key=SECRET,model='gemini-flash-latest',timeout_s=timeout,
                             transport=httpx.MockTransport(handler))

    async def test_official_url_header_and_json_contract(self):
        def handle(req):
            self.assertEqual(str(req.url),'https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent')
            self.assertEqual(req.headers['x-goog-api-key'],SECRET)
            body=json.loads(req.content)
            self.assertNotIn(SECRET,req.content.decode())
            self.assertNotIn('tools',body)
            self.assertEqual(body['systemInstruction']['parts'][0]['text'],'route')
            self.assertEqual(body['generationConfig']['responseMimeType'],'application/json')
            self.assertEqual(json.loads(body['contents'][0]['parts'][0]['text']),{'objective':'hello'})
            return response()
        self.assertEqual(json.loads(await self.backend(handle).complete('route',{'objective':'hello'})),{'speech':'已完成。'})

    async def test_polish_keeps_three_sentence_limit(self):
        polish=CodexDirectAndPolish(self.backend(lambda req:response('一。二。三。四。')))
        got=await polish.oralize(request(),BackendResponse('source','test','test',0))
        self.assertEqual(got.text,'一。 二。 三。')
        self.assertEqual(got.provider,'gemini-official-polish')

    async def test_multimodal_sends_audio_and_frame_bytes(self):
        audio=BinaryAsset(b'RIFF-test','audio/wav');frame=VideoFrame(b'jpeg-test',1000)
        def handle(req):
            parts=json.loads(req.content)['contents'][0]['parts']
            media=[part['inlineData'] for part in parts if 'inlineData' in part]
            self.assertEqual([m['mimeType'] for m in media],['audio/wav','image/jpeg'])
            self.assertEqual([base64.b64decode(m['data']) for m in media],[audio.data,frame.data])
            return response('画面中有一只猫。')
        result=await GeminiDirect(self.backend(handle)).execute(request(),context(audio,VideoInput(frames=(frame,))))
        self.assertEqual(result.text,'画面中有一只猫。')

    async def test_video_segment_and_text_only(self):
        seen=[]
        def handle(req):
            seen.append(json.loads(req.content)['contents'][0]['parts']);return response()
        direct=GeminiDirect(self.backend(handle))
        await direct.execute(request(),context(video=VideoInput(segment=VideoSegment(b'mp4',0,1000))))
        self.assertEqual(seen[0][1]['inlineData']['mimeType'],'video/mp4')
        await direct.execute(request(),context());self.assertEqual(len(seen[1]),1)

    async def test_http_errors_never_echo_secret_or_input(self):
        for code in [400,401,403,404,429,500,302]:
            with self.subTest(code=code):
                backend=self.backend(lambda req:httpx.Response(code,text=SECRET,headers={'location':'https://example.com'}))
                with self.assertRaises(RuntimeError) as error:await backend.complete('test',{})
                self.assertNotIn(SECRET,str(error.exception));self.assertIn(str(code),str(error.exception))

    async def test_invalid_empty_blocked_or_truncated_responses(self):
        bodies=[{}, {'candidates':[]}, {'candidates':[{'finishReason':'MAX_TOKENS'}]},
                {'candidates':[{'finishReason':'SAFETY'}]}, {'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':'[]'}]}}]},
                {'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':'bad JSON'}]}}]}]
        for body in bodies:
            with self.subTest(body=body),self.assertRaises(RuntimeError):
                await self.backend(lambda req:httpx.Response(200,json=body)).complete('test',{})

    async def test_thought_parts_are_not_answers(self):
        def handle(req):
            return httpx.Response(200,json={'candidates':[{'finishReason':'STOP','content':{'parts':[
                {'text':'private thought','thought':True},{'text':'{"speech":"ok"}'}]}}]})
        self.assertEqual(await self.backend(handle).complete('test',{}),'{"speech":"ok"}')

    async def test_timeout_and_cancellation(self):
        async def slow(req):await asyncio.sleep(10)
        with self.assertRaisesRegex(RuntimeError,'timed out'):
            await self.backend(slow,.01).complete('test',{})
        task=asyncio.create_task(self.backend(slow).complete('test',{}))
        await asyncio.sleep(.01);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task

    async def test_size_limit_before_request(self):
        called=False
        def handle(req):
            nonlocal called;called=True;return response()
        with self.assertRaisesRegex(ValueError,'20 MB'):
            await self.backend(handle).complete('test',{'input':'x'*20_000_000})
        self.assertFalse(called)


if __name__=='__main__':unittest.main()
