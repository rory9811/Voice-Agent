import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from demos.launcher.cli import _run, main
from demos.launcher.options import load_model_options, load_web_options, parse_launch_options
from demos.launcher.supervisor import StackSupervisor, process_start
from demos.server.configuration import WebConfiguration
from harness.settings import HarnessSetup, load_setup, save_setup, template
from harness.factory import build_harness


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

    def web_document(self):
        self.write('config/harness.json', template())
        return self.write('config/demo.json', {
            'model': {'url': 'http://127.0.0.1:9031'},
            'harness': {'config': 'harness.json'}, 'web': {'port': 9032},
        })

    def model_document(self, mode='audio'):
        return self.write('server/model_server.json', {
            'type': mode, 'path': '../weights', 'reference_audio': 'ref.wav', 'port': 9031,
        })

    def test_model_paths_and_video_alias(self):
        path = self.model_document('video')
        _, args, child = parse_launch_options(self.root, ['model', '--config', str(path)])
        self.assertEqual(args.model_type, 'omni')
        self.assertEqual(args.model_path, str(self.root/'weights'))
        self.assertEqual(args.ref_audio, str(path.parent/'ref.wav'))
        self.assertEqual(args.port, 9031)
        self.assertEqual(child[0], 'model')
        self.assertIn(str(path), child)

    def test_web_paths(self):
        path = self.web_document()
        _, args, child = parse_launch_options(self.root, ['web', '--config', str(path)])
        self.assertEqual(args.harness_config, str(path.parent/'harness.json'))
        self.assertEqual(args.model_url, 'http://127.0.0.1:9031')
        self.assertEqual(args.port, 9032)
        self.assertEqual(args.demo_config, path)
        self.assertEqual(child[0], 'web')

    def test_detach_argv_round_trip(self):
        for role, path, keys in [
            ('model', self.model_document(), ('model_type', 'model_path', 'ref_audio', 'port')),
            ('web', self.web_document(), ('harness_config', 'demo_config', 'model_url', 'port')),
        ]:
            with self.subTest(role=role):
                with patch.dict(os.environ, {'VENUS_LAUNCH_CWD': str(self.root)}):
                    _, args, child = parse_launch_options(
                        self.root/'another-project', [role, '--config', str(path.relative_to(self.root)), '--detach'])
                with patch.dict(os.environ, {'VENUS_LAUNCH_CWD': '/tmp'}):
                    _, after, _ = parse_launch_options(self.root/'another-project', child)
                for key in keys:
                    self.assertEqual(getattr(after, key), getattr(args, key), key)

    def test_override_precedence(self):
        path = self.web_document()
        other = self.write('other.json', template())
        with patch.dict(os.environ, {'VENUS_WEB_PORT': '9232', 'VENUS_MODEL_URL': 'http://10.0.0.2:8031', 'HARNESS_CONFIG': str(other)}):
            _, args, _ = parse_launch_options(self.root, ['web', '--config', str(path), '--port', '9332'])
        self.assertEqual(args.port, 9332)
        self.assertEqual(args.model_url, 'http://10.0.0.2:8031')
        self.assertEqual(args.harness_config, str(other))
        model = self.model_document()
        with patch.dict(os.environ, {'MODEL_PATH': str(self.root/'env-weights'), 'VENUS_MODEL_PORT': '9131'}):
            _, args, _ = parse_launch_options(self.root, ['model', '--config', str(model), '--port', '9231'])
        self.assertEqual(args.model_path, str(self.root/'env-weights'))
        self.assertEqual(args.port, 9231)

    def test_missing_invalid_unknown_and_wrong_types_fail(self):
        for loader in (load_web_options, load_model_options):
            with self.assertRaisesRegex(ValueError, 'Cannot read deployment'):
                loader(self.root/'missing.json')
            path = self.root/'broken.json'
            path.write_text('{')
            with self.assertRaises(ValueError):
                loader(path)
        h = {'config': 'h.json'}
        for data in [[], {'web': {'por': 8}, 'harness': h}, {'web': {'port': True}, 'harness': h},
                     {'model': {'url': 'ftp://x'}, 'harness': h}, {'startup_timeout': float('nan'), 'harness': h},
                     {'model': {'typo': 3}, 'harness': h}, {'model': {'url': 'http://x:1'}},
                     {'model': {'length_penalty': 9}, 'harness': h}]:
            with self.subTest(web=data), self.assertRaises(ValueError):
                load_web_options(self.write('bad.json', data))
        for data in [[], {'type': 'audip'}, {'port': 0}, {'port': True}, {'memory_minutes': -1},
                     {'typo': 1}, {'model': {'type': 'audio'}}]:
            with self.subTest(model=data), self.assertRaises(ValueError):
                load_model_options(self.write('bad.json', data))

    def test_old_formats_rejected(self):
        for data in [{'model_type': 'audio', 'model_path': 'w'},
                     {'model': {'path': 'w'}, 'harness': {'config': 'h.json'}}]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                load_web_options(self.write('old.json', data))

    def test_harness_reference_must_exist(self):
        path = self.web_document()
        (path.parent/'harness.json').unlink()
        with self.assertRaises(SystemExit):
            parse_launch_options(self.root, ['web', '--config', str(path)])

    def test_web_requires_harness_config(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parse_launch_options(self.root, ['web'])

    def test_status_and_stop_without_reading_broken_json(self):
        (self.root/'config.json').write_text('{')
        (self.root/'model_server.json').write_text('{')
        for role in ['model', 'web']:
            for action in ['--stop', '--status']:
                _, args, _ = parse_launch_options(self.root, [role, action])
                self.assertTrue(getattr(args, action[2:]))

    def test_roles_use_separate_state(self):
        runtime = self.root/'runtime'
        runtime.mkdir()
        StackSupervisor(runtime, 'web', ['true'], 'http://127.0.0.1:1/health', 1, {}).save()
        self.assertEqual(json.loads((runtime/'web.json').read_text())['status'], 'starting')
        (runtime/'web.json').unlink()
        (runtime/'model.json').write_text(json.dumps(
            {'status': 'ready', 'pid': os.getpid(), 'started': process_start(os.getpid())}))
        with patch.dict(os.environ, {'VENUS_ROOT': str(self.root)}):
            for role, alive in [('model', True), ('web', False)]:
                out = io.StringIO()
                with redirect_stdout(out):
                    _run([role, '--status'])
                self.assertIs(json.loads(out.getvalue())['alive'], alive, role)

    def launch_web(self, healthy=True):
        path = self.web_document()
        supervisor, ports = MagicMock(), MagicMock()
        with patch.dict(os.environ, {'VENUS_ROOT': str(self.root)}), \
                patch('demos.launcher.cli.setup_backend'), \
                patch('demos.launcher.cli.ensure_port_available', ports), \
                patch('demos.launcher.cli.healthy', return_value=healthy), \
                patch('demos.launcher.cli.StackSupervisor', supervisor):
            main(['web', '--config', str(path)])
        return path, supervisor, ports

    def test_web_role_command(self):
        path, supervisor, _ = self.launch_web()
        runtime, name, command, health, _, _ = supervisor.call_args.args
        self.assertEqual(name, 'web')
        self.assertIn('demos.server.cli', command)
        self.assertEqual(command[command.index('--model-server')+1], 'http://127.0.0.1:9031')
        self.assertEqual(command[command.index('--config')+1], str(path.parent/'harness.json'))
        self.assertEqual(command[command.index('--demo-config')+1], str(path))
        self.assertNotIn('--tokenizer-path', command)
        self.assertNotIn('--model-type', command)
        self.assertEqual(health, 'http://127.0.0.1:9032/health')

    def test_web_role_fails_fast_without_model(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            _, supervisor, _ = self.launch_web(healthy=False)
        self.assertEqual(caught.exception.code, 1)
        self.assertIn('ssh -N -L 9031:127.0.0.1:9031', err.getvalue())

    def test_web_role_does_not_bind_model_port(self):
        _, _, ports = self.launch_web()
        self.assertTrue(ports.call_args_list)
        self.assertEqual({call.args[1] for call in ports.call_args_list}, {9032})

    def test_shell_preserves_callers_directory(self):
        root = next(p for p in Path(__file__).resolve().parents if (p/"start.sh").exists())
        fake = self.root/'python'
        fake.write_text('#!/bin/sh\nprintf "%s" "$VENUS_LAUNCH_CWD"\n')
        fake.chmod(0o755)
        result = subprocess.run(['/bin/bash', str(root/'start.sh'), 'web', '--config', 'config/demo.json'], cwd=self.root,
                                env={'VENUS_PYTHON': str(fake), 'PATH': '/usr/bin:/bin'}, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, str(self.root))


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
            d.write_text(json.dumps({'model':{'url':'http://127.0.0.1:8031','length_penalty':0.9},'harness':{'config':'harness.json'},'web':{'port':8032}}))
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
            self.assertEqual(front['model']['url'],'http://127.0.0.1:8031')
            self.assertNotEqual(config.revision(),public['revision'])
            self.assertEqual(h.stat().st_mode & 0o777,0o600)
            with self.assertRaises(RuntimeError):config.save(public)

    def test_invalid_harness_section_and_unknown_fields_fail(self):
        for data in [{'duplex':{}},{'responses':{'typo':2}},{'feedback':{'queue_timeout_s':0}}]:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):HarnessSetup(data,'/tmp/config.json')


if __name__=='__main__':unittest.main()
