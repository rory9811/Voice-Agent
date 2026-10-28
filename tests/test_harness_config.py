import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.settings import HarnessSetup, save_setup, template
from harness.factory import build_harness


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

    def test_invalid_harness_section_and_unknown_fields_fail(self):
        for data in [{'duplex':{}},{'responses':{'typo':2}},{'feedback':{'queue_timeout_s':0}}]:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):HarnessSetup(data,'/tmp/config.json')


if __name__=='__main__':unittest.main()
