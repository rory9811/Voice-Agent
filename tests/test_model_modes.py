import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from demos.model.adapter import _load_real_blocking
from demos.server.app import create_app


def checkpoint(path, kind):
    path.mkdir(parents=True)
    (path/'config.json').write_text(json.dumps({'model_type':kind}))
    for file in ['tokenizer.json','model.safetensors','assets/HT_ref_audio.wav','assets/token2wav/flow.yaml','assets/token2wav/flow.pt','assets/token2wav/hift.pt','assets/token2wav/campplus.onnx','assets/token2wav/speech_tokenizer_v2_25hz.onnx']:
        p=path/file;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'fixture')
    return path


class ModelModeTests(unittest.TestCase):
    def test_audio_loader_disables_vision_without_video_memory(self):
        for mode in ['audio','omni']:
            base=MagicMock();base.eval.return_value=base;base.cuda.return_value=base
            duplex=SimpleNamespace(tokenizer=object());base.as_duplex.return_value=duplex
            auto=MagicMock();auto.from_pretrained.return_value=base
            with patch.dict('sys.modules',{'torch':SimpleNamespace(bfloat16='bf16'),'transformers':SimpleNamespace(AutoModel=auto,AutoTokenizer=MagicMock()),'librosa':SimpleNamespace()}):
                model,_,_=_load_real_blocking('/fake',40,None,mode)
            self.assertIs(model,duplex)
            if mode=='audio':
                self.assertFalse(auto.from_pretrained.call_args.kwargs['init_vision'])
                self.assertTrue(auto.from_pretrained.call_args.kwargs['init_tts'])
                base.use_memory.assert_not_called()
                base.as_duplex.assert_called_once_with(generate_audio=True)
            else:
                base.use_memory.assert_called_once_with(memory_minutes=40)

    def client(self, tmp, model_type):
        app=create_app(settings_path=Path(tmp)/'settings.json',model_type=model_type,tokenizer=None,model_server_url='http://unused')
        return TestClient(app, base_url="http://localhost")

    def test_status_lists_only_the_deployed_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            for model_type,mode in [('audio','audio'),('omni','camera')]:
                with self.client(tmp,model_type) as client:
                    result=client.get('/api/status').json()
                    self.assertEqual(result['input_modes'],[mode])
                    self.assertEqual(result['model'],'Realtime-Venus-'+model_type.capitalize())
                    self.assertNotIn('model_type',result)
                    self.assertNotIn('upload_limit_mb',result)

    def test_socket_rejects_a_mode_not_deployed(self):
        with tempfile.TemporaryDirectory() as tmp:
            for model_type,wrong in [('audio','camera'),('omni','audio')]:
                with self.client(tmp,model_type) as client:
                    with client.websocket_connect('ws://localhost/ws?mode='+wrong) as socket:
                        self.assertEqual(socket.receive_json()['code'],'input_mode')

    def test_socket_accepts_the_deployed_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            for model_type,mode in [('audio','audio'),('omni','camera')]:
                with self.client(tmp,model_type) as client:
                    with client.websocket_connect('ws://localhost/ws?mode='+mode) as socket:
                        self.assertEqual(socket.receive_json()['code'],'configuration')

    def test_upload_routes_are_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=self.client(tmp,'omni').app
            paths={route.path for route in app.routes}
            self.assertNotIn('/api/sessions/{sid}/video',paths)
            self.assertNotIn('/api/sessions/{sid}/audio',paths)


if __name__=='__main__':unittest.main()
