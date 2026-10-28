import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from demos.launcher.cli import _run
from demos.launcher.options import load_deployment, parse_launch_options
from demos.server.configuration import WebConfiguration
from harness.settings import HarnessSetup, load_setup, save_setup, template
from harness.factory import build_harness
from test_model_modes import checkpoint


class DeploymentConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path

    def document(self, mode='audio'):
        self.write('config/harness.json', template())
        return self.write('config/demo.json', {
            'model': {'type': mode, 'path': '../weights', 'reference_audio': 'ref.wav'},
            'harness': {'config': 'harness.json'}, 'web': {'port': 9032},
        })

    def test_nested_paths_and_video_alias(self):
        path = self.document('video')
        _, args, _, child = parse_launch_options(self.root, ['--config', str(path)])
        self.assertEqual(args.model_type, 'omni')
        self.assertEqual(args.model_path, str(self.root/'weights'))
        self.assertEqual(args.ref_audio, str(path.parent/'ref.wav'))
        self.assertEqual(args.harness_config, str(path.parent/'harness.json'))
        self.assertEqual(args.web_port, 9032)
        self.assertIn(str(path), child)

    def test_relative_config_and_child_are_independent_of_project_cwd(self):
        path = self.document()
        with patch.dict(os.environ, {'VENUS_LAUNCH_CWD': str(self.root)}):
            _, args, _, child = parse_launch_options(self.root/'another-project', ['--config', 'config/demo.json', '--detach'])
        with patch.dict(os.environ, {'VENUS_LAUNCH_CWD': '/tmp'}):
            _, after, _, _ = parse_launch_options(self.root/'another-project', child)
        self.assertEqual(after.model_path, args.model_path)
        self.assertEqual(after.harness_config, args.harness_config)
        self.assertEqual(after.demo_config, path)

    def test_override_precedence_and_harness_selection(self):
        path = self.document()
        other = self.write('other.json', template())
        with patch.dict(os.environ, {'VENUS_WEB_PORT':'9232','MODEL_PATH':str(self.root/'env-weights'),'HARNESS_CONFIG':str(other)}):
            _, args, _, _ = parse_launch_options(self.root, ['--config',str(path),'--web-port','9332'])
        self.assertEqual(args.web_port,9332)
        self.assertEqual(args.model_path,str(self.root/'env-weights'))
        self.assertEqual(args.harness_config,str(other))

    def test_missing_invalid_unknown_and_wrong_types_fail(self):
        with self.assertRaisesRegex(ValueError,'Cannot read deployment'):
            load_deployment(self.root/'missing.json')
        for data in [[], {'web_por':8}, {'web_port':True}, {'model_type':'audip'}, {'startup_timeout':float('nan')}, {'model':{'typo':3},'harness':{'config':'h.json'}}, {'model':{'type':'audio'}}, {'model':{},'model_type':'audio'}, {'length_penalty':9}]:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):load_deployment(self.write('bad.json',data))
        path=self.root/'broken.json';path.write_text('{')
        with self.assertRaises(ValueError):load_deployment(path)

    def test_harness_reference_must_exist(self):
        path=self.document();(path.parent/'harness.json').unlink()
        with self.assertRaises(SystemExit):parse_launch_options(self.root,['--config',str(path)])

    def test_legacy_config_and_shutdown_without_reading_broken_json(self):
        self.write('config.json',{'model_type':'audio','model_path':'weights','web_port':9132})
        _,args,_,_=parse_launch_options(self.root,[])
        self.assertEqual(args.model_path,str(self.root/'weights'))
        self.assertEqual(args.web_port,9132)
        (self.root/'config.json').write_text('{')
        for action in ['--stop','--status']:
            _,args,_,_=parse_launch_options(self.root,[action])
            self.assertTrue(getattr(args,action[2:]))

    def test_launcher_connects_both_configs_to_web_and_selected_model(self):
        path=self.document()
        checkpoint(self.root/'weights','minicpmo')
        (path.parent/'ref.wav').write_bytes(b'reference')
        with patch.dict(os.environ, {'VENUS_ROOT':str(self.root)}), patch('demos.launcher.cli.subprocess.run'), patch('demos.launcher.cli.setup_backend'), patch('demos.launcher.cli.ensure_ports_available'), patch('demos.launcher.cli.StackSupervisor') as supervisor:
            _run(['--config',str(path)])
        config,model,web=supervisor.call_args.args
        self.assertEqual(config.settings,path.parent/'harness.json')
        self.assertEqual(model[model.index('--model-type')+1],'audio')
        self.assertEqual(web[web.index('--config')+1],str(path.parent/'harness.json'))
        self.assertEqual(web[web.index('--demo-config')+1],str(path))

    def test_shell_preserves_callers_directory(self):
        root=next(p for p in Path(__file__).resolve().parents if (p/"start.sh").exists())
        fake=self.root/'python'
        fake.write_text('#!/bin/sh\nprintf "%s" "$VENUS_LAUNCH_CWD"\n')
        fake.chmod(0o755)
        result=subprocess.run(['/bin/bash',str(root/'start.sh'),'--config','config/demo.json'],cwd=self.root,env={'VENUS_PYTHON':str(fake),'PATH':'/usr/bin:/bin'},capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout,str(self.root))


class HarnessConfigTests(unittest.TestCase):
    def test_standalone_import_and_load_without_demo_on_pythonpath(self):
        package=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'harness.json';save_setup(template(),path)
            code='from harness.settings import load_setup; from harness.factory import load_harness; import sys; s=load_setup(sys.argv[1]); assert s.feedback.proactive_progress is False; assert not any(k == "demos" or k.startswith("demos.") for k in sys.modules)'
            env={**os.environ,'PYTHONPATH':os.pathsep.join([str(package), *(str(Path(p).resolve()) for p in sys.path if p)])}
            subprocess.run([sys.executable,'-c',code,str(path)],cwd=tmp,env=env,check=True,capture_output=True)

    def test_component_budgets_are_not_overwritten(self):
        data=template();data['harness']['delegate']['request_timeout_s']=77
        data['harness']['delegate']['oralization_timeout_s']=88
        data['harness']['result_management']['enabled']=False
        data['feedback']['queue_timeout_s']=95
        data['codex']['execution_timeout_s']=123
        setup=HarnessSetup(data,Path('/tmp/config.json'))
        with patch('harness.factory.task_backends',return_value=(object(),object())),patch('harness.factory.CodexAgentProvider'),patch('harness.factory.VenusOmniAgentHarness') as constructor:
            build_harness(setup)
        kwargs=constructor.call_args.kwargs
        self.assertEqual(kwargs['config'].delegate.request_timeout_s,77)
        self.assertEqual(kwargs['config'].delegate.oralization_timeout_s,88)
        self.assertFalse(kwargs['config'].result_management.enabled)
        self.assertEqual(kwargs['feedback_config'].queue_timeout_s,95)
        self.assertEqual(kwargs['general_config'].execution_timeout_s,123)

    def test_web_saves_harness_and_frontend_to_their_own_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);h=root/'harness.json';d=root/'demo.json'
            data=template();data['workspace']='tasks';data['codex']['command']=[sys.executable,'app-server']
            data['gemini']['api_key']='saved-key'
            save_setup(data,h)
            d.write_text(json.dumps({'model':{'type':'audio','path':'weights','length_penalty':0.9},'harness':{'config':'harness.json'},'web':{'port':8032}}))
            config=WebConfiguration(h,demo_path=d)
            config.login.check=MagicMock(return_value={'state':'logged_in','message':''})
            public=config.public();self.assertEqual(public['data']['duplex']['length_penalty'],0.9)
            self.assertEqual(public['data']['gemini']['api_key'],'')
            public['data']['duplex']['length_penalty']=1.2
            public['data']['feedback']['progress_interval_s']=45
            self.assertTrue(config.save(public)['ok'])
            saved=json.loads(h.read_text());front=json.loads(d.read_text())
            self.assertNotIn('duplex',saved)
            self.assertEqual(saved['feedback']['progress_interval_s'],45)
            self.assertEqual(saved['gemini']['api_key'],'saved-key')
            self.assertEqual(front['model']['length_penalty'],1.2)
            self.assertEqual(front['model']['path'],'weights')
            self.assertNotEqual(config.revision(),public['revision'])
            self.assertEqual(h.stat().st_mode & 0o777,0o600)
            with self.assertRaises(RuntimeError):config.save(public)

    def test_invalid_harness_section_and_unknown_fields_fail(self):
        for data in [{'duplex':{}},{'responses':{'typo':2}},{'feedback':{'queue_timeout_s':0}}]:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):HarnessSetup(data,'/tmp/config.json')


if __name__=='__main__':unittest.main()
